"""Logging shared by every module in the Qlik generation pipeline.

Persists all Qlik agent logs directly to MongoDB in the 'QT2F' database,
under the 'agent_logs' collection (and centralized API endpoints).
Non-blocking fire-and-forget architecture.
"""

import contextvars
import datetime
import logging
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

from app.qlik.config import config

load_dotenv(override=True)

_CONFIGURED = False
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="qlik-mongo-log")

# Context variables for tracking migration metadata across threads/tasks
_qlik_run_id_ctx = contextvars.ContextVar("qlik_run_id", default="unknown")
_qlik_space_id_ctx = contextvars.ContextVar("qlik_space_id", default="personal")
_qlik_app_id_ctx = contextvars.ContextVar("qlik_app_id", default="unknown")
_qlik_project_name_ctx = contextvars.ContextVar("qlik_project_name", default="Unknown")


def set_qlik_logging_context(
    run_id: Optional[str] = None,
    space_id: Optional[str] = None,
    app_id: Optional[str] = None,
    project_name: Optional[str] = None,
) -> None:
    """Set the logging context for the active Qlik migration task."""
    if run_id:
        _qlik_run_id_ctx.set(str(run_id).strip())
    if space_id:
        _qlik_space_id_ctx.set(str(space_id).strip())
    if app_id:
        _qlik_app_id_ctx.set(str(app_id).strip())
    if project_name:
        _qlik_project_name_ctx.set(str(project_name).strip())


def get_qlik_logging_context() -> Dict[str, str]:
    """Retrieve current migration context values."""
    return {
        "run_id": _qlik_run_id_ctx.get(),
        "space_id": _qlik_space_id_ctx.get(),
        "app_id": _qlik_app_id_ctx.get(),
        "project_name": _qlik_project_name_ctx.get(),
    }


def save_log_to_mongodb(payload: Dict[str, Any]) -> None:
    """Persist log entry to MongoDB 'QT2F' database, 'agent_logs' collection ONLY.
    
    Tries direct PyMongo connection if available, falling back to HTTP API endpoint.
    """
    # 1. Direct MongoDB insert (QT2F database, agent_logs collection ONLY)
    mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URL")
    if mongo_uri and mongo_uri.startswith("mongodb"):
        try:
            import pymongo
            client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            client["QT2F"]["agent_logs"].insert_one(dict(payload))
            return
        except Exception:
            pass

    # 2. HTTP API fallback strictly to Qlik storage microservice
    candidate_bases = []
    for raw in [config.MONGO_API_URL, os.getenv("MONGO_API_URL"), "https://aodeclx4wyi6727uozoyktnqb40fyzvt.lambda-url.ap-southeast-2.on.aws"]:
        if raw and str(raw).startswith("http"):
            cleaned = str(raw).rstrip("/")
            if cleaned not in candidate_bases:
                candidate_bases.append(cleaned)

    for base in candidate_bases:
        for route in ["/api/records/logs", "/api/records/agent-logs"]:
            url = f"{base}{route}"
            try:
                res = requests.post(url, json=payload, timeout=(1, 3))
                if res.status_code in (200, 201):
                    return
            except Exception:
                continue


def fire_and_forget_qlik_log(payload: Dict[str, Any]) -> None:
    """Submit log write to background pool without blocking generation."""
    try:
        _LOG_POOL.submit(save_log_to_mongodb, payload)
    except Exception:
        pass


class QlikMongoLogHandler(logging.Handler):
    """Python logging Handler that captures all Qlik logs and streams them to MongoDB."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Ignore HTTP/internal client logs to avoid infinite loop
            if record.name.startswith(("urllib3", "httpx", "requests", "pymongo", "asyncio")):
                return

            ctx = get_qlik_logging_context()
            r_id = getattr(record, "run_id", None) or ctx["run_id"]
            if not r_id or r_id == "unknown":
                return

            p_name = getattr(record, "project_name", None) or ctx["project_name"]
            p_id = getattr(record, "project_id", None) or getattr(record, "space_id", None) or ctx["space_id"]
            w_id = getattr(record, "workbook_id", None) or getattr(record, "app_id", None) or ctx["app_id"]
            details = getattr(record, "details", {})
            if details is not None and not isinstance(details, dict):
                details = {"info": str(details)}

            payload = {
                "id": str(uuid.uuid4()),
                "run_id": r_id,
                "run_no": r_id,
                "project_name": p_name or "Unknown",
                "project_id": p_id,
                "space_id": p_id,
                "workbook_id": w_id,
                "app_id": w_id,
                "agent_name": "Qlik Generation Agent",
                "log_level": record.levelname.upper(),
                "message": record.getMessage(),
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "details": details or {},
                "type": "agent_log",
            }
            fire_and_forget_qlik_log(payload)
        except Exception:
            self.handleError(record)


def setup_logging() -> None:
    """Initialize stream and MongoDB handlers for Qlik logging."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    # Console stream handler
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(stream_handler)

    # MongoDB persistent log handler
    mongo_handler = QlikMongoLogHandler()
    root.addHandler(mongo_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Obtain logger equipped with stdout and MongoDB handlers."""
    setup_logging()
    return logging.getLogger(name)


def log_qlik_info(message: str, details: Any = None) -> None:
    """Explicitly record an INFO log for the active Qlik migration."""
    ctx = get_qlik_logging_context()
    logger = logging.getLogger("app.qlik")
    logger.info(message)
    if ctx["run_id"] and ctx["run_id"] != "unknown":
        payload = {
            "id": str(uuid.uuid4()),
            "run_id": ctx["run_id"],
            "run_no": ctx["run_id"],
            "project_name": ctx["project_name"],
            "project_id": ctx["space_id"],
            "space_id": ctx["space_id"],
            "workbook_id": ctx["app_id"],
            "app_id": ctx["app_id"],
            "agent_name": "Qlik Generation Agent",
            "log_level": "INFO",
            "message": str(message),
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "details": details if isinstance(details, dict) else ({"info": str(details)} if details else {}),
            "type": "agent_log",
        }
        fire_and_forget_qlik_log(payload)


def log_qlik_warning(message: str, details: Any = None) -> None:
    """Explicitly record a WARNING log for the active Qlik migration."""
    ctx = get_qlik_logging_context()
    logger = logging.getLogger("app.qlik")
    logger.warning(message)
    if ctx["run_id"] and ctx["run_id"] != "unknown":
        payload = {
            "id": str(uuid.uuid4()),
            "run_id": ctx["run_id"],
            "run_no": ctx["run_id"],
            "project_name": ctx["project_name"],
            "project_id": ctx["space_id"],
            "space_id": ctx["space_id"],
            "workbook_id": ctx["app_id"],
            "app_id": ctx["app_id"],
            "agent_name": "Qlik Generation Agent",
            "log_level": "WARNING",
            "message": str(message),
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "details": details if isinstance(details, dict) else ({"info": str(details)} if details else {}),
            "type": "agent_log",
        }
        fire_and_forget_qlik_log(payload)


def log_qlik_error(message: str, details: Any = None) -> None:
    """Explicitly record an ERROR log for the active Qlik migration."""
    ctx = get_qlik_logging_context()
    logger = logging.getLogger("app.qlik")
    logger.error(message)
    if ctx["run_id"] and ctx["run_id"] != "unknown":
        payload = {
            "id": str(uuid.uuid4()),
            "run_id": ctx["run_id"],
            "run_no": ctx["run_id"],
            "project_name": ctx["project_name"],
            "project_id": ctx["space_id"],
            "space_id": ctx["space_id"],
            "workbook_id": ctx["app_id"],
            "app_id": ctx["app_id"],
            "agent_name": "Qlik Generation Agent",
            "log_level": "ERROR",
            "message": str(message),
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "details": details if isinstance(details, dict) else ({"info": str(details)} if details else {}),
            "type": "agent_log",
        }
        fire_and_forget_qlik_log(payload)

