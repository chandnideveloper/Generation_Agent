import os
import uuid
import datetime
import logging
from typing import Optional, Dict, Any
import httpx
import requests
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)
_TELEMETRY_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-activity-telemetry")

def get_mongo_api_url() -> str:
    """Fetch MONGO_API_URL from env, stripping any trailing slash."""
    url = os.getenv("MONGO_API_URL", "https://aodeclx4wyi6727uozoyktnqb40fyzvt.lambda-url.ap-southeast-2.on.aws")
    return url.rstrip("/")

def get_tableau_mongo_url() -> str:
    """Fetch MONGODB_URL for Tableau from env, stripping any trailing slash."""
    url = os.getenv("MONGODB_URL", "https://rizio6jk6d4psp4xzqzy2pl4nm0eibme.lambda-url.ap-southeast-2.on.aws")
    return url.rstrip("/")

def _resolve_project_name(project_name: Optional[str] = None) -> str:
    """Dynamically resolve project_name from parameter, contextvars, or environment."""
    if project_name and str(project_name).strip() and str(project_name).strip() not in ("Unknown", "null", "None"):
        return str(project_name).strip()
    try:
        from app.tableau.core.logging_utils import get_project_name
        ctx_tb = get_project_name()
        if ctx_tb and ctx_tb not in ("Unknown", "null", "None"):
            return str(ctx_tb).strip()
    except Exception:
        pass
    try:
        from app.qlik.util.logging_utils import _qlik_project_name_ctx
        ctx_ql = _qlik_project_name_ctx.get()
        if ctx_ql and ctx_ql not in ("Unknown", "null", "None"):
            return str(ctx_ql).strip()
    except Exception:
        pass
    env_name = os.getenv("PROJECT_NAME")
    if env_name and env_name not in ("Unknown", "null", "None"):
        return str(env_name).strip()
    return "Unknown"


async def log_activity_async(
    run_id: str,
    project_id: str,
    workbook_id: str,
    summary: str,
    status: str = "running",
    source_type: str = "tableau",
    project_name: Optional[str] = None,
) -> None:
    """
    Log an agent action strictly into the 'agent_actions' collection of the corresponding database:
    - If source_type == 'qlik': QT2F database, agent_actions collection.
    - If source_type == 'tableau': QT2F_Tableau database, agent_actions collection.
    """
    if not run_id:
        run_id = "unknown"
    if not project_id:
        project_id = "unknown"
    if not workbook_id:
        workbook_id = "unknown"
        
    eff_project_name = _resolve_project_name(project_name)

    eff_agent_name = os.getenv("AGENT_NAME", "BI Engineering Agent")
    payload = {
        "id": str(uuid.uuid4()),
        "run_id": run_id,
        "run_no": run_id,
        "project_name": eff_project_name,
        "agent_name": eff_agent_name,
        "activity_summary": summary,
        "action": summary,
        "status": status,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "project_id": project_id,
        "space_id": project_id,
        "workbook_id": workbook_id,
        "app_id": workbook_id,
        "type": "agent_action",
        "payload": {},
    }
    
    target_db = "QT2F" if source_type == "qlik" else "QT2F_Tableau"

    # 1. Direct MongoDB insertion strictly to target_db.agent_actions and target_db.activities
    mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URL")
    if mongo_uri and mongo_uri.startswith("mongodb"):
        try:
            import pymongo
            client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            client[target_db]["agent_actions"].insert_one(dict(payload))
            client[target_db]["activities"].insert_one(dict(payload))
            return
        except Exception as err:
            logger.debug(f"[ActivityLogger] Direct MongoDB insert to {target_db}.agent_actions failed: {err}")

    # 2. HTTP POST fallback to microservice /api/records/activities
    target_base = get_mongo_api_url() if source_type == "qlik" else get_tableau_mongo_url()
    url = f"{target_base}/api/records/activities"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload, timeout=3.0)
            alt_name = "Generation Agent" if payload.get("agent_name") == "BI Engineering Agent" else "BI Engineering Agent"
            alt_payload = dict(payload, id=str(uuid.uuid4()), agent_name=alt_name)
            await client.post(url, json=alt_payload, timeout=3.0)
    except Exception as e:
        logger.debug(f"[ActivityLogger] Failed to log async activity to {url}: {e}")

def log_activity_sync(
    run_id: str,
    project_id: str,
    workbook_id: str,
    summary: str,
    status: str = "running",
    source_type: str = "tableau",
    project_name: Optional[str] = None,
) -> None:
    """
    Log an agent action strictly into the 'agent_actions' collection of the corresponding database (fire-and-forget).
    """
    if not run_id:
        run_id = "unknown"
    if not project_id:
        project_id = "unknown"
    if not workbook_id:
        workbook_id = "unknown"

    eff_project_name = _resolve_project_name(project_name)
        
    eff_agent_name = os.getenv("AGENT_NAME", "BI Engineering Agent")
    payload = {
        "id": str(uuid.uuid4()),
        "run_id": run_id,
        "run_no": run_id,
        "project_name": eff_project_name,
        "agent_name": eff_agent_name,
        "activity_summary": summary,
        "action": summary,
        "status": status,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "project_id": project_id,
        "space_id": project_id,
        "workbook_id": workbook_id,
        "app_id": workbook_id,
        "type": "agent_action",
        "payload": {},
    }
    
    target_db = "QT2F" if source_type == "qlik" else "QT2F_Tableau"

    def _do_post():
        mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URL")
        if mongo_uri and mongo_uri.startswith("mongodb"):
            try:
                import pymongo
                client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
                client[target_db]["agent_actions"].insert_one(dict(payload))
                client[target_db]["activities"].insert_one(dict(payload))
                return
            except Exception as err:
                logger.debug(f"[ActivityLogger] Direct MongoDB insert to {target_db}.agent_actions failed: {err}")

        target_base = get_mongo_api_url() if source_type == "qlik" else get_tableau_mongo_url()
        url = f"{target_base}/api/records/activities"
        try:
            requests.post(url, json=payload, timeout=(1, 3))
            alt_name = "Generation Agent" if payload.get("agent_name") == "BI Engineering Agent" else "BI Engineering Agent"
            alt_payload = dict(payload, id=str(uuid.uuid4()), agent_name=alt_name)
            requests.post(url, json=alt_payload, timeout=(1, 3))
        except Exception as e:
            logger.debug(f"[ActivityLogger] Failed to log sync activity to {url}: {e}")
            
    _TELEMETRY_POOL.submit(_do_post)

