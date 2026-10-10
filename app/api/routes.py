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

    try:
        # Check the 'generation' collection (Unified/Tableau URL)
        record = await mongo_db["generation"].find_one(query, sort=[('_id', -1)])
        if record:
            if "_id" in record:
                record["_id"] = str(record["_id"])
            return JSONResponse(status_code=200, content=record)

        # Fallback to Qlik URL
        import httpx
        from app.qlik.config import config as qlik_config
        if qlik_config.MONGO_API_URL:
            # We try querying the exact endpoints the Qlik proxy might expose
            params = query
            
            async with httpx.AsyncClient() as client:
                qlik_base = qlik_config.MONGO_API_URL.rstrip('/')
                # Check /api/records/report-generation
                qlik_url = f"{qlik_base}/api/records/report-generation"
                try:
                    resp = await client.get(qlik_url, params=params, timeout=5)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, list) and data:
                            return JSONResponse(status_code=200, content=data[0])
                        elif isinstance(data, dict):
                            return JSONResponse(status_code=200, content=data)
                except Exception as e:
                    logger.warning(f"Failed to fetch from Qlik link /api/records/report-generation: {e}")
                
                # Check /report-generation
                qlik_url_alt = f"{qlik_base}/report-generation"
                try:
                    resp = await client.get(qlik_url_alt, params=params, timeout=5)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, list) and data:
                            return JSONResponse(status_code=200, content=data[0])
                        elif isinstance(data, dict):
                            return JSONResponse(status_code=200, content=data)
                except Exception as e:
                    logger.warning(f"Failed to fetch from Qlik link /report-generation: {e}")

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
