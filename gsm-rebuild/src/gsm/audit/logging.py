"""Structured JSON logging that drops sensitive fields by default."""

from __future__ import annotations

import json
import logging
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "content",
        "content_text",
        "content_fields",
        "fields",
        "text",
        "query",
        "query_text",
        "answer",
        "draft",
        "prompt",
        "completion",
        "payload",
        "topic",
        "secret",
        "password",
        "api_key",
        "authorization",
        "token",
    }
)
_RESERVED = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class RedactingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key in _RESERVED or key.startswith("_"):
                continue
            out[key] = "[redacted]" if key.lower() in SENSITIVE_KEYS else value
        if record.exc_info and record.exc_info[0] is not None:
            # Exception *types* only: messages could carry data from a driver.
            out["exc_type"] = record.exc_info[0].__name__
        return json.dumps(out, default=str, sort_keys=True)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingJsonFormatter())
    root = logging.getLogger("gsm")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"gsm.{name}")
