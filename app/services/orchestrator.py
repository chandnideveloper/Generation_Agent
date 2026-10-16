"""Direct in-process migration orchestrator.

Coordinates Qlik and Tableau generation directly in Python:
- No HTTP calls to ports 8005 or 7000.
- Completely isolates Qlik and Tableau engines.
- Formulates standardized UnifiedMigrateResponse.
- Zero credential leakage.
"""

import os
import logging
from typing import Any, Dict, Optional

from app.api.schemas import (
    ArtifactsSummary,
    DeploymentDetails,
    UnifiedMigrateRequest,
    UnifiedMigrateResponse,
)

logger = logging.getLogger(__name__)


def _sanitize_payload(obj: Any) -> Any:
    """Recursively scrub sensitive keys (PAT, secret, token, password)."""
    if isinstance(obj, dict):
        sanitized = {}
        for k, v in obj.items():
            key_lower = str(k).lower()
            if any(s in key_lower for s in ("pat", "token", "password", "secret", "authorization")):
                sanitized[k] = "***MASKED***"
            else:
                sanitized[k] = _sanitize_payload(v)
        return sanitized
    elif isinstance(obj, list):
        return [_sanitize_payload(item) for item in obj]
    return obj


class MigrationOrchestrator:
    """Dispatches migration directly to Qlik or Tableau generation engine."""

    async def execute_migration(
        self, request: UnifiedMigrateRequest, client_token: Optional[str] = None
    ) -> UnifiedMigrateResponse:
        source_type = request.source_type.lower()
        gh = request.target.github
        target_dir = gh.target_directory

        logger.info(
            f"[MigrationOrchestrator] Direct execution for source_type={source_type}, "
            f"run_id={request.run_id}, repo={gh.owner}/{gh.repo_name}@{gh.branch} in dir='{target_dir}'"
        )

        from app.services.activity_logger import log_activity_async
        # Resolve IDs for logging
        project_id = "unknown"
        workbook_id = "unknown"
        if source_type == "qlik":
            project_id = request.source.get("space_id", "unknown")
            workbook_id = request.source.get("app_id", "unknown")
        elif source_type == "tableau":
            project_id = request.source.get("project_id", "unknown")
            workbook_id = request.source.get("workbook_id", "unknown")

        # Resolve initial project name dynamically
        dynamic_project_name = (
            request.source.get("project_name")
            or request.source.get("folder_name")
            or request.source.get("app_name")
            or request.source.get("workbook_name")
            or getattr(request, "folder_name", None)
            or getattr(request, "project_name", None)
        )
        if source_type == "tableau" and dynamic_project_name:
            from app.tableau.core.logging_utils import set_logging_context
            set_logging_context(project_id, workbook_id, request.run_id, project_name=dynamic_project_name)
        elif source_type == "qlik" and dynamic_project_name:
            from app.qlik.util.logging_utils import set_qlik_logging_context
            set_qlik_logging_context(run_id=request.run_id, space_id=project_id, app_id=workbook_id, project_name=dynamic_project_name)

        # Agent Action: Stage 1
        await log_activity_async(
            run_id=request.run_id,
            project_id=project_id,
            workbook_id=workbook_id,
            summary="Report Generation started.",
            source_type=source_type,
            project_name=dynamic_project_name,
        )

        try:
            if source_type == "qlik":
                raw_result = await self._run_qlik_generation(request, client_token)
                final_res = self._normalize_qlik_result(request, raw_result)
            elif source_type == "tableau":
                raw_result = await self._run_tableau_generation(request, client_token)
                final_res = self._normalize_tableau_result(request, raw_result)
            else:
                raise ValueError(f"Unsupported source_type: '{source_type}'. Must be 'qlik' or 'tableau'")
        except Exception as gen_err:
            logger.error(f"[Orchestrator] Generation failed for {source_type} run_id={request.run_id}: {gen_err}")
            await self._save_failure_record(request, source_type, project_id, workbook_id, str(gen_err))
            raise

        # Resolve finalized project name from final_res or raw_result
        if isinstance(raw_result, dict) and raw_result.get("new_app_name"):
            dynamic_project_name = raw_result["new_app_name"]
        elif hasattr(final_res, "details") and isinstance(final_res.details, dict):
            proj_dict = final_res.details.get("project")
            if isinstance(proj_dict, dict) and proj_dict.get("name"):
                dynamic_project_name = proj_dict.get("name")
        if source_type == "tableau" and dynamic_project_name:
            from app.tableau.core.logging_utils import set_project_name_context
            set_project_name_context(dynamic_project_name)

        # Agent Action: Stage 5
        if final_res.status == "success":
            await log_activity_async(
                run_id=request.run_id,
                project_id=project_id,
                workbook_id=workbook_id,
                summary="Report Generation completed successfully.",
                source_type=source_type,
                project_name=dynamic_project_name,
            )

        # Persist generation record in MongoDB strictly into target database and collection
        try:
            import os
            import time
            import datetime
            import requests
            import re
            final_dict = final_res.model_dump() if hasattr(final_res, "model_dump") else dict(final_res)

            mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URL")
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

            if source_type == "tableau":
                mongo_base = (os.getenv("MONGODB_URL") or "https://rizio6jk6d4psp4xzqzy2pl4nm0eibme.lambda-url.ap-southeast-2.on.aws").rstrip("/")
                post_url = f"{mongo_base}/api/records/generation"
                tb_payload = dict(final_dict)
                if isinstance(raw_result, dict):
                    for k in ("semantic_model", "report", "new_app_name", "destination_folder", "calculated_fields"):
                        if k in raw_result:
                            tb_payload[k] = raw_result[k]

                tb_record = {
                    "id": str(request.run_id).strip(),
                    "run_id": str(request.run_id).strip(),
                    "project_id": str(project_id).strip(),
                    "workbook_id": str(workbook_id).strip(),
                    "project_name": str(dynamic_project_name or project_id or workbook_id).strip(),
                    "status": "completed" if final_res.status == "success" else "failed",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "timestamp": now_iso,
                    "db_location": final_res.db_location,
                    "fetch_url": final_res.db_location,
                    "database": "QT2F_Tableau",
                    "collection": "generation_results",
                    "payload": tb_payload,
                }
                # Direct PyMongo write to QT2F_Tableau.generation_results
                if mongo_uri and mongo_uri.startswith("mongodb"):
                    try:
                        import pymongo
                        client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
                        client["QT2F_Tableau"]["generation_results"].update_one(
                            {"run_id": str(request.run_id).strip()},
                            {"$set": tb_record},
                            upsert=True
                        )
                    except Exception as pe:
                        logger.debug(f"[Orchestrator] Direct PyMongo write failed: {pe}")

                # Microservice write (PATCH then POST with backoff on 429)
                params = {
                    "run_id": str(request.run_id).strip(),
                    "project_id": str(project_id).strip(),
                    "workbook_id": str(workbook_id).strip(),
                }
                saved = False
                for attempt in range(3):
                    try:
                        patch_resp = requests.patch(post_url, params=params, json=tb_record, timeout=15)
                        if patch_resp.status_code in (200, 204):
                            logger.info(f"[Orchestrator] Successfully updated Tableau generation record in QT2F_Tableau.generation_results via {post_url}")
                            saved = True
                            break
                        elif patch_resp.status_code == 429:
                            time.sleep(1.5 * (attempt + 1))
                            continue
                        elif patch_resp.status_code == 404:
                            break
                    except Exception as pe:
                        logger.warning(f"[Orchestrator] PATCH {post_url} error: {pe}")
                        time.sleep(1.0)

                if not saved:
                    for attempt in range(3):
                        try:
                            post_resp = requests.post(post_url, json=tb_record, timeout=20)
                            if post_resp.status_code in (200, 201):
                                logger.info(f"[Orchestrator] Successfully created Tableau generation record in QT2F_Tableau.generation_results via {post_url}")
                                saved = True
                                break
                            elif post_resp.status_code == 429:
                                time.sleep(1.5 * (attempt + 1))
                                continue
                        except Exception as post_err:
                            logger.error(f"[Orchestrator] POST {post_url} failed: {post_err}")
                            time.sleep(1.0)

            elif source_type == "qlik":
                from app.qlik.config import config as qlik_config
                qlik_base = (qlik_config.MONGO_API_URL or os.getenv("MONGO_API_URL") or "https://aodeclx4wyi6727uozoyktnqb40fyzvt.lambda-url.ap-southeast-2.on.aws").rstrip("/")
                post_url = f"{qlik_base}/api/records/generation"
                clean_app_id = re.sub(r"[\s_]+", "-", str(workbook_id or "").strip())
                eff_app = clean_app_id or str(workbook_id).strip()
                eff_space = str(project_id or "personal").strip()
                qlik_record = {
                    "id": str(request.run_id).strip(),
                    "run_id": str(request.run_id).strip(),
                    "space_id": eff_space,
                    "app_id": eff_app,
                    "space_name": eff_space,
                    "project_id": eff_space,
                    "workbook_id": eff_app,
                    "project_name": str(dynamic_project_name or eff_app).strip(),
                    "status": "completed" if final_res.status == "success" else "failed",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "timestamp": now_iso,
                    "db_location": final_res.db_location,
                    "fetch_url": final_res.db_location,
                    "database": "QT2F",
                    "collection": "generation_results",
                    "payload": final_dict,
                }
                # Direct PyMongo write to QT2F.generation_results
                if mongo_uri and mongo_uri.startswith("mongodb"):
                    try:
                        import pymongo
                        client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
                        client["QT2F"]["generation_results"].update_one(
                            {"run_id": str(request.run_id).strip()},
                            {"$set": qlik_record},
                            upsert=True
                        )
                    except Exception as pe:
                        logger.debug(f"[Orchestrator] Direct PyMongo write to QT2F.generation_results failed: {pe}")

                # Microservice write (PATCH then POST with backoff on 429)
                params = {
                    "run_id": str(request.run_id).strip(),
                    "space_id": eff_space,
                    "app_id": eff_app,
                }
                saved = False
                for attempt in range(3):
                    try:
                        patch_resp = requests.patch(post_url, params=params, json=qlik_record, timeout=15)
                        if patch_resp.status_code in (200, 204):
                            logger.info(f"[Orchestrator] Successfully updated Qlik generation record via PATCH {post_url}")
                            saved = True
                            break
                        elif patch_resp.status_code == 429:
                            time.sleep(1.5 * (attempt + 1))
                            continue
                        elif patch_resp.status_code == 404:
                            break
                    except Exception as pe:
                        logger.warning(f"[Orchestrator] PATCH {post_url} error: {pe}")
                        time.sleep(1.0)

                if not saved:
                    for attempt in range(3):
                        try:
                            p_res = requests.post(post_url, json=qlik_record, timeout=15)
                            if p_res.status_code in (200, 201):
                                logger.info(f"[Orchestrator] Successfully saved Qlik generation record to QT2F.generation_results at {post_url}")
                                break
                            elif p_res.status_code == 429:
                                time.sleep(1.5 * (attempt + 1))
                                continue
                        except Exception as p_err:
                            logger.warning(f"[Orchestrator] POST {post_url} failed: {p_err}")
                            time.sleep(1.0)

        except Exception as exc:
            logger.warning(f"Failed to persist generation record: {exc}")

        return final_res

    async def _run_qlik_generation(
        self, request: UnifiedMigrateRequest, client_token: Optional[str]
    ) -> Dict[str, Any]:
        """Directly invoke Qlik generation without HTTP."""
        from app.qlik.generator import generate
        from app.qlik.schemas import Deploy, GenerateRequest, Target
        from app.qlik.sources.mapping_client import MappingNotFound, fetch_mapping

        gh = request.target.github
        source = request.source
        app_id = source.get("app_id")
        run_id = request.run_id
        target_dir = gh.target_directory
        space_id = source.get("space_id") or "personal"
        project_name = source.get("project_name") or source.get("folder_name") or app_id or "Qlik_App"

        # Initialize Qlik logging context so all logs flow to QT2F.agent_logs
        from app.qlik.util.logging_utils import set_qlik_logging_context, log_qlik_info
        set_qlik_logging_context(
            run_id=run_id,
            space_id=space_id,
            app_id=app_id,
            project_name=project_name,
        )
        log_qlik_info(f"[Qlik] Initiated migration workflow for app '{app_id}' (run_id: {run_id})")

        # 1. Resolve mapping document
        if request.mapping_result:
            document = request.mapping_result
        else:
            try:
                document = fetch_mapping(app_id=app_id, run_id=run_id, space_id=space_id)
            except MappingNotFound as exc:
                raise ValueError(str(exc)) from exc
            except Exception as exc:
                raise RuntimeError(f"Qlik mapping fetch failed: {exc}") from exc

        # 2. Build Qlik GenerateRequest
        qlik_req = GenerateRequest(
            source_type="qlik",
            run_id=run_id,
            app_id=app_id,
            space_id=source.get("space_id"),
            target=Target.POWERBI_DESKTOP,
            deploy=Deploy.GITHUB,
            org=gh.owner,
            repo=gh.repo_name,
            branch=gh.branch,
            git_pat=gh.git_pat,
            department_repo=target_dir,
            folder_name=source.get("folder_name"),
            write_to_disk=True,
        )

        # 3. Direct execution (generate is synchronous CPU-bound packager)
        try:
            import asyncio
            result = await asyncio.to_thread(generate, document, qlik_req)
            if hasattr(result, "model_dump"):
                return result.model_dump()
            return dict(result)
        except Exception as exc:
            logger.exception("Direct Qlik generation failed")
            raise RuntimeError(f"Qlik generation failed: {exc}") from exc

    async def _run_tableau_generation(
        self, request: UnifiedMigrateRequest, client_token: Optional[str]
    ) -> Dict[str, Any]:
        """Directly invoke Tableau CoordinatorAgent without HTTP."""
        from app.tableau.agents.coordinatoragent import CoordinatorAgent
        from app.tableau.agents.file_agent import FileAgent
        from app.tableau.agents.folder_agent import FolderAgent
        from app.tableau.api.schemas import TmdlRequest
        from app.tableau.core_logic.reportgenerator import ReportGenerator
        from app.tableau.core_logic.tmdlgenerator import TmdlGenerator

        gh = request.target.github
        source = request.source
        wb_id = source.get("workbook_id")
        proj_id = source.get("project_id")
        run_id = request.run_id
        target_dir = gh.target_directory

        # 1. Formulate TmdlRequest
        tmdl_req = TmdlRequest(
            folder_name=target_dir,
            project_id=proj_id,
            workbook_id=wb_id,
            run_id=run_id,
            deployment_type="GIT",
            git_org=gh.owner,
            git_repo=gh.repo_name,
            git_pat=gh.git_pat,
            branch=gh.branch,
            token=client_token or "local-dev-token",
            mapping_result=getattr(request, "mapping_result", None),
        )

        # 2. Instantiate request-scoped agents
        req_file_agent = FileAgent("FileAgent")
        req_folder_agent = FolderAgent("FolderAgent")
        req_tmdl_gen = TmdlGenerator(req_file_agent, req_folder_agent)
        req_report_gen = ReportGenerator(req_file_agent)

        coordinator = CoordinatorAgent(
            "Coordinator",
            req_folder_agent,
            req_report_gen,
            req_tmdl_gen,
        )

        try:
            raw_result = await coordinator.select_app_by_name(tmdl_req)
            if isinstance(raw_result, dict) and raw_result.get("status") == "error":
                raise RuntimeError(raw_result.get("message", "Tableau generation reported error"))
            return raw_result
        finally:
            try:
                for agent_holder in (req_report_gen, req_tmdl_gen):
                    fa = getattr(agent_holder, "file_agent", None)
                    if fa is not None and hasattr(fa, "clear_captured_files"):
                        fa.clear_captured_files()
            except Exception:
                pass

    def _normalize_qlik_result(
        self, request: UnifiedMigrateRequest, raw_result: Dict[str, Any]
    ) -> UnifiedMigrateResponse:
        gh = request.target.github
        clean_result = _sanitize_payload(raw_result)

        deploy_info = clean_result.get("deployment", {})
        raw_deploy_status = str(deploy_info.get("status", "")).lower()
        commit_sha = deploy_info.get("commit") or deploy_info.get("commit_sha")

        if raw_deploy_status in ("success", "completed", "ok") or (
            deploy_info.get("provider") == "github" and commit_sha
        ):
            deploy_status = "completed"
        elif raw_deploy_status in ("skipped", "none"):
            deploy_status = "skipped"
        else:
            deploy_status = "failed"

        summary = clean_result.get("summary", {})
        artifacts = ArtifactsSummary(
            semantic_model=summary.get("semantic_model", {}),
            report=summary.get("report", {}),
        )

        commit_url = deploy_info.get("url")
        if not commit_url and commit_sha:
            commit_url = f"https://github.com/{gh.owner}/{gh.repo_name}/commit/{commit_sha}"

        deployment = DeploymentDetails(
            platform="github",
            status=deploy_status,
            owner=gh.owner,
            repo_name=gh.repo_name,
            branch=gh.branch,
            commit_sha=commit_sha,
            commit_url=commit_url,
            details={
                "files_written": deploy_info.get("file_count") or deploy_info.get("files_written", 0),
                "stale_deleted": deploy_info.get("stale_deleted", 0),
                "path": deploy_info.get("path"),
            },
        )

        import re
        from app.qlik.config import config as qlik_config
        clean_app_id = re.sub(r"[\s_]+", "-", str(request.source.get("app_id", "")).strip())
        eff_app = clean_app_id or str(request.source.get("app_id", "")).strip()
        eff_space = str(request.source.get("space_id") or "personal").strip()
        qlik_base = (qlik_config.MONGO_API_URL or os.getenv("MONGO_API_URL") or "https://aodeclx4wyi6727uozoyktnqb40fyzvt.lambda-url.ap-southeast-2.on.aws").rstrip('/')
        get_url = f"{qlik_base}/api/records/generation?run_id={request.run_id}&app_id={eff_app}&space_id={eff_space}"

        db_loc = clean_result.get("db_location") or get_url
        if not str(db_loc).startswith("http"):
            db_loc = get_url

        clean_result["db_location"] = db_loc
        clean_result["fetch_url"] = db_loc
        clean_result["database"] = "QT2F"
        clean_result["collection"] = "generation_results"

        msg = clean_result.get("message") or "Qlik migration completed."
        msg_clean = msg.split(" | Response saved to DB location")[0].strip()
        final_msg = f"{msg_clean} | Response saved to DB location: {db_loc}"
        clean_result["message"] = final_msg

        return UnifiedMigrateResponse(
            status="success" if deploy_status == "completed" else "error",
            message=final_msg,
            db_location=db_loc,
            source_type="qlik",
            run_id=request.run_id,
            target="fabric",
            artifacts=artifacts,
            deployment=deployment,
            details=clean_result,
        )

    def _normalize_tableau_result(
        self, request: UnifiedMigrateRequest, raw_result: Dict[str, Any]
    ) -> UnifiedMigrateResponse:
        gh = request.target.github
        clean_result = _sanitize_payload(raw_result)

        status_str = clean_result.get("status", "").lower()
        deploy_status = "completed" if status_str in ("ok", "success") else "failed"

        summary = clean_result.get("summary", {})
        project_meta = clean_result.get("project", {})

        tables_count = summary.get("tables_processed", 0)
        params_count = summary.get("parameters_generated", 0)
        total_tables = summary.get("total_tables") or (tables_count + params_count)

        artifacts = ArtifactsSummary(
            semantic_model={
                "tables": total_tables,
                "total_tables": total_tables,
                "source_tables": tables_count,
                "parameter_tables": params_count,
                "custom_sql_tables": summary.get("custom_sql_tables", 0),
                "measures": summary.get("measures_created", 0),
                "calculated_fields": summary.get("calculated_fields_converted", 0),
                "parameters": params_count,
            },
            report={
                "pages": summary.get("pages_generated", 0),
                "visuals": summary.get("visuals_generated", 0),
                "skipped_visuals": clean_result.get("skipped_visuals", []),
            },
        )

        commit_sha = project_meta.get("commit_sha") or project_meta.get("commit")
        commit_url = project_meta.get("github_url")
        if commit_sha and not commit_url:
            commit_url = f"https://github.com/{gh.owner}/{gh.repo_name}/commit/{commit_sha}"

        deployment = DeploymentDetails(
            platform="github",
            status=deploy_status,
            owner=gh.owner,
            repo_name=gh.repo_name,
            branch=gh.branch,
            commit_sha=commit_sha,
            commit_url=commit_url,
            details={
                "destination_path": project_meta.get("destination_path"),
                "repository": project_meta.get("repository"),
                "branch": project_meta.get("branch"),
            },
        )

        proj_id = str(request.source.get("project_id", "")).strip()
        wb_id = str(request.source.get("workbook_id", "")).strip()
        base_mongo = (os.getenv("MONGODB_URL") or "https://rizio6jk6d4psp4xzqzy2pl4nm0eibme.lambda-url.ap-southeast-2.on.aws").rstrip('/')
        get_url = f"{base_mongo}/api/records/generation?run_id={request.run_id}&project_id={proj_id}&workbook_id={wb_id}"

        db_loc = clean_result.get("db_location") or get_url
        if not str(db_loc).startswith("http"):
            db_loc = get_url

        clean_result["db_location"] = db_loc
        clean_result["fetch_url"] = db_loc
        clean_result["database"] = "QT2F_Tableau"
        clean_result["collection"] = "generation_results"

        msg = clean_result.get("message") or "Tableau migration completed."
        msg_clean = msg.split(" | Response saved to DB location")[0].strip()
        final_msg = f"{msg_clean} | Response saved to DB location: {db_loc}"
        clean_result["message"] = final_msg

        return UnifiedMigrateResponse(
            status="success" if deploy_status == "completed" else "error",
            message=final_msg,
            db_location=db_loc,
            source_type="tableau",
            run_id=request.run_id,
            target="fabric",
            artifacts=artifacts,
            deployment=deployment,
            details=clean_result,
        )

    async def _save_failure_record(
        self, request: UnifiedMigrateRequest, source_type: str, project_id: str, workbook_id: str, error_message: str
    ):
        try:
            import os
            import requests
            import datetime
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URL")

            if source_type == "tableau":
                mongo_base = (os.getenv("MONGODB_URL") or "https://rizio6jk6d4psp4xzqzy2pl4nm0eibme.lambda-url.ap-southeast-2.on.aws").rstrip("/")
                post_url = f"{mongo_base}/api/records/generation"
                rec = {
                    "id": str(request.run_id).strip(),
                    "run_id": str(request.run_id).strip(),
                    "project_id": str(project_id).strip(),
                    "workbook_id": str(workbook_id).strip(),
                    "project_name": str(project_id or workbook_id).strip(),
                    "status": "failed",
                    "error_message": error_message,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "timestamp": now_iso,
                    "database": "QT2F_Tableau",
                    "collection": "generation_results",
                    "payload": {"status": "failed", "error": error_message},
                }
                params = {"run_id": str(request.run_id).strip(), "project_id": str(project_id).strip(), "workbook_id": str(workbook_id).strip()}
                try:
                    p = requests.patch(post_url, params=params, json=rec, timeout=10)
                    if p.status_code in (404, 405):
                        requests.post(post_url, json=rec, timeout=10)
                except Exception:
                    pass

            elif source_type == "qlik":
                from app.qlik.config import config as qlik_config
                qlik_base = (qlik_config.MONGO_API_URL or os.getenv("MONGO_API_URL") or "https://aodeclx4wyi6727uozoyktnqb40fyzvt.lambda-url.ap-southeast-2.on.aws").rstrip("/")
                post_url = f"{qlik_base}/api/records/generation"
                rec = {
                    "id": str(request.run_id).strip(),
                    "run_id": str(request.run_id).strip(),
                    "space_id": str(project_id or "personal").strip(),
                    "app_id": str(workbook_id).strip(),
                    "status": "failed",
                    "error_message": error_message,
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "timestamp": now_iso,
                    "database": "QT2F",
                    "collection": "generation_results",
                    "payload": {"status": "failed", "error": error_message},
                }
                params = {"run_id": str(request.run_id).strip(), "space_id": str(project_id or "personal").strip(), "app_id": str(workbook_id).strip()}
                try:
                    p = requests.patch(post_url, params=params, json=rec, timeout=10)
                    if p.status_code in (404, 405):
                        requests.post(post_url, json=rec, timeout=10)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Failed to record migration failure: {e}")


orchestrator = MigrationOrchestrator()
