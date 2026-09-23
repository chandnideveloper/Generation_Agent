import uuid
import json
import re
from typing import Dict, List, Optional, Set, Tuple
from app.tableau.core.logging_utils import log_info, log_error, log_warning
from app.tableau.utils.path_utils import indent_block
from app.tableau.services.action_logger import ActionLogger
from .tmdl_helpers import TmdlHelperMixin

class TmdlGenerator(TmdlHelperMixin):
    async def add_parameters(self, parameters: list, semantic_folder: str, api_data: dict = None):
        if not parameters: return {"success": True, "message": "No parameters to add"}

        all_calcs = []
        if api_data:
            calc_lods = api_data.get("Calculated Fields & LODs", [])
            if isinstance(calc_lods, list):
                for block in calc_lods:
                    if isinstance(block, dict):
                        all_calcs.extend(block.get("calculated_fields", []))
            elif isinstance(calc_lods, dict):
                all_calcs.extend(calc_lods.get("calculated_fields", []))

            if not all_calcs:
                all_calcs.extend(api_data.get("calculated_fields_lods", []))

        # Build a set of parameter table names for matching
        param_table_names = {}  # maps table_name -> param dict
        for p in parameters:
            p_pbi = p.get("powerbi", {})
            p_raw_dax = p_pbi.get("dax") or p.get("dax") or ""
            p_clean = re.sub(r'```[a-zA-Z]*', '', str(p_raw_dax)).replace('```', '').strip()
            while p_clean.startswith('='): p_clean = p_clean[1:].strip()

            p_name = p.get("name", "Parameter")
            if "=" in p_clean and not p_clean.lstrip().upper().startswith("VAR "):
                parts = p_clean.split('=', 1)
                left = parts[0].strip()
                if '\n' not in left and len(left) < 100:
                    p_name = left

            p_name = self._sanitize_name(p_name)
            param_table_names[p_name] = p

        log_info(f"[add_parameters] Parameter table names: {list(param_table_names.keys())}")
        log_info(f"[add_parameters] Collected {len(all_calcs)} calculated fields for parameter matching")

        # Build map: param_table_name -> list of matching calc fields
        param_measures_map: Dict[str, List] = {ptn: [] for ptn in param_table_names}

        for cf in all_calcs:
            if not isinstance(cf, dict): continue
            cf_dax = cf.get("dax_formula") or cf.get("powerbi_formula") or ""
            cf_table = str(cf.get("table_name") or "").strip()
            cf_deps = cf.get("dependencies", [])
            cf_name = cf.get("name", "")

            for ptn, p in param_table_names.items():
                p_orig_name = str(p.get("name", "")).strip()

                # If the calc field explicitly targets another table (e.g., Fact_Sales), don't adopt it here
                if cf_table and cf_table != ptn:
                    continue

                # Match strategy 1: calc field's table_name matches the parameter table name
                matched = (cf_table == ptn)

                # Match strategy 2: calc field's DAX references the parameter table name
                if not matched and f"'{ptn}'" in cf_dax:
                    matched = True

                # Match strategy 3: dependencies include the parameter's original name
                if not matched and p_orig_name and p_orig_name in cf_deps:
                    matched = True

                if matched:
                    param_measures_map[ptn].append(cf)
                    dax_ref_flag = "yes" if f"'{ptn}'" in cf_dax else "no"
                    dep_flag = "yes" if p_orig_name in cf_deps else "no"
                    log_info(f"[add_parameters] Calc field '{cf_name}' matched to parameter table '{ptn}' (table_name='{cf_table}', dax_ref={dax_ref_flag}, dep={dep_flag})")


        def_path = f"{semantic_folder}/definition/tables"
        failed_files = []
        files_to_push_dict = {}

        for param in parameters:
            pbi_data = param.get("powerbi", {})
            raw_dax = pbi_data.get("dax") or param.get("dax") or ""

            clean_dax = re.sub(r'```[a-zA-Z]*', '', str(raw_dax)).replace('```', '').strip()
            while clean_dax.startswith('='): clean_dax = clean_dax[1:].strip()

            name = param.get("name", "Parameter")
            if "=" in clean_dax and not clean_dax.lstrip().upper().startswith("VAR "):
                parts = clean_dax.split('=', 1)
                left = parts[0].strip()
                if '\n' not in left and len(left) < 100:
                    name = left

            name = self._sanitize_name(name)

            dax = self._clean_dax(str(raw_dax), is_parameter=True) if raw_dax else ""

            ref_column = "Value"
            ref_table = name # [FIX] Default ref_table to the parameter name
            source_column_str = f"[{ref_column}]"
            match = re.search(r"'?([a-zA-Z0-9_ -]+)'?\[([a-zA-Z0-9_ -]+)\]", clean_dax)
            if match:
                ref_table = match.group(1)
                ref_column = match.group(2)
                source_column_str = f"{ref_table}[{ref_column}]"

            default = pbi_data.get("current_value") or param.get("suggested_default", "0")
            data_type = pbi_data.get("data_type") or param.get("data_type", "integer")
            data_type = str(data_type).lower()

            if not dax:
                log_info(f"Skipping parameter '{name}' - no DAX expression provided")
                continue

            if data_type in ["integer", "int", "whole number", "int64"]:
                format_str = "0"
                default_val = str(default) if str(default).isdigit() else "0"
                summarize_by = "sum"
            elif data_type in ["string", "text"]:
                format_str = "General"
                default_val = f'"{default}"'
                summarize_by = "none"
            else:
                format_str = "General"
                default_val = str(default)
                summarize_by = "sum"

            # Check if any calc fields are targeted to this parameter table
            targeted_calcs = param_measures_map.get(name, [])

            # Check for measures targeting this parameter from the top-level measures array
            mapped_measures = []
            if api_data:
                raw_measures = api_data.get("measures", [])
                if isinstance(raw_measures, list):
                    for m in raw_measures:
                        if isinstance(m, dict):
                            m_table = self._sanitize_name(m.get("table_name") or m.get("table") or "")
                            if m_table == name:
                                mapped_measures.append(m)

            measure_blocks = []
            
            # Always include the default harvest measure
            harvest_name = f'Selected{name.replace(" ", "").replace("-", "").replace("%", "Percent")}'
            harvest_measure_block = f"""    measure '{self._escape_tmdl_identifier(harvest_name)}'
        expression: SELECTEDVALUE('{self._escape_tmdl_identifier(name)}'[{ref_column}], {default_val})
        formatString: {format_str}
        isHidden: true"""
            measure_blocks.append(harvest_measure_block)

            # Add measures from targeted calcs (Calculated Fields & LODs)
            for tc in targeted_calcs:
                tc_type = str(tc.get("type", "")).strip().lower()
                if tc_type == "measure":
                    measure_name = self._sanitize_name(tc.get("name", "CustomFilter"))
                    if measure_name != harvest_name:
                        raw_meas_dax = tc.get("dax_formula") or tc.get("powerbi_formula") or "1"
                        cleaned_meas_dax = self._clean_dax(raw_meas_dax)
                        measure_blocks.append(f"""    measure '{self._escape_tmdl_identifier(measure_name)}' =
        {indent_block(cleaned_meas_dax, 8).strip()}""")
                        log_info(f"[add_parameters] Using '{measure_name}' as measure for parameter '{name}'")

            # Add measures from top-level measures
            for m in mapped_measures:
                measure_name = self._sanitize_name(m.get("name"))
                if measure_name != harvest_name:
                    if not any(self._sanitize_name(tc.get("name")) == measure_name for tc in targeted_calcs):
                        raw_meas_dax = m.get("dax_formula") or m.get("powerbi_formula") or "1"
                        cleaned_meas_dax = self._clean_dax(raw_meas_dax)
                        measure_blocks.append(f"""    measure '{self._escape_tmdl_identifier(measure_name)}' =
        {indent_block(cleaned_meas_dax, 8).strip()}""")
                        log_info(f"[add_parameters] Using '{measure_name}' as top-level measure for parameter '{name}'")

            measure_block = "\n\n".join(measure_blocks)

            # [FIX] Quote ref_table if it has spaces
            safe_ref_table = f"'{ref_table}'" if " " in ref_table else ref_table
            source_column_str = f"{safe_ref_table}[{ref_column}]"

            # [FIX] Escape table and column names in the main TMDL template
            tmdl = f"""table '{self._escape_tmdl_identifier(name)}'

    column '{self._escape_tmdl_identifier(ref_column)}'
        formatString: {format_str}
        summarizeBy: {summarize_by}
        isNameInferred
        sourceColumn: {source_column_str}
        annotation SummarizationSetBy = Automatic

    partition '{self._escape_tmdl_identifier(name)}' = calculated
        mode: import
        source = {dax}

{measure_block}

    annotation PBI_Id = {str(uuid.uuid4()).replace("-", "")}
    """
            file_path = f"{def_path}/{name}.tmdl"
            files_to_push_dict[file_path] = (tmdl, "add")

        files_to_push = [(p, c, st) for p, (c, st) in files_to_push_dict.items()]
        if files_to_push:
            ok = await self.file_agent.batch_update_files(files_to_push)
            if not ok:
                failed_files.extend([f[0] for f in files_to_push])
                log_error(f"Failed to push {len(files_to_push)} parameter files")
                return {"success": False, "message": "Failed to create parameter files", "failed_files": failed_files}

        log_info(f"Added {len(parameters)} parameters")
        return {"success": True, "message": f"Added {len(parameters)} parameters"}

    async def generate_tmdl_structure(self, api_data: Dict, folder_path: str, app_name: str, is_direct_lake: bool = False, direct_lake_schema: str = None):
        failed_files = []
        created_files = []

        try:
            if isinstance(api_data, list): api_data = api_data[0]

            # Get context from logging_utils
            from app.tableau.core.logging_utils import _project_id_ctx, _workbook_id_ctx, _run_id_ctx, get_log_token
            p_id = _project_id_ctx.get()
            w_id = _workbook_id_ctx.get()
            r_id = _run_id_ctx.get()
            token = get_log_token()

            await self.action_logger.send_activity_to_api(
                p_id, w_id, r_id, 
                technical_message=f"Starting TMDL structure generation for: {app_name}",
                token=token
            )

            datasource_map = self._build_datasource_map(api_data)

            # --- Type-guarded field extraction ---
            tables = api_data.get('tables', [])
            custom_sql = api_data.get('custom_sql', [])
            # Normalize new API schema: bi_table_name -> table_name, bi_column_name -> name, bi_datatype -> datatype
            tables = self._normalize_tables(tables)
            custom_sql = self._normalize_tables(custom_sql)
            # Filter out any non-dict entries or entries missing table_name
            tables = [t for t in tables if isinstance(t, dict) and t.get('table_name')]
            custom_sql = [cs for cs in custom_sql if isinstance(cs, dict) and cs.get('table_name')]
            all_tables = tables + custom_sql

            log_info(f"api_data top-level keys: {list(api_data.keys()) if isinstance(api_data, dict) else type(api_data)}")
            log_info(f"Found {len(tables)} tables, {len(custom_sql)} custom_sql entries => {len(all_tables)} total tables")

            relationships = api_data.get('relationships', [])
            if not isinstance(relationships, list): relationships = []
            measures = api_data.get('measures', [])
            if not isinstance(measures, list): measures = []
            measures = [m for m in measures if isinstance(m, dict) and m.get('name')]
            calcs = api_data.get('calculated_fields', [])
            if not isinstance(calcs, list): calcs = []
            all_sets = api_data.get('sets', [])
            if not isinstance(all_sets, list): all_sets = []
            if is_direct_lake:
                parameters = []
                log_info("[TMDL] DirectLake mode: skipping mapping JSON parameters — all tables (including parameters) sourced from lakehouse")
            else:
                parameters = api_data.get('parameters', [])
                if not isinstance(parameters, list): parameters = []
            actions = api_data.get('actions', [])
            if not isinstance(actions, list): actions = []

            # --- Extract LOD expressions & calculated fields from 'Calculated Fields & LODs' ---
            lod_expressions = []
            calc_fields_from_lods = []
            calc_and_lods = api_data.get('Calculated Fields & LODs', {})
            if isinstance(calc_and_lods, dict):
                lod_expressions = calc_and_lods.get('lod_expressions', [])
                calc_fields_from_lods = calc_and_lods.get('calculated_fields', [])
            elif isinstance(calc_and_lods, list):
                for item in calc_and_lods:
                    if isinstance(item, dict):
                        if 'lod_expressions' in item:
                            lod_expressions.extend(item.get('lod_expressions', []))
                        if 'calculated_fields' in item:
                            calc_fields_from_lods.extend(item.get('calculated_fields', []))
            # Fallback: check top-level keys
            if not lod_expressions:
                lod_expressions = api_data.get('lod_expressions', [])
            if not calc_fields_from_lods:
                calc_fields_from_lods = api_data.get('calculated_fields_lods', [])

            log_info(f"Found {len(lod_expressions)} LOD expressions to convert to DAX measures")
            log_info(f"Found {len(calc_fields_from_lods)} calculated fields from 'Calculated Fields & LODs'")

            # Split sets: dynamic → calc group tables (Calculation Items); static → calculated columns
            calc_item_sets = [s for s in all_sets if s.get('type', '').lower() == 'dynamic']
            regular_sets = [s for s in all_sets if s.get('type', '').lower() == 'static']

            col_to_table_map = {}
            valid_table_names = []

            for tbl in all_tables:
                t_name = self._sanitize_name(tbl['table_name'])
                valid_table_names.append(t_name)
                for col in tbl.get('columns', []):
                    if col.get('name'):
                        col_to_table_map[col['name']] = t_name

            for param in parameters:
                raw_dax = param.get("powerbi", {}).get("dax") or param.get("dax") or ""
                clean_dax = re.sub(r'```[a-zA-Z]*', '', str(raw_dax)).replace('```', '').strip()
                while clean_dax.startswith('='): clean_dax = clean_dax[1:].strip()

                name = param.get("name", "Parameter")
                if "=" in clean_dax and not clean_dax.lstrip().upper().startswith("VAR "):
                    parts = clean_dax.split('=', 1)
                    left = parts[0].strip()
                    if '\n' not in left and len(left) < 100:
                        name = left

                name = self._sanitize_name(name)
                valid_table_names.append(name)

                # Combine dynamic relationship extraction from parameter DAX (e.g. DISTINCT('Customers'[CustomerName]))
                match = re.search(r"'?([a-zA-Z0-9_ -]+)'?\[([a-zA-Z0-9_ -]+)\]", clean_dax)
                if match:
                    ref_table = match.group(1)
                    ref_column = match.group(2)
                    relationships.append({
                        "fromTable": name,
                        "fromColumn": ref_column,
                        "toTable": ref_table,
                        "toColumn": ref_column,
                        "cardinality": "onetomany_explicit",
                        "crossFilterDirection": "both"
                    })

            # [COMMENTED OUT] Dynamic calculation groups are now handled as calculated columns in target tables
            # dynamic_calc_groups = []
            # for idx, ds in enumerate(calc_item_sets):
            #     cg_name = ds.get('powerbi', {}).get('name')
            #     if not cg_name:
            #         cg_name = ds.get('name') or f"CalculationGroup_{idx}"
            #     cg_name = self._sanitize_name(cg_name)
            #
            #     calc_items = ds.get('powerbi', {}).get('calculation_items', [])
            #     if not calc_items:
            #         # Fallback if old format
            #         calc_items = [ds]
            #
            #     dynamic_calc_groups.append((cg_name, calc_items))
            #     valid_table_names.append(cg_name)

            # --- Build LOD expressions map by table name ---
            lod_measures_map: Dict[str, List] = {}
            lod_calcs_map: Dict[str, List] = {}
            for lod in lod_expressions:
                if not isinstance(lod, dict): continue
                lod_table = self._sanitize_name(lod.get('table_name', ''))
                if lod_table:
                    lod_type = str(lod.get('type', '')).strip().lower()
                    if lod_type == 'dimension':
                        lod_calcs_map.setdefault(lod_table, []).append(lod)
                    else:
                        lod_measures_map.setdefault(lod_table, []).append(lod)

            # --- Extract URL Actions as Measures ---
            url_action_measures = []
            for action in actions:
                if not isinstance(action, dict): continue
                tb_action = action.get('tableau_action', {})
                if tb_action.get('type') == 'url':
                    pbi_eq = action.get('powerbi_equivalent', {})
                    # using the specific `dax_code` value according to the user
                    dax_code = pbi_eq.get('dax_code')
                    if dax_code and '=' in dax_code:
                        # Extract left-hand side (name) and right-hand side (formula)
                        parts = dax_code.split('=', 1)
                        measure_name = parts[0].strip()
                        measure_expr = parts[1].strip()

                        url_action_measures.append({
                            'name': measure_name,
                            'dax_formula': measure_expr,
                            # Explicit target table will be resolved to valid_table_names[0] if left None
                            # But we'll force it to the first valid table if present
                            'table': valid_table_names[0] if valid_table_names else None
                        })

            if url_action_measures:
                log_info(f"Adding {len(url_action_measures)} URL actions as measures")
                measures.extend(url_action_measures)

            # --- Build calculated fields & extra measures map by table name ---
            calc_fields_map: Dict[str, List] = {}
            extra_measures_map: Dict[str, List] = {}

            for cf in calc_fields_from_lods:
                if not isinstance(cf, dict): continue
                cf_table = self._sanitize_name(cf.get('table_name', ''))
                cf_type = str(cf.get('type', '')).strip().lower()
                
                if cf_table:
                    if cf_type == 'measure':
                         extra_measures_map.setdefault(cf_table, []).append(cf)
                    else:
                         calc_fields_map.setdefault(cf_table, []).append(cf)

            semantic_def_path = f"{folder_path}/definition"

            # 1. Write the Database block
            db_path = f"{semantic_def_path}/database.tmdl"
            db_ok = await self.file_agent.create_or_update_file(db_path, self._generate_database_tmdl(), "Init database")
            if not db_ok:
                failed_files.append(db_path)
                log_error(f"Failed to push required file: {db_path}")
                return {"success": False, "message": f"Failed to create file: database.tmdl", "failed_files": failed_files}
            created_files.append(db_path)

            # Check for extract mode to generate expressions.tmdl
            extract_mode = any(info.get('mode') == 'extract' for info in datasource_map.values())
            log_info(f"[TMDL] extract_mode={extract_mode}")
            lakehouse_info = None

            dl_results_raw = api_data.get('data_layer_results')
            if isinstance(dl_results_raw, str):
                try:
                    dl_results_raw = json.loads(dl_results_raw)
                except (json.JSONDecodeError, TypeError, ValueError) as e:
                    log_warning(f"[TMDL] data_layer_results is a string but not valid JSON: {e}")
            dl_record = None
            if isinstance(dl_results_raw, dict):
                dl_record = dl_results_raw
            elif isinstance(dl_results_raw, list) and dl_results_raw:
                dl_record = dl_results_raw[0]

            dl_payload = dl_record.get('payload', {}) if isinstance(dl_record, dict) else {}
            processing_summary = dl_payload.get('processing_summary', []) if isinstance(dl_payload, dict) else []
            if not (isinstance(processing_summary, list) and processing_summary):
                # Fallback: in some flows payload keys are promoted to api_data top-level.
                promoted_summary = api_data.get('processing_summary', [])
                if isinstance(promoted_summary, list) and promoted_summary:
                    processing_summary = promoted_summary
            has_data_layer = isinstance(processing_summary, list) and len(processing_summary) > 0

            # expressions.tmdl should be generated when data-layer context exists.
            # This avoids missing file creation when mode flags are not propagated.
            should_generate_expressions = has_data_layer
            log_info(
                f"[TMDL] expressions gate: has_data_layer={has_data_layer}, "
                f"is_direct_lake={is_direct_lake}, extract_mode={extract_mode}, "
                f"has_dl_record={bool(dl_record)}"
            )
            if should_generate_expressions:
                lakehouse_dict = None
                workspace_id = None
                if isinstance(processing_summary, list) and processing_summary:
                    fab_artifacts = processing_summary[0].get('fabric_artifacts', {})
                    lakehouse_dict = fab_artifacts.get('lakehouse', {})
                    # Extract workspace_id from notebook_job URL
                    nb_job = fab_artifacts.get('notebook_job', {})
                    # notebook_job.url can be null when notebook execution is not triggered
                    nb_url = (nb_job.get('url') or '') if isinstance(nb_job, dict) else ''
                    ws_match = re.search(r'/workspaces/([a-zA-Z0-9-]+)/', nb_url) if nb_url else None
                    if ws_match:
                        workspace_id = ws_match.group(1)
                
                if lakehouse_dict:
                    lakehouse_info = lakehouse_dict
                    lakehouse_info['workspace_id'] = workspace_id
                    
                    dl_schema = processing_summary[0].get('lakehouse_schema')
                    if dl_schema:
                        lakehouse_info['schema'] = dl_schema
                        log_info(f"[TMDL] Using lakehouse_schema from Data Layer: {dl_schema}")
                    
                    # Generate dynamic PBI_RemovedChildren
                    removed_children = []
                    if isinstance(processing_summary, list) and processing_summary:
                        ep = processing_summary[0].get('extract_pipeline', {})
                        parquet_files = ep.get('parquet_files', [])
                        if not parquet_files:
                            parquet_files = ep.get('bronze_tables', [])
                        for pf in parquet_files:
                            base_name = pf.replace('.parquet', '').lower()
                            removed_children.append({
                                "remoteItemId": {
                                    "analysisServicesObject": {
                                        "sourceName": None,
                                        "sourceLineageTag": f"[dbo].[{base_name}]"
                                    }
                                },
                                "objectType": "Table"
                            })
                            removed_children.append({
                                "remoteItemId": {
                                    "analysisServicesObject": {
                                        "sourceName": None,
                                        "sourceLineageTag": f"[dbo].[{base_name}_silver]"
                                    }
                                },
                                "objectType": "Table"
                            })
                    pbi_removed_children_json = json.dumps(removed_children, separators=(',', ':'))
                    
                    template_path = "app/templates/static/expressions.tmdl"
                    try:
                        with open(template_path, 'r', encoding='utf-8') as f:
                            expr_template = f.read()
                        
                        expr_content = expr_template.replace('{lakehouse_name}', lakehouse_info.get('name', 'Lakehouse'))
                        expr_content = expr_content.replace('{workspace_id}', lakehouse_info.get('workspace_id', '<workspace-id>'))
                        expr_content = expr_content.replace('{lakehouse_id}', lakehouse_info.get('id', '<lakehouse-id>'))
                        expr_content = expr_content.replace('{guid}', str(uuid.uuid4()))
                        expr_content = expr_content.replace('{pbi_removed_children}', pbi_removed_children_json)
                        
                        expr_path = f"{semantic_def_path}/expressions.tmdl"
                        expr_ok = await self.file_agent.create_or_update_file(expr_path, expr_content, "Init expressions for DirectLake")
                        if expr_ok:
                            created_files.append(expr_path)
                            log_info(f"[TMDL] Created expressions.tmdl (is_direct_lake={is_direct_lake}, extract_mode={extract_mode})")
                        else:
                            log_error(f"[TMDL] Failed to create expressions.tmdl")
                    except (OSError, IOError, ValueError, KeyError) as expr_err:
                        log_error(f"[TMDL] Error creating expressions.tmdl: {expr_err}")
                else:
                    log_info("[TMDL] expressions.tmdl requested but no lakehouse info found in data_layer_results")
            else:
                log_info("[TMDL] Skipping expressions.tmdl because data-layer processing_summary is missing")

            default_table = None
            if all_tables:
                default_table = self._sanitize_name(all_tables[0]['table_name'])

            table_calcs_map = {name: [] for name in valid_table_names}

            # Distribute static sets using powerbi.target_table
            for item in regular_sets:
                if not isinstance(item, dict): continue
                target_table = self._sanitize_name(item.get('powerbi', {}).get('target_table', '')) or None

                if not target_table or target_table not in table_calcs_map:
                    target_table = default_table

                if target_table and target_table in table_calcs_map:
                    table_calcs_map[target_table].append(item)
                elif default_table:
                    table_calcs_map[default_table].append(item)

            # [NEW] Distribute dynamic sets as calculated columns
            for ds in calc_item_sets:
                if not isinstance(ds, dict): continue
                target_table = self._sanitize_name(ds.get('powerbi', {}).get('target_table', '')) or None
                calc_cols = ds.get('powerbi', {}).get('calculated_columns', [])
                
                if not target_table:
                    target_table = default_table
                
                if target_table in table_calcs_map:
                    for col in calc_cols:
                        table_calcs_map[target_table].append(col)
                elif default_table:
                    for col in calc_cols:
                        table_calcs_map[default_table].append(col)

            # Distribute calculated_fields using base_field / formula scanning
            for item in calcs:
                if not isinstance(item, dict): continue
                base_field = item.get('base_field')
                target_table = None

                if base_field and base_field in col_to_table_map:
                    target_table = col_to_table_map[base_field]
                else:
                    formula = item.get('powerbi_formula') or item.get('dax_formula') or ""
                    matches = re.findall(r'\[([^\]]+)\]', formula)
                    for match in matches:
                        if match in col_to_table_map:
                            target_table = col_to_table_map[match]
                            break

                if not target_table:
                    target_table = default_table

                if target_table in table_calcs_map:
                    table_calcs_map[target_table].append(item)
                else:
                    if default_table:
                        table_calcs_map[default_table].append(item)

            # --- Distribute measures across tables based on explicit 'table' field ---
            measures_map: Dict[str, List] = {name: [] for name in valid_table_names}
            fallback_table = default_table or (valid_table_names[0] if valid_table_names else None)

            for m in measures:
                if not isinstance(m, dict): continue
                # Use the explicit 'table' field from the measure
                raw_table = m.get('table', '')
                target = self._sanitize_name(raw_table) if raw_table else None

                # If the table isn't in valid_table_names, fall back
                if target and target not in measures_map:
                    log_info(f"Measure '{m.get('name')}' references unknown table '{target}', falling back to '{fallback_table}'")
                    target = fallback_table

                if not target:
                    target = fallback_table

                if target:
                    measures_map.setdefault(target, []).append(m)

            log_info(f"Distributed {len(measures)} measures across tables: {{{', '.join(f'{k}: {len(v)}' for k, v in measures_map.items() if v)}}}")

            # 3. Generate per-table TMDL files
            tables_created = 0
            tables_to_push_dict = {}
            for tbl in all_tables:
                t_name = self._sanitize_name(tbl['table_name'])

                ds_key = tbl.get('datasource_id') or tbl.get('datasource')
                table_conn_info = {}
                if ds_key:
                    t_name_norm = self._normalize_table_name(tbl.get('table_name', ''))
                    specific_key = f"{ds_key}_{t_name_norm}"
                    table_conn_info = datasource_map.get(specific_key)
                    if not table_conn_info:
                        table_conn_info = datasource_map.get(ds_key, {})
                if not table_conn_info and datasource_map:
                    schema_name_field = tbl.get('schema_name', '')
                    raw_schema = None
                    if schema_name_field:
                        schema_match = re.match(r'\[?([^\]\[]+)\]?\.\[?([^\]\[]+)\]?', schema_name_field)
                        if schema_match:
                            raw_schema = schema_match.group(1).lower()

                    if raw_schema:
                        for conn_info in datasource_map.values():
                            conn_schema = conn_info.get('schema')
                            if conn_schema and str(conn_schema).lower() == raw_schema:
                                table_conn_info = conn_info
                                break

                    if not table_conn_info:
                        table_conn_info = next(iter(datasource_map.values()), {})

                # Measures: distribute to the table referenced in the DAX formula
                tbl_measures = measures_map.get(t_name, [])
                
                # Append LOD expressions as DAX measures for this table
                lod_for_table = lod_measures_map.get(t_name, [])
                if lod_for_table:
                    tbl_measures = tbl_measures + lod_for_table
                    log_info(f"Added {len(lod_for_table)} LOD measures to table '{t_name}'")

                # Extra measures from 'Calculated Fields & LODs' (type=measure)
                extra_m_for_table = extra_measures_map.get(t_name, [])
                if extra_m_for_table:
                    tbl_measures = tbl_measures + extra_m_for_table
                    log_info(f"Added {len(extra_m_for_table)} extra measures from calculated fields to table '{t_name}'")

                # Calculated columns: from sets + calculated_fields + calc_fields_from_lods per table
                tbl_specific_calcs = list(table_calcs_map.get(t_name, []))
                calc_fields_for_table = calc_fields_map.get(t_name, [])
                if calc_fields_for_table:
                    tbl_specific_calcs = tbl_specific_calcs + calc_fields_for_table
                    log_info(f"Added {len(calc_fields_for_table)} calculated fields to table '{t_name}'")

                # Append dimension-type LOD expressions as calculated columns
                lod_calcs_for_table = lod_calcs_map.get(t_name, [])
                if lod_calcs_for_table:
                    tbl_specific_calcs = tbl_specific_calcs + lod_calcs_for_table
                    log_info(f"Added {len(lod_calcs_for_table)} dimension-type LOD columns to table '{t_name}'")

                direct_lake_schema = lakehouse_info.get('schema') if lakehouse_info else None
                tmd_content, ldt_list = self._generate_table_tmdl(tbl, table_conn_info, tbl_measures, tbl_specific_calcs, lakehouse_info=lakehouse_info, is_direct_lake=is_direct_lake, direct_lake_schema=direct_lake_schema)
                table_path = f"{semantic_def_path}/tables/{t_name}.tmdl"
                tables_to_push_dict[table_path] = (tmd_content, "add")
                tables_created += 1

                # Collect LocalDateTable metadata from Date-type calculated columns
                for ldt in ldt_list:
                    local_table_name = f"LocalDateTable_{ldt['local_table_id']}"
                    ldt_tmdl = self._generate_local_date_table_tmdl(
                        ldt['parent_table'], ldt['col_name'],
                        ldt['local_table_id'], ldt['rel_id'], ldt['col_lineage_tag']
                    )
                    ldt_path = f"{semantic_def_path}/tables/{local_table_name}.tmdl"
                    tables_to_push_dict[ldt_path] = (ldt_tmdl, "add")
                    valid_table_names.append(local_table_name)
                    # Add the variation relationship
                    relationships.append({
                        "fromTable": ldt['parent_table'],
                        "fromColumn": ldt['col_name'],
                        "toTable": local_table_name,
                        "toColumn": "Date",
                        "cardinality": "ManyToOne",
                        "_override_rel_id": ldt['rel_id'],
                        "joinOnDateBehavior": "datePartOnly"
                    })
                    log_info(f"Queued LocalDateTable '{local_table_name}' for '{t_name}'['{ldt['col_name']}']")


            # Build the Model file after all tables and relationships are collected
            rels_tmdl = ""
            if relationships:
                rels_tmdl = self._generate_relationships_tmdl(relationships, valid_table_names)

            model_content = self._generate_model_tmdl(valid_table_names, rels_tmdl, has_calc_groups=False, is_direct_lake=is_direct_lake, lakehouse_info=lakehouse_info)
            model_path = f"{semantic_def_path}/model.tmdl"
            tables_to_push_dict[model_path] = (model_content, "add")

            # 4. Generate Calculation Group tables (one for each dynamic set)
            # [COMMENTED OUT] Dynamic calculation groups are now handled as calculated columns
            # for cg_name, calc_items in dynamic_calc_groups:
            #     cg_content = self._generate_calculation_group_tmdl(cg_name, calc_items)
            #     cg_path = f"{semantic_def_path}/tables/{cg_name}.tmdl"
            #     tables_to_push_dict[cg_path] = (cg_content, "add")
            #     log_info(f"Created calculation group table '{cg_name}' with {len(calc_items)} calculation items")

            tables_to_push = [(p, c, st) for p, (c, st) in tables_to_push_dict.items()]
            created_files.extend([f[0] for f in tables_to_push])
            
            if tables_to_push:
                batch_ok = await self.file_agent.batch_update_files(tables_to_push)
                if not batch_ok:
                    failed_files.extend([f[0] for f in tables_to_push])
                    log_error(f"Failed to push {len(tables_to_push)} table files")
                    return {"success": False, "message": "Failed to create table files in batch", "failed_files": failed_files}

            # Validation: ensure at least one table was created when tables exist in the data
            if all_tables and tables_created == 0:
                msg = "No table .tmdl files were created despite tables being present in the source data"
                log_error(msg)
                return {"success": False, "message": msg, "failed_files": failed_files}

            log_info(f"Generated TMDL for {tables_created} tables and relationships. All {len(created_files)} files pushed successfully.")
            await self.action_logger.send_activity_to_api(
                p_id, w_id, r_id, 
                technical_message=f"Successfully generated TMDL for {tables_created} tables and pushed {len(created_files)} files",
                token=token
            )
            return {"success": True, "message": "TMDL Generated", "created_files": created_files}

        except (KeyError, ValueError, TypeError, AttributeError, OSError, RuntimeError) as e:
            log_error(f"TMDL Gen failed: {e}")
            return {"success": False, "message": str(e), "failed_files": failed_files}

    def __init__(self, file_agent, folder_agent, action_logger: Optional[ActionLogger] = None):
        self.file_agent = file_agent
        self.folder_agent = folder_agent
        self.action_logger = action_logger or ActionLogger()

