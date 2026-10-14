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
    """Log an agent action strictly to QT2F.agent_actions."""
    effective_run_id = run_id or app_id or "unknown"
    effective_space = workspace_id or "personal"
    effective_app = app_id or "unknown"

    from app.services.activity_logger import log_activity_async
    try:
        await log_activity_async(
            run_id=effective_run_id,
            project_id=effective_space,
            workbook_id=effective_app,
            summary=action_desc,
            status="success",
            source_type="qlik",
            project_name=project_name,
        )
    except Exception as e:
        logger.debug(f"[GenLogger] Action logging to QT2F.agent_actions failed: {e}")

