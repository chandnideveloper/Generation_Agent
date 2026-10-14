"""Unified Generation Agent Routes.

Exposes:
- POST /migrate: Main entry point for Qlik and Tableau generation and deployment
- GET /health: Health check endpoint
"""

import os
import logging
from typing import Optional
from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import JSONResponse

from app.api.schemas import (
    UnifiedErrorResponse,
    UnifiedMigrateRequest,
    UnifiedMigrateResponse,
)
from app.services.orchestrator import orchestrator
from app.tableau.core.config import mongo_db

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_bearer_token(authorization: Optional[str] = Header(None)) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return authorization


@router.get("/health", tags=["Health"])
async def health():
    return {
        "status": "ok",
        "service": "unified-generation-agent",
        "supported_sources": ["qlik", "tableau"],
        "target": "fabric",
    }


def _extract_record_timestamp(rec: Any) -> float:
    if not isinstance(rec, dict):
        return 0.0
    import datetime
    for key in ("updated_at", "created_at", "timestamp"):
        val = rec.get(key)
        if val and isinstance(val, str):
            try:
                clean = val.replace("Z", "+00:00")
                return datetime.datetime.fromisoformat(clean).timestamp()
            except Exception:
                pass
    if rec.get("_ts"):
        try:
            return float(rec["_ts"])
        except Exception:
            pass
    payload = rec.get("payload")
    if isinstance(payload, dict):
        for key in ("updated_at", "created_at", "timestamp"):
            val = payload.get(key)
            if val and isinstance(val, str):
                try:
                    clean = val.replace("Z", "+00:00")
                    return datetime.datetime.fromisoformat(clean).timestamp()
                except Exception:
                    pass
    return 0.0


@router.get(
    "/api/records/generation",
    summary="Get latest generation response for a given run_id, app/workbook_id, and space/project_id (Qlik or Tableau)",
)
async def get_migration_status(
    run_id: Optional[str] = None,
    project_id: Optional[str] = None,
    space_id: Optional[str] = None,
    workbook_id: Optional[str] = None,
    app_id: Optional[str] = None,
    source_type: Optional[str] = None,
    source: Optional[str] = None,
):
    # Build query check based on provided parameters
    eff_run_id = run_id.strip() if run_id else None
    eff_project_id = project_id.strip() if project_id else None
    eff_space_id = space_id.strip() if space_id else None
    eff_workbook_id = workbook_id.strip() if workbook_id else None
    eff_app_id = app_id.strip() if app_id else None

    if not any([eff_run_id, eff_project_id, eff_space_id, eff_workbook_id, eff_app_id]):
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Please provide at least one query parameter (run_id, project_id, workbook_id, etc.)"}
        )

    import re
    import httpx
    import asyncio
    from app.qlik.config import config as qlik_config

    src = (source_type or source or "").lower().strip()
    is_tableau_specific = src == "tableau" or (eff_workbook_id and not eff_app_id) or (eff_project_id and not eff_space_id)
    is_qlik_specific = src == "qlik" or (eff_app_id and not eff_workbook_id) or (eff_space_id and not eff_project_id)

    # Candidate records collected: list of dicts with _source tag
    candidates = []

    # 1. Fetch from Tableau Microservice / Proxy
    tb_base = (os.getenv("MONGODB_URL") or "https://rizio6jk6d4psp4xzqzy2pl4nm0eibme.lambda-url.ap-southeast-2.on.aws").rstrip('/')
    tb_url = f"{tb_base}/api/records/generation"

    # We only skip Tableau if caller explicitly requested Qlik
    if not is_qlik_specific:
        tb_param_sets = []
        # Case A: Full specific parameters
        target_proj = eff_project_id or eff_space_id
        target_wb = eff_workbook_id or eff_app_id
        if eff_run_id and target_proj and target_wb:
            tb_param_sets.append({
                "run_id": eff_run_id,
                "project_id": target_proj,
                "workbook_id": target_wb,
            })
        elif eff_run_id:
            # Case B: Only run_id provided
            tb_param_sets.append({"run_id": eff_run_id})

        async with httpx.AsyncClient() as client:
            for p in tb_param_sets:
                for attempt in range(2):
                    try:
                        resp = await client.get(tb_url, params=p, timeout=15)
                        if resp.status_code == 200:
                            data = resp.json()
                            if isinstance(data, list):
                                for item in data:
                                    if isinstance(item, dict):
                                        item["_resolved_source"] = "tableau"
                                        candidates.append(item)
                            elif isinstance(data, dict):
                                data["_resolved_source"] = "tableau"
                                candidates.append(data)
                            break
                        elif resp.status_code == 429:
                            await asyncio.sleep(1.0)
                            continue
                        elif resp.status_code == 404:
                            break
                    except Exception as e:
                        logger.debug(f"[Tableau GET generation] attempt {attempt+1} error: {e}")

    # 2. Fetch from Qlik Microservice
    qlik_base = (qlik_config.MONGO_API_URL or os.getenv("MONGO_API_URL") or "https://aodeclx4wyi6727uozoyktnqb40fyzvt.lambda-url.ap-southeast-2.on.aws").rstrip('/')
    qlik_url = f"{qlik_base}/api/records/generation"

    # We only skip Qlik if caller explicitly requested Tableau
    if not is_tableau_specific:
        qlik_param_sets = []
        target_space = eff_space_id or eff_project_id or "personal"
        target_app = eff_app_id or (re.sub(r"[\s_]+", "-", eff_workbook_id) if eff_workbook_id else None)
        if eff_run_id:
            if target_app:
                qlik_param_sets.append({
                    "run_id": eff_run_id,
                    "space_id": target_space,
                    "app_id": target_app,
                })
            else:
                qlik_param_sets.append({
                    "run_id": eff_run_id,
                    "space_id": target_space,
                    "app_id": "926e0fd3-a729-41af-91b8-4f9e9787d7da",
                })

        async with httpx.AsyncClient() as client:
            for p in qlik_param_sets:
                for attempt in range(2):
                    try:
                        resp = await client.get(qlik_url, params=p, timeout=15)
                        if resp.status_code == 200:
                            data = resp.json()
                            if isinstance(data, list):
                                for item in data:
                                    if isinstance(item, dict):
                                        item["_resolved_source"] = "qlik"
                                        candidates.append(item)
                            elif isinstance(data, dict):
                                data["_resolved_source"] = "qlik"
                                candidates.append(data)
                            break
                        elif resp.status_code == 429:
                            await asyncio.sleep(1.0)
                            continue
                        elif resp.status_code == 404:
                            break
                    except Exception as e:
                        logger.debug(f"[Qlik GET generation] attempt {attempt+1} error: {e}")

    # 3. Direct MongoDB query across QT2F_Tableau and QT2F if mongodb:// URI configured
    mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URL")
    if mongo_uri and mongo_uri.startswith("mongodb"):
        try:
            import pymongo
            mongo_client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            target_dbs = []
            if is_tableau_specific:
                target_dbs = ["QT2F_Tableau"]
            elif is_qlik_specific:
                target_dbs = ["QT2F"]
            else:
                target_dbs = ["QT2F_Tableau", "QT2F"]

            for db_name in target_dbs:
                coll = mongo_client[db_name]["generation_results"]
                find_q = {}
                if eff_run_id:
                    find_q["run_id"] = eff_run_id
                docs = list(coll.find(find_q).sort([('_id', -1), ('created_at', -1)]).limit(5))
                for doc in docs:
                    if "_id" in doc:
                        doc["_id"] = str(doc["_id"])
                    doc["_resolved_source"] = "tableau" if db_name == "QT2F_Tableau" else "qlik"
                    candidates.append(doc)
        except Exception as pe:
            logger.debug(f"Direct PyMongo generation_results query failed: {pe}")

    if not candidates:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "message": f"No generation response found for query: run_id={eff_run_id}, project_id={eff_project_id}, workbook_id={eff_workbook_id}"}
        )

    # Filter candidates by platform if specific platform was requested
    filtered = candidates
    if is_tableau_specific:
        tb_filtered = [c for c in candidates if c.get("_resolved_source") == "tableau"]
        if tb_filtered:
            filtered = tb_filtered
    elif is_qlik_specific:
        qlik_filtered = [c for c in candidates if c.get("_resolved_source") == "qlik"]
        if qlik_filtered:
            filtered = qlik_filtered

    # Sort all matching candidates: completed status first, then by latest timestamp epoch descending!
    def _rank_candidate(c: dict):
        status_rank = 1 if str(c.get("status", "")).lower() == "completed" else 0
        ts_rank = _extract_record_timestamp(c)
        return (status_rank, ts_rank)

    sorted_candidates = sorted(filtered, key=_rank_candidate, reverse=True)
    best_record = sorted_candidates[0]
    best_record.pop("_resolved_source", None)

    return JSONResponse(status_code=200, content=best_record)


@router.get(
    "/api/records/activities",
    summary="Get agent actions / activities trail for a given run_id",
)
@router.get(
    "/activities",
    summary="Alias for /api/records/activities",
)
@router.get(
    "/api/records/activities/qlik",
    summary="Alias for Qlik agent activities",
)
async def get_agent_activities(
    run_id: Optional[str] = None,
    space_id: Optional[str] = None,
    app_id: Optional[str] = None,
):
    if not run_id:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "run_id parameter is required."}
        )

    import os
    # 1. First check MongoDB directly
    mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URL")
    if mongo_uri and mongo_uri.startswith("mongodb"):
        try:
            import pymongo
            client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
            for db_name in ["QT2F", "QT2F_Tableau"]:
                coll = client[db_name]["agent_actions"]
                docs = list(coll.find({"run_id": run_id.strip()}).sort("_id", -1))
                if not docs:
                    docs = list(client[db_name]["activities"].find({"run_id": run_id.strip()}).sort("_id", -1))
                if docs:
                    for d in docs:
                        if "_id" in d:
                            d["_id"] = str(d["_id"])
                    return JSONResponse(status_code=200, content=docs)
        except Exception as err:
            logger.warning(f"Direct MongoDB query for activities failed: {err}")

    # 2. Check remote microservice
    import httpx
    from app.qlik.config import config as qlik_config
    bases = [
        qlik_config.MONGO_API_URL,
        os.getenv("TELEMETRY_LOCAL_API"),
    ]

    for base in bases:
        if not base or not base.startswith("http"):
            continue
        url = f"{base.rstrip('/')}/api/records/activities"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params={"run_id": run_id.strip()}, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    return JSONResponse(status_code=200, content=data)
        except Exception as e:
            logger.warning(f"Failed to fetch activities from {url}: {e}")

    return JSONResponse(
        status_code=404,
        content={"status": "error", "message": f"No activities found for run_id={run_id}"}
    )



@router.post(
    "/migrate",
    response_model=UnifiedMigrateResponse,
    responses={
        400: {"model": UnifiedErrorResponse},
        404: {"model": UnifiedErrorResponse},
        500: {"model": UnifiedErrorResponse},
    },
    summary="Migrate Qlik or Tableau report to Fabric (TMDL/PBIR) deployed to GitHub",
)
async def migrate(
    request: UnifiedMigrateRequest,
    token: Optional[str] = Depends(_get_bearer_token),
):
    logger.info(
        f"[POST /migrate] Received request: source_type={request.source_type}, run_id={request.run_id}"
    )

    try:
        response = await orchestrator.execute_migration(request, client_token=token)
        return response

    except ValueError as ve:
        err_msg = str(ve)
        is_not_found = "not found" in err_msg.lower()
        status_code = status.HTTP_404_NOT_FOUND if is_not_found else status.HTTP_400_BAD_REQUEST
        error_code = "RESOURCE_NOT_FOUND" if is_not_found else "INVALID_REQUEST"
        logger.warning(f"[POST /migrate] Client error ({status_code}): {err_msg}")
        return JSONResponse(
            status_code=status_code,
            content=UnifiedErrorResponse(
                status="error",
                source_type=getattr(request, "source_type", None),
                run_id=getattr(request, "run_id", None),
                service=f"{request.source_type}-generation",
                error_code=error_code,
                message=err_msg,
            ).model_dump(),
        )

    except RuntimeError as re:
        logger.error(f"[POST /migrate] Generation runtime error: {re}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=UnifiedErrorResponse(
                status="error",
                source_type=getattr(request, "source_type", None),
                run_id=getattr(request, "run_id", None),
                service=f"{request.source_type}-generation",
                error_code="GENERATION_EXECUTION_ERROR",
                message=str(re),
            ).model_dump(),
        )

    except Exception as e:
        logger.exception(f"[POST /migrate] Unexpected exception: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=UnifiedErrorResponse(
                status="error",
                source_type=getattr(request, "source_type", None),
                run_id=getattr(request, "run_id", None),
                service="unified-generation-agent",
                error_code="INTERNAL_SERVER_ERROR",
                message=f"An unexpected error occurred during migration: {str(e)}",
            ).model_dump(),
        )
