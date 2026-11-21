from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
import logging
import time
from datetime import datetime, timezone

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, UnidentifiedImageError

from app.config import (
    ALLOWED_IMAGE_MIME_TYPES, MAX_UPLOAD_BYTES, OUTPUT_DIR, TEMP_DIR,
    CORS_ORIGINS, APP_NAME
)
from app.converters.image import image_to_image, images_to_pdf
from app.database import create_job, finish_job, get_job, initialize_database, list_jobs
from app.config import REDIS_URL, RATE_LIMIT_PER_MINUTE
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    initialize_database()
    app.state.redis = Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    app.state.redis_available = False
    app.state.redis_retry_at = 0.0
    app.state.local_rate_limits = {}
    try:
        app.state.redis.ping()
        app.state.redis_available = True
        logger.info("Redis rate limiter connected.")
    except RedisError:
        app.state.redis_retry_at = time.monotonic() + 15
        logger.warning("Redis unavailable; using a per-process rate limiter until Redis reconnects.")
    yield
    app.state.redis.close()

app = FastAPI(
    title=APP_NAME,
    version="0.1.0",
    description="API konversi gambar ke gambar dan PDF. Riwayat job disimpan di PostgreSQL; rate limit memakai Redis dengan fallback lokal saat Redis tidak tersedia.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Job-ID", "Content-Disposition"],
)

@app.get("/health/live", tags=["health"])
def liveness():
    return {"status": "ok", "service": APP_NAME}

@app.get("/health", tags=["health"])
def health(request: Request):
    checks = {"postgres": False, "redis": False, "rate_limiter": "local"}
    try:
        from app.database import engine
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:
        logger.exception("PostgreSQL health check failed")
    try:
        checks["redis"] = bool(request.app.state.redis.ping())
        if checks["redis"]:
            checks["rate_limiter"] = "redis"
    except Exception:
        request.app.state.redis_available = False
        request.app.state.redis_retry_at = time.monotonic() + 15
        logger.warning("Redis health check failed; local rate limiter is active.")
    status = "ok" if checks["postgres"] and checks["redis"] else "degraded"
    return {"status": status, "service": APP_NAME, "checks": checks}


def _local_rate_limit_exceeded(request: Request, client_ip: str, minute: int) -> bool:
    """Per-process fallback used while Redis is unavailable."""
    limits: dict[tuple[str, int], int] = request.app.state.local_rate_limits
    # Keep only the current and previous minute to bound memory use.
    for bucket in tuple(limits):
        if bucket[1] < minute - 1:
            del limits[bucket]
    bucket = (client_ip, minute)
    count = limits.get(bucket, 0) + 1
    limits[bucket] = count
    return count > RATE_LIMIT_PER_MINUTE

@app.middleware("http")
async def conversion_rate_limit(request: Request, call_next):
    if request.method == "POST" and request.url.path.startswith("/api/v1/convert/"):
        client_ip = request.client.host if request.client else "unknown"
        minute = int(datetime.now(timezone.utc).timestamp()) // 60
        use_redis = request.app.state.redis_available

        # Retry Redis periodically after a failure instead of waiting for a restart.
        if not use_redis and time.monotonic() >= request.app.state.redis_retry_at:
            try:
                request.app.state.redis.ping()
                request.app.state.redis_available = True
                use_redis = True
                logger.info("Redis rate limiter reconnected.")
            except RedisError:
                request.app.state.redis_retry_at = time.monotonic() + 15

        exceeded = False
        if use_redis:
            try:
                key = f"rate:{client_ip}:{minute}"
                count = request.app.state.redis.incr(key)
                if count == 1:
                    request.app.state.redis.expire(key, 65)
                exceeded = count > RATE_LIMIT_PER_MINUTE
            except RedisError:
                request.app.state.redis_available = False
                request.app.state.redis_retry_at = time.monotonic() + 15
                logger.warning("Redis rate limiter unavailable; switching to a per-process fallback.")
                exceeded = _local_rate_limit_exceeded(request, client_ip, minute)
        else:
            exceeded = _local_rate_limit_exceeded(request, client_ip, minute)

        if exceeded:
            return JSONResponse({"detail": "Batas request per menit terlampaui."}, status_code=429, headers={"Retry-After": "60"})
    return await call_next(request)

async def save_upload(upload: UploadFile, destination: Path) -> None:
    if upload.content_type not in ALLOWED_IMAGE_MIME_TYPES:
        raise HTTPException(status_code=415, detail="Tipe file tidak didukung.")

    total = 0
    try:
        with destination.open("wb") as target:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Ukuran file melewati batas.")
                target.write(chunk)
    finally:
        await upload.close()

    if total == 0:
        raise HTTPException(status_code=400, detail="File kosong.")

    # Validasi berdasarkan isi file, bukan hanya ekstensi atau MIME.
    try:
        with Image.open(destination) as image:
            image.verify()
    except (UnidentifiedImageError, OSError):
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="File bukan gambar yang valid.")

def make_download_response(path: Path, filename: str, media_type: str, job_id: str | None = None):
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=filename,
        headers={"X-Job-ID": job_id} if job_id else None,
    )

def job_payload(job):
    return {"id": job.id, "operation": job.operation, "status": job.status,
            "created_at": job.created_at, "completed_at": job.completed_at,
            "download_url": f"/api/v1/jobs/{job.id}/download" if job.status == "completed" else None}

@app.get("/api/v1/jobs", tags=["jobs"])
def jobs(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    return {"items": [job_payload(job) for job in list_jobs(limit, offset)], "limit": limit, "offset": offset}

@app.get("/api/v1/jobs/{job_id}", tags=["jobs"])
def job_detail(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job tidak ditemukan.")
    return job_payload(job)

@app.get("/api/v1/jobs/{job_id}/download", tags=["jobs"])
def download_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job tidak ditemukan.")
    if job.status != "completed" or not job.output_path or not Path(job.output_path).is_file():
        raise HTTPException(status_code=410, detail="Hasil job sudah tidak tersedia.")
    return make_download_response(Path(job.output_path), job.output_filename or "download", job.media_type or "application/octet-stream")

@app.post("/api/v1/convert/image")
async def convert_image(
    file: UploadFile = File(...),
    output_format: str = "png",
    quality: int = 90,
    background: str = "white",
):
    """
    Konversi satu gambar. Format keluaran: jpg, jpeg, png, webp.
    Untuk PNG transparan -> JPG, background dapat diatur misalnya 'white' atau '#ffffff'.
    """
    output_format = output_format.lower().strip()
    if output_format not in {"jpg", "jpeg", "png", "webp"}:
        raise HTTPException(status_code=422, detail="Format output tidak didukung.")
    if not 1 <= quality <= 100:
        raise HTTPException(status_code=422, detail="Quality harus antara 1 sampai 100.")

    job_id = uuid4().hex
    source_path = TEMP_DIR / f"{job_id}_source"
    extension = "jpg" if output_format == "jpeg" else output_format
    output_path = OUTPUT_DIR / f"{job_id}.{extension}"
    create_job(job_id, "image-to-image")

    try:
        await save_upload(file, source_path)
        image_to_image(
            source_path=source_path,
            output_path=output_path,
            output_format=extension,
            quality=quality,
            background=background,
        )
        media = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[extension]
        finish_job(job_id, "completed", str(output_path), f"converted.{extension}", media)
        return make_download_response(output_path, f"converted.{extension}", media, job_id)
    except HTTPException:
        finish_job(job_id, "failed")
        raise
    except Exception:
        finish_job(job_id, "failed")
        logger.exception("Image conversion failed; job_id=%s", job_id)
        raise HTTPException(status_code=500, detail="Konversi gagal. Periksa file dan coba lagi.")
    finally:
        source_path.unlink(missing_ok=True)
        # File output sengaja dipertahankan agar FileResponse dapat mengirimkannya.
        # Untuk production, tambahkan job cleanup terjadwal pada OUTPUT_DIR.

@app.post("/api/v1/convert/jpg-to-pdf")
async def jpg_to_pdf(file: UploadFile = File(...)):
    return await _single_image_to_pdf(file)

@app.post("/api/v1/convert/png-to-pdf")
async def png_to_pdf(file: UploadFile = File(...)):
    return await _single_image_to_pdf(file)

async def _single_image_to_pdf(file: UploadFile):
    job_id = uuid4().hex
    source_path = TEMP_DIR / f"{job_id}_source"
    output_path = OUTPUT_DIR / f"{job_id}.pdf"
    create_job(job_id, "image-to-pdf")
    try:
        await save_upload(file, source_path)
        images_to_pdf([source_path], output_path)
        finish_job(job_id, "completed", str(output_path), "converted.pdf", "application/pdf")
        return make_download_response(output_path, "converted.pdf", "application/pdf", job_id)
    except HTTPException:
        finish_job(job_id, "failed")
        raise
    except Exception:
        finish_job(job_id, "failed")
        logger.exception("Image to PDF conversion failed; job_id=%s", job_id)
        raise HTTPException(status_code=500, detail="Konversi ke PDF gagal.")
    finally:
        source_path.unlink(missing_ok=True)

@app.post("/api/v1/convert/images-to-pdf")
async def multiple_images_to_pdf(files: list[UploadFile] = File(...)):
    """Gabungkan beberapa gambar menjadi satu PDF; urutan mengikuti urutan upload."""
    if not files:
        raise HTTPException(status_code=400, detail="Pilih minimal satu gambar.")
    if len(files) > 30:
        raise HTTPException(status_code=413, detail="Maksimal 30 gambar per proses.")

    job_id = uuid4().hex
    source_paths: list[Path] = []
    output_path = OUTPUT_DIR / f"{job_id}.pdf"
    create_job(job_id, "images-to-pdf")
    try:
        for index, upload in enumerate(files):
            source = TEMP_DIR / f"{job_id}_{index}"
            source_paths.append(source)
            await save_upload(upload, source)
        images_to_pdf(source_paths, output_path)
        finish_job(job_id, "completed", str(output_path), "images.pdf", "application/pdf")
        return make_download_response(output_path, "images.pdf", "application/pdf", job_id)
    except HTTPException:
        finish_job(job_id, "failed")
        raise
    except Exception:
        finish_job(job_id, "failed")
        logger.exception("Multiple images to PDF failed; job_id=%s", job_id)
        raise HTTPException(status_code=500, detail="Konversi beberapa gambar gagal.")
    finally:
        for path in source_paths:
            path.unlink(missing_ok=True)

@app.get("/")
def root():
    return {
        "name": APP_NAME,
        "docs": "/docs",
        "health": "/health",
        "endpoints": [
            "POST /api/v1/convert/image",
            "POST /api/v1/convert/jpg-to-pdf",
            "POST /api/v1/convert/png-to-pdf",
            "POST /api/v1/convert/images-to-pdf",
        ],
    }
