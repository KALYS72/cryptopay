"""Структурированные JSON-логи (на Render их удобно читать и фильтровать). Локально — читаемый текст."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Стандартные поля LogRecord — всё остальное, переданное через extra=..., попадёт в JSON.
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {key: value for key, value in vars(record).items() if key not in _RESERVED}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": "cryptopay",
            "logger": record.name,
            "msg": record.getMessage(),
            **_extras(record),
        }
        if record.exc_info:
            entry["err"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class PrettyFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        time = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        extras = " ".join(f"{k}={v}" for k, v in _extras(record).items())
        line = f"[{time}] {record.levelname:<7} {record.getMessage()}" + (f"  {extras}" if extras else "")
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str, pretty: bool) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    if level == "silent":
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.CRITICAL + 1)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(PrettyFormatter() if pretty else JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Доступ к страницам логирует наш middleware; встроенный access-лог uvicorn дублировал бы его.
    logging.getLogger("uvicorn.access").disabled = True
