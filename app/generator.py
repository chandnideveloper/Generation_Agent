"""Generation orchestrator.

    mapping payload -> semantic model (TMDL)
                    -> report (PBIR)          [unless semantic_model_only]
                    -> PBIP package
                    -> disk / Fabric / GitHub / DevOps

The artifacts are identical for `powerbi_desktop` and `fabric`; only the
packaging and destination differ, so both targets share one code path.
"""

import datetime
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from app.config import config
from app.deploy import devops_deployer, fabric_deployer, github_deployer
from app.deploy import fabric_data_provisioner
from app.model.semantic_model import build_semantic_model
from app.model.table_tmdl import supabase_file_url
from app.package import artifacts as artifact_builder
from app.package import package_store, pbip_packager, validator
from app.package.comprehensive_validator import run_comprehensive_validation
from app.report.report_writer import build_report
from app.schemas import Deploy, GenerateRequest, Target
from app.util.ids import safe_filename, slug
from app.util.logging_utils import get_logger
from app.util import payload as P
from app.util.payload import app_identity, unwrap_mapping


logger = get_logger(__name__)

# Agent-action/result logging is telemetry, not part of the generation
# result - it must never add its own network latency (or, worse, hang
# indefinitely) to the request path. `generate()` is a plain sync function
# called ~12 times per run for distinct milestones, and used to `requests.
# post` sequentially against up to 5 candidate bases *inline* on the calling
# thread; when none of those bases were reachable (a slow/unset
# MONGO_API_URL, no local logging sidecar running), every single call
# blocked for its full per-attempt timeout, multiplying into tens of seconds
# of pure dead time per run - and once one attempt's connect actually hung
# rather than being refused, that time was unbounded. Dispatching to a
# small background pool makes every telemetry call fire-and-forget, exactly
# like the equivalent fix already applied on the mapping-agent side.
_TELEMETRY_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-telemetry")


def _post_with_fallback(path: str, payload: Dict[str, Any], timeout: tuple = (0.2, 0.5)) -> None:
    import requests
    bases = [
        "http://127.0.0.1:8008",
        config.MONGO_API_URL,
    ]
    for base in bases:
        if not base:
            continue
        try:
            url = f"{base.rstrip('/')}/{path.lstrip('/')}"
            res = requests.post(url, json=payload, timeout=timeout)
            if res.status_code in (200, 201):
                return
        except Exception:
            pass


def _log_action_sync(action: str, request: GenerateRequest, app_name: str, details: str = "") -> None:
    """Record one agent action for the run's activity trail, without
    blocking generation on the logging endpoint being reachable."""
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
    _TELEMETRY_POOL.submit(_post_with_fallback, "agent-actions", payload, (1, 3))


def generate(mapping_document: Dict[str, Any], request: GenerateRequest) -> Dict[str, Any]:
    """Build (and optionally deploy) a Power BI package from a mapping result."""
    mapping = unwrap_mapping(mapping_document)
    identity = app_identity(mapping)

    # 1. Fully dynamic app_name with timestamp (%Y%m%d_%H%M%S)
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    ts_str = now_utc.strftime("%Y%m%d_%H%M%S")
    app_name_raw = request.app_name or identity.get("app_name") or "App"
    # Clean base name by converting spaces and special characters to underscores
    clean_base = re.sub(r'[^A-Za-z0-9]+', '_', str(app_name_raw)).strip('_')
    # Strip any preexisting timestamp to avoid chaining timestamps on repeated runs
    clean_base = re.sub(r'_\d{8}_\d{6}$', '', clean_base) or "App"
    app_name = f"{clean_base}_{ts_str}"

    # 2. Fully dynamic run_id with timestamp + random entropy fallback if missing
    run_id = (
        request.run_id
        or identity.get("run_id")
        or f"run-{ts_str}-{uuid.uuid4().hex[:6]}"
    )

    _log_action_sync("Generating Power BI TMDL model & semantic relationships", request, app_name, f"Starting generation for app {app_name}")

    if request.offline_sample_data:
        from app.model import offline_source
        mapping = dict(mapping)
        mapping["tables"] = offline_source.apply(P.tables(mapping))

    model_files, model_report = build_semantic_model(mapping, app_name)
    _log_action_sync("Generated Power BI TMDL model", request, app_name, f"Emitted {len(model_files)} TMDL model files")

    # File-based (CSV/QVD) tables are built above pointing at a placeholder
    # Supabase Storage URL (see table_tmdl.py) - real content only exists
    # there if some other process happened to stage the exact filename.
    # When actually deploying to Fabric with real credentials, and the
    # mapping payload carries real downloaded file content (see
    # az-qlikengine-api-repo's fetchDataFiles), upload each file into a
    # Fabric Lakehouse now and rewrite the placeholder URL to the real
    # OneLake location - a post-processing text swap rather than threading
    # Fabric credentials through the TMDL builder itself, so this has zero
    # effect on any run that isn't an authenticated Fabric deploy.
    csv_data_files = P.data_files(mapping)
    if request.deploy == Deploy.FABRIC and request.workspace_id and request.fabric_access_token and csv_data_files:
        provisioned = fabric_data_provisioner.provision_data_files(
            csv_data_files, request.workspace_id, request.fabric_access_token, f"{app_name}_DataFiles",
        )
        if provisioned:
            for clean_name, onelake_url in provisioned.items():
                placeholder = supabase_file_url(clean_name)
                for path, content in list(model_files.items()):
                    if placeholder in content:
                        model_files[path] = content.replace(placeholder, onelake_url)
            _log_action_sync(
                "Provisioned CSV/file data into Fabric Lakehouse", request, app_name,
                f"{len(provisioned)} file(s) uploaded and referenced in place of the Supabase placeholder",
            )

    report_files: Dict[str, str] = {}
    notes = []
    report_stats: Dict[str, Any] = {}
    if request.target != Target.SEMANTIC_MODEL_ONLY:
        _log_action_sync("Generating PBIR visual layout & JSON definitions", request, app_name, "Building PBIR visual definitions")
        report_files, notes, report_stats = build_report(
            mapping, app_name, f"../{app_name}.SemanticModel"
        )
        _log_action_sync("Generated PBIR report files", request, app_name, f"Emitted {len(report_files)} report & visual files")

        # Surfacing whether the report's theme actually came from the source
        # Qlik app or a generic fallback (see unified-parsing's
        # theme_and_styling.source and app/report/theme.py) makes a silent
        # "no branding found" gap visible in the run's own activity trail,
        # instead of looking identical to a successful theme carry-through.
        theme_source = P.as_dict(P.as_dict(mapping.get("app_layout")).get("theme")).get("source")
        if theme_source in ("qlik_theme", "qlik_theme_partial"):
            _log_action_sync(
                "Resolved report theme from source Qlik app", request, app_name,
                f"Applied the source app's own color palette/branding (source={theme_source})",
            )
        else:
            _log_action_sync(
                "Applied default report theme", request, app_name,
                "No usable theme/palette was found on the source app; used the generic fallback palette",
            )

        nav_button_count = report_stats.get("navigation_buttons", 0) if isinstance(report_stats, dict) else 0
        _log_action_sync(
            "Wired page navigation buttons", request, app_name,
            f"{nav_button_count} navigation button(s) across {report_stats.get('pages', 0) if isinstance(report_stats, dict) else 0} page(s)",
        )

    package = pbip_packager.build_package(app_name, model_files, report_files)
    package_store.save(run_id, package, request.app_id or identity.get("app_id"))
    _log_action_sync("Persisted package to run store", request, app_name, f"run_id={run_id}")

    # 3. Disk persistence, unless the caller asked to skip it (push_only /
    # write_to_disk=False - e.g. a Fabric-only push where nothing local is
    # needed). Every run that IS written goes under its own dynamic folder:
    # generated/<app_name>/<run_id>/. This used to run unconditionally
    # regardless of either flag, silently ignoring the caller's choice.
    disk_write_result: Dict[str, Any] = {}
    if request.write_to_disk and not request.push_only:
        destination = os.path.join(
            config.OUTPUT_DIR,
            app_name,
            slug(run_id, fallback=f"run-{ts_str}"),
        )
        disk_write_result = pbip_packager.write_to_disk(package, destination)
        _log_action_sync("Wrote generated package to disk", request, app_name, f"path={disk_write_result.get('output_path')}")
    else:
        _log_action_sync("Skipped disk write", request, app_name, "push_only/write_to_disk=False requested")

    # 4. Structural and comprehensive validation of the emitted package
    validation = validator.validate(package, app_name)
    comprehensive_val = run_comprehensive_validation(mapping, package, app_name, model_report, notes)

    _log_action_sync(
        "Validated generated package structure and semantics", request, app_name,
        f"val_status={comprehensive_val['validation_status']}, can_deploy={comprehensive_val['can_deploy']}, "
        f"errors={len(comprehensive_val['errors'])}, warnings={len(comprehensive_val['warnings'])}",
    )

    # Calculate dynamic object counts for generation summary
    num_tables = len([p for p in package if "/definition/tables/" in p and p.endswith(".tmdl") and not p.split("/")[-1].startswith("LocalDateTable_")])
    num_measures = model_report.get("measures", 0)
    num_calc_cols = model_report.get("calculated_columns", 0)
    num_rels = model_report.get("relationships_written", 0)
    num_pages = len([p for p in package if "/pages/" in p and p.endswith("page.json")])
    num_visuals = len([p for p in package if "/visuals/" in p and p.endswith("visual.json")])
    num_filters = comprehensive_val["summary"]["filters"]["total"]

    gen_summary = {
        "tables": num_tables,
        "measures": num_measures,
        "calculated_columns": num_calc_cols,
        "relationships": num_rels,
        "pages": num_pages,
        "visuals": num_visuals,
        "filters": num_filters,
    }

    val_status = comprehensive_val["validation_status"]
    can_deploy = comprehensive_val["can_deploy"]

    # 5. Pre-deployment Validation Gate
    deployment_res: Dict[str, Any] = {}
    if request.deploy != Deploy.NONE:
        if not can_deploy:
            deployment_res = {
                "status": "blocked",
                "reason": "Deployment blocked due to critical validation errors",
                "errors": comprehensive_val["errors"],
            }
            _log_action_sync(
                "Deployment blocked", request, app_name,
                f"Deployment blocked: {len(comprehensive_val['errors'])} critical error(s) found",
            )
        else:
            _log_action_sync(
                "Starting deployment", request, app_name,
                f"target={request.deploy.value}, workspace={request.workspace_id or request.space_id}",
            )
            deployment_res = _deploy(package, app_name, request, {}, mapping=mapping, ts_str=ts_str)
            dep_status = deployment_res.get("status", "unknown")
            _log_action_sync(
                "Deployment finished", request, app_name,
                f"target={request.deploy.value}, status={dep_status}",
            )
    else:
        deployment_res = {"status": "skipped", "reason": "no deployment requested"}

    deployment_status = deployment_res.get("status", "skipped")

    result: Dict[str, Any] = {
        "status": "success" if validation["ok"] else "warning",
        "generation_status": "success",
        "validation_status": val_status,
        "deployment_status": deployment_status,
        "generation": gen_summary,
        "validation": {
            "ok": validation.get("ok", True) and val_status in ("success", "warning"),
            **comprehensive_val["summary"],
            "errors": comprehensive_val["errors"],
            "warnings": comprehensive_val["warnings"],
        },
        "target": request.target.value,
        "deploy": request.deploy.value,
        "app_id": request.app_id or identity.get("app_id"),
        "app_name": app_name,
        "run_id": run_id,
        "output_path": disk_write_result.get("output_path"),
        "file_count": len(package),
        "total_bytes": artifact_builder.measure(package)["total_bytes"],
        "summary": {"semantic_model": model_report, "report": report_stats},
        "visual_notes": notes,
        "deployment": deployment_res,
        "errors": comprehensive_val["errors"],
        "warnings": comprehensive_val["warnings"],
    }

    _log_action_sync("Created Fabric PBIP deployment package", request, app_name, f"Package created: {len(package)} files, {result['total_bytes']} bytes")

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

    if deployment_status == "blocked":
        err_reasons = "; ".join(e.get("reason", "Validation error") for e in comprehensive_val["errors"][:3])
        result["message"] = f"Package generated locally; deployment blocked due to validation errors: {err_reasons}"
    elif deployment_status == "error":
        result["message"] = f"Package generated locally, but deployment to {request.deploy.value} failed: {deployment_res.get('error')}"
    elif deployment_status == "success":
        dep = deployment_res
        items_desc = ", ".join(f"{k}: {v}" for k, v in dep.get("items", {}).items())
        result["message"] = f"{_message(result)} | Successfully deployed to {request.deploy.value} ({items_desc})"
    else:
        result["message"] = _message(result)

    # Persist report generation result in MongoDB
    _save_to_mongodb(result, request, app_name)
    _log_action_sync("Report generation completed", request, app_name, f"Completed status={result['status']}, {len(package)} files")

    return result


def _save_to_mongodb(result: Dict[str, Any], request: GenerateRequest, app_name: str) -> None:
    """Persist the generation result in the MongoDB microservice, without
    blocking the caller on that service being reachable (see _log_action_sync)."""
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

    _TELEMETRY_POOL.submit(_post_with_fallback, "report-generation", doc, (2, 5))


def _deploy(
    package: Dict[str, str],
    app_name: str,
    request: GenerateRequest,
    result: Dict[str, Any],
    mapping: Optional[Dict[str, Any]] = None,
    ts_str: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the requested deployment. Failures are reported, not raised."""
    if request.deploy == Deploy.NONE:
        return {"status": "skipped", "reason": "no deployment requested"}

    ts = ts_str or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    if request.folder_name:
        clean_folder = re.sub(r'_\d{8}_\d{6}$', '', str(request.folder_name)).strip('/')
        clean_folder = re.sub(r'[^A-Za-z0-9]+', '_', clean_folder).strip('_')
        folder_name = f"{clean_folder}_{ts}" if clean_folder else app_name
    else:
        folder_name = app_name

    prefix = "/".join(
        part for part in (
            request.department_repo or config.DESTINATION_BASE,
            folder_name,
        ) if part
    )

    try:
        if request.deploy == Deploy.FABRIC:
            ws = request.workspace_id
            tok = request.fabric_access_token
            if not ws or ws == "personal" or ws.startswith("{{"):
                return {
                    "status": "error",
                    "provider": "fabric",
                    "error": f"Invalid Fabric Workspace ID '{ws}'. Please specify a valid Fabric Workspace GUID (e.g. '4a212d5c-abf8-44ac-9cf5-7e47ed9aaf26') in fabric_group_id or workspace_id.",
                }
            if not tok or tok.startswith("{{"):
                return {
                    "status": "error",
                    "provider": "fabric",
                    "error": "Invalid Fabric Access Token: placeholder '{{FABRIC_ACCESS_TOKEN}}' was not set. Please provide a valid Bearer token for Microsoft Fabric.",
                }

            return fabric_deployer.deploy(
                package, app_name, ws, tok
            )
        if request.deploy == Deploy.GITHUB:
            # Build list of legacy un-timestamped paths in this workspace that would cause duplicate name conflicts
            dept = (request.department_repo or config.DESTINATION_BASE or "").strip("/")
            raw_name = request.app_name or (mapping and app_identity(mapping).get("app_name")) or ""
            clean_base = re.sub(r'_\d{8}_\d{6}$', '', app_name)
            clean_pfxs = []
            if dept and (raw_name or clean_base):
                for candidate in filter(None, [raw_name, clean_base, "FleetVision KSA"]):
                    clean_c = candidate.strip()
                    # Never target bare "FleetVision" - that belongs to the user's existing bar_chart project!
                    if clean_c.lower() in ("fleetvision", "fleet-vision"):
                        continue
                    clean_pfxs.extend([
                        f"{dept}/{clean_c}.Report",
                        f"{dept}/{clean_c}.SemanticModel",
                        f"{dept}/{slug(clean_c)}",
                        f"{dept}/{slug(clean_c)}.pbip",
                        f"{dept}/{clean_c}.pbip",
                    ])
                clean_pfxs = list(dict.fromkeys(clean_pfxs))

            return github_deployer.deploy(
                package=package,
                prefix=prefix,
                branch=request.branch,
                message=request.commit_message,
                token=request.git_pat or request.token or request.github_pat or request.pat,
                org=request.org or request.git_org or request.github_org,
                repo=request.repo or request.git_repo or request.github_repo,
                clean_prefixes=clean_pfxs,
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
