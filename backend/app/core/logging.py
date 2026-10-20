"""Loguru configuration."""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from app.core.config import settings


def configure_logging() -> None:
    """Configure loguru sinks."""
    logger.remove()
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}:{function}:{line}</cyan> | "
        "<level>{message}</level>"
    )

    logger.add(sys.stderr, level="INFO" if not settings.debug else "DEBUG", format=log_format)

    log_path: Path = settings.storage_logs_dir / "naw.log"
    # P0 Security: file handler NEVER goes below INFO — DEBUG would bypass
    # Fernet encryption and record decrypted secrets in plaintext on disk.
    file_level = "INFO" if not settings.debug else "DEBUG"
    logger.add(
        str(log_path),
        level=file_level,
        rotation="20 MB",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
        format=log_format,
    )


configure_logging()
