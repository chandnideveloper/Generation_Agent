"""Direct in-process migration orchestrator.

Coordinates Qlik and Tableau generation directly in Python:
- No HTTP calls to ports 8005 or 7000.
- Completely isolates Qlik and Tableau engines.
- Formulates standardized UnifiedMigrateResponse.
- Zero credential leakage.
"""

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

        # Agent Action: Stage 1
        await log_activity_async(
            run_id=request.run_id,
            project_id=project_id,
            workbook_id=workbook_id,
            summary="Report Generation started."
        )

        if source_type == "qlik":
            raw_result = await self._run_qlik_generation(request, client_token)
            final_res = self._normalize_qlik_result(request, raw_result)
        elif source_type == "tableau":
            raw_result = await self._run_tableau_generation(request, client_token)
            final_res = self._normalize_tableau_result(request, raw_result)
        else:
            raise ValueError(f"Unsupported source_type: '{source_type}'. Must be 'qlik' or 'tableau'")

        # Agent Action: Stage 5
        if final_res.status == "success":
            await log_activity_async(
                run_id=request.run_id,
                project_id=project_id,
                workbook_id=workbook_id,
                summary="Report Generation completed successfully."
            )

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

        # 1. Resolve mapping document
        if request.mapping_result:
            document = request.mapping_result
        else:
            try:
                document = fetch_mapping(app_id=app_id, run_id=run_id)
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

        return UnifiedMigrateResponse(
            status="success" if deploy_status == "completed" else "error",
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

        artifacts = ArtifactsSummary(
            semantic_model={
                "tables": summary.get("tables_processed", 0),
                "custom_sql_tables": summary.get("custom_sql_tables", 0),
                "measures": summary.get("measures_created", 0),
                "calculated_fields": summary.get("calculated_fields_converted", 0),
                "parameters": summary.get("parameters_generated", 0),
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

        return UnifiedMigrateResponse(
            status="success" if deploy_status == "completed" else "error",
            source_type="tableau",
            run_id=request.run_id,
            target="fabric",
            artifacts=artifacts,
            deployment=deployment,
            details=clean_result,
        )


orchestrator = MigrationOrchestrator()
