"""Unified Generation Agent Routes.

Exposes:
- POST /migrate: Main entry point for Qlik and Tableau generation and deployment
- GET /health: Health check endpoint
"""

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


@router.get(
    "/api/records/generation",
    summary="Get generation response for a given run_id, app/workbook_id, and space/project_id (Qlik or Tableau)",
)
async def get_migration_status(
    run_id: Optional[str] = None,
    project_id: Optional[str] = None,
    space_id: Optional[str] = None,
    workbook_id: Optional[str] = None,
    app_id: Optional[str] = None,
):
    if mongo_db is None:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": "MongoDB is not configured."}
        )

    # Build query based on provided parameters
    query = {}
    if run_id:
        query["run_id"] = run_id.strip()
    if project_id:
        query["project_id"] = project_id.strip()
    if space_id:
        query["space_id"] = space_id.strip()
    if workbook_id:
        query["workbook_id"] = workbook_id.strip()
    if app_id:
        query["app_id"] = app_id.strip()

    if not query:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Please provide at least one query parameter (run_id, project_id, etc.)"}
        )

    import re
    # Clean and normalize IDs
    clean_app_id = re.sub(r"[\s_]+", "-", app_id.strip()) if app_id else None
    clean_wb_id = re.sub(r"[\s_]+", "-", workbook_id.strip()) if workbook_id else None
    eff_app_id = clean_app_id or clean_wb_id or (app_id.strip() if app_id else (workbook_id.strip() if workbook_id else None))
    eff_space_id = (space_id.strip() if space_id else (project_id.strip() if project_id else None))
    eff_run_id = run_id.strip() if run_id else None

    try:
        import httpx
        from app.qlik.config import config as qlik_config

        # 1. Try Qlik MongoDB Microservice
        if qlik_config.MONGO_API_URL:
            qlik_base = qlik_config.MONGO_API_URL.rstrip('/')
            qlik_url = f"{qlik_base}/api/records/generation"

            # Prepare param candidates (with normalized app_id, default space_id, etc.)
            candidate_params = []
            if eff_run_id:
                candidate_params.append({
                    "space_id": eff_space_id or "personal",
                    "app_id": eff_app_id or "926e0fd3-a729-41af-91b8-4f9e9787d7da",
                    "run_id": eff_run_id,
                })
                if app_id and app_id.strip() != eff_app_id:
                    candidate_params.append({
                        "space_id": eff_space_id or "personal",
                        "app_id": app_id.strip(),
                        "run_id": eff_run_id,
                    })

            async with httpx.AsyncClient() as client:
                for p in candidate_params:
                    try:
                        resp = await client.get(qlik_url, params=p, timeout=15)
                        if resp.status_code == 200:
                            data = resp.json()
                            if data:
                                if isinstance(data, list):
                                    return JSONResponse(status_code=200, content=data[0])
                                return JSONResponse(status_code=200, content=data)
                    except Exception as e:
                        logger.warning(f"Error querying Qlik generation records with {p}: {e}")

        # 2. Try Tableau MongoDB (mongo_db['generation'])
        if mongo_db is not None:
            tb_query = {}
            if eff_run_id:
                tb_query["run_id"] = eff_run_id
            if eff_space_id:
                tb_query["project_id"] = eff_space_id
            if eff_app_id:
                tb_query["workbook_id"] = eff_app_id

            record = await mongo_db["generation"].find_one(tb_query, sort=[('_id', -1)])
            if record:
                if "_id" in record:
                    record["_id"] = str(record["_id"])
                return JSONResponse(status_code=200, content=record)

        return JSONResponse(
            status_code=404,
            content={"status": "error", "message": f"No generation response found for query: {query}"}
        )
    except Exception as e:
        logger.exception(f"[GET /api/records/generation] Error fetching record: {e}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)}
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
