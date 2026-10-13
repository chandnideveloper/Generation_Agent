"""API logger client for Qlik Generation Agent."""

import datetime
import logging
import os
import uuid
import aiohttp
from app.qlik.config import Config
from app.qlik.util.logging_utils import fire_and_forget_qlik_log

logger = logging.getLogger(__name__)


async def log_action_to_api(
    action_desc: str,
    app_id: str = None,
    details: str = None,
    run_id: str = None,
    workspace_id: str = None,
    project_name: str = None,
    token: str = None,
) -> None:
    """Log an agent action to the external monitoring API and QT2F.agent_logs."""
    effective_run_id = run_id or app_id or "unknown"
    effective_space = workspace_id or "personal"
    effective_app = app_id or "unknown"
    effective_proj = project_name or "Unknown"

    log_payload = {
        "id": str(uuid.uuid4()),
        "run_id": effective_run_id,
        "run_no": effective_run_id,
        "project_name": effective_proj,
        "project_id": effective_space,
        "space_id": effective_space,
        "workbook_id": effective_app,
        "app_id": effective_app,
        "agent_name": "Qlik Generation Agent",
        "log_level": "INFO",
        "message": action_desc,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "details": {"info": str(details)} if details else {},
        "type": "agent_log",
    }
    # Fire and forget into QT2F.agent_logs
    fire_and_forget_qlik_log(log_payload)

    # Also log to activity logger
    from app.services.activity_logger import log_activity_async
    try:
        await log_activity_async(
            run_id=effective_run_id,
            project_id=effective_space,
            workbook_id=effective_app,
            summary=action_desc,
            status="success",
        )
    except Exception as e:
        logger.debug(f"[GenLogger] Activity logging failed: {e}")

