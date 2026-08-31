"""Generation orchestrator.

    mapping payload -> semantic model (TMDL)
                    -> report (PBIR)          [unless semantic_model_only]
                    -> PBIP package
                    -> disk / Fabric / GitHub / DevOps

The artifacts are identical for `powerbi_desktop` and `fabric`; only the
packaging and destination differ, so both targets share one code path.
"""

import os
from typing import Any, Dict

from app.config import config
from app.deploy import devops_deployer, fabric_deployer, github_deployer
from app.model.semantic_model import build_semantic_model
from app.package import artifacts as artifact_builder
from app.package import package_store, pbip_packager, validator
from app.report.report_writer import build_report
from app.schemas import Deploy, GenerateRequest, Target
from app.util.ids import safe_filename, slug
from app.util.logging_utils import get_logger
from app.util import payload as P
from app.util.payload import app_identity, unwrap_mapping


logger = get_logger(__name__)


def _log_action_sync(action: str, request: GenerateRequest, app_name: str, details: str = "") -> None:
    """Log an agent action to MongoDB agent_actions collection."""
    import requests
    run_id = request.run_id or "unknown"
    workspace_id = request.workspace_id or request.space_id or "personal"
    app_id = request.app_id or "unknown"
    payload = {
        "agent_name": "Generation Agent",
        "activity_summary": action,
        "action": action,
        "details": details or action,
        "run_id": run_id,
        "run_no": run_id,
        "correlation_id": run_id,
        "app_id": app_id,
        "workbook_id": app_id,
        "workspace_id": workspace_id,
        "project_id": workspace_id,
        "project_name": app_name or "Unknown",
        "type": "agent_activity",
        "status": "success",
    }
    bases = [
        "http://127.0.0.1:8008",
        "http://localhost:8008",
        "http://127.0.0.1:8005",
        "http://localhost:8005",
        config.MONGO_API_URL,
    ]
    for base in bases:
        if not base:
            continue
        try:
            url = f"{base.rstrip('/')}/agent-actions"
            res = requests.post(url, json=payload, timeout=(1, 3))
            if res.status_code in (200, 201):
                return
        except Exception:
            pass


def generate(mapping_document: Dict[str, Any], request: GenerateRequest) -> Dict[str, Any]:
    """Build (and optionally deploy) a Power BI package from a mapping result."""
    mapping = unwrap_mapping(mapping_document)
    identity = app_identity(mapping)
    app_name = safe_filename(request.app_name or identity["app_name"], "QlikApp")

    _log_action_sync("Generating Power BI TMDL model & semantic relationships", request, app_name, f"Starting generation for app {app_name}")

    if request.offline_sample_data:
        from app.model import offline_source
        mapping = dict(mapping)
        mapping["tables"] = offline_source.apply(P.tables(mapping))

    model_files, model_report = build_semantic_model(mapping, app_name)
    _log_action_sync("Generated Power BI TMDL model", request, app_name, f"Emitted {len(model_files)} TMDL model files")

    report_files: Dict[str, str] = {}
    notes = []
    report_stats: Dict[str, Any] = {}
    if request.target != Target.SEMANTIC_MODEL_ONLY:
        _log_action_sync("Generating PBIR visual layout & JSON definitions", request, app_name, "Building PBIR visual definitions")
        report_files, notes, report_stats = build_report(
            mapping, app_name, f"../{app_name}.SemanticModel"
        )
        _log_action_sync("Generated PBIR report files", request, app_name, f"Emitted {len(report_files)} report & visual files")

    package = pbip_packager.build_package(app_name, model_files, report_files)
    run_id = request.run_id or identity["run_id"]
    package_store.save(run_id, package, request.app_id or identity["app_id"])

    # Structural validation of the emitted package — the checks here are the
    # ones that actually stop Desktop and Fabric opening a project.
    validation = validator.validate(package, app_name)

    result: Dict[str, Any] = {
        "status": "success" if validation["ok"] else "warning",
        "validation": validation,
        "target": request.target.value,
        "deploy": request.deploy.value,
        "app_id": request.app_id or identity["app_id"],
        "app_name": app_name,
        "run_id": run_id,
        "file_count": len(package),
        "total_bytes": artifact_builder.measure(package)["total_bytes"],
        "summary": {"semantic_model": model_report, "report": report_stats},
        "visual_notes": notes,
        "deployment": {},
    }

    _log_action_sync("Created Fabric PBIP deployment package", request, app_name, f"Package created: {len(package)} files, {result['total_bytes']} bytes")

    # Desktop needs the folder on disk; other targets only if asked; push_only
    # overrides both — the package is already cached above for /download, and
    # _deploy() below runs unconditionally, so push_only skips no real work.
    if not request.push_only and (request.write_to_disk or request.target == Target.POWERBI_DESKTOP):
        destination = os.path.join(
            config.OUTPUT_DIR, slug(app_name), slug(run_id or "run")
        )
        result.update(pbip_packager.write_to_disk(package, destination))

    # Return the generated files themselves, not just counts, unless the
    # caller opted out (a large model's full text can dwarf the response).
    if request.include_artifacts:
        if request.index_only:
            result["artifact_index"] = artifact_builder.build_index(package, app_name)
        else:
            tree = artifact_builder.build_tree(package, app_name)
            result["artifacts"] = tree
            result["semantic_model"] = tree.get("semantic_model", {})
            result["report"] = tree.get("report", {})

    result["deployment"] = _deploy(package, app_name, request, result)
    result["message"] = _message(result)

    # Persist report generation result in MongoDB
    _save_to_mongodb(result, request, app_name)
    _log_action_sync("Report generation completed successfully", request, app_name, f"Completed status={result['status']}, {len(package)} files")

    return result


def _save_to_mongodb(result: Dict[str, Any], request: GenerateRequest, app_name: str) -> None:
    """Persist the generation result in the MongoDB microservice."""
    import requests
    doc = {
        "id": request.run_id or request.app_id or slug(app_name),
        "app_id": request.app_id,
        "run_id": request.run_id,
        "workspace_id": request.workspace_id or request.space_id or "personal",
        "space_id": request.space_id,
        "app_name": app_name,
        "folder_name": request.department_repo or "Qlik_Migrated",
        "report_result": {
            "status": result.get("status"),
            "message": result.get("message"),
            "file_count": result.get("file_count"),
            "total_bytes": result.get("total_bytes"),
            "summary": result.get("summary"),
            "visual_notes": [n.dict() if hasattr(n, "dict") else n for n in result.get("visual_notes", [])],
            "validation": result.get("validation"),
            "deployment": result.get("deployment"),
            "output_path": result.get("output_path"),
        },
    }

    bases = [
        "http://127.0.0.1:8008",
        "http://localhost:8008",
        "http://127.0.0.1:8005",
        "http://localhost:8005",
        config.MONGO_API_URL,
    ]

    for base in bases:
        if not base:
            continue
        try:
            url = f"{base.rstrip('/')}/report-generation"
            res = requests.post(url, json=doc, timeout=(2, 5))
            if res.status_code in (200, 201):
                logger.info("Saved report generation result to MongoDB: %s", url)
                return
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not save report generation to %s: %s", base, exc)


def _deploy(
    package: Dict[str, str], app_name: str, request: GenerateRequest, result: Dict[str, Any]
) -> Dict[str, Any]:
    """Run the requested deployment. Failures are reported, not raised."""
    if request.deploy == Deploy.NONE:
        return {"status": "skipped", "reason": "no deployment requested"}

    prefix = "/".join(
        part for part in (
            request.department_repo or config.DESTINATION_BASE,
            request.folder_name or slug(app_name),
        ) if part
    )

    try:
        if request.deploy == Deploy.FABRIC:
            return fabric_deployer.deploy(
                package, app_name, request.workspace_id, request.fabric_access_token
            )
        if request.deploy == Deploy.GITHUB:
            return github_deployer.deploy(
                package=package,
                prefix=prefix,
                branch=request.branch,
                message=request.commit_message,
                token=request.git_pat or request.token or request.github_pat or request.pat,
                org=request.org or request.git_org or request.github_org,
                repo=request.repo or request.git_repo or request.github_repo,
            )
        if request.deploy == Deploy.DEVOPS:
            return devops_deployer.deploy(
                package, prefix, request.branch, request.commit_message,
                repo=request.repo,
            )
    except Exception as exc:  # noqa: BLE001
        # The package is already built and on disk; a deployment failure must
        # not discard it.
        logger.error("Deployment via %s failed: %s", request.deploy.value, exc)
        return {"status": "error", "provider": request.deploy.value, "error": str(exc)}

    return {"status": "skipped", "reason": f"unknown target {request.deploy}"}


def _message(result: Dict[str, Any]) -> str:
    model = result["summary"].get("semantic_model") or {}
    report = result["summary"].get("report") or {}
    parts = [
        f"{model.get('tables', 0)} tables",
        f"{model.get('measures', 0)} measures",
        f"{model.get('relationships_written', 0)} relationships",
    ]
    if report:
        parts.append(f"{report.get('pages', 0)} pages")
        parts.append(f"{report.get('visuals', 0)} visuals")
    detail = ", ".join(parts)

    attention = []
    visuals_to_review = len([n for n in result["visual_notes"] if n["severity"] != "info"])
    if visuals_to_review:
        attention.append(f"{visuals_to_review} visual(s) substituted")
    dax_to_rewrite = model.get("dax_needs_rewrite", 0)
    if dax_to_rewrite:
        attention.append(f"{dax_to_rewrite} measure(s) need a DAX rewrite")
    skipped = len(model.get("relationships_skipped") or [])
    if skipped:
        attention.append(f"{skipped} relationship(s) skipped")

    suffix = f"; needs review: {', '.join(attention)}" if attention else ""
    return f"Generated {detail}{suffix}."
