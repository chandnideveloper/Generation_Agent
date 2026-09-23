import os
import logging
import httpx
import requests
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# Ensure environment variables are loaded (for MONGO_API_URL)
load_dotenv(override=True)

logger = logging.getLogger(__name__)
_TELEMETRY_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-activity-telemetry")

def get_mongo_api_url() -> str:
    """Fetch MONGO_API_URL from env, stripping any trailing slash."""
    url = os.getenv("MONGO_API_URL", "http://127.0.0.1:8008")
    return url.rstrip("/")

async def log_activity_async(run_id: str, project_id: str, workbook_id: str, summary: str) -> None:
    """
    Log an agent action to the central MongoDB Activity API asynchronously.
    """
    if not run_id:
        run_id = "unknown"
    if not project_id:
        project_id = "unknown"
    if not workbook_id:
        workbook_id = "unknown"
        
    payload = {
        "run_id": run_id,
        "agent_name": "Report Generation",
        "activity_summary": summary,
        "status": "running",
        "project_id": project_id,
        "workbook_id": workbook_id
    }
    
    url = f"{get_mongo_api_url()}/activities"
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=5.0)
            response.raise_for_status()
    except Exception as e:
        logger.error(f"[ActivityLogger] Failed to log async activity to {url}: {e}")

def log_activity_sync(run_id: str, project_id: str, workbook_id: str, summary: str) -> None:
    """
    Log an agent action to the central MongoDB Activity API synchronously (fire-and-forget).
    """
    if not run_id:
        run_id = "unknown"
    if not project_id:
        project_id = "unknown"
    if not workbook_id:
        workbook_id = "unknown"
        
    payload = {
        "run_id": run_id,
        "agent_name": "Report Generation",
        "activity_summary": summary,
        "status": "running",
        "project_id": project_id,
        "workbook_id": workbook_id
    }
    
    url = f"{get_mongo_api_url()}/activities"
    
    def _do_post():
        try:
            res = requests.post(url, json=payload, timeout=(1, 3))
            res.raise_for_status()
        except Exception as e:
            logger.error(f"[ActivityLogger] Failed to log sync activity to {url}: {e}")
            
    _TELEMETRY_POOL.submit(_do_post)
