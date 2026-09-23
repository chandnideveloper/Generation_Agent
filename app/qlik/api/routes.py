"""HTTP surface for the generation agent."""

import io

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.qlik.config import config
from app.qlik.generator import generate
from app.qlik.package import package_store, zip_builder
from app.qlik.report.visual_catalog import MANUAL, NATIVE, SUBSTITUTIONS
from app.qlik.schemas import Deploy, DownloadRequest, GenerateRequest, GenerateResponse, Target
from app.qlik.sources.mapping_client import MappingNotFound, fetch_mapping
from app.qlik.util.ids import safe_filename
from app.qlik.util.logging_utils import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.get("/api/health")
def health_endpoint():
    # /health and / are owned by health() below (richer payload: targets,
    # deploy readiness, mapping_api/output_dir). This route used to also
    # claim /health with a shallower {"status": "ok"} body, and since it was
    # registered first, FastAPI matched it before health() ever ran -
    # silently shadowing the real health check and its `targets` field.
    return {"status": "ok", "service": "generation-agent"}


@router.post("/generate", response_model=GenerateResponse)
@router.post("/api/generate", response_model=GenerateResponse)
@router.post("/generation", response_model=GenerateResponse)
@router.post("/api/generation", response_model=GenerateResponse)
@router.post("/tmdl", response_model=GenerateResponse)
@router.post("/api/tmdl", response_model=GenerateResponse)
def generate_endpoint(request: GenerateRequest):
    """Build a Power BI package from a mapping result.

    `/tmdl` is kept as an alias so existing callers keep working.
    """
    if not any([request.mapping_result, request.app_id, request.run_id]):
        raise HTTPException(
            status_code=400,
            detail="Provide mapping_result, or an app_id / run_id to fetch it with.",
        )

    try:
        document = request.mapping_result or fetch_mapping(request.app_id, request.run_id)
    except MappingNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not load mapping: %s", exc)
        raise HTTPException(status_code=502, detail=f"Mapping fetch failed: {exc}") from exc

    try:
        return generate(document, request)
    except (TypeError, ValueError, KeyError) as exc:
        logger.exception("Generation failed")
        raise HTTPException(status_code=500, detail=f"Generation failed: {exc}") from exc


@router.post("/download")
@router.post("/api/download")
def download_endpoint(request: DownloadRequest):
    """Return the generated PBIP folder as a .zip that opens directly in
    Power BI Desktop.

    Prefers the package `/generate` already cached for this run_id
    (package_store); if nothing is cached, the mapping result is re-fetched
    and generation is re-run (push_only, no disk write, no deploy) purely to
    rebuild the same files for this download.
    """
    package = package_store.load(request.run_id)
    app_name = request.app_name

    if package is None:
        try:
            document = fetch_mapping(request.app_id, request.run_id)
        except MappingNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not load mapping for download: %s", exc)
            raise HTTPException(status_code=502, detail=f"Mapping fetch failed: {exc}") from exc

        regen_request = GenerateRequest(
            app_id=request.app_id,
            run_id=request.run_id,
            app_name=request.app_name,
            target=Target.POWERBI_DESKTOP,
            deploy=Deploy.NONE,
            write_to_disk=False,
            push_only=True,
            include_artifacts=False,
        )
        try:
            result = generate(document, regen_request)
        except (TypeError, ValueError, KeyError) as exc:
            logger.exception("Regeneration for download failed")
            raise HTTPException(status_code=500, detail=f"Generation failed: {exc}") from exc
        app_name = app_name or result.get("app_name")
        package = package_store.load(request.run_id)

    if not package:
        raise HTTPException(
            status_code=404,
            detail=f"No package found for run_id={request.run_id!r}. Run /generate first.",
        )

    zip_bytes = zip_builder.build_zip(package)
    filename = f"{safe_filename(app_name or request.run_id, 'QlikApp')}.zip"
    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/visual-support")
def visual_support():
    """What converts natively, what gets substituted, and what needs work."""
    return {
        "native": sorted(NATIVE),
        "substituted": {
            qlik: {"mapped_to": target, "reason": reason, "suggestion": suggestion}
            for qlik, (target, reason, suggestion) in SUBSTITUTIONS.items()
        },
        "manual": {
            qlik: {"reason": reason, "suggestion": suggestion}
            for qlik, (reason, suggestion) in MANUAL.items()
        },
    }


@router.get("/")
@router.get("/health")
def health():
    return {
        "status": "healthy",
        "service": "qlik-generation-agent",
        "targets": ["powerbi_desktop", "fabric", "semantic_model_only"],
        "deploy": {
            "fabric": "per-request token",
            "github": config.github_ready(),
            "devops": config.devops_ready(),
        },
        "mapping_api": config.MONGO_API_URL,
        "output_dir": config.OUTPUT_DIR,
    }
