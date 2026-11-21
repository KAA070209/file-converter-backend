from datetime import datetime, timezone

from sqlalchemy import DateTime, String, create_engine, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    sessionmaker,
)

from app.config import DATABASE_URL


# Koneksi PostgreSQL
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 5},
)

SessionLocal = sessionmaker(
    bind=engine,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


class ConversionJob(Base):
    __tablename__ = "conversion_jobs"

    id: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
    )

    operation: Mapped[str] = mapped_column(
        String(64),
        index=True,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(16),
        index=True,
        nullable=False,
    )

    output_path: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
    )

    output_filename: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    media_type: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


def initialize_database() -> None:
    """Tes koneksi dan buat tabel jika belum tersedia."""
    with engine.connect() as connection:
        connection.exec_driver_sql("SELECT 1")

    Base.metadata.create_all(bind=engine)


def create_job(job_id: str, operation: str) -> None:
    with SessionLocal.begin() as session:
        job = ConversionJob(
            id=job_id,
            operation=operation,
            status="processing",
        )
        session.add(job)


def finish_job(
    job_id: str,
    status: str,
    path: str | None = None,
    filename: str | None = None,
    media_type: str | None = None,
) -> None:
    with SessionLocal.begin() as session:
        job = session.get(ConversionJob, job_id)

        if job is not None:
            job.status = status
            job.output_path = path
            job.output_filename = filename
            job.media_type = media_type
            job.completed_at = datetime.now(timezone.utc)


def get_job(job_id: str) -> ConversionJob | None:
    with SessionLocal() as session:
        return session.get(ConversionJob, job_id)


def list_jobs(limit: int = 20, offset: int = 0) -> list[ConversionJob]:
    with SessionLocal() as session:
        statement = (
            select(ConversionJob)
            .order_by(ConversionJob.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        return list(session.scalars(statement).all())