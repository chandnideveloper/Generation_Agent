import logging
import datetime
import os
import json
import asyncio
import httpx
import contextvars  # <--- Added
from typing import Dict, Any, Optional
from app.tableau.core.config import Config
from app.tableau.core.http_client import get_resilient_client
from app.tableau.api.schemas import CosmosLogRecord, CosmosGenerationRecord

# --- Configuration ---
from app.tableau.core.config import mongo_db

# Configure standard python logging to console
import sys
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    force=True,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)
logger.info("Logging system initialized.")

# --- Deadletter Store & Background Task Helpers ---
DEADLETTER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "deadletter_store")

def store_deadletter(url: str, payload: Dict, error_msg: str):
    try:
        os.makedirs(DEADLETTER_DIR, exist_ok=True)
        filepath = os.path.join(DEADLETTER_DIR, "failed_cosmos_writes.jsonl")
        record = {
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "url": url,
            "error": str(error_msg),
            "payload": payload
        }
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        logger.error(f"[Deadletter Store] Saved failed write for {url} to {filepath}")
    except Exception as e:
        logger.error(f"[Deadletter Store] CRITICAL: Failed to write to deadletter store: {e}")

def _handle_task_result(task: asyncio.Task):
    try:
        exc = task.exception()
        if exc:
            logger.error(f"[BackgroundTask: {task.get_name()}] Unhandled exception: {exc}")
    except asyncio.CancelledError:
        pass

def run_background_task(coro, task_name="background_task") -> Optional[asyncio.Task]:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            task = loop.create_task(coro, name=task_name)
            task.add_done_callback(_handle_task_result)
            return task
    except RuntimeError:
        pass
    return None

# --- Context Variables ---
# This allows us to access the context deep in the call stack without passing it as an argument
_request_token_ctx = contextvars.ContextVar("request_token", default=None)
_project_id_ctx = contextvars.ContextVar("project_id", default="System")
_workbook_id_ctx = contextvars.ContextVar("workbook_id", default="System")
_run_id_ctx = contextvars.ContextVar("run_id", default="System")

def set_log_token(token: str):
    """Sets the Bearer token for the current request context."""
    _request_token_ctx.set(token)

def set_logging_context(project_id: str, workbook_id: str, run_id: str):
    """Sets the context for logging IDs."""
    print(f"DEBUG: [logging_utils] Setting context: {project_id}, {workbook_id}, {run_id}")
    _project_id_ctx.set(project_id)
    _workbook_id_ctx.set(workbook_id)
    _run_id_ctx.set(run_id)
    print(f"DEBUG: [logging_utils] Current _project_id_ctx.get(): {_project_id_ctx.get()}")

def get_log_token() -> Optional[str]:
    """Retrieves the current Bearer token."""
    return _request_token_ctx.get()

async def _do_post_log(payload: Dict):
    """
    Internal helper to send the log to MongoDB asynchronously.
    """
    try:
        try:
            validated_payload = CosmosLogRecord(**payload).model_dump()
        except Exception as ve:
            logger.warning(f"[Log Storage] Payload failed contract validation: {ve}. Sending raw payload.")
            validated_payload = payload

        if mongo_db is not None:
            await mongo_db["logs"].insert_one(validated_payload)
        else:
            logger.warning("[Log Storage] MONGODB_URL not configured. Skipping save.")
    except Exception as e:
        err_msg = f"Failed to send log to MongoDB: {e}"
        logger.error(f"[Log Storage] {err_msg}")
        store_deadletter("mongodb://logs", payload, err_msg)

def fire_and_forget_log(payload: Dict):
    """
    Schedules the log MongoDB call on the running event loop without blocking.
    """
    run_background_task(_do_post_log(payload), task_name="fire_and_forget_log")

# --- 1. Core Logging Logic ---

def build_payload(level: str, message: str, details: Any = None, project_id=None, run_id=None):
    """
    Constructs the JSON payload for the MongoDB API.
    Retrieves project_id and run_id from context if not provided.
    """
    p_id = project_id or _project_id_ctx.get()
    r_id = run_id or _run_id_ctx.get()
    w_id = _workbook_id_ctx.get()

    # [FIX] Ensure details is always a dict to prevent 422 Unprocessable Entity errors
    # If it's a string, wrap it in a dict.
    if details is not None and not isinstance(details, dict):
        details = {"info": str(details)}
    
    return {
        "project_name": p_id,
        "project_id": p_id,
        "run_id": r_id,
        "workbook_id": w_id,
        "agent_name": "Generation Agent",
        "log_level": level.upper(),
        "message": str(message),
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "details": details or {}
    }

# --- 2. Synchronous Loggers (Used by standard scripts) ---

def log_info(message: str):
    """Logs info to Console and fires API request."""
    logger.info(message)
    fire_and_forget_log(build_payload("INFO", message))

def log_warning(message: str):
    """Logs warning to Console and fires API request."""
    logger.warning(message)
    fire_and_forget_log(build_payload("WARNING", message))

def log_error(message: str):
    """Logs error to Console and fires API request."""
    logger.error(message)
    fire_and_forget_log(build_payload("ERROR", message))

# --- 3. Async Wrappers (Used by Agents) ---

async def log_action_to_api(message: str, details: str = None):
    """
    Async wrapper for API actions. Used by CoordinatorAgent.
    """
    logger.info(f"API ACTION: {message} | {details}")
    fire_and_forget_log(build_payload("INFO", message, details))

async def log_error_to_api(message: str, details: str = None):
    """
    Async wrapper for API errors. Used by FolderAgent.
    """
    logger.error(f"API ERROR: {message} | {details}")
    fire_and_forget_log(build_payload("ERROR", message, details))

# --- 4. Result Saving ---

async def save_result_to_api(result_data: Dict[str, Any]):
    """
    Sends the generation result to the MongoDB Database.
    """
    if mongo_db is None:
        logger.warning("[Storage] MONGODB_URL is not set. Skipping save.")
        return

    # Log the attempt
    await log_action_to_api(f"[Storage] Saving results to MongoDB", result_data.get("project_id"))

    # Construct Payload
    payload = {
        "run_id": result_data.get("run_id", "Unknown"),
        "project_id": result_data.get("project_id", "Unknown"),
        "workbook_id": result_data.get("workbook_id", "Unknown"),
        "project_name": result_data.get("project_id", "Unknown"),
        "status": "completed",
        "payload": result_data
    }

    try:
        try:
            validated_payload = CosmosGenerationRecord(**payload).model_dump()
        except Exception as ve:
            logger.warning(f"[Storage] Generation payload failed contract validation: {ve}. Sending raw.")
            validated_payload = payload

        await mongo_db["generation"].insert_one(validated_payload)
        await log_action_to_api("[Storage] Successfully saved result")
    except Exception as e:
        err_msg = f"Failed to save result to MongoDB: {str(e)}"
        await log_error_to_api(f"[Storage] {err_msg}")
        store_deadletter("mongodb://generation", payload, err_msg)

# --- 5. Replaced `send_log_to_api` Function ---
async def send_log_to_api(self, project_id: str, workbook_id: str, run_id: str, log_level: str, message: str, details: Optional[Dict[str, Any]] = None):
    """
    Sends a log entry to the Centralized MongoDB API (Fire-and-forget).
    """
    base_url = COSMOS_DB_API_URL
    if not base_url: return

    api_url = f"{base_url.rstrip('/')}/api/records/logs"
    
    payload = {
        "project_name": project_id,
        "run_id": run_id,
        "agent_name": "generation",
        "log_level": log_level.upper(),
        "message": message,
        "project_id": project_id,
        "workbook_id": workbook_id,
        "details": details or {}
    }
    
    # Note: fire_and_forget_log now internally calls _do_post_log which handles the token
    fire_and_forget_log(payload)