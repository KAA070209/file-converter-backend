# File Converter API

Backend FastAPI untuk konversi gambar ke gambar dan PDF. PostgreSQL menyimpan riwayat job, Redis menyediakan rate limit, dan file sementara/hasil disimpan pada volume data.

## Fitur

- JPG, PNG, WEBP, BMP, dan TIFF ke JPG, PNG, atau WEBP.
- Satu gambar ke PDF dan sampai 30 gambar ke satu PDF.
- Validasi isi gambar dan batas ukuran upload (default 20 MiB per file).
- Riwayat job dan endpoint unduhan berdasarkan ID.
- Health check untuk API, PostgreSQL, dan Redis.
- Rate limit per alamat IP (default 60 request konversi per menit), memakai Redis dan fallback per proses saat Redis tidak tersedia.
- OpenAPI interaktif di `/docs`, spesifikasi JSON di `/openapi.json`.

## Menjalankan dengan Docker (direkomendasikan)

Salin `.env.example` ke `.env`, ubah `POSTGRES_PASSWORD`, lalu jalankan:

```bash
docker compose up --build -d
```

API tersedia di `http://localhost:8000`; dokumentasi interaktif di `http://localhost:8000/docs`. PostgreSQL dan Redis hanya diekspos di jaringan internal Compose. Data tersimpan pada volume `postgres_data`, `redis_data`, dan `converter_data`.

Untuk menghentikan layanan tanpa menghapus data:

```bash
docker compose down
```

`docker compose down -v` menghapus volume dan seluruh database/file hasil.

## Menjalankan lokal

Gunakan Python 3.11 atau 3.12, lalu siapkan PostgreSQL dan Redis lokal. Buat virtual environment dan install dependency:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

Salin `.env.example` ke `.env` dan atur kredensial PostgreSQL. Aplikasi memuat `.env` secara otomatis saat dijalankan secara lokal. Contoh PowerShell untuk mengganti URL sementara:

```powershell
$env:DATABASE_URL="postgresql+psycopg://converter:password@localhost:5432/converter"
$env:REDIS_URL="redis://localhost:6379/0"
uvicorn app.main:app --reload
```

Saat startup, tabel `conversion_jobs` dibuat otomatis jika belum ada. Untuk deployment dengan perubahan skema berkelanjutan, gunakan migrasi Alembic.

## Konfigurasi

| Variabel | Default | Keterangan |
|---|---|---|
| `APP_NAME` | `File Converter API` | Nama layanan |
| `DATABASE_URL` | `postgresql+psycopg://converter:converter_dev_only@localhost:5432/converter` | URL SQLAlchemy PostgreSQL |
| `REDIS_URL` | `redis://localhost:6379/0` | URL Redis |
| `POSTGRES_PASSWORD` | disetel Compose | Password PostgreSQL Compose; ganti untuk deployment |
| `MAX_UPLOAD_MB` | `20` | Batas ukuran setiap file |
| `RATE_LIMIT_PER_MINUTE` | `60` | Batas request konversi per IP per menit |
| `CORS_ORIGINS` | `http://localhost:3000` | Origin frontend, dipisahkan koma |
| `DATA_DIR` | `./data` | Direktori temporary dan hasil |

## API v1

Semua respons error FastAPI memakai bentuk `{ "detail": "..." }`. Upload menggunakan `multipart/form-data`. Endpoint konversi mengembalikan file langsung dan header `X-Job-ID`; gunakan ID itu untuk melihat status atau mengunduh ulang hasil.

### `POST /api/v1/convert/image`

Field multipart `file`; query opsional `output_format` (`jpg`, `jpeg`, `png`, `webp`, default `png`), `quality` (1–100, default 90), dan `background` (default `white`, dipakai untuk transparansi ke JPG).

```bash
curl -f -X POST "http://localhost:8000/api/v1/convert/image?output_format=jpg&quality=90&background=white" \
  -F "file=@gambar.png" -o hasil.jpg
```

### `POST /api/v1/convert/jpg-to-pdf` dan `POST /api/v1/convert/png-to-pdf`

Field multipart `file`; hasil berupa PDF. Validasi berdasarkan konten tetap menerima format gambar yang didukung meski endpoint diberi nama khusus JPG/PNG.

```bash
curl -f -X POST http://localhost:8000/api/v1/convert/jpg-to-pdf \
  -F "file=@foto.jpg" -o hasil.pdf
```

### `POST /api/v1/convert/images-to-pdf`

Field multipart `files` berulang, 1–30 gambar. Urutan halaman mengikuti urutan pengiriman.

```bash
curl -f -X POST http://localhost:8000/api/v1/convert/images-to-pdf \
  -F "files=@halaman-1.jpg" -F "files=@halaman-2.png" -o gabungan.pdf
```

### Riwayat dan unduhan

- `GET /api/v1/jobs?limit=20&offset=0` — daftar job terbaru (limit 1–100).
- `GET /api/v1/jobs/{job_id}` — status dan metadata job.
- `GET /api/v1/jobs/{job_id}/download` — unduh ulang hasil; 410 jika file sudah tidak tersedia.

Status job: `processing`, `completed`, `failed`. Job memuat `id`, `operation`, `created_at`, `completed_at`, dan `download_url`.

### Health

- `GET /health/live` — liveness API.
- `GET /health` — status dependency PostgreSQL dan Redis pada `checks`. Status `degraded` menandakan salah satunya tidak tersedia.

Jika Redis belum berjalan, API tetap menerima konversi dengan rate limit lokal per proses. Rate limit lokal tidak dibagi antar worker atau instance; Redis dicoba kembali otomatis setiap 15 detik. Endpoint `/health` tetap menampilkan status `degraded` agar Redis yang belum tersedia terlihat.

### Kode status umum

`200` berhasil; `400` file kosong/tidak valid; `413` file terlalu besar atau jumlah gambar di atas batas; `415` MIME tidak didukung; `422` parameter tidak valid; `429` rate limit terlampaui; `404` job tidak ditemukan; `410` file hasil tidak tersedia. Jika Redis tidak tersedia, konversi tetap berjalan dengan rate limit lokal.

## Integrasi Next.js

```ts
const form = new FormData();
form.append("file", selectedFile);
const response = await fetch("http://localhost:8000/api/v1/convert/jpg-to-pdf", {
  method: "POST",
  body: form,
});
if (!response.ok) {
  const error = await response.json();
  throw new Error(error.detail ?? "Konversi gagal");
}
const jobId = response.headers.get("X-Job-ID");
const blob = await response.blob();
```

Jangan mengatur `Content-Type` manual untuk `FormData`; browser perlu menambahkan multipart boundary. Frontend dapat memakai `jobId` pada `/api/v1/jobs/{jobId}`.

## Catatan deployment

Tabel awal dibuat dengan `create_all`; gunakan migrasi skema (misalnya Alembic) sebelum evolusi database production. Atur password kuat, origin CORS yang spesifik, TLS/reverse proxy, retensi dan cleanup file hasil, backup PostgreSQL, serta monitoring. Rate limit saat ini mengandalkan IP koneksi; atur proxy tepercaya secara benar sebelum memakai header forwarded. File hasil disimpan lokal sehingga deployment multi-instance memerlukan shared/object storage. Konversi berjalan di proses API; untuk beban tinggi pindahkan ke worker queue.
