"""One log file per agent under logs/, rotated daily. Never log secrets."""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler

from config import LOG_DIR

_loggers: dict[str, logging.Logger] = {}


def get(name: str) -> logging.Logger:
    if name in _loggers:
        return _loggers[name]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger(f"desk.{name}")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        h = TimedRotatingFileHandler(
            LOG_DIR / f"{name}.log", when="midnight", backupCount=14, encoding="utf-8", utc=False
        )
        h.setFormatter(logging.Formatter("%(asctime)s\t%(message)s"))
        lg.addHandler(h)
    _loggers[name] = lg
    return lg


def quiet_http_libs() -> None:
    """httpx/httpcore/websockets loggers stay at WARNING (no URLs/headers in logs)."""
    for n in ("httpx", "httpcore", "websockets", "hpack"):
        logging.getLogger(n).setLevel(logging.WARNING)
