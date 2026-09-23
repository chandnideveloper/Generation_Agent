import re
from typing import Dict, Any, Tuple
from app.tableau.core.logging_utils import log_info

class MetadataPreScannerMixin:
    def _parse_metadata_fields(self, api_data: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str], Dict[str, str], str, set, set, set, list]:
        """
        Extracts semantic mapping dictionaries from the raw API JSON:
        Returns:
            field_to_table, field_to_datatype, display_to_bi_name, field_to_format, default_table, fields_with_dax, measure_set, known_source_columns, calculated_fields_metadata
        """
        field_to_table, field_to_datatype = {}, {}
        display_to_bi_name = {}  # Maps display names (e.g. "Customer Name") -> BI column names (e.g. "CustomerName")
        field_to_format = {}     # Maps field names to their BI/Tableau format strings
        known_source_columns = set() # Set of (table_name, column_name) to track valid source fields
        default_table = "UnknownTable"
        fields_with_dax = set()
        measure_set = set()
        calculated_fields_metadata = [] # NEW: Collection for final response

        # --- 1. Physical tables ---
        all_tables = api_data.get("tables", [])
        for table in all_tables:
            if isinstance(table, list): continue
            # Normalize new API schema keys
            if 'bi_table_name' in table and not table.get('table_name'):
                table['table_name'] = table['bi_table_name']
            t_name = table.get("table_name", "Unknown")
            is_param_table = table.get("is_parameter_table", False)
            if default_table == "UnknownTable":
                default_table = t_name
            for col in table.get("columns", []):
                if isinstance(col, dict):
                    if 'bi_column_name' in col and not col.get('name'):
                        col['name'] = col['bi_column_name']
                    if 'bi_datatype' in col and not col.get('datatype'):
                        col['datatype'] = col['bi_datatype']
                    if not col.get('datatype') and col.get('tableau_datatype'):
                        col['datatype'] = col['tableau_datatype']
                source_col_name = col.get("name", "")
                if not source_col_name: continue

                # Determine the canonical BI column name:
                # Priority: bi_column_name > renamed_column_name > tableau_renamed_column_name > name
                bi_col_name = None
                if isinstance(col, dict):
                    bi_col_name = col.get('bi_column_name') or col.get('renamed_column_name') or col.get('tableau_renamed_column_name')
                # Use BI column name as the canonical name if available and different from source name
                col_name = bi_col_name if bi_col_name else source_col_name

                dax = None
                if isinstance(col, dict):
                    dax = col.get("dax_formula") or col.get("powerbi_formula") or col.get("powerbi", {}).get("dax")
                if dax:
                    fields_with_dax.add(col_name)

                # For parameter tables, do NOT overwrite existing field mappings from real source tables
                if is_param_table and col_name in field_to_table:
                    continue

                field_to_table[col_name] = t_name
                field_to_datatype[col_name] = col.get("datatype", "string")
                if isinstance(col, dict) and col.get("format"):
                    field_to_format[col_name] = self._map_format_to_pbi_string(col["format"])
                known_source_columns.add((t_name, col_name))

                # Always register scoped mappings
                display_to_bi_name[f"{t_name}.{source_col_name}"] = col_name
                display_to_bi_name[f"{t_name}.{col_name}"] = col_name

                # Register source column name as alias mapping to BI name (e.g. "CustomerName" -> "Customer Name")
                if bi_col_name and source_col_name != bi_col_name:
                    if source_col_name not in field_to_table or not is_param_table:
                        field_to_table[source_col_name] = t_name
                        field_to_datatype[source_col_name] = col.get("datatype", "string")
                        if isinstance(col, dict) and col.get("format"):
                            field_to_format[source_col_name] = self._map_format_to_pbi_string(col["format"])
                    display_to_bi_name[source_col_name] = col_name  # "CustomerName" -> "Customer Name"

                # Also register space-separated alias (e.g. "StockItemName" -> "Stock Item Name")
                alias = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', source_col_name)
                if alias != source_col_name and alias != col_name:
                    display_to_bi_name[f"{t_name}.{alias}"] = col_name
                    if alias not in field_to_table or not is_param_table:
                        field_to_table[alias] = t_name
                        field_to_datatype[alias] = col.get("datatype", "string")
                    display_to_bi_name[alias] = col_name  # "Customer Name" -> "Customer Name" (BI name)
                # Also register the Tableau column name if different from BI column name
                tab_col = col.get('tableau_column_name', '')
                if tab_col and tab_col != col_name:
                    display_to_bi_name[f"{t_name}.{tab_col}"] = col_name
                    if tab_col not in field_to_table:
                        field_to_table[tab_col] = t_name
                        field_to_datatype[tab_col] = col.get("datatype", "string")
                        display_to_bi_name[tab_col] = col_name

        for cs in api_data.get("custom_sql", []):
            if 'bi_table_name' in cs and not cs.get('table_name'):
                cs['table_name'] = cs['bi_table_name']
            t_name = cs.get("table_name") or cs.get("name") or "Unknown"
            if default_table == "UnknownTable":
                default_table = t_name
            for col in cs.get("columns", []):
                if isinstance(col, dict):
                    if 'bi_column_name' in col and not col.get('name'):
                        col['name'] = col['bi_column_name']
                    if 'bi_datatype' in col and not col.get('datatype'):
                        col['datatype'] = col['bi_datatype']
                    if not col.get('datatype') and col.get('tableau_datatype'):
                        col['datatype'] = col['tableau_datatype']
                source_col_name = col.get("name", "")
                if not source_col_name: continue

                # Determine the canonical BI column name
                bi_col_name = None
                if isinstance(col, dict):
                    bi_col_name = col.get('bi_column_name') or col.get('renamed_column_name') or col.get('tableau_renamed_column_name')
                col_name = bi_col_name if bi_col_name else source_col_name

                dax = None
                if isinstance(col, dict):
                    dax = col.get("dax_formula") or col.get("powerbi_formula") or col.get("powerbi", {}).get("dax")
                if dax:
                    fields_with_dax.add(col_name)

                field_to_table[col_name] = t_name
                field_to_datatype[col_name] = col.get("datatype", "string")
                if isinstance(col, dict) and col.get("format"):
                    field_to_format[col_name] = self._map_format_to_pbi_string(col["format"])
                known_source_columns.add((t_name, col_name))

                # Always register scoped mappings
                display_to_bi_name[f"{t_name}.{source_col_name}"] = col_name
                display_to_bi_name[f"{t_name}.{col_name}"] = col_name

                # Register source column name as alias mapping to BI name
                if bi_col_name and source_col_name != bi_col_name:
                    field_to_table[source_col_name] = t_name
                    field_to_datatype[source_col_name] = col.get("datatype", "string")
                    if isinstance(col, dict) and col.get("format"):
                        field_to_format[source_col_name] = self._map_format_to_pbi_string(col["format"])
                    display_to_bi_name[source_col_name] = col_name

                alias = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', source_col_name)
                if alias != source_col_name and alias != col_name:
                    display_to_bi_name[f"{t_name}.{alias}"] = col_name
                    field_to_table[alias] = t_name
                    field_to_datatype[alias] = col.get("datatype", "string")
                    display_to_bi_name[alias] = col_name
                tab_col = col.get('tableau_column_name', '')
                if tab_col and tab_col != col_name:
                    display_to_bi_name[f"{t_name}.{tab_col}"] = col_name
                    if tab_col not in field_to_table:
                        field_to_table[tab_col] = t_name
                        field_to_datatype[tab_col] = col.get("datatype", "string")
                        display_to_bi_name[tab_col] = col_name

        # --- 3. Measures from root "measures" list ---
        raw_measures = api_data.get("measures", [])
        if not isinstance(raw_measures, list): raw_measures = []

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
            target = m.get('table') or m.get('table_name')

            if not target or target not in known_table_names:
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
                if m.get("format"):
                    pbi_fmt = self._map_format_to_pbi_string(m["format"])
                    field_to_format[m_name] = pbi_fmt
            
            # Export metadata for response
            calculated_fields_metadata.append({
                "name": m_name,
                "table": target,
                "type": "measure",
                "dax_formula": m.get("dax_formula") or m.get("powerbi_formula"),
                "format": m.get("format")
            })

        # --- 3a. Parameters from root "parameters" list ---
        raw_params = api_data.get("parameters", [])
        if isinstance(raw_params, list):
            for p in raw_params:
                if not isinstance(p, dict): continue
                p_name = p.get("name", "")
                bi_name = p.get("bi_column_name") or p_name
                tab_name = p.get("tableau_column_name") or p_name
                
                # Parameters in DirectLake/Modern mode are their own tables
                field_to_table[bi_name] = p_name
                field_to_table[p_name] = p_name
                field_to_datatype[bi_name] = p.get("datatype", "string")
                field_to_datatype[p_name] = p.get("datatype", "string")
                
                # Register scoped mappings for parameter
                display_to_bi_name[f"{p_name}.{p_name}"] = bi_name
                display_to_bi_name[f"{p_name}.{bi_name}"] = bi_name
                display_to_bi_name[f"{p_name}.{tab_name}"] = bi_name
                
                if tab_name and tab_name != bi_name:
                    field_to_table[tab_name] = p_name
                    display_to_bi_name[tab_name] = bi_name
                    if p.get("format"):
                        field_to_format[tab_name] = self._map_format_to_pbi_string(p["format"])
                
                if p_name != bi_name:
                    display_to_bi_name[p_name] = bi_name

                # Register "Value" column for DirectLake parameter tables
                field_to_table[f"{p_name}[Value]"] = p_name
                
                # Export metadata
                calculated_fields_metadata.append({
                    "name": bi_name,
                    "table": p_name,
                    "type": "parameter",
                    "dax_formula": p.get("powerbi", {}).get("dax") or p.get("dax")
                })

        # --- 4. Calculated fields from "Calculated Fields & LODs" section ---
        calc_lods_raw = api_data.get("Calculated Fields & LODs", [])
        calc_lods = calc_lods_raw if isinstance(calc_lods_raw, list) else [calc_lods_raw] if isinstance(calc_lods_raw, dict) else []

        for block in calc_lods:
            if not isinstance(block, dict): continue
            for cf in block.get("calculated_fields", []):
                cf_name = cf.get("name", "")
                cf_table = cf.get("table") or cf.get("table_name") or default_table
                cf_type = str(cf.get("type", "")).strip().lower()
                
                # Register scoped mapping for calculated field
                if cf_name:
                    display_to_bi_name[f"{cf_table}.{cf_name}"] = cf_name

                # Export metadata
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
                    if cf.get("format"):
                        field_to_format[cf_name] = self._map_format_to_pbi_string(cf["format"])

            for lod in block.get("lod_expressions", []):
                lod_name = lod.get("name", "")
                lod_table = lod.get("table") or lod.get("table_name") or default_table
                
                # Register scoped mapping for LOD
                if lod_name:
                    display_to_bi_name[f"{lod_table}.{lod_name}"] = lod_name

                # Export metadata
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
                    if lod.get("format"):
                        field_to_format[lod_name] = self._map_format_to_pbi_string(lod["format"])

        # --- 4a. Standalone Calculated Fields ---
        raw_calcs = api_data.get("calculated_fields", [])
        if isinstance(raw_calcs, list):
            for cf in raw_calcs:
                if not isinstance(cf, dict): continue
                cf_name = cf.get("name", "")
                cf_table = cf.get("table") or cf.get("table_name") or default_table
                cf_type = cf.get("type", "dimension").lower()
                
                # Register scoped mapping for standalone calculated field
                if cf_name:
                    display_to_bi_name[f"{cf_table}.{cf_name}"] = cf_name
                
                # Export metadata
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

        # --- 4b. Sets (Dynamic sets -> Calculation Groups) ---
        all_sets = api_data.get('sets', [])
        if isinstance(all_sets, list):
            for s in all_sets:
                if not isinstance(s, dict): continue
                s_name = s.get("name")
                if not s_name: continue

                s_type = s.get("type", "").lower()
                if s_type == "dynamic":
                    cg_name = s.get("powerbi", {}).get("name")
                    if not cg_name:
                        cg_name = f"Calculation Group {s_name}"
                    field_to_table[s_name] = cg_name
                    field_to_datatype[s_name] = "string"
                    display_to_bi_name[s_name] = cg_name
                    display_to_bi_name[f"{cg_name}.{s_name}"] = cg_name
                    # Also register any specific calculated_columns of the dynamic set (e.g. Stock_Item_Name_Set_2_In_Out)
                    target_table = s.get("powerbi", {}).get("target_table") or default_table
                    for cc in s.get("powerbi", {}).get("calculated_columns", []):
                        cc_name = cc.get("name")
                        if cc_name:
                            field_to_table[cc_name] = target_table
                            field_to_datatype[cc_name] = "string"
                            fields_with_dax.add(cc_name)
                    log_info(f"[CoordinatorAgent] Registered dynamic set '{s_name}' (CG: '{cg_name}') with {len(s.get('powerbi', {}).get('calculated_columns', []))} columns mapped to '{target_table}'")
                else:
                    s_table = s.get("powerbi", {}).get("target_table") or default_table
                    display_to_bi_name[f"{s_table}.{s_name}"] = s_name
                    if s_name not in field_to_table:
                        field_to_table[s_name] = s_table
                        field_to_datatype[s_name] = "string"

        # --- 5. Pre-scan ALL sheet visuals and register unknown fields ---
        pre_scan_visuals = api_data.get("visuals", {})
        pre_scan_sheets = pre_scan_visuals.get("sheet_visuals", []) if isinstance(pre_scan_visuals, dict) else []
        if isinstance(pre_scan_sheets, dict):
            pre_scan_sheets = pre_scan_sheets.get("sheets", [])

        for sv in pre_scan_sheets:
            if not isinstance(sv, dict):
                continue
            field_arrays = []
            for key in ("rows", "columns", "marks_text", "marks_color", "marks_detail", "marks_size"):
                arr = sv.get(key, [])
                if isinstance(arr, list):
                    for item in arr:
                        raw_f = None
                        fmt = None
                        if isinstance(item, dict):
                            raw_f = item.get("field")
                            fmt = item.get("format")
                        elif isinstance(item, str):
                            raw_f = item
                        
                        if not raw_f: continue
                        
                        # Register field for mapping
                        import re as _re
                        cleaned_reg = raw_f
                        agg_match = _re.match(r'^[A-Z0-9_]+\((.*)\)$', raw_f, _re.IGNORECASE)
                        if agg_match:
                            cleaned_reg = agg_match.group(1).strip()
                            
                        if cleaned_reg not in field_to_table:
                            field_to_table[cleaned_reg] = default_table
                            field_to_datatype[cleaned_reg] = "real"
                        
                        field_arrays.append(raw_f)

            for raw_field in field_arrays:
                if not isinstance(raw_field, str) or not raw_field.strip():
                    continue
                cleaned = raw_field.strip()
                if cleaned.lower() in ("none", "", "null", "n/a"):
                    continue
                import re as _re
                agg_match = _re.match(r'^[A-Z0-9_]+\((.*)\)$', cleaned, _re.IGNORECASE)
                if agg_match:
                    cleaned = agg_match.group(1).strip()
                if not cleaned:
                    continue

                if cleaned not in field_to_table:
                    field_to_table[cleaned] = default_table
                    field_to_datatype[cleaned] = "real"
                    log_info(f"[CoordinatorAgent] Auto-registered visual field '{cleaned}' as column on '{default_table}'")

                alias = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', cleaned)
                if alias != cleaned and alias not in field_to_table:
                    field_to_table[alias] = field_to_table[cleaned]
                    field_to_datatype[alias] = field_to_datatype[cleaned]
                    display_to_bi_name[alias] = cleaned

        # --- 6. Pre-scan ALL dashboard visuals for formats ---
        pre_scan_dashboards = api_data.get("dashboards", [])
        if isinstance(pre_scan_dashboards, list):
            for db in pre_scan_dashboards:
                if not isinstance(db, dict): continue
                for dv in db.get("dashboard_visuals", []):
                    if not isinstance(dv, dict): continue
                    for key in ("rows", "columns", "marks_text", "marks_color", "marks_detail", "marks_size"):
                        arr = dv.get(key, [])
                        if isinstance(arr, list):
                            for item in arr:
                                raw_f = None
                                if isinstance(item, dict):
                                    raw_f = item.get("field")
                                elif isinstance(item, str):
                                    raw_f = item
                                
                                if not raw_f: continue
                                
                                # Auto-register field for mapping
                                import re as _re
                                cleaned_reg = raw_f
                                agg_match = _re.match(r'^[A-Z0-9_]+\((.*)\)$', raw_f, _re.IGNORECASE)
                                if agg_match:
                                    cleaned_reg = agg_match.group(1).strip()
                                
                                if cleaned_reg not in field_to_table:
                                    field_to_table[cleaned_reg] = default_table
                                    field_to_datatype[cleaned_reg] = "real"

        log_info(f"[CoordinatorAgent] After pre-scan: {len(field_to_table)} fields, {len(measure_set)} measures registered")
        
        # NOTE: Do not merge sheet visual formats into this global field_to_format map.
        # Format selection must be visual-scoped to avoid one sheet overriding another.
        # Visual-level formats are resolved later from each visual payload in _get_local_field_to_format.

        return field_to_table, field_to_datatype, display_to_bi_name, field_to_format, default_table, fields_with_dax, measure_set, known_source_columns, calculated_fields_metadata

    def _map_format_to_pbi_string(self, f_obj: Any) -> str:
        """Converts Tableau/Mapping format object to Power BI format string."""
        if not f_obj: return None
        if isinstance(f_obj, str): return f_obj
        if not isinstance(f_obj, dict): return None
        
        bi_f = f_obj.get("bi_format", {})
        if not isinstance(bi_f, dict): bi_f = {}
        
        # If explicit format_string exists, use it
        fmt_str = bi_f.get("format_string")
        if fmt_str: return fmt_str
        
        fmt_type = str(f_obj.get("format_type", "")).lower()
        
        if "currency" in fmt_type:
            symbol_raw = bi_f.get("currency_symbol", "$")
            s_char = "$"
            if "€" in symbol_raw: 
                s_char = "€"
                # Match user snippet: "\"€\" #,0.00;-\"€\" #,0.00;\"€\" #,0.00"
                return f'"{s_char}" #,0.00;-"{s_char}" #,0.00;"{s_char}" #,0.00'
            elif "₹" in symbol_raw or "Indian Rupee" in symbol_raw: 
                s_char = "₹" 
                if "16393" in str(f_obj.get("locale", "")):
                    s_char = "₹"
                # Match user snippet: "\"₹\" #,0.00;\"₹\" -#,0.00;\"₹\" #,0.00"
                return f'"{s_char}" #,0.00;"{s_char}" -#,0.00;"{s_char}" #,0.00'
            elif "£" in symbol_raw: s_char = "£"
            elif "¥" in symbol_raw: s_char = "¥"
            elif symbol_raw and len(symbol_raw) > 0 and not symbol_raw[0].isalnum():
                s_char = symbol_raw[0]
            
            return f'"{s_char}" #,0.00;"{s_char}" -#,0.00;"{s_char}" #,0.00'
        
        if "number" in fmt_type:
            decimal_places = f_obj.get("decimal_places", 0)
            if decimal_places > 0:
                return "#,0." + ("0" * decimal_places)
            return "#,0"
            
        if "percentage" in fmt_type:
            return "0.00%"
            
        if "automatic" in fmt_type or "general" in fmt_type:
            return "G"
            
        return "G"
