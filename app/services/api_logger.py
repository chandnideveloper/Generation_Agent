"""API logger client for Generation Agent."""

import logging
import aiohttp
from app.config import Config

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
    """Log an agent action to the external monitoring API."""
    cfg = Config()
    mongo_url = cfg.MONGO_API_URL or "http://127.0.0.1:8008"
    url = f"{mongo_url.rstrip('/')}/agent-actions"
    data = {
        "agent_name": "Generation Agent",
        "activity_summary": action_desc,
        "action": action_desc,
        "details": details or (f"Processing app {app_id}" if app_id else "General generation action"),
        "correlation_id": run_id or app_id or "unknown",
        "run_id": run_id or "",
        "run_no": run_id or "",
        "app_id": app_id or "",
        "workbook_id": app_id or "",
        "workspace_id": workspace_id or "personal",
        "project_id": workspace_id or "personal",
        "project_name": project_name or "Unknown",
        "type": "agent_activity",
        "status": "success",
    }
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status not in [200, 201]:
                    logger.warning(f"[GenLogger] Failed to log action to MongoDB: status={resp.status}")
    except Exception as e:  # noqa: BLE001 - logging must never block generation
        logger.debug(f"[GenLogger] MongoDB action logging skipped: {e}")
