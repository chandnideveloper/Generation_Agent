"""Logging shared by every module."""

import logging
import sys

from app.qlik.config import config

_CONFIGURED = False
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
