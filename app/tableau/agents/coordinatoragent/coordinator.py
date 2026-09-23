from autogen import AssistantAgent
import aiohttp
import httpx
import re
import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from app.tableau.core.config import Config
from app.tableau.core.logging_utils import log_info, log_error, log_warning, log_action_to_api
from app.tableau.services.action_logger import ActionLogger
from app.tableau.core_logic.metadata_exporter.metadata_exporter import MetadataExporter
import json
from .memory_file_agent import MemoryFileAgent
from app.tableau.agents.fabric_agent.fabric_agent import FabricAgent
from app.tableau.agents.git_agent.git_agent import GitAgent
from app.services.activity_logger import log_activity_async
from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient

from .folder_manager import FolderManagerMixin
from .metadata_extractor import MetadataExtractorMixin
from .metadata_pre_scanner import MetadataPreScannerMixin
from .dashboard_pre_processor import DashboardPreProcessorMixin
from .calculation_group_processor import CalculationGroupProcessorMixin

class CoordinatorAgent(AssistantAgent, FolderManagerMixin, MetadataExtractorMixin, MetadataPreScannerMixin, DashboardPreProcessorMixin, CalculationGroupProcessorMixin):
    def __init__(self, name, folder_agent, report_generator, tmdl_generator):
        super().__init__(name=name, code_execution_config={"use_docker": False})
        self.folder_agent = folder_agent
        self.report_generator = report_generator
        self.tmdl_generator = tmdl_generator
        self.action_logger = ActionLogger()

        # Link action_logger to sub-generators
        self.report_generator.action_logger = self.action_logger
        self.tmdl_generator.action_logger = self.action_logger

    def _clean_mapping_names(self, api_data: dict):
        """Recursively cleans BI column/table names in the metadata to remove brackets and strip spaces."""
        def clean(name):
            if not isinstance(name, str): return name
            name = name.strip()
            if name.startswith('[') and name.endswith(']'):
                name = name[1:-1].strip()
            return name
        for t in api_data.get('tables', []):
            if isinstance(t, dict):
                if 'bi_table_name' in t: t['bi_table_name'] = clean(t['bi_table_name'])
                if 'table_name' in t: t['table_name'] = clean(t['table_name'])
                for col in t.get('columns', []):
                    if isinstance(col, dict):
                        if 'bi_column_name' in col: col['bi_column_name'] = clean(col['bi_column_name'])
                        if 'name' in col: col['name'] = clean(col['name'])
        for cs in api_data.get('custom_sql', []):
            if isinstance(cs, dict):
                if 'bi_table_name' in cs: cs['bi_table_name'] = clean(cs['bi_table_name'])
                if 'table_name' in cs: cs['table_name'] = clean(cs['table_name'])
                for col in cs.get('columns', []):
                    if isinstance(col, dict):
                        if 'bi_column_name' in col: col['bi_column_name'] = clean(col['bi_column_name'])
                        if 'name' in col: col['name'] = clean(col['name'])
        for m in api_data.get('measures', []):
            if isinstance(m, dict):
                if 'name' in m: m['name'] = clean(m['name'])
                if 'table' in m: m['table'] = clean(m['table'])
                if 'table_name' in m: m['table_name'] = clean(m['table_name'])
        for block in api_data.get("Calculated Fields & LODs", []):
            for cf in block.get("calculated_fields", []):
                if 'name' in cf: cf['name'] = clean(cf['name'])
            for lod in block.get("lod_expressions", []):
                if 'name' in lod: lod['name'] = clean(lod['name'])
        for cf in api_data.get("calculated_fields", []):
            if isinstance(cf, dict):
                if 'name' in cf: cf['name'] = clean(cf['name'])
                if 'table' in cf: cf['table'] = clean(cf['table'])
                if 'table_name' in cf: cf['table_name'] = clean(cf['table_name'])
        for p in api_data.get("parameters", []):
            if isinstance(p, dict):
                if 'name' in p: p['name'] = clean(p['name'])
                if 'bi_column_name' in p: p['bi_column_name'] = clean(p['bi_column_name'])
                if 'tableau_column_name' in p: p['tableau_column_name'] = clean(p['tableau_column_name'])

    async def _fetch_token(self, session: aiohttp.ClientSession, token_id: str):
        """Fetch the actual PAT directly from Azure Key Vault using access policy."""
        if not token_id:
            return None

        # 1. Local environment variable fallback for token resolution
        import os
        # Case-insensitive environment key lookup matching the token_id
        env_key = token_id.replace("-", "_").upper()
        local_pat = os.getenv(token_id) or os.getenv(env_key) or os.getenv(token_id.replace("-", "_"))
        if local_pat:
            log_info(f"[CoordinatorAgent] Resolved PAT for '{token_id}' from local environment variable: {env_key}")
            return local_pat

        # Generic environment key fallback based on type (Git vs DevOps)
        if "git" in token_id.lower() or "github" in token_id.lower():
            git_pat = os.getenv("GIT_PAT") or os.getenv("GITHUB_PAT")
            if git_pat:
                log_info(f"[CoordinatorAgent] Resolved Git PAT from local fallback environment variable (GIT_PAT/GITHUB_PAT)")
                return git_pat
        else:
            ado_pat = os.getenv("AZURE_DEVOPS_PAT") or os.getenv("ADO_PAT")
            if ado_pat:
                log_info(f"[CoordinatorAgent] Resolved Azure DevOps PAT from local fallback environment variable (AZURE_DEVOPS_PAT/ADO_PAT)")
                return ado_pat
            
        kv_url = Config.KEYVAULT_API_URL
        if not kv_url:
            log_warning("[CoordinatorAgent] KEYVAULT_API_URL is not configured in environment.")
            return None
            
        secret_names = [token_id]
        suffix_name = f"{token_id}-token-value"
        if suffix_name not in secret_names:
            secret_names.append(suffix_name)

        try:
            async with DefaultAzureCredential() as credential:
                async with SecretClient(vault_url=kv_url, credential=credential) as client:
                    for secret_name in secret_names:
                        try:
                            secret = await client.get_secret(secret_name)
                            if secret_name != token_id:
                                log_info(f"[CoordinatorAgent] Resolved token_id '{token_id}' via fallback secret name '{secret_name}'")
                            return secret.value
                        except Exception as inner_ex:
                            # Allow fallback for not-found secret names while preserving hard auth/network failures.
                            if "SecretNotFound" in str(inner_ex):
                                continue
                            raise
        except Exception as e:
            log_warning(f"[CoordinatorAgent] Failed to fetch token for {token_id} from Key Vault: {e}")
        return None

    @staticmethod
    def _normalize_deployment_type(value):
        """Normalize deployment mode to one of: Azure Devops, GIT, Direct Fabric."""
        if value is None:
            return None

        v = str(value).strip().lower()
        if not v:
            return None

        if "fabric" in v:
            return "Direct Fabric"
        if "git" in v or "github" in v:
            return "GIT"
        if "devops" in v or v in ("ado", "azure devops", "azure-devops", "azure_devops"):
            return "Azure Devops"
        if v in ("true", "1", "yes", "on"):
            return "Azure Devops"
        if v in ("false", "0", "no", "off"):
            # Historical fallback used by existing API behavior.
            return "Direct Fabric"

        return None

    async def _save_generation_record(self, run_id, project_id, workbook_id, project_name, status, step="generation", extra_metadata=None, payload=None, token=None):
        """Helper to send generation status/payload to MongoDB."""
        try:
            from app.tableau.core.config import mongo_db
            if mongo_db is None:
                log_warning("[CoordinatorAgent] MONGODB_URL not configured. Skipping save.")
                return
            
            record = {
                "id": run_id,
                "run_id": run_id,
                "project_id": project_id,
                "workbook_id": workbook_id,
                "project_name": project_name or project_id,
                "step": step,
                "status": status,
                "payload": payload or {}
            }
            
            if extra_metadata:
                record.update(extra_metadata)
            
            log_info(f"[CoordinatorAgent] Saving generation record ({status}) to MongoDB")
            await mongo_db["generation"].insert_one(record)
            log_info(f"[CoordinatorAgent] Successfully saved generation record | Status: {status}")
        except Exception as e:
            log_error(f"[CoordinatorAgent] Failed to save generation record to MongoDB: {e}")

    async def select_app_by_name(self, request):
        project_id = getattr(request, 'project_id', None)
        workbook_id = getattr(request, 'workbook_id', None)
        run_id = getattr(request, 'run_id', None)
        token = getattr(request, 'token', None)

        from app.tableau.core.logging_utils import set_logging_context
        if project_id and workbook_id and run_id:
            set_logging_context(project_id, workbook_id, run_id)

        await log_action_to_api("Initiated migration workflow", "Start")

        # department_repo will be resolved after fetching settings from API
        department_repo = None

        target_folder = getattr(request, 'folder_name', None)
        project_id = getattr(request, 'project_id', None)
        workbook_id = getattr(request, 'workbook_id', None)
        run_id = getattr(request, 'run_id', None)
        token = getattr(request, 'token', None)

        if not all([target_folder, project_id, workbook_id, run_id]):
            err_msg = "Missing required API parameters. Ensure folder_name, project_id, workbook_id, and run_id are provided."
            error_response = {"status": "error", "message": err_msg}
            await self._save_generation_record(
                run_id, project_id, workbook_id, None, 
                status="failed", 
                payload={"error": err_msg, "final_response": error_response}, 
                token=token
            )
            return error_response

        data_layer_triggered = getattr(request, 'data_layer_triggered', False)
        deployment_type = getattr(request, 'deployment_type', None)
        fabric_access_token = getattr(request, 'fabric_access_token', None)
        group_id = getattr(request, 'group_id', None)

        # --- LOG: WORKFLOW STARTED (WITH TOKEN) ---
        await self.action_logger.send_activity_to_api(
            project_id, workbook_id, run_id, 
            technical_message=f"Starting extraction for target folder: {target_folder} (DirectLake: {data_layer_triggered})",
            token=token
        )

        api_headers = {"Content-Type": "application/json"}
        if token:
            api_headers["Authorization"] = f"Bearer {token}"

        api_data, report_data = None, None
        new_app_name, destination_folder = "Unknown_App", ""
           # PyArmor-safe: pre-initialize all summary variables so locals() checks are not needed
        pages = []
        all_tables = []
        measure_names = []
        calculated_fields_metadata = []
        visuals_count = 0
        parameters_count = 0
        summary = []
        actions_by_source = {}
        raw_project_name = None
        original_payload_backup = {}
        workspace_name = None
        target_branch = "main"

        try:
            from app.tableau.core.config import mongo_db
            async with aiohttp.ClientSession(headers=api_headers) as session:
                
                # Fetch Mapping Data from MongoDB
                if mongo_db is None:
                    raise ValueError("MONGODB_URL is not configured.")
                
                first_record = await mongo_db["mapping"].find_one({
                    "project_id": project_id,
                    "workbook_id": workbook_id,
                    "run_id": run_id
                })
                if not first_record:
                    first_record = await mongo_db["mapping_results"].find_one({
                        "project_id": project_id,
                        "workbook_id": workbook_id,
                        "run_id": run_id
                    })
                
                if not first_record:
                    err_msg = f"No mapping records found for project_id={project_id}, workbook_id={workbook_id}, run_id={run_id}"
                    log_error(f"[CoordinatorAgent] {err_msg}")
                    await self.action_logger.send_error_to_api(
                        project_id, workbook_id, run_id,
                        "No mapping data returned from API", err_msg,
                        token=token
                    )
                    error_response = {"status": "error", "message": err_msg}
                    await self._save_generation_record(
                        run_id, project_id, workbook_id, None, 
                        status="failed", 
                        payload={"error": err_msg, "final_response": error_response}, 
                        token=token
                    )
                    return error_response
                    
                outer_payload = first_record.get("payload", {})

                raw_project_name = (outer_payload.get("workbook_metadata") or {}).get("name") or \
                                   first_record.get("project_name") or \
                                   (outer_payload.get("metadata") or {}).get("project_name") or \
                                   "Unknown_Project"
                cleaned_name = re.sub(r'[^a-zA-Z0-9_\s]', '', raw_project_name)
                cleaned_name = cleaned_name.replace(" ", "_")
                sanitized_name = re.sub(r'_+', '_', cleaned_name).strip("_")

                # --- Resolve credentials from request overrides, .env Config, or settings API ---
                req_git_org = getattr(request, "git_org", None)
                req_git_repo = getattr(request, "git_repo", None)
                req_git_pat = getattr(request, "git_pat", None)
                req_git_branch = getattr(request, "branch", None) or getattr(request, "git_branch", None)

                git_org = req_git_org or Config.GIT_ORG
                git_repo = req_git_repo or Config.GIT_REPO_NAME
                git_pat = req_git_pat or Config.GIT_PAT
                git_branch = req_git_branch or "main"
                git_token_id = None

                ado_org, ado_project, ado_repo, ado_token_id, ado_branch = None, None, None, None, None
                ado_pat = getattr(request, "ado_pat", None) or Config.AZURE_DEVOPS_PAT

                # Only probe settings API if Git credentials are still missing
                if not (git_org and git_repo and git_pat) and Config.DEPLOYMENT_SETTINGS_API_URL:
                    try:
                        settings_base = Config.DEPLOYMENT_SETTINGS_API_URL.rstrip('/')
                        git_settings_url = f"{settings_base}/deployment/git"
                        async with session.get(git_settings_url, timeout=aiohttp.ClientTimeout(total=5)) as g_resp:
                            if g_resp.status == 200:
                                g_data = await g_resp.json()
                                if isinstance(g_data, list) and g_data: g_data = g_data[0]
                                if isinstance(g_data, dict):
                                    git_org = git_org or g_data.get("git_org")
                                    git_repo = git_repo or g_data.get("git_repo")
                                    git_token_id = g_data.get("token_id")
                                    git_branch = git_branch or g_data.get("git_branch")
                                    log_info(f"[CoordinatorAgent] Fetched Git settings: {git_org}/{git_repo} (branch: {git_branch}, token_id: {git_token_id})")
                                    
                                    if git_token_id and not git_pat and Config.KEYVAULT_API_URL:
                                        git_pat = await self._fetch_token(session, git_token_id)
                                        if git_pat: log_info("[CoordinatorAgent] Successfully fetched Git PAT from Key Vault")
                            else:
                                log_warning(f"[CoordinatorAgent] Git settings API returned {g_resp.status} at {git_settings_url}")
                    except Exception as ge:
                        log_warning(f"[CoordinatorAgent] Could not fetch Git settings: {ge}")

                if deployment_type == "Azure Devops" and not ado_pat and Config.DEPLOYMENT_SETTINGS_API_URL:
                    try:
                        settings_base = getattr(Config, "DEPLOYMENT_SETTINGS_API_URL", "").rstrip('/')
                        ado_settings_url = f"{settings_base}/deployment/azure-devops"
                        async with session.get(ado_settings_url, timeout=aiohttp.ClientTimeout(total=5)) as a_resp:
                            if a_resp.status == 200:
                                a_data = await a_resp.json()
                                if isinstance(a_data, list) and a_data: a_data = a_data[0]
                                if isinstance(a_data, dict):
                                    ado_org = a_data.get("azure_devops_org")
                                    ado_project = a_data.get("azure_devops_project")
                                    ado_repo = a_data.get("azure_devops_repo")
                                    ado_token_id = a_data.get("token_id")
                                    ado_branch = a_data.get("azure_devops_branch")
                                    log_info(f"[CoordinatorAgent] Fetched Azure DevOps settings: {ado_org}/{ado_project}/{ado_repo} (branch: {ado_branch}, token_id: {ado_token_id})")
                                    
                                    if ado_token_id and Config.KEYVAULT_API_URL:
                                        ado_pat = await self._fetch_token(session, ado_token_id)
                                        if ado_pat: log_info("[CoordinatorAgent] Successfully fetched Azure DevOps PAT from Key Vault")
                            else:
                                log_warning(f"[CoordinatorAgent] Azure DevOps settings API returned {a_resp.status} at {ado_settings_url}")
                    except Exception as ae:
                        log_warning(f"[CoordinatorAgent] Could not fetch Azure DevOps settings: {ae}")

                # --- NEW: Fetch Deployment Type dynamically (if not provided in request) ---
                if not deployment_type:
                    deployment_type = "GIT" # Default fallback
                    try:
                        dev_data = await mongo_db["settings"].find_one({"type": "deploy_to_azure_devops"})
                        if isinstance(dev_data, dict):
                            val = dev_data.get("deploy_to_azure_devops") or dev_data.get("status") or dev_data.get("value") or dev_data.get("deployment_type")
                            if val is not None:
                                normalized = self._normalize_deployment_type(val)
                                deployment_type = normalized or "GIT"
                            log_info(f"[CoordinatorAgent] Dynamic Deployment Type fetched from DB: {deployment_type} (Source: {val})")
                    except Exception as ex:
                        log_warning(f"[CoordinatorAgent] Failed to fetch deployment type: {ex}. Using default: {deployment_type}")
                else:
                    normalized = self._normalize_deployment_type(deployment_type)
                    if not normalized:
                        err_msg = f"Unsupported deployment_type '{deployment_type}'. Use one of: Azure Devops, GIT, Direct Fabric"
                        log_error(f"[CoordinatorAgent] {err_msg}")
                        await self.action_logger.send_error_to_api(
                            project_id,
                            workbook_id,
                            run_id,
                            "Invalid deployment type",
                            err_msg,
                            token=token,
                        )
                        error_response = {"status": "error", "message": err_msg}
                        await self._save_generation_record(
                            run_id, project_id, workbook_id, raw_project_name, 
                            status="failed", 
                            payload={"error": err_msg, "final_response": error_response}, 
                            token=token
                        )
                        return error_response
                    deployment_type = normalized
                    log_info(f"[CoordinatorAgent] Using Deployment Type from request body: {deployment_type}")

                # Ensure deployment_type is canonical even when fetched dynamically.
                deployment_type = self._normalize_deployment_type(deployment_type) or deployment_type

                # --- Post-Fetch Validation based on Deployment Type ---
                if deployment_type == "Azure Devops":
                    if not all([ado_org, ado_project, ado_repo]):
                        err_msg = "Azure DevOps settings (org/project/repo) missing in API response"
                        log_error(f"[CoordinatorAgent] {err_msg}")
                        await self.action_logger.send_error_to_api(project_id, workbook_id, run_id, "Missing ADO Config", err_msg, token=token)
                        error_response = {"status": "error", "message": err_msg}
                        await self._save_generation_record(
                            run_id, project_id, workbook_id, raw_project_name, 
                            status="failed", 
                            payload={"error": err_msg, "final_response": error_response}, 
                            token=token
                        )
                        return error_response
                    if not ado_pat:
                        err_msg = f"Azure DevOps PAT could not be resolved from Key Vault for token_id '{ado_token_id}'"
                        log_error(f"[CoordinatorAgent] {err_msg}")
                        await self.action_logger.send_error_to_api(project_id, workbook_id, run_id, "Missing ADO PAT", err_msg, token=token)
                        error_response = {"status": "error", "message": err_msg}
                        await self._save_generation_record(
                            run_id, project_id, workbook_id, raw_project_name, 
                            status="failed", 
                            payload={"error": err_msg, "final_response": error_response}, 
                            token=token
                        )
                        return error_response
                elif deployment_type == "GIT":
                    if not all([git_org, git_repo]):
                        err_msg = "Git settings (org/repo) missing in API response"
                        log_error(f"[CoordinatorAgent] {err_msg}")
                        await self.action_logger.send_error_to_api(project_id, workbook_id, run_id, "Missing Git Config", err_msg, token=token)
                        error_response = {"status": "error", "message": err_msg}
                        await self._save_generation_record(
                            run_id, project_id, workbook_id, raw_project_name, 
                            status="failed", 
                            payload={"error": err_msg, "final_response": error_response}, 
                            token=token
                        )
                        return error_response
                    if not git_pat:
                        err_msg = f"Git PAT could not be resolved from Key Vault for token_id '{git_token_id}'"
                        log_error(f"[CoordinatorAgent] {err_msg}")
                        await self.action_logger.send_error_to_api(project_id, workbook_id, run_id, "Missing Git PAT", err_msg, token=token)
                        error_response = {"status": "error", "message": err_msg}
                        await self._save_generation_record(
                            run_id, project_id, workbook_id, raw_project_name, 
                            status="failed", 
                            payload={"error": err_msg, "final_response": error_response}, 
                            token=token
                        )
                        return error_response
                elif deployment_type == "Direct Fabric":
                    if not fabric_access_token:
                        err_msg = "deployment_type is Direct Fabric but fabric_access_token is missing"
                        log_error(f"[CoordinatorAgent] {err_msg}")
                        await self.action_logger.send_error_to_api(project_id, workbook_id, run_id, "Missing Fabric Access Token", err_msg, token=token)
                        error_response = {"status": "error", "message": err_msg}
                        await self._save_generation_record(
                            run_id, project_id, workbook_id, raw_project_name, 
                            status="failed", 
                            payload={"error": err_msg, "final_response": error_response}, 
                            token=token
                        )
                        return error_response
                else:
                    err_msg = f"Unsupported deployment_type '{deployment_type}'. Use one of: Azure Devops, GIT, Direct Fabric"
                    log_error(f"[CoordinatorAgent] {err_msg}")
                    await self.action_logger.send_error_to_api(project_id, workbook_id, run_id, "Invalid deployment type", err_msg, token=token)
                    error_response = {"status": "error", "message": err_msg}
                    await self._save_generation_record(
                        run_id, project_id, workbook_id, raw_project_name, 
                        status="failed", 
                        payload={"error": err_msg, "final_response": error_response}, 
                        token=token
                    )
                    return error_response

                department_repo = getattr(request, 'department_repo', None) or ado_repo
                if not department_repo:
                    log_warning("[CoordinatorAgent] department_repo is NOT resolved (ado_repo from API was empty).")
                log_info(f"[CoordinatorAgent] Resolved Department Repo: {department_repo}")

                timezone_name = "Australia/Sydney"

                timezone_name = "Australia/Sydney"
                try:
                    settings_data = await mongo_db["settings"].find_one({"project_id": project_id})
                    if isinstance(settings_data, dict):
                        settings_obj = settings_data.get("settings", settings_data)
                        timezone_value = settings_obj.get("timezone") if isinstance(settings_obj, dict) else None
                        if isinstance(timezone_value, str) and timezone_value.strip():
                            timezone_name = timezone_value.strip()
                except Exception as ex:
                    log_warning(f"[CoordinatorAgent] Failed to fetch timezone from DB: {ex}. Using default timezone {timezone_name}.")

                now_utc = datetime.datetime.now(datetime.timezone.utc)

                def _format_timestamp_for_tz(tz_name: str):
                    try:
                        return now_utc.astimezone(ZoneInfo(tz_name)).strftime("%Y%m%d_%H%M%S")
                    except (ZoneInfoNotFoundError, ValueError):
                        pass
                    try:
                        import pytz
                        return now_utc.astimezone(pytz.timezone(tz_name)).strftime("%Y%m%d_%H%M%S")
                    except Exception:
                        return None

                timestamp = _format_timestamp_for_tz(timezone_name)
                if not timestamp and timezone_name != "Australia/Sydney":
                    log_warning(f"[CoordinatorAgent] Invalid/unavailable timezone '{timezone_name}' from settings API. Falling back to Australia/Sydney.")
                    timestamp = _format_timestamp_for_tz("Australia/Sydney")

                if not timestamp:
                    log_warning("[CoordinatorAgent] Unable to resolve Australia/Sydney timezone. Falling back to UTC timestamp.")
                    timestamp = now_utc.strftime("%Y%m%d_%H%M%S")

                # Limit to 100 chars + timestamp to stay under Fabric's item name limits
                base_name = sanitized_name[:100].strip("_")
                new_app_name = f"{base_name}_{timestamp}"
                log_info(f"[CoordinatorAgent] Standardized Name: {new_app_name}")

                clean_target = (target_folder or os.getenv("DESTINATION_BASE", "test-workspace")).strip("/ ")
                if clean_target:
                    destination_folder = f"{clean_target}/{new_app_name}"
                else:
                    destination_folder = new_app_name

                inner_payload = outer_payload.get("payload")
                api_data = inner_payload if inner_payload else outer_payload
                self._clean_mapping_names(api_data)
                
                # Fetch data_layer_results for extract mode (lakehouse info)
                is_direct_lake = False
                lakehouse_id = None
                try:
                    dl_record = await mongo_db["data-layer"].find_one({
                        "project_id": project_id, 
                        "workbook_id": workbook_id, 
                        "run_id": run_id
                    })
                    if dl_record:
                                api_data["data_layer_results"] = dl_record
                                log_info(f"[CoordinatorAgent] Injected data_layer_results into api_data from route: data-layer")
                                
                                # Extract lakehouse_id and metadata from data layer summary
                                lakehouse_name = None
                                lakehouse_schema = None
                                try:
                                    summary = dl_record.get("payload", {}).get("processing_summary", [])
                                    if summary and isinstance(summary, list):
                                        lh_info = summary[0].get("fabric_artifacts", {}).get("lakehouse", {})
                                        lakehouse_id = lh_info.get("id")
                                        lakehouse_name = lh_info.get("name")
                                        lakehouse_schema = summary[0].get("lakehouse_schema")
                                        log_info(f"[CoordinatorAgent] Extracted DirectLake info: {lakehouse_id=}, {lakehouse_name=}, {lakehouse_schema=}")
                                except Exception as ex:
                                    log_warning(f"[CoordinatorAgent] Could not extract lakehouse metadata: {ex}")

                                # If the connection is Direct Lake (extract), prioritize data layer payload
                                # ONLY if data_layer_triggered is True
                                if data_layer_triggered:
                                    dl_inner_payload = dl_record.get("payload", {})
                                    
                                    # Try to find tables in source_metadata (nested) or at top level
                                    dl_tables = dl_inner_payload.get("tables")
                                    dl_custom_sql = dl_inner_payload.get("custom_sql")
                                    
                                    if not dl_tables and isinstance(summary, list) and summary:
                                        # Pull from source_metadata if present in processing_summary[0]
                                        src_meta = summary[0].get("source_metadata", {})
                                        dl_tables = src_meta.get("tables", [])
                                        dl_custom_sql = src_meta.get("custom_sql", [])

                                    if isinstance(dl_inner_payload, dict) and dl_tables:
                                        log_info(f"[CoordinatorAgent] Direct Lake (Extract) triggered and {len(dl_tables)} tables detected. Switching api_data to use data-layer payload.")
                                        # Store original mapping data for reference if needed
                                        original_tables = api_data.get("tables", [])
                                        original_custom_sql = api_data.get("custom_sql", [])
                                        api_data["mapping_payload_backup"] = api_data.copy()
                                        # Update api_data with data-layer metadata but keep data_layer_results
                                        dl_results_tmp = api_data["data_layer_results"]
                                        api_data.update(dl_inner_payload)
                                        # Ensure tables and custom_sql are top-level for standard generator consumption
                                        api_data["tables"] = dl_tables
                                        api_data["custom_sql"] = dl_custom_sql
                                        api_data["data_layer_results"] = dl_results_tmp
                                        is_direct_lake = True
                                        self._clean_mapping_names(api_data)

                                        # --- [NEW] Inject parameter tables as DirectLake entities ---
                                        # The data layer creates parameter tables in the lakehouse but does NOT
                                        # include them in source_metadata.tables. We synthesize table definitions
                                        # from the original mapping parameters so they flow through the standard
                                        # TMDL generation pipeline.
                                        params_as_tables = []
                                        if isinstance(summary, list) and summary:
                                            params_as_tables = summary[0].get("parameters_as_tables", [])
                                        if params_as_tables:
                                            original_params = api_data.get("mapping_payload_backup", {}).get("parameters", [])
                                            log_info(f"[CoordinatorAgent] Found {len(params_as_tables)} parameter tables to inject as DirectLake entities: {params_as_tables}")

                                            if not isinstance(api_data.get("relationships"), list):
                                                api_data["relationships"] = []
                                            if not isinstance(api_data.get("measures"), list):
                                                api_data["measures"] = []

                                            for param in (original_params or []):
                                                if not isinstance(param, dict): continue
                                                p_name = param.get("name", "")
                                                if p_name not in params_as_tables: continue

                                                # Extract column info from parameter DAX
                                                pbi_data = param.get("powerbi", {})
                                                raw_dax = pbi_data.get("dax") or param.get("dax") or ""
                                                clean_dax = re.sub(r'```[a-zA-Z]*', '', str(raw_dax)).replace('```', '').strip()
                                                while clean_dax.startswith('='): clean_dax = clean_dax[1:].strip()

                                                ref_column = "Value"
                                                ref_table = None
                                                m_ref = re.search(r"'?([a-zA-Z0-9_ -]+)'?\[([a-zA-Z0-9_ -]+)\]", clean_dax)
                                                if m_ref:
                                                    ref_table = m_ref.group(1).strip()
                                                    ref_column = m_ref.group(2).strip()

                                                data_type = pbi_data.get("data_type") or param.get("data_type", "string")

                                                # Synthesize table definition
                                                param_table_def = {
                                                    "table_name": p_name,
                                                    "columns": [{"name": ref_column, "datatype": data_type}],
                                                    "is_parameter_table": True
                                                }
                                                api_data["tables"].append(param_table_def)
                                                
                                                # Store the ref_column in the parameter object for slicer builder
                                                param["_directlake_column"] = ref_column
                                                param["_directlake_table"] = p_name
                                                
                                                log_info(f"[CoordinatorAgent] Injected parameter '{p_name}' as DirectLake table with column '{ref_column}' ({data_type})")

                                                # Add relationship to referenced table
                                                if ref_table:
                                                    api_data["relationships"].append({
                                                        "fromTable": p_name,
                                                        "fromColumn": ref_column,
                                                        "toTable": ref_table,
                                                        "toColumn": ref_column,
                                                        "cardinality": "ManyToOne",
                                                        "crossFilterDirection": "both"
                                                    })
                                                    log_info(f"[CoordinatorAgent] Added relationship: '{p_name}'[{ref_column}] -> '{ref_table}'[{ref_column}]")

                                                # Add SELECTEDVALUE measure
                                                default = pbi_data.get("current_value") or param.get("suggested_default", "0")
                                                dt = str(data_type).lower()
                                                if dt in ["integer", "int", "whole number", "int64"]:
                                                    default_val = str(default) if str(default).isdigit() else "0"
                                                elif dt in ["string", "text"]:
                                                    default_val = f'"{default}"'
                                                else:
                                                    default_val = str(default)

                                                measure_name = f"Selected{p_name.replace(' ', '').replace('-', '').replace('%', 'Percent')}"
                                                api_data["measures"].append({
                                                    "name": measure_name,
                                                    "table": p_name,
                                                    "dax_formula": f"SELECTEDVALUE('{p_name}'[{ref_column}], {default_val})"
                                                })
                                                log_info(f"[CoordinatorAgent] Added SELECTEDVALUE measure '{measure_name}' for parameter table '{p_name}'")

                                        # --- Merge bi_column_name metadata from original mapping tables into data layer tables ---
                                        # Build lookup: { (bi_table_name or tableau_table_name) -> { tableau_column_name -> col_dict } }
                                        mapping_col_lookup = {}
                                        # Include standard tables and custom SQL
                                        for orig_tbl in (original_tables or []) + (original_custom_sql or []):
                                            if not isinstance(orig_tbl, dict): continue
                                            tbl_key = orig_tbl.get('bi_table_name') or orig_tbl.get('table_name') or ''
                                            tbl_key_tab = orig_tbl.get('tableau_table_name') or ''
                                            col_map = {}
                                            for oc in orig_tbl.get('columns', []):
                                                if not isinstance(oc, dict): continue
                                                tab_cname = oc.get('tableau_column_name', '')
                                                if tab_cname:
                                                    col_map[tab_cname] = oc
                                                # Also index by name key
                                                name_key = oc.get('name', '')
                                                if name_key and name_key != tab_cname:
                                                    col_map[name_key] = oc
                                            if tbl_key:
                                                mapping_col_lookup[tbl_key] = col_map
                                            if tbl_key_tab and tbl_key_tab != tbl_key:
                                                mapping_col_lookup[tbl_key_tab] = col_map

                                        # --- [NEW] Include Parameters as reference entities ---
                                        original_params = original_payload_backup.get("parameters", []) or api_data.get("mapping_payload_backup", {}).get("parameters", [])
                                        for param in (original_params or []):
                                            if not isinstance(param, dict): continue
                                            p_name = param.get('name') or param.get('tableau_column_name')
                                            if not p_name: continue
                                            # For parameters, the parameter itself contains the column metadata
                                            # We map it so that it can match a Data Layer table of the same name
                                            mapping_col_lookup[p_name] = {
                                                "Value": param, # Default DirectLake parameter column name
                                                p_name: param,
                                                param.get('tableau_column_name', ''): param
                                            }
                                            bi_name = param.get('bi_column_name')
                                            if bi_name:
                                                mapping_col_lookup[bi_name] = mapping_col_lookup[p_name]

                                        merged_count = 0
                                        for dl_tbl in (dl_tables or []) + (dl_custom_sql or []):
                                            if not isinstance(dl_tbl, dict): continue
                                            dl_tbl_name = dl_tbl.get('bi_table_name') or dl_tbl.get('table_name') or dl_tbl.get('tableau_table_name') or ''
                                            col_map = mapping_col_lookup.get(dl_tbl_name, {})
                                            if not col_map:
                                                # Try matching by tableau_table_name
                                                dl_tbl_tab = dl_tbl.get('tableau_table_name', '')
                                                col_map = mapping_col_lookup.get(dl_tbl_tab, {})
                                            for dl_col in dl_tbl.get('columns', []):
                                                if not isinstance(dl_col, dict): continue
                                                # Find matching original column
                                                col_key = dl_col.get('tableau_column_name') or dl_col.get('name') or ''
                                                orig_col = col_map.get(col_key)
                                                if orig_col:
                                                    # Inject bi_column_name if not present
                                                    if 'bi_column_name' not in dl_col and 'bi_column_name' in orig_col:
                                                        dl_col['bi_column_name'] = orig_col['bi_column_name']
                                                        merged_count += 1
                                                    if 'bi_datatype' not in dl_col and 'bi_datatype' in orig_col:
                                                        dl_col['bi_datatype'] = orig_col['bi_datatype']
                                                    if 'tableau_renamed_column_name' not in dl_col and 'tableau_renamed_column_name' in orig_col:
                                                        dl_col['tableau_renamed_column_name'] = orig_col['tableau_renamed_column_name']
                                        log_info(f"[CoordinatorAgent] Merged bi_column_name metadata for {merged_count} columns from mapping into data-layer tables")
                                    else:
                                        log_warning(f"[CoordinatorAgent] Direct Lake triggered but no tables found in data-layer payload.")
                                else:
                                    log_info(f"[CoordinatorAgent] Data layer results found but Direct Lake NOT triggered. Using standard import mode.")
                    else:
                        log_info(f"[CoordinatorAgent] No data_layer_results found in MongoDB - skipping lakehouse integration")
                except Exception as dl_err:
                    # Log error but don't fail, continue with mapping data only
                    log_info(f"[CoordinatorAgent] Could not fetch data_layer_results: {dl_err} - continuing without lakehouse info")


                log_info(f"[CoordinatorAgent] Mode: {'Direct Lake' if is_direct_lake else 'Standard'}")
                log_info(f"[CoordinatorAgent] outer_payload keys: {list(outer_payload.keys()) if isinstance(outer_payload, dict) else type(outer_payload)}")
                log_info(f"[CoordinatorAgent] api_data keys: {list(api_data.keys()) if isinstance(api_data, dict) else type(api_data)}")
                all_tables = api_data.get("tables", [])
                log_info(f"[CoordinatorAgent] api_data has {len(all_tables)} tables, {len(api_data.get('custom_sql', []))} custom_sql")

                # Delegate parsing and extraction logic to the mixin
                field_to_table, field_to_datatype, display_to_bi_name, field_to_format, default_table, fields_with_dax, measure_set, known_source_columns, calculated_fields_metadata = self._parse_metadata_fields(api_data)

                # --- Build sheet visuals index for dashboard lookup ---
                visuals_data = api_data.get("visuals", {})
                raw_sheets = visuals_data.get("sheet_visuals", []) or api_data.get("sheets_visuals", [])
                sheets = raw_sheets if isinstance(raw_sheets, list) else raw_sheets.get("sheets", [])

                # Index sheets by name for dashboard layout reference
                sheet_by_name = {}
                for sheet in sheets:
                    sheet_by_name[sheet.get("name", "")] = sheet

                # --- Build Sheets Formatting Index (for Tooltips, Axes, etc) ---
                formatting_and_styling = api_data.get("formatting_and_styling", {})
                sheets_formatting = formatting_and_styling.get("sheets_formatting", []) if isinstance(formatting_and_styling, dict) else []
                formatting_by_sheet = {fmt.get("sheet_name", ""): fmt for fmt in sheets_formatting if isinstance(fmt, dict)}

                # --- Process Dashboards as pages (if present) ---
                dashboards = visuals_data.get("dashboards", []) if isinstance(visuals_data, dict) else []

                pages, page_order = [], []
                page_idx = 0

                # Delegate parsing and extraction logic to the mixin
                pages, page_order, page_idx = self._process_dashboards(dashboards, sheet_by_name, formatting_by_sheet, page_idx)

                # Build actions_by_source index: {source_visual_name: [action, ...]}
                raw_actions = api_data.get("actions", [])
                actions_by_source = {}
                if isinstance(raw_actions, list):
                    for act in raw_actions:
                        if not isinstance(act, dict): continue
                        ta = act.get("tableau_action", {})
                        pe = act.get("powerbi_equivalent", {})
                        # Source can come from multiple keys
                        src = pe.get("source_visual") or ta.get("source_worksheet") or ta.get("source_sheet") or ""
                        if src:
                            actions_by_source.setdefault(src, []).append(act)

                # Add ALL sheet visuals as individual pages
                # (even those already embedded in dashboard pages)
                for sheet in sheets:
                    page_idx += 1
                    page_id = f"Page{page_idx}"
                    page_order.append(page_id)

                    # Preserve full filter dicts with tableau_type
                    clean_filters = []
                    for f_item in sheet.get("filters", []):
                        if not f_item: continue
                        if isinstance(f_item, dict):
                            val = f_item.get("tableau_original_name") or f_item.get("filter_name") or f_item.get("name") or f_item.get("field") or f_item.get("caption") or f_item.get("column")
                            if isinstance(val, dict):
                                val = val.get("name") or val.get("field_name") or str(val)
                            if val and str(val).strip() not in ("None", ""):
                                # Preserve ALL metadata from f_item but ensure tableau_original_name is set
                                new_f = f_item.copy() if isinstance(f_item, dict) else {}
                                new_f["tableau_original_name"] = str(val).strip()
                                new_f["tableau_type"] = f_item.get("tableau_type", "quick")
                                clean_filters.append(new_f)
                        else:
                            if str(f_item).strip() not in ("None", ""):
                                clean_filters.append({
                                    "tableau_original_name": str(f_item).strip(),
                                    "tableau_type": "quick"
                                })

                    pages.append({
                        "page_id": page_id, "display_name": sheet.get("name", f"Sheet {page_idx}"),
                        "page_json": {"displayOption": "FitToPage", "height": 720, "width": 1280},
                        "mark_type": sheet.get("mark_type", "Automatic"),
                        "power_bi_visual_type": sheet.get("power_bi_visual_type", ""),
                        "rows": sheet.get("rows", []), "columns": sheet.get("columns", []),
                        "marks_text": sheet.get("marks_text", []),
                        "marks_color": sheet.get("marks_color", []),
                        "marks_detail": sheet.get("marks_detail", []),
                        "marks_size": sheet.get("marks_size", []),
                        "measures": sheet.get("measures", []),
                        "filters": clean_filters,
                        "slicers": sheet.get("slicers", []),
                        "sets": sheet.get("sets", []),
                        "parameters": sheet.get("parameters", []),
                        "visual_properties": sheet.get("visual_properties", {}),
                        "legend_position": sheet.get("legend_position", ""),
                        "tooltip_formatting": formatting_by_sheet.get(sheet.get("name", ""), {}).get("tooltip_formatting", []),
                        "visuals": []
                    })

                # If no pages at all (no dashboards, no standalone sheets), fall back
                if not pages:
                    for idx, sheet in enumerate(sheets):
                        page_id = f"Page{idx + 1}"
                        page_order.append(page_id)
                        clean_filters = []
                        for f_item in sheet.get("filters", []):
                            if not f_item: continue
                            if isinstance(f_item, dict):
                                val = f_item.get("filter_name") or f_item.get("name") or f_item.get("field") or f_item.get("caption") or f_item.get("column")
                                if isinstance(val, dict):
                                    val = val.get("name") or val.get("field_name") or str(val)
                                if val and str(val).strip() != "None":
                                    # Preserve metadata if it's a dict
                                    if isinstance(f_item, dict):
                                        new_f = f_item.copy()
                                        new_f["tableau_original_name"] = str(val).strip()
                                        clean_filters.append(new_f)
                                    else:
                                        clean_filters.append(str(val).strip())
                            else:
                                if str(f_item).strip() != "None":
                                    clean_filters.append(str(f_item).strip())
                        pages.append({
                            "page_id": page_id, "display_name": sheet.get("name", f"Sheet {idx + 1}"),
                            "page_json": {"displayOption": "FitToPage", "height": 720, "width": 1280},
                            "mark_type": sheet.get("mark_type", "Automatic"),
                            "power_bi_visual_type": sheet.get("power_bi_visual_type", ""),
                            "rows": sheet.get("rows", []), "columns": sheet.get("columns", []),
                            "measures": sheet.get("measures", []),
                            "filters": clean_filters,
                            "legend_position": sheet.get("legend_position", ""),
                            "tooltip_formatting": formatting_by_sheet.get(sheet.get("name", ""), {}).get("tooltip_formatting", []),
                            "visuals": []
                        })

                # --- Process Stories as pages ---
                # SKIPPED: As per user request, stories should not create visuals or sheets.
                pass

                # --- NEW LOGIC: Promote Visual Formulas to Measures ---
                self._promote_visual_formulas_to_measures(
                    api_data, pages, field_to_table, field_to_datatype, measure_set, default_table, display_to_bi_name, known_source_columns, fields_with_dax, field_to_format
                )

                # --- NEW LOGIC: Calculation Group Dynamic Measures ---
                self._process_calculation_groups(
                    api_data, pages, fields_with_dax, field_to_table, field_to_datatype, measure_set, default_table, display_to_bi_name, known_source_columns
                )

                measures_list = api_data.get("measures", [])
                if isinstance(measures_list, list):
                    for m in measures_list:
                        if isinstance(m, dict) and m.get("name"):
                            measure_set.add(m["name"])
                measure_names = list(measure_set)

                report_data = {
                    "parameters": [] if is_direct_lake else api_data.get("parameters", []), "pages": pages,
                    "parameter_metadata": api_data.get("mapping_payload_backup", {}).get("parameters", api_data.get("parameters", [])),
                    "actions": api_data.get("actions", []),
                    "actions_by_source": actions_by_source,
                    "field_to_table": field_to_table, "field_to_datatype": field_to_datatype,
                    "field_to_format": field_to_format,
                    "measure_names": measure_names,
                    "default_table": default_table,
                    "display_to_bi_name": display_to_bi_name,
                    "embedded_assets": api_data.get("embedded_assets", {}),
                    "is_direct_lake": is_direct_lake
                }
                if is_direct_lake:
                    log_info("[CoordinatorAgent] DirectLake mode: parameters cleared from report_data — all tables (including parameters) sourced from lakehouse")

                # --- LOG: METADATA PARSED (WITH TOKEN) ---
                await self.action_logger.send_activity_to_api(
                    project_id, workbook_id, run_id, 
                    technical_message=f"Successfully parsed metadata for App: {new_app_name}",
                    token=token
                )

        except Exception as e:
            log_error(f"Failed to fetch/process API data: {e}")
            # --- LOG: METADATA PROCESSING ERROR (WITH TOKEN) ---
            await self.action_logger.send_error_to_api(
                project_id, workbook_id, run_id, 
                "Error during JSON parsing/mapping", str(e), 
                token=token
            )
            error_response = {"status": "error", "message": str(e)}
            await self._save_generation_record(
                run_id, project_id, workbook_id, raw_project_name, 
                status="failed", 
                payload={"error": str(e), "final_response": error_response}, 
                token=token
            )
            return error_response

        # 1. Clean / Prepare Destination (Only if Azure DevOps enabled)
        if deployment_type == "Azure Devops":
            await self.clean_destination_folder(destination_folder, repo=department_repo, org=ado_org, project=ado_project, token=ado_pat, branch=ado_branch)
        else:
            log_info(f"[CoordinatorAgent] {deployment_type} selected. Skipping folder cleanup for {destination_folder}")

        # --- LOG: CREATED FOLDER (WITH TOKEN) ---
        await self.action_logger.send_activity_to_api(
            project_id, workbook_id, run_id, 
            technical_message=f"Created {destination_folder} folder",
            token=token
        )

        # Set up memory interceptor
        memory_file_agent = MemoryFileAgent(self.report_generator.file_agent, bypass_real_agent=(deployment_type != "Azure Devops"))
        original_rg_agent = self.report_generator.file_agent
        original_tg_agent = self.tmdl_generator.file_agent

        self.report_generator.file_agent = memory_file_agent
        self.tmdl_generator.file_agent = memory_file_agent

        if deployment_type == "Azure Devops":
            # Provide Azure DevOps context before any file writes happen.
            memory_file_agent.repo = department_repo
            memory_file_agent.org = ado_org
            memory_file_agent.project = ado_project
            memory_file_agent.token = ado_pat
            memory_file_agent.branch = ado_branch

            memory_file_agent.real_agent.repo = department_repo
            memory_file_agent.real_agent.org = ado_org
            memory_file_agent.real_agent.project = ado_project
            memory_file_agent.real_agent.branch = ado_branch

        try:
            # 2. Generate Reports & Visuals
            # Agent Action: Stage 3
            await log_activity_async(
                run_id=run_id,
                project_id=project_id,
                workbook_id=workbook_id,
                summary="Generating Power BI visual layouts (PBIR) from source sheets."
            )

            success = await self.report_generator.generate_and_push_report(
                destination_folder,
                new_app_name,
                report_data=report_data,
                repo=department_repo,
                org=ado_org,
                project=ado_project,
                token=ado_pat
            )

            if not success:
                raise Exception("Failed to generate static project files")

            # --- LOG: REPORT GENERATED (WITH TOKEN) ---
            await self.action_logger.send_activity_to_api(
                project_id, workbook_id, run_id, 
                technical_message="Successfully generated Report files",
                token=token
            )

            # 3. Generate TMDL Models
            semantic_folder = f"{destination_folder}/{new_app_name}.SemanticModel"
        
            # Construct direct lake schema name: schema_<workbook_id>_<run_id>
            dl_schema = None
            if is_direct_lake:
                # Sanitize IDs by replacing hyphens with underscores
                safe_wid = workbook_id.replace('-', '_')
                safe_rid = run_id.replace('-', '_')
                dl_schema = f"schema_{safe_wid}_{safe_rid}"
                log_info(f"[CoordinatorAgent] Using Direct Lake schema: {dl_schema}")

            # Agent Action: Stage 2
            await log_activity_async(
                run_id=run_id,
                project_id=project_id,
                workbook_id=workbook_id,
                summary="Building TMDL semantic model."
            )

            tmdl_result = await self.tmdl_generator.generate_tmdl_structure(
                api_data, semantic_folder, new_app_name, 
                is_direct_lake=is_direct_lake,
                direct_lake_schema=dl_schema
            )

            if not tmdl_result.get("success"):
                failed = tmdl_result.get('failed_files', [])
                msg = f"TMDL generation failed: {tmdl_result.get('message')}. Failed files: {failed}"
                raise Exception(msg)

            if report_data and report_data.get("parameters") and not is_direct_lake:
                param_result = await self.tmdl_generator.add_parameters(report_data["parameters"], semantic_folder, api_data)
                if param_result and not param_result.get("success"):
                    failed = param_result.get('failed_files', [])
                    msg = f"Parameter generation failed: {param_result.get('message')}. Failed files: {failed}"
                    raise Exception(msg)

            # --- LOG: TMDL GENERATED (WITH TOKEN) ---
            await self.action_logger.send_activity_to_api(
                project_id, workbook_id, run_id, 
                technical_message="Successfully generated Semantic Model (TMDL)",
                token=token
            )
        except Exception as e:
            log_error(f"Migration generation failed: {e}")
            # --- CLEANUP: Delete partially-created folder ---
            cleanup_ok = await self._cleanup_folder(destination_folder, repo=department_repo, org=ado_org, project=ado_project, token=ado_pat, branch=ado_branch)
            cleanup_note = " Partial folder cleaned up." if cleanup_ok else " WARNING: Partial folder cleanup failed."
        
            err_msg = f"{str(e)}.{cleanup_note}"
            await self.action_logger.send_error_to_api(
                project_id, workbook_id, run_id, 
                f"Error during generation.{cleanup_note}", str(e), 
                token=token
            )
            error_response = {"status": "error", "message": err_msg}
            await self._save_generation_record(
                run_id, project_id, workbook_id, raw_project_name, 
                status="failed", 
                payload={"error": err_msg, "final_response": error_response}, 
                token=token
            )
            try:
                memory_file_agent.clear_captured_files()
            except Exception:
                pass
            return error_response
        finally:
            self.report_generator.file_agent = original_rg_agent
            self.tmdl_generator.file_agent = original_tg_agent

        # --- DIRECT FABRIC UPLOAD (IF SELECTED) ---
        if deployment_type == "Direct Fabric":
            log_info(f"[CoordinatorAgent] Direct Fabric upload initiated for {new_app_name}")
            await self.action_logger.send_activity_to_api(
                project_id, workbook_id, run_id, 
                technical_message=f"Direct Fabric upload initiated for {new_app_name}",
                token=token
            )
        
            if not fabric_access_token:
                err_msg = "Devops is False but fabric_access_token is missing"
                log_error(f"[CoordinatorAgent] {err_msg}")
                await self.action_logger.send_error_to_api(
                    project_id, workbook_id, run_id, 
                    "Fabric upload failed", err_msg, 
                    token=token
                )
                error_response = {"status": "error", "message": err_msg}
                await self._save_generation_record(
                    run_id, project_id, workbook_id, raw_project_name, 
                    status="failed", 
                    payload={"error": err_msg, "final_response": error_response}, 
                    token=token
                )
                return error_response
        
            if not group_id:
                err_msg = "Devops is False but group_id (workspace ID) is missing"
                log_error(f"[CoordinatorAgent] {err_msg}")
                await self.action_logger.send_error_to_api(
                    project_id, workbook_id, run_id, 
                    "Fabric upload failed", err_msg, 
                    token=token
                )
                error_response = {"status": "error", "message": err_msg}
                await self._save_generation_record(
                    run_id, project_id, workbook_id, raw_project_name, 
                    status="failed", 
                    payload={"error": err_msg, "final_response": error_response}, 
                    token=token
                )
                return error_response

            try:
                # Agent Action: Stage 4
                await log_activity_async(
                    run_id=run_id,
                    project_id=project_id,
                    workbook_id=workbook_id,
                    summary="Deploying generated Power BI project to Microsoft Fabric."
                )

                fabric_agent = FabricAgent()
            
                # Proactive Token Validation
                log_info(f"[CoordinatorAgent] Validating Fabric token for workspace {group_id}...")
                validation_check = await fabric_agent.get_items(group_id, fabric_access_token)
                if validation_check is None:
                    raise Exception(
                        "Fabric Access Token has EXPIRED or is INVALID (unauthorized). "
                        "The caller is not authenticated to access this resource. "
                        "Provide a fresh user-delegated Fabric token (OBO), not an app-only token."
                    )

                # Fetch Workspace Name
                workspace_details = await fabric_agent.get_workspace(group_id, fabric_access_token)
                if workspace_details:
                    workspace_name = workspace_details.get("displayName")
                    log_info(f"[CoordinatorAgent] Fetched Workspace Name: {workspace_name}")

                # Group files by item type
                semantic_model_files = {}
                report_files = {}
            
                destination_folder_norm = destination_folder.replace('\\', '/')
                semantic_prefix = f"{destination_folder_norm}/{new_app_name}.SemanticModel/"
                report_prefix = f"{destination_folder_norm}/{new_app_name}.Report/"
            
                log_info(f"[CoordinatorAgent] Sorting {len(memory_file_agent.captured_files)} captured files into Fabric items...")
                log_info(f"[CoordinatorAgent] Semantic Prefix: {semantic_prefix}")
                log_info(f"[CoordinatorAgent] Report Prefix: {report_prefix}")

                for path, content in memory_file_agent.captured_files.items():
                    # Normalize path separators for comparison
                    norm_path = path.replace('\\', '/')

                    # Exclude git-integration metadata files (must NOT be sent in Fabric API payload)
                    if norm_path.endswith('.platform') or norm_path.endswith('.pbi/localSettings.json'):
                        log_info(f"[CoordinatorAgent] Excluding git-metadata file: {norm_path}")
                        continue

                    if norm_path.startswith(semantic_prefix):
                        rel_path = norm_path[len(semantic_prefix):]
                        semantic_model_files[rel_path] = content
                    elif norm_path.startswith(report_prefix):
                        rel_path = norm_path[len(report_prefix):]
                        report_files[rel_path] = content
                    else:
                        # Log paths that don't match for debugging
                        if "Test-workspace" in norm_path:
                            log_info(f"[CoordinatorAgent] File skipped (no prefix match): {norm_path}")

                log_info(f"[CoordinatorAgent] Files sorted: {len(semantic_model_files)} for Semantic Model, {len(report_files)} for Report")

                # If no files are generated, raise an exception to prevent empty folder creation and fail the migration
                if not semantic_model_files and not report_files:
                    raise Exception(f"No semantic model or report files were generated for workbook {new_app_name}. Migration cannot proceed.")

                # 0. Create Parent Folder
                folder_id = await fabric_agent.create_folder(group_id, new_app_name, fabric_access_token)
                if folder_id == "Accepted":
                    log_info(f"[CoordinatorAgent] Folder creation is async. Polling for Folder GUID (max 60s)...")
                    import asyncio
                    for attempt in range(1, 61):
                        await asyncio.sleep(1)
                        items = await fabric_agent.get_items(group_id, fabric_access_token)
                        if items and isinstance(items, list):
                            for item in items:
                                # Match by displayName (case-insensitive) and type
                                i_name = str(item.get("displayName", "")).strip()
                                i_type = str(item.get("type", "")).lower()
                                if i_name.lower() == new_app_name.lower() and i_type == "folder":
                                    folder_id = item.get("id")
                                    log_info(f"[CoordinatorAgent] Folder GUID found on attempt {attempt}: {folder_id}")
                                    break
                        if folder_id != "Accepted": break
            
                if not folder_id or folder_id == "Accepted":
                    log_warning(f"[CoordinatorAgent] Folder GUID NOT resolved after polling. Items will be created at ROOT.")
                    folder_id = None
                else:
                    log_info(f"[CoordinatorAgent] SUCCESS: Items will be created inside folder {new_app_name} ({folder_id})")

                # 1. Upload Semantic Model
                sm_id = None
                if semantic_model_files:
                    sm_parts = FabricAgent.convert_to_definition_parts(semantic_model_files)
                    # Create inside folder if available
                    sm_id = await fabric_agent.create_item(group_id, new_app_name, "SemanticModel", sm_parts, fabric_access_token, parent_item_id=folder_id)
                    if not sm_id:
                        raise Exception("Failed to create Semantic Model in Fabric")
                    log_info(f"[CoordinatorAgent] Semantic Model status: {sm_id}")

                    # If ID is "Accepted" (async), try to find the real GUID to link the report
                    if sm_id == "Accepted":
                        log_info(f"[CoordinatorAgent] Semantic Model creation is async. Polling for GUID to link report (max 60s)...")
                        import asyncio
                        for attempt in range(1, 21): # Try 20 times, every 3s
                            await asyncio.sleep(3)
                            items = await fabric_agent.get_items(group_id, fabric_access_token)
                            if items and isinstance(items, list):
                                for item in items:
                                    if item.get("displayName") == new_app_name and item.get("type") == "SemanticModel":
                                        sm_id = item.get("id")
                                        log_info(f"[CoordinatorAgent] Semantic Model GUID found on attempt {attempt}: {sm_id}")
                                        break
                            if sm_id != "Accepted": break

                # 2. Upload Report
                if report_files:
                    log_info(f"[CoordinatorAgent] Initiating Report upload for {new_app_name}...")
                
                    # Update definition.pbir to use direct connection (required for API upload)
                    pbir_path = "definition.pbir"
                    if pbir_path in report_files:
                        try:
                            pbir_content = report_files[pbir_path]
                            # Ensure it's a string for json.loads
                            if isinstance(pbir_content, bytes):
                                pbir_content = pbir_content.decode('utf-8')
                        
                            pbir_data = json.loads(pbir_content)
                        
                            # Determine datasetId (use real GUID if found, else null for async)
                            linked_sm_id = sm_id if (sm_id and sm_id != "Accepted") else None
                            if sm_id == "Accepted":
                                log_warning(f"[CoordinatorAgent] Semantic Model GUID not resolved yet. Linking with datasetId: null")
                        
                            pbir_data["datasetReference"] = {
                                "byConnection": {
                                    "connectionString": None,
                                    "pbiServiceModelId": None,
                                    "pbiModelVirtualServerName": "sobe_wowvirtualserver",
                                    "pbiModelDatabaseName": linked_sm_id,
                                    "name": "EntityDataSource",
                                    "connectionType": "pbiServiceXmlaStyleLive"
                                }
                            }
                            report_files[pbir_path] = json.dumps(pbir_data, indent=2)
                            log_info(f"[CoordinatorAgent] Successfully patched definition.pbir with byConnection (datasetId: {linked_sm_id})")
                        except Exception as pe:
                            log_warning(f"[CoordinatorAgent] Failed to update definition.pbir for linking: {pe}")
                    else:
                        log_warning(f"[CoordinatorAgent] definition.pbir not found in report_files, linking skipped")

                    r_parts = FabricAgent.convert_to_definition_parts(report_files)
                    # Create inside folder if available (Match name with Semantic Model)
                    r_id = await fabric_agent.create_item(group_id, new_app_name, "Report", r_parts, fabric_access_token, parent_item_id=folder_id)
                    if not r_id:
                        raise Exception("Failed to create Report in Fabric")
                    log_info(f"[CoordinatorAgent] Report creation status: {r_id}")
                
                    # If ID is "Accepted" (async), poll for the GUID
                    if r_id == "Accepted":
                        log_info(f"[CoordinatorAgent] Report creation is async. Polling for GUID (max 60s)...")
                        for attempt in range(1, 21):
                            await asyncio.sleep(3)
                            items = await fabric_agent.get_items(group_id, fabric_access_token)
                            if items and isinstance(items, list):
                                for item in items:
                                    if item.get("displayName") == new_app_name and item.get("type") == "Report":
                                        r_id = item.get("id")
                                        log_info(f"[CoordinatorAgent] Report GUID found on attempt {attempt}: {r_id}")
                                        break
                            if r_id != "Accepted": break
                else:
                    log_warning(f"[CoordinatorAgent] No report files found to upload for {new_app_name}")

                await self.action_logger.send_activity_to_api(
                    project_id, workbook_id, run_id, 
                    technical_message=f"Successfully uploaded {new_app_name} to Fabric workspace {group_id}",
                    token=token
                )
            except Exception as fe:
                log_error(f"[CoordinatorAgent] Fabric upload failed: {fe}")
                await self.action_logger.send_error_to_api(
                    project_id, workbook_id, run_id, 
                    "Fabric upload failed", str(fe), 
                    token=token
                )
                error_response = {"status": "error", "message": f"Fabric upload failed: {str(fe)}"}
                await self._save_generation_record(
                    run_id, project_id, workbook_id, raw_project_name, 
                    status="failed", 
                    payload={"error": str(fe), "final_response": error_response}, 
                    token=token
                )
                try:
                    memory_file_agent.clear_captured_files()
                except Exception:
                    pass
                return error_response

        # --- GIT (GITHUB) UPLOAD (IF SELECTED) ---
        if deployment_type == "GIT":
            log_info(f"[CoordinatorAgent] GIT upload initiated for {new_app_name}")
            await self.action_logger.send_activity_to_api(
                project_id, workbook_id, run_id, 
                technical_message=f"GIT upload initiated for {new_app_name}",
                token=token
            )
        
            try:
                git_agent = GitAgent(org=git_org, repo=git_repo, pat=git_pat)
                # Push to a branch (prioritize git_branch fetched from API)
                target_branch = git_branch or getattr(request, 'branch', None) or Config.get_branch() or "main"
            
                success = await git_agent.batch_push_files(
                    files=memory_file_agent.captured_files,
                    branch=target_branch,
                    commit_message=f"Tableau Migration: {new_app_name}"
                )
            
                if not success:
                    raise Exception("GitHub push failed. Check logs/Config.")
                
                await self.action_logger.send_activity_to_api(
                    project_id, workbook_id, run_id, 
                    technical_message=f"Successfully uploaded {new_app_name} to GitHub repository",
                    token=token
                )
            except Exception as ge:
                log_error(f"[CoordinatorAgent] GIT upload failed: {ge}")
                await self.action_logger.send_error_to_api(
                    project_id, workbook_id, run_id, 
                    "GIT upload failed", str(ge), 
                    token=token
                )
                error_response = {"status": "error", "message": f"GIT upload failed: {str(ge)}"}
                await self._save_generation_record(
                    run_id, project_id, workbook_id, raw_project_name, 
                    status="failed", 
                    payload={"error": str(ge), "final_response": error_response}, 
                    token=token
                )
                try:
                    memory_file_agent.clear_captured_files()
                except Exception:
                    pass
                return error_response

        await log_action_to_api("Migration completed", new_app_name)

        # --- LOG: WORKFLOW COMPLETED (WITH TOKEN) ---
        await self.action_logger.send_activity_to_api(
            project_id, workbook_id, run_id, 
            technical_message=f"Workflow completed successfully for {new_app_name}",
            token=token
        )

        # --- PREPARE FINAL RESPONSE EARLY (TO STORE IN COSMOS DB) ---
        # Count visuals and parameters
        if api_data:
            parameters_count = len(api_data.get("parameters", []))
            visuals_data = api_data.get("visuals", {})
            raw_sheets = visuals_data.get("sheet_visuals", []) or api_data.get("sheets_visuals", [])
            visuals_count = len(raw_sheets if isinstance(raw_sheets, list) else raw_sheets.get("sheets", []))

        # Build the Azure DevOps / Fabric repo URL for the generated folder
        group_id = getattr(request, 'group_id', None)
        fabric_repo_url = f"https://app.fabric.microsoft.com/groups/{group_id}/list?experience=fabric-developer"

        # Construct project metadata based on deployment type
        project_metadata = {
            "name": new_app_name,
            "deployment_type": deployment_type
        }

        # Normalize deployment_type for robust comparison
        dtype_lower = str(deployment_type or "").lower().strip()

        if "fabric" in dtype_lower:
            project_metadata.update({
                "destination_path": None,
                "repository": None,
                "branch": None,
                "workspace_name": workspace_name,
                "fabric_url": fabric_repo_url,
                "github_url": None
            })
        elif "git" in dtype_lower:
            last_sha = getattr(git_agent, "last_commit_sha", None) if "git_agent" in locals() else None
            project_metadata.update({
                "destination_path": destination_folder,
                "repository": git_repo,
                "branch": target_branch,
                "commit_sha": last_sha,
                "commit": last_sha,
                "fabric_url": None,
                "github_url": f"https://github.com/{git_org}/{git_repo}/commit/{last_sha}" if last_sha else f"https://github.com/{git_org}/{git_repo}"
            })
        else: # Default to Azure DevOps logic
            project_metadata.update({
                "destination_path": destination_folder,
                "repository": department_repo,
                "branch": ado_branch or target_branch,
                "fabric_url": None,
                "github_url": None
            })

        final_response_data = {
            "status": "success",
            "message": "PBIX project generated and deployed successfully.",
            "project": project_metadata,
            "summary": {
                "pages_generated": len(pages),
                "tables_processed": len(all_tables),
                "custom_sql_tables": len(api_data.get("custom_sql", [])) if api_data else 0,
                "measures_created": len(measure_names),
                "calculated_fields_converted": len(calculated_fields_metadata),
                "visuals_generated": visuals_count,
                "parameters_generated": parameters_count,
            },
            "skipped_visuals": getattr(self.report_generator, "skipped_visuals", []),
        }

        # --- SEND JSON BODY COMPREHENSIVE METADATA TO COSMOS DB ---
        # Generate the true PBIP folder JSON payload dynamically from intercepted generated files
        folder_tree = memory_file_agent.build_file_tree(destination_folder)

        # The tree contains "{new_app_name}.SemanticModel" and "{new_app_name}.Report" and "readme"
        semantic_model_data = folder_tree.get(f"{new_app_name}.SemanticModel", {})
        report_data_out = folder_tree.get(f"{new_app_name}.Report", {})

        project_name = raw_project_name or project_id

        extra_metadata = {
            "fabric_url": fabric_repo_url,
            "folder_name": target_folder,
            "group_id": group_id,
            "lakehouse_id": lakehouse_id,
            "data_layer_triggered": data_layer_triggered
        }

        record_payload = {
            "new_app_name": new_app_name,
            "destination_folder": destination_folder,
            "semantic_model": semantic_model_data,
            "report": report_data_out,
            "calculated_fields": calculated_fields_metadata,
            "final_response": final_response_data
        }

        await self._save_generation_record(
            run_id, project_id, workbook_id, project_name, 
            status="completed", 
            extra_metadata=extra_metadata, 
            payload=record_payload, 
            token=token
        )

        # Release large in-memory PBIP payloads after Cosmos write
        try:
            memory_file_agent.clear_captured_files()
        except Exception:
            pass

        return final_response_data

