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
