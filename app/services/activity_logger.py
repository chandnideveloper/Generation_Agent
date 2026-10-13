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

async def log_activity_async(run_id: str, project_id: str, workbook_id: str, summary: str, status: str = "running") -> None:
    """
    Log an agent action to the central MongoDB Activity API asynchronously and directly to MongoDB.
    """
    if not run_id:
        run_id = "unknown"
    if not project_id:
        project_id = "unknown"
    if not workbook_id:
        workbook_id = "unknown"
        
    payload = {
        "run_id": run_id,
        "run_no": run_id,
        "agent_name": "Generation Agent",
        "activity_summary": summary,
        "action": summary,
        "status": status,
        "project_id": project_id,
        "space_id": project_id,
        "workbook_id": workbook_id,
        "app_id": workbook_id,
        "type": "agent_activity",
    }
    
    # 1. Direct MongoDB insertion if MONGODB_URL is available
    mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URL")
    if mongo_uri and mongo_uri.startswith("mongodb"):
        try:
            import pymongo
            client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            for db_name in ["QT2F", "QT2F_Tableau"]:
                try:
                    client[db_name]["activities"].insert_one(dict(payload))
                    client[db_name]["agent_logs"].insert_one(dict(payload))
                except Exception:
                    pass
        except Exception as err:
            logger.debug(f"[ActivityLogger] Direct MongoDB insert failed: {err}")

    # 2. HTTP POST fallback to /api/records/activities
    candidate_bases = []
    for raw in [os.getenv("MONGODB_URL"), get_mongo_api_url(), os.getenv("TELEMETRY_LOCAL_API")]:
        if raw and raw.startswith("http"):
            b = raw.rstrip("/")
            if b not in candidate_bases:
                candidate_bases.append(b)

    for base in candidate_bases:
        for endpoint in ["/api/records/activities", "/activities"]:
            url = f"{base.rstrip('/')}{endpoint}"
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, json=payload, timeout=3.0)
                    if response.status_code in (200, 201):
                        return
            except Exception as e:
                logger.debug(f"[ActivityLogger] Failed to log async activity to {url}: {e}")

def log_activity_sync(run_id: str, project_id: str, workbook_id: str, summary: str, status: str = "running") -> None:
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
        "run_no": run_id,
        "agent_name": "Generation Agent",
        "activity_summary": summary,
        "action": summary,
        "status": status,
        "project_id": project_id,
        "space_id": project_id,
        "workbook_id": workbook_id,
        "app_id": workbook_id,
        "type": "agent_activity",
    }
    
    def _do_post():
        mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URL")
        if mongo_uri and mongo_uri.startswith("mongodb"):
            try:
                import pymongo
                client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
                for db_name in ["QT2F", "QT2F_Tableau"]:
                    try:
                        client[db_name]["activities"].insert_one(dict(payload))
                        client[db_name]["agent_logs"].insert_one(dict(payload))
                    except Exception:
                        pass
            except Exception as err:
                logger.debug(f"[ActivityLogger] Direct MongoDB insert failed: {err}")

        candidate_bases = []
        for raw in [os.getenv("MONGODB_URL"), get_mongo_api_url(), os.getenv("TELEMETRY_LOCAL_API")]:
            if raw and raw.startswith("http"):
                b = raw.rstrip("/")
                if b not in candidate_bases:
                    candidate_bases.append(b)

        for base in candidate_bases:
            for endpoint in ["/api/records/activities", "/activities"]:
                url = f"{base.rstrip('/')}{endpoint}"
                try:
                    res = requests.post(url, json=payload, timeout=(1, 3))
                    if res.status_code in (200, 201):
                        return
                except Exception as e:
                    logger.debug(f"[ActivityLogger] Failed to log sync activity to {url}: {e}")
            
    _TELEMETRY_POOL.submit(_do_post)
