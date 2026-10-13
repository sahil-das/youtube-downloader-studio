"""Structured, bounded logging setup with sensitive data filtering."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "ytd_studio"
LOG_FILENAME = "app.log"
MAX_LOG_BYTES = 2 * 1024 * 1024  # 2 MB per file
BACKUP_COUNT = 3

_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|token|access_token|refresh_token|api_key|apikey|secret|cookie|session_id|authorization)\s*([=:])\s*([^\s&\"',;]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(r"(?i)(https?://)([^/\s:@]+):([^/\s@]+)@"),
        r"\1[REDACTED]:[REDACTED]@",
    ),
)


def redact_sensitive_text(text: str) -> str:
    """Mask potential tokens, passwords, cookies, or credentials in log strings."""
    if not text:
        return text
    sanitized = text
    for pattern, replacement in _SENSITIVE_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


class SensitiveDataFilter(logging.Filter):
    """Logging filter that strips sensitive values from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = redact_sensitive_text(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:
            pass
        return True


def parse_log_level(level_name: str) -> int:
    """Convert a string log level name to a logging module integer constant."""
    mapping = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    return mapping.get(str(level_name).strip().upper(), logging.INFO)


def setup_logging(log_dir: Path, level: str = "INFO") -> Path:
    """Configure application logging with rotating file handler and console output."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOG_FILENAME
    numeric_level = parse_log_level(level)

    root_logger = logging.getLogger(LOGGER_NAME)
    root_logger.setLevel(numeric_level)

    # Clear existing handlers if re-initializing
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sensitive_filter = SensitiveDataFilter()

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(sensitive_filter)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(sensitive_filter)
    root_logger.addHandler(console_handler)

    root_logger.propagate = False
    root_logger.info("Logging initialized at %s (level=%s)", log_file, level.upper())
    return log_file


def set_log_level(level: str) -> None:
    """Update the active logging verbosity dynamically."""
    numeric_level = parse_log_level(level)
    root_logger = logging.getLogger(LOGGER_NAME)
    root_logger.setLevel(numeric_level)
    for handler in root_logger.handlers:
        handler.setLevel(numeric_level)


def get_logger(name: str = "") -> logging.Logger:
    """Return a child logger scoped under the application namespace."""
    if not name or name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    if name.startswith(f"{LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
