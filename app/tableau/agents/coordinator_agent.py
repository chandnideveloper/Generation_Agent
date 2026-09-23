from autogen import AssistantAgent
import aiohttp
import httpx
import re
import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from app.tableau.core.config import Config
from app.tableau.core.logging_utils import log_info, log_error, log_warning, log_action_to_api
from app.tableau.services.action_logger import ActionLogger
from app.tableau.core_logic.metadata_exporter import MetadataExporter
import json
class MemoryFileAgent:
    def __init__(self, real_agent):
        self.real_agent = real_agent
        self.captured_files = {}
    async def create_or_update_file(self, file_path, content, commit_message):
        self.captured_files[file_path] = content
        return await self.real_agent.create_or_update_file(file_path, content, commit_message)
    async def batch_update_files(self, changes, repo=None):
        for path, content, action in changes:
            if action in ["add", "edit"] and content is not None:
                self.captured_files[path] = content
        return await self.real_agent.batch_update_files(changes, repo=repo)
    def build_file_tree(self, base_folder):
        tree = {}
        for path, content in self.captured_files.items():
            if not path.startswith(base_folder):
                continue
            rel_path = path[len(base_folder):].lstrip("/")
            parts = rel_path.split("/")
            
            current = tree
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
                
            filename = parts[-1]
            if filename.endswith(".json"):
                try:
                    current[filename] = json.loads(content)
                except (json.JSONDecodeError, TypeError, ValueError):
                    current[filename] = str(content)
            elif filename.endswith(".tmdl") and "table" in content[:20]:
                current[filename] = self.parse_tmdl_table(content)
            else:
                current[filename] = str(content)
        return tree
    def parse_tmdl_table(self, tmdl_str):
        import re
        lines = tmdl_str.replace('\r', '').split('\n')
        table_dict = {
            "name": "",
            "columns": [],
            "measures": [],
            "partitions": [],
            "calculationItems": []
        }
        
        current_type = None
        current_obj = None
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
                
            m_table = re.match(r"^table\s+'?([^'\n]+)'?", line)
            if m_table:
                table_dict["name"] = m_table.group(1)
                continue
                
            m_col = re.match(r"^\s+column\s+'?([^'\n=]+)'?", line)
            if m_col:
                current_obj = {"name": m_col.group(1), "properties": {}}
                table_dict["columns"].append(current_obj)
                current_type = "column"
                if "=" in line:
                    current_obj["isCalculated"] = True
                    current_obj["expression"] = line.split("=", 1)[1].strip() + "\n"
                continue
                
            m_meas = re.match(r"^\s+measure\s+'?([^'\n=]+)'?", line)
            if m_meas:
                current_obj = {"name": m_meas.group(1), "properties": {}, "expression": ""}
                table_dict["measures"].append(current_obj)
                current_type = "measure"
                if "=" in line:
                    current_obj["expression"] += line.split("=", 1)[1].strip() + "\n"
                continue
                
            m_part = re.match(r"^\s+partition\s+'?([^'\n=]+)'?", line)
            if m_part:
                current_obj = {"name": m_part.group(1), "properties": {}, "expression": ""}
                table_dict["partitions"].append(current_obj)
                current_type = "partition"
                if "=" in line:
                    current_obj["expression"] += line.split("=", 1)[1].strip() + "\n"
                continue
            m_calc = re.match(r"^\s+calculationItem\s+'?([^'\n=]+)'?", line)
            if m_calc:
                current_obj = {"name": m_calc.group(1), "properties": {}, "expression": ""}
                table_dict["calculationItems"].append(current_obj)
                current_type = "calculationItem"
                if "=" in line:
                    current_obj["expression"] += line.split("=", 1)[1].strip() + "\n"
                continue
                
            if current_obj is not None:
                if current_type in ("column", "measure", "partition", "calculationItem"):
                    m_prop = re.match(r"^\s+([a-zA-Z0-9_]+)\s*[:=]\s*(.+)", line)
                    if m_prop and current_type not in ("partition"):
                        k, v = m_prop.group(1), m_prop.group(2)
                        # Filter out properties vs expressions for measures/calcItems
                        if k in ("formatString", "isHidden", "displayFolder", "description", "dataType", "sourceColumn", "summarizeBy", "sortByColumn", "annotation"):
                            current_obj["properties"][k.strip()] = v.strip()
                        else:
                            if "expression" in current_obj:
                                current_obj["expression"] += stripped + "\n"
                            elif current_type == "column":
                                current_obj["isCalculated"] = True
                                current_obj["expression"] = stripped + "\n"
                    else:
                        if "expression" in current_obj:
                            current_obj["expression"] += stripped + "\n"
                        elif current_type == "column" and not stripped.startswith("column") and not stripped.startswith("measure") and ":" not in stripped:
                            current_obj["isCalculated"] = True
                            current_obj["expression"] = (current_obj.get("expression") or "") + stripped + "\n"
        for lst in (table_dict["columns"], table_dict["measures"], table_dict["partitions"], table_dict["calculationItems"]):
            for obj in lst:
                if "expression" in obj:
                    obj["expression"] = obj["expression"].strip()
        return table_dict
class CoordinatorAgent(AssistantAgent):
    def __init__(self, name, folder_agent, report_generator, tmdl_generator):
        super().__init__(name=name, code_execution_config={"use_docker": False})
        self.folder_agent = folder_agent
        self.report_generator = report_generator
        self.tmdl_generator = tmdl_generator
        self.action_logger = ActionLogger()
        
        # Link action_logger to sub-generators
        self.report_generator.action_logger = self.action_logger
        self.tmdl_generator.action_logger = self.action_logger
    async def clean_destination_folder(self, destination_folder: str, repo=Config.REPO):
        log_info(f"[CoordinatorAgent] Checking/Cleaning destination: {destination_folder}")
        items = await self.folder_agent.get_contents(destination_folder, repo=repo)
        if not items:
            return True
        
        deletes = []
        for item in items:
            if not item.get('isFolder', False):
                deletes.append((item['path'].lstrip('/'), None, "delete"))
        if deletes:
            success = await self.report_generator.file_agent.batch_update_files(deletes, repo=repo)
            return success
        return True
    async def _cleanup_folder(self, destination_folder: str, repo=Config.REPO):
        """Delete all files in a partially-created folder after an error."""
        log_info(f"[CoordinatorAgent] Cleaning up failed folder: {destination_folder}")
        try:
            items = await self.folder_agent.get_contents(destination_folder, repo=repo)
            if not items:
                log_info(f"[CoordinatorAgent] No items found in {destination_folder}, nothing to clean up")
                return True
            deletes = []
            for item in items:
                if not item.get('isFolder', False):
                    deletes.append((item['path'].lstrip('/'), None, "delete"))
            if deletes:
                success = await self.report_generator.file_agent.batch_update_files(deletes, repo=repo)
                if success:
                    log_info(f"[CoordinatorAgent] Successfully cleaned up {len(deletes)} files from {destination_folder}")
                else:
                    log_error(f"[CoordinatorAgent] Failed to clean up files from {destination_folder}")
                return success
            return True
        except Exception as e:
            log_error(f"[CoordinatorAgent] Cleanup failed for {destination_folder}: {e}")
            return False
    def _identify_primary_key(self, table_name: str, columns: list) -> str:
        """Heuristic to find the identity column of a table."""
        def normalize(s):
            return str(s).upper().replace("_", "").replace(" ", "").rstrip("S")
        t_norm = normalize(table_name)
        # Common candidates: TABLE_ID or TAB_ID
        candidate = f"{t_norm}ID"
        first_col_id = None
        for idx, col in enumerate(columns):
            if not isinstance(col, dict): continue
            real_name = col.get('name', '')
            c_norm = normalize(real_name)
            
            if c_norm == candidate: return real_name
            if c_norm == "ID": return real_name
            # Fallback to the first column if it looks like an ID
            if idx == 0 and c_norm.endswith("ID"): 
                first_col_id = real_name
        return first_col_id if first_col_id else None
    def _extract_text_formatting(self, zone_node, text_headers):
        """Recursively walk zone_hierarchy to find text formatting for headers."""
        if not isinstance(zone_node, dict):
            return
        
        ft = zone_node.get("formatted_text", {})
        if not isinstance(ft, dict):
            ft = {}
        runs = ft.get("runs", [])
        if isinstance(runs, list) and runs:
            for run in runs:
                if not isinstance(run, dict): continue
                run_text = run.get("text", "").strip()
                attrs = run.get("attributes", {})
                if not run_text:
                    continue
                # Match to existing text_headers by text content
                for th in text_headers:
                    if run_text in th.get("text", "") or th.get("text", "") in run_text:
                        th["font_name"] = attrs.get("fontname", "Arial")
                        th["font_size"] = attrs.get("fontsize", "16")
                        th["font_color"] = attrs.get("fontcolor", "#000000")
                        th["bold"] = attrs.get("bold", "false") == "true"
                        th["text"] = run_text  # Use the exact text from the zone
                        break
        
        for child in zone_node.get("children", []):
            self._extract_text_formatting(child, text_headers)
    def _clean_mapping_names(self, api_data: dict):
        """Recursively cleans BI column/table names in the metadata to remove brackets."""
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
    def _promote_visual_formulas_to_measures(self, api_data: dict, pages: list, field_to_table: dict, field_to_datatype: dict, measure_set: set, default_table: str, display_to_bi_name: dict = None, known_source_columns: set = None, fields_with_dax: set = None):
        """
        Scans visuals for formulas in rows/columns and promotes them to explicit measures.
        Handles various Tableau formats:
           CNTD(Appointment Id)
           AGG(Retention Rate)
           CNTD(PATIENT_ID PATIENTS)
           DISTINCT(CustomerID (Fact_Sales))
        """
        import re
        # Aggregation regex to match FUNC(Inner)
        agg_functions = ["SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "CNTD", "CTD", "CNT", "ATTR", "MEDIAN", "STDEV", "VAR", "DISTINCT", "AGG"]
        agg_regex = re.compile(r'^(' + '|'.join(agg_functions) + r')\s*\((.*)\)$', re.IGNORECASE)
        # Pattern for Col (Table) -> group 1: Col, group 2: Table
        inner_paren_regex = re.compile(r'^(.*?)\s*\((.*?)\)$', re.IGNORECASE)
        
        all_visuals = []
        for page in pages:
            if page.get("is_dashboard"):
                all_visuals.extend(page.get("dashboard_visuals", []))
            else:
                all_visuals.append(page)
        existing_measures = {m.get("name") for m in api_data.get("measures", []) if isinstance(m, dict)}
        new_promoted_measures = {}
        
        # Mapping for display/tableau names to BI names
        bi_map = display_to_bi_name or {}
        for vis in all_visuals:
            if not isinstance(vis, dict): continue
            
            # Check all possible field keys for formulas
            for key in ["rows", "columns", "measures", "marks_text", "marks_color", "marks_detail", "marks_size"]:
                fields = vis.get(key, [])
                if not isinstance(fields, list): continue
                
                for idx, field in enumerate(fields):
                    if not isinstance(field, str): continue
                    
                    cleaned_field = field.strip()
                    match = agg_regex.match(cleaned_field)
                    if not match: continue
                    
                    agg_func = match.group(1).upper()
                    inner = match.group(2).strip()
                    
                    # 1. Handle AGG(Measure) -> Strip AGG as it means field is already aggregated in Tableau
                    if agg_func == "AGG":
                        real_inner = bi_map.get(inner, inner)
                        fields[idx] = real_inner
                        continue
                    # 2. Parse Column and Table from inner
                    col_name = inner
                    table_name = default_table
                    
                    # Try Col (Table) format
                    p_match = inner_paren_regex.match(inner)
                    if p_match:
                        col_name = p_match.group(1).strip()
                        table_name = p_match.group(2).strip()
                    else:
                        # Try space-separated "COL TABLE" format
                        parts = inner.rsplit(None, 1)
                        if len(parts) == 2:
                            maybe_col, maybe_table = parts
                            # Check if the last word matches any known table name
                            if maybe_table in set(field_to_table.values()):
                                col_name = maybe_col.strip()
                                table_name = maybe_table.strip()
                    
                    # 3. Resolve BI names
                    real_col = bi_map.get(col_name, col_name)
                    # Resolve table if it was the default
                    target_table = table_name if table_name != default_table else field_to_table.get(real_col, default_table)
                    
                    # Map Tableau functions to DAX
                    dax_func_map = {
                        "DISTINCT": "DISTINCTCOUNT",
                        "SUM": "SUM",
                        "AVG": "AVERAGE",
                        "MIN": "MIN",
                        "MAX": "MAX",
                        "COUNT": "COUNT",
                        "CNT": "COUNT",
                        "COUNTD": "DISTINCTCOUNT",
                        "CNTD": "DISTINCTCOUNT",
                        "CTD": "DISTINCTCOUNT"
                    }
                    dax_func = dax_func_map.get(agg_func, agg_func)
                    
                    # 4. Final target table sanity check (case-insensitive)
                    valid_table_names = set(field_to_table.values())
                    if target_table not in valid_table_names:
                        for vt in valid_table_names:
                            if vt.lower() == target_table.lower():
                                target_table = vt
                                break
                            elif vt.rstrip('s').lower() == target_table.rstrip('s').lower():
                                target_table = vt
                                break
                    
                    # Derive a descriptive measure name
                    if col_name.lower() in ["ctd", "ctd_id", "count_distinct"] and real_col != col_name:
                         measure_name = f"Total {target_table}"
                    else:
                         measure_name = f"{agg_func} of {col_name}"
                         if target_table and target_table != default_table:
                             measure_name += f" ({target_table})"
                    
                    # Look for custom axis title as a better name
                    axes = vis.get("visual_properties", {}).get("axes", [])
                    target_axis = "Y-Axis (Rows)" if key == "rows" else "X-Axis (Columns)"
                    for axis in axes:
                        if axis.get("target") == target_axis:
                            title_obj = axis.get("axis_title", {})
                            if title_obj.get("custom") and title_obj.get("text") and title_obj.get("text") != "Auto":
                                measure_name = title_obj.get("text")
                                break
                    
                    # Only create measure if the source field actually exists in metadata (prevents phantom measures like 'CNTD of ctd')
                    is_known_col = known_source_columns and (target_table, real_col) in known_source_columns
                    is_known_calc = fields_with_dax and real_col in fields_with_dax
                    
                    if (is_known_col or is_known_calc) and measure_name not in existing_measures and measure_name not in new_promoted_measures:
                        new_promoted_measures[measure_name] = {
                            "name": measure_name,
                            "dax_formula": f"{dax_func}('{target_table}'[{real_col}])",
                            "table": target_table
                        }
                    
                    # Update the visual to use the new measure name
                    fields[idx] = measure_name
                    
                    # Register in field mappings
                    field_to_table[measure_name] = target_table
                    field_to_datatype[measure_name] = "real"
                    measure_set.add(measure_name)
                    
                    log_info(f"[CoordinatorAgent] Promoted formula '{field}' to measure '{measure_name}' on table '{target_table}'")
        if new_promoted_measures:
            if "measures" not in api_data:
                api_data["measures"] = []
            api_data["measures"].extend(new_promoted_measures.values())
            log_info(f"[CoordinatorAgent] Created {len(new_promoted_measures)} new promoted measures from visual formulas.")
    async def select_app_by_name(self, request):
        project_id = getattr(request, 'project_id', None)
        workbook_id = getattr(request, 'workbook_id', None)
        run_id = getattr(request, 'run_id', None)
        token = getattr(request, 'token', None)
        from app.tableau.core.logging_utils import set_logging_context
        if project_id and workbook_id and run_id:
            set_logging_context(project_id, workbook_id, run_id)
        await log_action_to_api("Initiated migration workflow", "Start")
        department_repo = getattr(request, 'department_repo', None) or Config.REPO
        
        target_folder = getattr(request, 'folder_name', None)
        project_id = getattr(request, 'project_id', None)
        workbook_id = getattr(request, 'workbook_id', None)
        run_id = getattr(request, 'run_id', None)
        token = getattr(request, 'token', None)
        
        if not all([target_folder, project_id, workbook_id, run_id]):
            return {"status": "error", "message": "Missing required API parameters. Ensure folder_name, project_id, workbook_id, and run_id are provided."}
        # --- LOG: WORKFLOW STARTED (WITH TOKEN) ---
        await self.action_logger.send_activity_to_api(
            project_id, workbook_id, run_id, 
            technical_message=f"Starting extraction for target folder: {target_folder}",
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
        workspace_name = None
        try:
            async with aiohttp.ClientSession(headers=api_headers) as session:
                base_url = Config.COSMOS_DB_API_URL.rstrip('/')
                api_url = f"{base_url}/api/records/mapping"
                params = {"project_id": project_id, "workbook_id": workbook_id, "run_id": run_id}
                
                async with session.get(api_url, params=params) as resp:
                    if resp.status != 200:
                        # --- LOG: API FETCH ERROR (WITH TOKEN) ---
                        err_msg = f"API request failed: {resp.status}"
                        await self.action_logger.send_error_to_api(
                            project_id, workbook_id, run_id, 
                            "Failed to fetch source metadata", err_msg, 
                            token=token
                        )
                        return {"status": "error", "message": err_msg}
                    
                    data = await resp.json()
                    first_record = data[0]
                    outer_payload = first_record.get("payload", {})
                    
                    raw_project_name = outer_payload.get("workbook_metadata", {}).get("name") or first_record.get("project_name") or outer_payload.get("metadata", {}).get("project_name") or "Unknown_Project"
                    cleaned_name = re.sub(r'[^a-zA-Z0-9_\s]', '', raw_project_name)
                    cleaned_name = cleaned_name.replace(" ", "_")
                    sanitized_name = re.sub(r'_+', '_', cleaned_name).strip("_")
                    
                    timezone_name = "Australia/Sydney"
                    try:
                        settings_url = f"{base_url}/api/records/settings"
                        async with session.get(settings_url) as settings_resp:
                            if settings_resp.status == 200:
                                settings_data = await settings_resp.json()
                                if isinstance(settings_data, list) and settings_data:
                                    settings_data = settings_data[0]
                                if isinstance(settings_data, dict):
                                    settings_obj = settings_data.get("settings", settings_data)
                                    timezone_value = settings_obj.get("timezone") if isinstance(settings_obj, dict) else None
                                    if isinstance(timezone_value, str) and timezone_value.strip():
                                        timezone_name = timezone_value.strip()
                            else:
                                log_warning(f"[CoordinatorAgent] Settings API returned {settings_resp.status}; using default timezone {timezone_name}.")
                    except (httpx.HTTPError, OSError, ValueError, KeyError) as ex:
                        log_warning(f"[CoordinatorAgent] Failed to fetch timezone from settings API: {ex}. Using default timezone {timezone_name}.")

                    now_utc = datetime.datetime.now(datetime.timezone.utc)

                    def _format_timestamp_for_tz(tz_name: str):
                        try:
                            return now_utc.astimezone(ZoneInfo(tz_name)).strftime("%Y%m%d_%H%M%S")
                        except (ZoneInfoNotFoundError, ValueError):
                            pass
                        try:
                            import pytz
                            return now_utc.astimezone(pytz.timezone(tz_name)).strftime("%Y%m%d_%H%M%S")
                        except (ImportError, AttributeError, KeyError, ValueError):
                            return None

                    timestamp = _format_timestamp_for_tz(timezone_name)
                    if not timestamp and timezone_name != "Australia/Sydney":
                        log_warning(f"[CoordinatorAgent] Invalid/unavailable timezone '{timezone_name}' from settings API. Falling back to Australia/Sydney.")
                        timestamp = _format_timestamp_for_tz("Australia/Sydney")

                    if not timestamp:
                        log_warning("[CoordinatorAgent] Unable to resolve Australia/Sydney timezone. Falling back to UTC timestamp.")
                        timestamp = now_utc.strftime("%Y%m%d_%H%M%S")

                    new_app_name = f"{sanitized_name}_{timestamp}"
                    
                    destination_folder = f"pbip-deploy-tmdl/{target_folder}/{new_app_name}"
                    inner_payload = outer_payload.get("payload")
                    api_data = inner_payload if inner_payload else outer_payload
                    self._clean_mapping_names(api_data)
                    
                    st = first_record
                    data_layer_triggered_flag = getattr(request, 'data_layer_triggered', False)
                    is_direct_lake = False
                    lakehouse_id = None
                    
                    # Fetch data_layer_results for extract mode (lakehouse info)
                    try:
                        dl_url = f"{base_url}/api/records/data-layer"
                        dl_params = {"project_id": project_id, "workbook_id": workbook_id, "run_id": run_id}
                        async with session.get(dl_url, params=dl_params) as dl_resp:
                            if dl_resp.status == 200:
                                dl_data = await dl_resp.json()
                                if isinstance(dl_data, list) and dl_data:
                                    dl_record = dl_data[0]
                                    api_data["data_layer_results"] = dl_record
                                    log_info(f"[CoordinatorAgent] Injected data_layer_results into api_data")
                                    
                                    # Extract Lakehouse metadata if present
                                    try:
                                        dl_payload = dl_record.get('payload', {})
                                        summary = dl_payload.get('processing_summary', [])
                                        if summary:
                                            lh_info = summary[0].get('fabric_artifacts', {}).get('lakehouse', {})
                                            lakehouse_id = lh_info.get('id')
                                            lakehouse_name = lh_info.get('name')
                                            lakehouse_schema = summary[0].get('lakehouse_schema')
                                            log_info(f"[CoordinatorAgent] Resolved DirectLake metadata: {lakehouse_id=}, {lakehouse_name=}, {lakehouse_schema=}")
                                    except Exception as e:
                                        log_warning(f"[CoordinatorAgent] Error extracting lakehouse metadata: {e}")

                                    if data_layer_triggered_flag:
                                        log_info(f"[CoordinatorAgent] Direct Lake (triggered) detected. Forcing DirectLake mode.")
                                        is_direct_lake = True

                                        # --- [NEW] Inject parameter tables as DirectLake entities ---
                                        params_as_tables = []
                                        if isinstance(summary, list) and summary:
                                            params_as_tables = summary[0].get("parameters_as_tables", [])
                                        if params_as_tables:
                                            original_params = api_data.get("parameters", [])
                                            log_info(f"[CoordinatorAgent] Found {len(params_as_tables)} parameter tables to inject: {params_as_tables}")

                                            if not isinstance(api_data.get("relationships"), list):
                                                api_data["relationships"] = []
                                            if not isinstance(api_data.get("measures"), list):
                                                api_data["measures"] = []

                                            for param in (original_params or []):
                                                if not isinstance(param, dict): continue
                                                p_name = param.get("name", "")
                                                if p_name not in params_as_tables: continue

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

                                                param_table_def = {
                                                    "table_name": p_name,
                                                    "columns": [{"name": ref_column, "datatype": data_type}],
                                                    "is_parameter_table": True
                                                }
                                                if not isinstance(api_data.get("tables"), list):
                                                    api_data["tables"] = []
                                                api_data["tables"].append(param_table_def)
                                                
                                                # Store the ref_column in the parameter object for slicer builder
                                                param["_directlake_column"] = ref_column
                                                param["_directlake_table"] = p_name
                                                
                                                log_info(f"[CoordinatorAgent] Injected parameter '{p_name}' as DirectLake table with column '{ref_column}' ({data_type})")

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
                                                log_info(f"[CoordinatorAgent] Added SELECTEDVALUE measure '{measure_name}' for parameter '{p_name}'")
                                elif isinstance(dl_data, dict):
                                    api_data["data_layer_results"] = dl_data
                                    log_info(f"[CoordinatorAgent] Injected data_layer_results into api_data")
                            else:
                                log_info(f"[CoordinatorAgent] No data_layer_results found (status {dl_resp.status}) - skipping lakehouse integration")
                    except Exception as dl_err:
                        log_info(f"[CoordinatorAgent] Could not fetch data_layer_results: {dl_err} - continuing without lakehouse info")
                    
                    log_info(f"[CoordinatorAgent] outer_payload keys: {list(outer_payload.keys()) if isinstance(outer_payload, dict) else type(outer_payload)}")
                    log_info(f"[CoordinatorAgent] api_data keys: {list(api_data.keys()) if isinstance(api_data, dict) else type(api_data)}")
                    log_info(f"[CoordinatorAgent] api_data has {len(api_data.get('tables', []))} tables, {len(api_data.get('custom_sql', []))} custom_sql")
                    
                    field_to_table, field_to_datatype = {}, {}
                    display_to_bi_name = {}  # Maps display names (e.g. "Customer Name") -> BI column names (e.g. "CustomerName")
                    known_source_columns = set() # Set of (table_name, column_name) to track valid source fields
                    default_table = "UnknownTable"
                    fields_with_dax = set()
                    calculated_fields_metadata = [] # NEW: Collection for final response
                    # --- 1. Physical tables ---
                    all_tables = api_data.get("tables", [])
                    for table in all_tables:
                        if isinstance(table, list): continue
                        # Normalize new API schema keys
                        if 'bi_table_name' in table and not table.get('table_name'):
                            table['table_name'] = table['bi_table_name']
                        t_name = table.get("table_name", "Unknown")
                        if default_table == "UnknownTable":
                            default_table = t_name
                        for col in table.get("columns", []):
                            # Normalize column keys
                            if isinstance(col, dict):
                                if 'bi_column_name' in col and not col.get('name'):
                                    col['name'] = col['bi_column_name']
                                if 'bi_datatype' in col and not col.get('datatype'):
                                    col['datatype'] = col['bi_datatype']
                                if not col.get('datatype') and col.get('tableau_datatype'):
                                    col['datatype'] = col['tableau_datatype']
                            col_name = col.get("name", "")
                            if not col_name: continue
                            
                            dax = None
                            if isinstance(col, dict):
                                dax = col.get("dax_formula") or col.get("powerbi_formula") or col.get("powerbi", {}).get("dax")
                            if dax:
                                fields_with_dax.add(col_name)
                                
                            field_to_table[col_name] = t_name
                            field_to_datatype[col_name] = col.get("datatype", "string")
                            known_source_columns.add((t_name, col_name))
                            # Also register space-separated alias (e.g. "StockItemName" -> "Stock Item Name")
                            alias = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', col_name)
                            if alias != col_name:
                                field_to_table[alias] = t_name
                                field_to_datatype[alias] = col.get("datatype", "string")
                                display_to_bi_name[alias] = col_name  # "Customer Name" -> "CustomerName"
                            # Also register the Tableau column name if different from BI column name
                            tab_col = col.get('tableau_column_name', '')
                            if tab_col and tab_col != col_name and tab_col not in field_to_table:
                                field_to_table[tab_col] = t_name
                                field_to_datatype[tab_col] = col.get("datatype", "string")
                                display_to_bi_name[tab_col] = col_name
                        # --- NEW: Identify PK and map CTD/ctd to it ---
                        pk = self._identify_primary_key(t_name, table.get("columns", []))
                        if pk:
                            # Map literal "CTD" and "ctd" to the primary key column
                            for shorthand in ["CTD", "ctd"]:
                                if shorthand not in field_to_table:
                                    field_to_table[shorthand] = t_name
                                    field_to_datatype[shorthand] = field_to_datatype.get(pk, "string")
                                    display_to_bi_name[shorthand] = pk
                                    log_info(f"[CoordinatorAgent] Mapped shorthand '{shorthand}' to primary key '{pk}' for table '{t_name}'")
                    for cs in api_data.get("custom_sql", []):
                        if not isinstance(cs, dict): continue
                        if 'bi_table_name' in cs and not cs.get('table_name'):
                            cs['table_name'] = cs['bi_table_name']
                        t_name = cs.get("table_name") or cs.get("name") or "Unknown"
                        if default_table == "UnknownTable":
                            default_table = t_name
                            
                        for col in cs.get("columns", []):
                            if not isinstance(col, dict): continue
                            if 'bi_column_name' in col and not col.get('name'):
                                col['name'] = col['bi_column_name']
                            if 'bi_datatype' in col and not col.get('datatype'):
                                col['datatype'] = col['bi_datatype']
                            
                            col_name = col.get("name", "")
                            if not col_name: continue
                            
                            dax = col.get("dax_formula") or col.get("powerbi_formula") or col.get("powerbi", {}).get("dax")
                            if dax:
                                fields_with_dax.add(col_name)
                                
                            field_to_table[col_name] = t_name
                            field_to_datatype[col_name] = col.get("datatype", "string")
                            known_source_columns.add((t_name, col_name))
                            
                            # Also register aliases and tableau names
                            alias = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', col_name)
                            if alias != col_name:
                                field_to_table[alias] = t_name
                                field_to_datatype[alias] = col.get("datatype", "string")
                                display_to_bi_name[alias] = col_name
                            
                            tab_col = col.get('tableau_column_name', '')
                            if tab_col and tab_col != col_name and tab_col not in field_to_table:
                                field_to_table[tab_col] = t_name
                                field_to_datatype[tab_col] = col.get("datatype", "string")
                                display_to_bi_name[tab_col] = col_name
                        
                        # Identify PK for mapping
                        pk = self._identify_primary_key(t_name, cs.get("columns", []))
                        if pk:
                             for shorthand in ["CTD", "ctd"]:
                                 if shorthand not in field_to_table:
                                     field_to_table[shorthand] = t_name
                                     display_to_bi_name[shorthand] = pk
                    # --- 3. Measures from root "measures" list ---
                    # Parse DAX formula for 'TableName'[Column] references to map to correct table
                    raw_measures = api_data.get("measures", [])
                    if not isinstance(raw_measures, list): raw_measures = []
                    measure_set = set()
                    
                    # Build a set of known table names for lookup
                    known_table_names = set()
                    for table in all_tables:
                        if isinstance(table, dict) and table.get("table_name"):
                            known_table_names.add(table["table_name"])
                    for cs in api_data.get("custom_sql", []):
                        cs_name = cs.get("table_name") or cs.get("name")
                        if cs_name:
                            known_table_names.add(cs_name)
                    # Add parameter names as known table names
                    for p in api_data.get("parameters", []):
                        if isinstance(p, dict) and p.get("name"):
                            known_table_names.add(p.get("name"))
                    
                    for m in raw_measures:
                        if not isinstance(m, dict): continue
                        m_name = m.get("name", "")
                        if not m_name: continue
                        measure_set.add(m_name)
                        fields_with_dax.add(m_name)
                        # Prioritize explicit 'table' or 'table_name' property from metadata
                        target = m.get('table') or m.get('table_name')
                        
                        if not target or target not in known_table_names:
                            # Fallback: Parse 'TableName'[Column] references from DAX
                            dax = m.get('dax_formula') or m.get('powerbi_formula') or ''
                            table_refs = re.findall(r"'([^']+)'\[", dax)
                            target = default_table
                            for ref in table_refs:
                                if ref in known_table_names:
                                    target = ref
                                    break
                        
                        if m_name not in field_to_table:
                            field_to_table[m_name] = target
                            field_to_datatype[m_name] = "real"
                    # --- 4. Calculated fields from "Calculated Fields & LODs" section ---
                    calc_lods = api_data.get("Calculated Fields & LODs", [])
                    if isinstance(calc_lods, list):
                        for block in calc_lods:
                            for cf in block.get("calculated_fields", []):
                                cf_name = cf.get("name", "")
                                cf_table = cf.get("table") or cf.get("table_name") or default_table
                                cf_type = str(cf.get("type", "")).strip().lower()
                                
                                # Export metadata for response
                                calculated_fields_metadata.append({
                                    "name": cf_name,
                                    "table": cf_table,
                                    "type": cf_type,
                                    "dax_formula": cf.get("dax_formula") or cf.get("powerbi_formula")
                                })

                                if cf_name: fields_with_dax.add(cf_name)
                                if cf_name and cf_name not in field_to_table:
                                    field_to_table[cf_name] = cf_table
                                    field_to_datatype[cf_name] = "string"
                                    if cf_type == "measure":
                                        field_to_datatype[cf_name] = "real"
                                        measure_set.add(cf_name)
                                    else:
                                        field_to_datatype[cf_name] = "string"
                            for lod in block.get("lod_expressions", []):
                                lod_name = lod.get("name", "")
                                lod_table = lod.get("table") or lod.get("table_name") or default_table
                                
                                # Export metadata for response
                                calculated_fields_metadata.append({
                                    "name": lod_name,
                                    "table": lod_table,
                                    "type": "lod",
                                    "dax_formula": lod.get("dax_formula") or lod.get("powerbi_formula")
                                })

                                if lod_name: fields_with_dax.add(lod_name)
                                if lod_name and lod_name not in field_to_table:
                                    field_to_table[lod_name] = lod_table
                                    lod_type = str(lod.get("type", "")).strip().lower()
                                    if lod_type == "dimension":
                                        field_to_datatype[lod_name] = "string"
                                    else:
                                        field_to_datatype[lod_name] = "real"
                                        measure_set.add(lod_name)
                    # --- 4b. Sets (Dynamic sets -> Calculation Groups) ---
                    all_sets = api_data.get('sets', [])
                    if isinstance(all_sets, list):
                        for s in all_sets:
                            if not isinstance(s, dict): continue
                            s_name = s.get("name")
                            if not s_name: continue
                            
                            s_type = s.get("type", "").lower()
                            if s_type == "dynamic":
                                # Calculation Group mapping
                                cg_name = s.get("powerbi", {}).get("name")
                                if not cg_name:
                                    cg_name = f"Calculation Group {s_name}"
                                
                                field_to_table[s_name] = cg_name
                                field_to_datatype[s_name] = "string"
                                display_to_bi_name[s_name] = cg_name
                                log_info(f"[CoordinatorAgent] Registered dynamic set '{s_name}' as PBI calculation group '{cg_name}'")
                            else:
                                s_table = s.get("powerbi", {}).get("target_table") or default_table
                                if s_name not in field_to_table:
                                    field_to_table[s_name] = s_table
                                    field_to_datatype[s_name] = "string"
                    # --- 5. Pre-scan ALL sheet visuals and register unknown fields ---
                    # Fields like Revenue, Customer Avg Order Value, Brand Clean, etc.
                    # may only exist as references in visuals (not in tables/measures/calc fields).
                    # Register them so visuals are never silently dropped.
                    pre_scan_visuals = api_data.get("visuals", {})
                    pre_scan_sheets = pre_scan_visuals.get("sheet_visuals", []) if isinstance(pre_scan_visuals, dict) else []
                    if isinstance(pre_scan_sheets, dict):
                        pre_scan_sheets = pre_scan_sheets.get("sheets", [])
                    
                    for sv in pre_scan_sheets:
                        if not isinstance(sv, dict):
                            continue
                        # Collect all field references from the visual
                        field_arrays = []
                        for key in ("rows", "columns", "marks_text", "marks_color", "marks_detail", "marks_size"):
                            arr = sv.get(key, [])
                            if isinstance(arr, list):
                                field_arrays.extend(arr)
                        
                        for raw_field in field_arrays:
                            if not isinstance(raw_field, str) or not raw_field.strip():
                                continue
                            cleaned = raw_field.strip()
                            if cleaned.lower() in ("none", "", "null", "n/a"):
                                continue
                            # Strip Tableau AGG wrappers like SUM(...), ATTR(...), MONTH(...), YEAR(...)
                            import re as _re
                            agg_match = _re.match(r'^[A-Z0-9_]+\((.*)\)$', cleaned, _re.IGNORECASE)
                            if agg_match:
                                cleaned = agg_match.group(1).strip()
                            if not cleaned:
                                continue
                            
                            # Register field if not already known
                            if cleaned not in field_to_table:
                                field_to_table[cleaned] = default_table
                                field_to_datatype[cleaned] = "real"
                                log_info(f"[CoordinatorAgent] Auto-registered visual field '{cleaned}' as column on '{default_table}'")
                            
                            # Also register the space-separated alias
                            alias = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', cleaned)
                            if alias != cleaned and alias not in field_to_table:
                                field_to_table[alias] = field_to_table[cleaned]
                                field_to_datatype[alias] = field_to_datatype[cleaned]
                                display_to_bi_name[alias] = cleaned
                    
                    log_info(f"[CoordinatorAgent] After pre-scan: {len(field_to_table)} fields, {len(measure_set)} measures registered")
                    # --- Build sheet visuals index for dashboard lookup ---
                    visuals_data = api_data.get("visuals", {})
                    raw_sheets = visuals_data.get("sheet_visuals", []) or api_data.get("sheets_visuals", [])
                    sheets = raw_sheets if isinstance(raw_sheets, list) else raw_sheets.get("sheets", [])
                    
                    # Index sheets by name for dashboard layout reference
                    sheet_by_name = {}
                    normalized_sheet_by_name = {}
                    def normalize_name(n):
                        if not isinstance(n, str): return ""
                        return n.lower().replace(" ", "").replace("-", "").replace("–", "").replace("—", "")

                    for sheet in sheets:
                        s_name = sheet.get("name", "")
                        sheet_by_name[s_name] = sheet
                        normalized_sheet_by_name[normalize_name(s_name)] = sheet
                    # --- Build Sheets Formatting Index (for Tooltips, Axes, etc) ---
                    formatting_and_styling = api_data.get("formatting_and_styling", {})
                    sheets_formatting = formatting_and_styling.get("sheets_formatting", []) if isinstance(formatting_and_styling, dict) else []
                    formatting_by_sheet = {fmt.get("sheet_name", ""): fmt for fmt in sheets_formatting if isinstance(fmt, dict)}
                    
                    # --- Process Dashboards as pages (if present) ---
                    dashboards = visuals_data.get("dashboards", []) if isinstance(visuals_data, dict) else []
                    
                    pages, page_order = [], []
                    page_idx = 0
                    
                    if dashboards:
                        # Each dashboard becomes a page with multiple visuals
                        for dash in dashboards:
                            page_idx += 1
                            page_id = f"Page{page_idx}"
                            page_order.append(page_id)
                            
                            dash_name = dash.get("dashboard_name", f"Dashboard {page_idx}")
                            dash_objects = dash.get("dashboard_objects", [])
                            
                            # Collect all sheet visuals referenced in this dashboard
                            dashboard_visuals = []
                            for obj_name in dash_objects:
                                sv = sheet_by_name.get(obj_name) or normalized_sheet_by_name.get(normalize_name(obj_name))
                                if sv:
                                    # Preserve full filter dicts (keep tableau_type for howCreated logic)
                                    clean_filters = []
                                    for f_item in sv.get("filters", []):
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
                                    
                            dashboard_visuals.append({
                                "display_name": sv.get("name", obj_name),
                                "mark_type": sv.get("mark_type", "Automatic"),
                                "power_bi_visual_type": sv.get("power_bi_visual_type", ""),
                                "rows": sv.get("rows", []),
                                "columns": sv.get("columns", []),
                                "marks_text": sv.get("marks_text", []),
                                "marks_color": sv.get("marks_color", []),
                                "marks_detail": sv.get("marks_detail", []),
                                "marks_size": sv.get("marks_size", []),
                                "measures": sv.get("measures", []),
                                "filters": clean_filters,
                                "slicers": sv.get("slicers", []),
                                "sets": sv.get("sets", []),
                                "parameters": sv.get("parameters", []),
                                "visual_properties": sv.get("visual_properties", {}),
                                "legend_position": sv.get("legend_position", ""),
                                "tooltip_formatting": formatting_by_sheet.get(obj_name, {}).get("tooltip_formatting", [])
                            })
                            
                            # Extract text headers from dashboard_objects (those not in sheet_by_name and starting with "Text Header")
                            text_headers = []
                            for obj_name in dash_objects:
                                if obj_name.startswith("Text Header") and obj_name not in sheet_by_name:
                                    # Extract display text from zone_hierarchy
                                    header_text = obj_name.replace("Text Header (", "").rstrip(")")
                                    text_headers.append({"text": header_text})
                            
                            # Try to extract font info from zone_hierarchy
                            zone_hierarchy = dash.get("zone_hierarchy", {})
                            main_zones = zone_hierarchy.get("Main", [])
                            for zone in main_zones:
                                self._extract_text_formatting(zone, text_headers)
                            
                            # Extract buttons
                            buttons_data = dash.get("buttons", [])
                            buttons = []
                            if isinstance(buttons_data, list):
                                for btn in buttons_data:
                                    if isinstance(btn, dict) and btn.get("navigate_to"):
                                        buttons.append({
                                            "navigate_to": btn.get("navigate_to", ""),
                                            "button_style": btn.get("button_style", "text"),
                                            "image_name": btn.get("image_name", ""),
                                            "button_text": btn.get("button_text", "Navigate")
                                        })
                            
                            # Extract drill-throughs 
                            drill_throughs = dash.get("drill_throughs", [])
                            clean_drills = []
                            if isinstance(drill_throughs, list):
                                for dt in drill_throughs:
                                    if isinstance(dt, dict) and dt.get("drill_name"):
                                        clean_drills.append(dt)
                            
                            # Extract bookmarks
                            bookmarks = dash.get("bookmarks", [])
                            
                            pages.append({
                                "page_id": page_id,
                                "display_name": dash_name,
                                "page_json": {"displayOption": "FitToPage", "height": 720, "width": 1280},
                                "is_dashboard": True,
                                "dashboard_visuals": dashboard_visuals,
                                "text_headers": text_headers,
                                "buttons": buttons,
                                "drill_throughs": clean_drills,
                                "bookmarks": bookmarks,
                                "filters": []
                            })
                    
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
                                "tooltip_formatting": formatting_by_sheet.get(sheet.get("name", ""), {}).get("tooltip_formatting", []),
                                "visuals": []
                            })
                    
                    # --- Process Stories as pages ---
                    # SKIPPED: As per user request, stories should not create visuals or sheets.
                    pass
                    
                    # --- NEW LOGIC: Promote Visual Formulas to Measures ---
                    self._promote_visual_formulas_to_measures(
                        api_data, pages, field_to_table, field_to_datatype, measure_set, default_table, display_to_bi_name, known_source_columns, fields_with_dax
                    )
                    # --- NEW LOGIC: Calculation Group Dynamic Measures ---
                    # If calculation groups exist, Power BI disables implicit measures. We must convert simple agg columns to explicit measures.
                    calc_item_sets = [s for s in api_data.get('sets', []) if isinstance(s, dict) and s.get('type', '').lower() == 'dynamic']
                    if calc_item_sets:
                        log_info(f"[CoordinatorAgent] Calculation group detected. Scanning visuals to convert simple aggregated columns to explicit measures.")
                        all_visuals = []
                        for page in pages:
                            all_visuals.extend(page.get("dashboard_visuals", []))
                            if not page.get("is_dashboard"):
                                all_visuals.append(page)
                        
                        raw_visuals = api_data.get("visuals", {})
                        if isinstance(raw_visuals, dict):
                            raw_sheets = raw_visuals.get("sheet_visuals", []) or api_data.get("sheets_visuals", [])
                            if isinstance(raw_sheets, dict):
                                raw_sheets = raw_sheets.get("sheets", [])
                            for rs in raw_sheets:
                                if isinstance(rs, dict):
                                    all_visuals.append(rs)
                        agg_functions = ["SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "CNTD", "CTD"]
                        agg_regex = re.compile(r'^(' + '|'.join(agg_functions) + r')\((.*)\)$', re.IGNORECASE)
                        existing_measures = {m.get("name") for m in api_data.get("measures", []) if isinstance(m, dict)}
                        new_explicit_measures = {}
                        for vis in all_visuals:
                            if not isinstance(vis, dict): continue
                            for key in ["rows", "columns", "measures", "marks_text", "marks_color", "marks_detail", "marks_size"]:
                                fields = vis.get(key, [])
                                if not isinstance(fields, list): continue
                                for idx, field in enumerate(fields):
                                    if not isinstance(field, str): continue
                                    match = agg_regex.match(field.strip())
                                    if match:
                                        agg_func = match.group(1).upper()
                                        col_name = match.group(2).strip()
                                        
                                        if col_name in fields_with_dax:
                                            continue
                                            
                                        dax_func_map = {"AVG": "AVERAGE", "COUNTD": "DISTINCTCOUNT"}
                                        dax_func = dax_func_map.get(agg_func, agg_func)
                                        
                                        t_name = field_to_table.get(col_name, default_table)
                                        real_col = display_to_bi_name.get(col_name, col_name)
                                        
                                        measure_name = f"{agg_func} of {col_name}"
                                        
                                        # Only create measure if the source field actually exists in metadata
                                        is_known_col = (t_name, real_col) in known_source_columns
                                        is_known_calc = real_col in fields_with_dax
                                        
                                        if (is_known_col or is_known_calc) and measure_name not in existing_measures and measure_name not in new_explicit_measures:
                                            new_explicit_measures[measure_name] = {
                                                "name": measure_name,
                                                "dax_formula": f"{dax_func}('{t_name}'[{real_col}])",
                                                "table": t_name
                                            }
                                        
                                        fields[idx] = measure_name
                        
                        if new_explicit_measures:
                            if "measures" not in api_data:
                                api_data["measures"] = []
                            api_data["measures"].extend(new_explicit_measures.values())
                            log_info(f"[CoordinatorAgent] Created {len(new_explicit_measures)} new explicit measures for Calculation Group support.")
                            
                            for m_name, m_dict in new_explicit_measures.items():
                                measure_set.add(m_name)
                                field_to_table[m_name] = m_dict["table"]
                                field_to_datatype[m_name] = "real"
                    # ----------------------------------------------------------------
                    
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
                        "measure_names": measure_names,
                        "default_table": default_table,
                        "display_to_bi_name": display_to_bi_name,
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
            return {"status": "error", "message": str(e)}
        # 1. Clean / Prepare Destination
        await self.clean_destination_folder(destination_folder, repo=department_repo)
        
        # --- LOG: CREATED FOLDER (WITH TOKEN) ---
        await self.action_logger.send_activity_to_api(
            project_id, workbook_id, run_id, 
            technical_message=f"Created {destination_folder} folder", # <-- Updated here
            token=token
        )
        # Set up memory interceptor
        memory_file_agent = MemoryFileAgent(self.report_generator.file_agent)
        original_rg_agent = self.report_generator.file_agent
        original_tg_agent = self.tmdl_generator.file_agent
        
        self.report_generator.file_agent = memory_file_agent
        self.tmdl_generator.file_agent = memory_file_agent
        try:
            # 2. Generate Reports & Visuals
            success = await self.report_generator.generate_and_push_report(
                destination_folder, new_app_name, report_data=report_data
            )
            if not success:
                err_msg = "Failed to generate static project files"
                # --- CLEANUP: Delete partially-created folder ---
                cleanup_ok = await self._cleanup_folder(destination_folder, repo=department_repo)
                cleanup_note = " Partial folder cleaned up." if cleanup_ok else " WARNING: Partial folder cleanup failed."
                # --- LOG: REPORT GENERATION ERROR (WITH TOKEN) ---
                await self.action_logger.send_error_to_api(
                    project_id, workbook_id, run_id, 
                    err_msg + cleanup_note, err_msg, 
                    token=token
                )
                return {"status": "error", "message": err_msg + cleanup_note}
                
            # --- LOG: REPORT GENERATED (WITH TOKEN) ---
            await self.action_logger.send_activity_to_api(
                project_id, workbook_id, run_id, 
                technical_message="Successfully generated Report files",
                token=token
            )
            # 3. Generate TMDL Models
            try:
                semantic_folder = f"{destination_folder}/{new_app_name}.SemanticModel"
                tmdl_result = await self.tmdl_generator.generate_tmdl_structure(
                    api_data, semantic_folder, new_app_name, is_direct_lake=is_direct_lake
                )
                if not tmdl_result.get("success"):
                    failed = tmdl_result.get('failed_files', [])
                    err_msg = f"TMDL generation failed: {tmdl_result.get('message')}. Failed files: {failed}"
                    # --- CLEANUP: Delete partially-created folder ---
                    cleanup_ok = await self._cleanup_folder(destination_folder, repo=department_repo)
                    cleanup_note = " Partial folder cleaned up." if cleanup_ok else " WARNING: Partial folder cleanup failed."
                    await self.action_logger.send_error_to_api(
                        project_id, workbook_id, run_id, 
                        err_msg + cleanup_note, tmdl_result.get('message'), 
                        token=token
                    )
                    return {"status": "error", "message": err_msg + cleanup_note}
                if report_data and report_data.get("parameters") and not is_direct_lake:
                    param_result = await self.tmdl_generator.add_parameters(report_data["parameters"], semantic_folder, api_data)
                    if param_result and not param_result.get("success"):
                        failed = param_result.get('failed_files', [])
                        err_msg = f"Parameter generation failed: {param_result.get('message')}. Failed files: {failed}"
                        # --- CLEANUP: Delete partially-created folder ---
                        cleanup_ok = await self._cleanup_folder(destination_folder, repo=department_repo)
                        cleanup_note = " Partial folder cleaned up." if cleanup_ok else " WARNING: Partial folder cleanup failed."
                        await self.action_logger.send_error_to_api(
                            project_id, workbook_id, run_id, 
                            err_msg + cleanup_note, param_result.get('message'), 
                            token=token
                        )
                        return {"status": "error", "message": err_msg + cleanup_note}
                
                # --- LOG: TMDL GENERATED (WITH TOKEN) ---
                await self.action_logger.send_activity_to_api(
                    project_id, workbook_id, run_id, 
                    technical_message="Successfully generated Semantic Model (TMDL)",
                    token=token
                )
            except Exception as e:
                # --- CLEANUP: Delete partially-created folder on exception ---
                cleanup_ok = await self._cleanup_folder(destination_folder, repo=department_repo)
                cleanup_note = " Partial folder cleaned up." if cleanup_ok else " WARNING: Partial folder cleanup failed."
                # --- LOG: TMDL ERROR (WITH TOKEN) ---
                await self.action_logger.send_error_to_api(
                    project_id, workbook_id, run_id, 
                    f"Error generating TMDL Semantic Model.{cleanup_note}", str(e), 
                    token=token
                )
                return {"status": "error", "message": f"TMDL generation failed: {e}.{cleanup_note}"}
        finally:
            self.report_generator.file_agent = original_rg_agent
            self.tmdl_generator.file_agent = original_tg_agent
        await log_action_to_api("Migration completed", new_app_name)
        
        # --- LOG: WORKFLOW COMPLETED (WITH TOKEN) ---
        await self.action_logger.send_activity_to_api(
            project_id, workbook_id, run_id, 
            technical_message=f"Workflow completed successfully for {new_app_name}",
            token=token
        )
        
        # --- PREPARE FINAL RESPONSE EARLY (TO STORE IN COSMOS DB) ---
        # Count visuals and parameters
        visuals_count = 0
        parameters_count = 0
        # Count visuals and parameters
        # NOTE: visuals_count/parameters_count are NOT re-initialized here to 0,
        # because they were already pre-initialized above and may have been set inside the try block.
        if api_data:
            parameters_count = len(api_data.get("parameters", []))
            visuals_data = api_data.get("visuals", {})
            raw_sheets = visuals_data.get("sheet_visuals", []) or api_data.get("sheets_visuals", [])
            visuals_count = len(raw_sheets if isinstance(raw_sheets, list) else raw_sheets.get("sheets", []))
            
        # Build the Azure DevOps / Fabric repo URL for the generated folder
         # Build the Azure DevOps / Fabric repo URL for the generated folder
        group_id = getattr(request, 'group_id', None)
        fabric_repo_url = f"https://app.fabric.microsoft.com/groups/{group_id}/list?experience=fabric-developer"

        # Construct project metadata based on deployment type
        project_metadata = {
            "name": new_app_name,
            "deployment_type": getattr(request, 'deployment_type', "Azure Devops")
        }

        # Normalize deployment_type for robust comparison
        dtype_lower = str(project_metadata["deployment_type"]).lower().strip()

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
            project_metadata.update({
                "destination_path": None,
                "repository": getattr(request, 'git_repo', None),
                "branch": Config.get_branch(),
                "fabric_url": None,
                "github_url": None # Placeholder for GitAgent URL logic if needed
            })
        else: # Default to Azure DevOps logic
            project_metadata.update({
                "destination_path": destination_folder,
                "repository": department_repo,
                "branch": Config.get_branch(),
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
        try:
            # Generate the true PBIP folder JSON payload dynamically from intercepted generated files
            folder_tree = memory_file_agent.build_file_tree(destination_folder)
            
            # The tree contains "{new_app_name}.SemanticModel" and "{new_app_name}.Report" and "readme"
            semantic_model_data = folder_tree.get(f"{new_app_name}.SemanticModel", {})
            report_data_out = folder_tree.get(f"{new_app_name}.Report", {})
            
            project_name = raw_project_name or project_id
            
            payload = {
                "id": run_id,
                "run_id": run_id,
                "project_id": project_id,
                "workbook_id": workbook_id,
                "project_name": project_name,
                "fabric_url": fabric_repo_url, # <--- Added Fabric URL here
                "folder_name": target_folder,
                "group_id": getattr(request, 'group_id', None),
                "lakehouse_id": lakehouse_id,
                "data_layer_triggered": data_layer_triggered_flag,
                "step": "generation",
                "status": "completed",
                "payload": {
                    "new_app_name": new_app_name,
                    "destination_folder": destination_folder,
                    "semantic_model": semantic_model_data,
                    "report": report_data_out,
                    "calculated_fields": calculated_fields_metadata,
                    "final_response": final_response_data # <--- Added Postman output here
                }
            }
            
            post_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            if token:
                post_headers["Authorization"] = f"Bearer {token}"
            
            base_url = Config.COSMOS_DB_API_URL.rstrip('/')
            target_url = f"{base_url}/api/records/generation"
            log_info(f"[CoordinatorAgent] Sending comprehensive JSON metadata to {target_url}")
            
            import time
            start_time = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    metadata_resp = await client.post(target_url, json=payload, headers=post_headers)
                    metadata_resp.raise_for_status()
                
                duration = (time.perf_counter() - start_time) * 1000
                log_info(f"[CoordinatorAgent] Successfully sent JSON metadata to Cosmos DB | Time: {duration:.2f}ms")
            except httpx.HTTPStatusError as e:
                error_body = e.response.text
                log_error(f"[CoordinatorAgent] API returned error: {e.response.status_code} - {error_body}")
            except Exception as e:
                log_error(f"[CoordinatorAgent] Failed to connect to Cosmos DB API: {str(e)}")
        except Exception as e:
            log_error(f"[CoordinatorAgent] Error preparing/sending comprehensive metadata to Cosmos DB: {e}")
        
        return final_response_data