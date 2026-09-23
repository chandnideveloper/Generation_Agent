# app/agents/tmdl_generator.py

import uuid
import json
import re
from typing import Dict, List, Optional, Set, Tuple
from app.tableau.core.logging_utils import log_info, log_error
from app.tableau.utils.path_utils import indent_block

class TmdlGenerator:
    def __init__(self, file_agent, folder_agent):
        self.file_agent = file_agent
        self.folder_agent = folder_agent

    def _map_data_type(self, json_type: str) -> str:
        """Maps JSON/API data types to TMDL (Semantic Model) types."""
        jt = json_type.lower()
        if jt in ['string', 'text']: return 'string'
        if jt in ['integer', 'int', 'int64', 'whole number']: return 'int64'
        if jt in ['real', 'decimal', 'float', 'double', 'number', 'decimal number']: return 'double'
        if jt in ['boolean', 'bool', 'true/false']: return 'boolean'
        if jt in ['date', 'datetime', 'date/time']: return 'dateTime'
        return 'string'

    def _sanitize_name(self, name: str) -> str:
        if not name: return "Unknown"
        return name.replace('\n', ' ').replace('\r', '').replace('\t', ' ').strip()

    def _normalize_tables(self, tables: List[Dict]) -> List[Dict]:
        """Normalize table dicts: map bi_table_name -> table_name, bi_column_name -> name, bi_datatype -> datatype."""
        for tbl in tables:
            if not isinstance(tbl, dict): continue
            # Normalize table name
            if 'bi_table_name' in tbl and not tbl.get('table_name'):
                tbl['table_name'] = tbl['bi_table_name']
            # Normalize columns
            for col in tbl.get('columns', []):
                if not isinstance(col, dict): continue
                if 'bi_column_name' in col and not col.get('name'):
                    col['name'] = col['bi_column_name']
                if 'bi_datatype' in col and not col.get('datatype'):
                    col['datatype'] = col['bi_datatype']
                # Also keep tableau_datatype as fallback
                if not col.get('datatype') and col.get('tableau_datatype'):
                    col['datatype'] = col['tableau_datatype']
        return tables

    def _clean_dax(self, formula: str, is_parameter: bool = False, is_measure: bool = False, 
                   table_to_cols: Dict[str, Set[str]] = None,
                   display_to_qualified: Dict[str, str] = None) -> str:
        if not formula:
            return ""
        
        # 1. Remove Markdown code blocks
        clean = re.sub(r'```[a-zA-Z]*', '', formula).replace('```', '').strip()
        
        # 2. Remove leading equals sign
        while clean.startswith('='):
            clean = clean[1:].strip()
            
        # 3. Fix Tableau ISNULL -> DAX ISBLANK
        clean = re.sub(r'(?i)\bISNULL\s*\(', 'ISBLANK(', clean)
        
        # 4. Fix Tableau's SUM(INT([Col])) -> DAX SUM([Col])
        clean = re.sub(r'(?i)\bSUM\s*\(\s*INT\s*\(\s*(\[[^\]]+\])\s*\)\s*\)', r'SUM(\1)', clean)
        
        # 5. Fix Tableau's COUNT(INT(...))
        clean = re.sub(r'(?i)\bCOUNT\s*\(\s*INT\s*\(\s*(\[[^\]]+\])\s*\)\s*\)', r'COUNT(\1)', clean)

        # 6. Rule: CNTD(x), COUNTD(x) -> DISTINCTCOUNT(x)
        # Also handle common typos/variants like ctd, count_distinct
        clean = re.sub(r'(?i)\b(CNTD|COUNTD|CTD|COUNTDISTINCT|COUNT_DISTINCT)\s*\(', 'DISTINCTCOUNT(', clean)

        # 7. Rule: AVG(x) -> AVERAGE(x)
        clean = re.sub(r'(?i)\bAVG\s*\(', 'AVERAGE(', clean)

        # 8. Rule: AGG(x) -> remove wrapper
        while "AGG(" in clean.upper():
            match = re.search(r'(?i)\bAGG\s*\((.*)\)', clean)
            if match:
                if clean.upper().startswith("AGG(") and clean.endswith(")"):
                    clean = clean[4:-1].strip()
                else:
                    clean = re.sub(r'(?i)\bAGG\s*\((.*?)\)', r'\1', clean)
            else:
                break

        # 9. Rule: USERNAME() / USERPRINCIPALNAME() / CUSTOMDATA() validation
        if not is_measure and not is_parameter:
            forbidden = ["USERNAME(", "USERPRINCIPALNAME(", "CUSTOMDATA(", "USERCULTURE("]
            upper_clean = clean.upper()
            if any(f in upper_clean for f in forbidden):
                log_error(f"Security Alert: Calculated column detected with forbidden function (USERNAME/CUSTOMDATA): {clean}")

        if is_parameter:
            # 10. Safety check for "Variable = ..." syntax (Handles %, spaces, etc.)
            if "=" in clean and not clean.lstrip().upper().startswith("VAR "):
                parts = clean.split('=', 1)
                left_side = parts[0].strip()
                if '\n' not in left_side and len(left_side) < 100:
                    clean = parts[1].strip()

        # 11. Table AND Column Correction: 'WrongTable'[WrongCol] -> 'RightTable'[RightCol]
        if table_to_cols and "'" in clean:
            valid_tables = list(table_to_cols.keys())
            full_matches = re.finditer(r"'([^']+)'\[([^\]]+)\]", clean)
            for m in full_matches:
                ref_table = m.group(1)
                ref_col = m.group(2)
                real_table = ref_table
                if ref_table not in valid_tables:
                    for vt in valid_tables:
                        if vt.lower() == ref_table.lower() or vt.rstrip('s').lower() == ref_table.rstrip('s').lower():
                            real_table = vt
                            clean = clean.replace(f"'{ref_table}'[", f"'{vt}'[")
                            break
                if real_table in table_to_cols:
                    table_cols = table_to_cols[real_table]
                    if ref_col not in table_cols:
                        for vc in table_cols:
                            if vc.lower() == ref_col.lower() or vc.rstrip('s').lower() == ref_col.rstrip('s').lower():
                                clean = clean.replace(f"[{ref_col}]", f"[{vc}]")
                                break
            table_matches = re.finditer(r"'([^']+)'(?![\[])", clean)
            for m in table_matches:
                ref_table = m.group(1)
                if ref_table not in valid_tables:
                    for vt in valid_tables:
                        if vt.lower() == ref_table.lower() or vt.rstrip('s').lower() == ref_table.rstrip('s').lower():
                            clean = clean.replace(f"'{ref_table}'", f"'{vt}'")
                            break

        # 12. Strict Identifier Mapping: DEPARTMENT -> 'APPOINTMENTS'[Department]
        if display_to_qualified:
            matches = []
            tokens = re.finditer(r"\b([A-Z0-9_ -]+)\b", clean, re.IGNORECASE)
            for m in tokens:
                token = m.group(1)
                t_upper = token.upper()
                if t_upper in display_to_qualified:
                    start_idx = m.start()
                    if start_idx > 0 and clean[start_idx-1] == "'": continue
                    matches.append((start_idx, m.end(), display_to_qualified[t_upper]))
            
            matches.sort(key=lambda x: x[0], reverse=True)
            for start, end, replacement in matches:
                clean = clean[:start] + replacement + clean[end:]

        # 13. Scalar Context Enforcement (Measures Only)
        if is_measure:
            def wrap_raw_columns(formula_str: str) -> str:
                col_pattern = r"'([^']+)'\[([^\]]+)\]"
                safe_funcs = ["MAX", "MIN", "SUM", "AVERAGE", "COUNT", "DISTINCTCOUNT", "CALCULATE", "FILTER", "RELATED", "RELATEDTABLE", "FILTER", "KEEPFILTERS", "IF", "SWITCH", "HASONEVALUE", "ISFILTERED", "SELECTEDVALUE"]
                def repl(match):
                    full_ref = match.group(0)
                    start = match.start()
                    prefix = formula_str[:start].strip().upper()
                    if any(prefix.endswith(f + "(") for f in safe_funcs) or any(prefix.endswith(f + " (") for f in safe_funcs):
                        return full_ref
                    return f"MAX({full_ref})"
                return re.sub(col_pattern, repl, formula_str)
            clean = wrap_raw_columns(clean)

        return clean

    def _normalize_table_name(self, name: str) -> str:
        if not name:
            return ""
        name = name.replace("[", "").replace("]", "").replace('"', "").replace("`", "").strip().lower()
        parts = name.split(".")
        if len(parts) > 1:
            last = parts[-1].strip()
            if last in ['csv', 'xlsx', 'xls', 'txt', 'tsv', 'json']:
                return name
        return parts[-1].strip()

    def _find_target_connection(self, connection_info: Dict, table_def: Dict, t_name: str) -> Dict:
        all_conns = connection_info.get('connections', [])
        if not all_conns: return {}
        
        search_name = str(table_def.get('tableau_table_name') or table_def.get('table_name') or t_name).lower()
        
        schema_name_raw = table_def.get('schema_name', '')
        qualified_name = re.sub(r'[\[\]]', '', schema_name_raw).strip().lower() if schema_name_raw else ''
        
        # 1. Match by friendly_name
        for c in all_conns:
            if str(c.get('friendly_name', '')).lower() == search_name:
                return c
        
        # 2. Match by database filename
        for c in all_conns:
            db_path = str(c.get('database', '')).lower()
            if search_name in db_path:
                return c

        # 3. Match by friendly_name or database filename with extensions stripped
        def strip_ext(s: str) -> str:
            s = s.lower()
            for ext in ['.csv', '.xlsx', '.xls', '.txt', '.tsv', '.json']:
                if s.endswith(ext):
                    return s[:-len(ext)]
            return s
            
        search_name_clean = strip_ext(search_name)
        for c in all_conns:
            f_name = strip_ext(str(c.get('friendly_name', '')))
            db_name = strip_ext(str(c.get('database', '')))
            if f_name == search_name_clean or db_name == search_name_clean:
                return c

        # 4. Match by worksheets/tables list
        for c in all_conns:
            items = [str(w).lower() for w in (c.get('worksheets', []) + c.get('tables', []))]
            for item in items:
                if item == search_name or item.endswith('.' + search_name) or search_name.endswith('.' + item):
                    return c

        # 5. Match using schema_name against connection tables
        if qualified_name:
            for c in all_conns:
                items = [str(w).lower() for w in (c.get('tables', []) + c.get('worksheets', []))]
                for item in items:
                    if item == qualified_name or item.endswith('.' + qualified_name) or qualified_name.endswith('.' + item):
                        return c
        
        # 6. Match by file type indicators
        is_excel_table = "$" in schema_name_raw or "excel" in str(table_def.get('relation_type', '')).lower()
        is_csv_table = "#csv" in schema_name_raw or ".csv" in search_name
        
        if is_excel_table:
            for c in all_conns:
                c_type = str(c.get('type', '')).lower()
                if 'excel' in c_type or 'spreadsheet' in c_type:
                    return c
        elif is_csv_table:
            for c in all_conns:
                c_type = str(c.get('type', '')).lower()
                if 'text' in c_type or 'csv' in c_type or 'scan' in c_type:
                    return c
        
        return all_conns[0]

    def _build_datasource_map(self, api_data: Dict) -> Dict[str, Dict]:
        ds_map = {}
        default_info = {
            "type": None,
            "server": None, 
            "database": None,
            "schema": None
        }

        try:
            ds_raw = api_data.get('datasources', [])
            if isinstance(ds_raw, dict): 
                ds_list = ds_raw.get('datasources', [])
            else:
                ds_list = ds_raw

            for ds in ds_list:
                ds_id = ds.get('id')
                connections = ds.get('connections', [])
                info = default_info.copy()

                if connections:
                    conn = connections[0]
                    conn_type = conn.get('type') or ds.get('connection_type') or ''
                    conn_type = conn_type.lower()
                    
                    info['type'] = conn_type
                    
                    if conn.get('server'): 
                        info['server'] = conn.get('server')
                    
                    if conn.get('database'): 
                        info['database'] = conn.get('database')
                    
                    if conn.get('schema'): 
                        info['schema'] = conn.get('schema')
                    elif conn_type == 'snowflake':
                        ds_name = ds.get('name', '')
                        match = re.search(r'\(([^)]+)\)$', ds_name)
                        if match: 
                            info['schema'] = match.group(1)
                    
                    if conn.get('google_cloud_project_id'):
                        info['google_cloud_project_id'] = conn.get('google_cloud_project_id')
                    if conn.get('url'):
                        info['url'] = conn.get('url')

                raw_mode = ds.get('mode', '')
                info['mode'] = str(raw_mode).lower() if raw_mode else ''
                info['connections'] = connections

                if ds_id:
                    ds_map[str(ds_id)] = info
                
                ds_name = ds.get('name')
                if ds_name:
                    ds_map[str(ds_name)] = info

                # Table-specific connection mapping
                for conn in connections:
                    if not isinstance(conn, dict): continue
                    conn_type = (conn.get('type') or ds.get('connection_type') or '').lower()
                    conn_info = default_info.copy()
                    conn_info['type'] = conn_type
                    if conn.get('server'): conn_info['server'] = conn.get('server')
                    if conn.get('database'): conn_info['database'] = conn.get('database')
                    if conn.get('schema'): conn_info['schema'] = conn.get('schema')
                    elif conn_type == 'snowflake':
                        ds_name = ds.get('name', '')
                        match = re.search(r'\(([^)]+)\)$', ds_name)
                        if match: conn_info['schema'] = match.group(1)
                    
                    if conn.get('google_cloud_project_id'):
                        conn_info['google_cloud_project_id'] = conn.get('google_cloud_project_id')
                    if conn.get('url'):
                        conn_info['url'] = conn.get('url')
                    
                    conn_info['mode'] = info['mode']
                    conn_info['connections'] = [conn]

                    tables = conn.get('tables', [])
                    if not isinstance(tables, list): tables = []
                    worksheets = conn.get('worksheets', [])
                    if not isinstance(worksheets, list): worksheets = []
                    
                    for ref in (tables + worksheets):
                        if not isinstance(ref, str): continue
                        full_norm = ref.replace("[", "").replace("]", "").replace('"', "").replace("`", "").strip().lower()
                        suffix_norm = full_norm.split(".")[-1].strip()
                        
                        for norm in set([full_norm, suffix_norm]):
                            if not norm: continue
                            if ds_id:
                                ds_map[f"{ds_id}_{norm}"] = conn_info
                            if ds_name:
                                ds_map[f"{ds_name}_{norm}"] = conn_info

            # Dynamic lookup for tables from top-level api_data tables list if not already registered via worksheets/tables
            all_tables = api_data.get('tables', [])
            if not isinstance(all_tables, list): all_tables = []
            custom_sql = api_data.get('custom_sql', [])
            if not isinstance(custom_sql, list): custom_sql = []
            
            for tbl in (all_tables + custom_sql):
                if not isinstance(tbl, dict): continue
                ds_id = tbl.get('datasource_id') or tbl.get('datasource')
                if not ds_id: continue
                
                matching_ds = None
                for ds in ds_list:
                    if not isinstance(ds, dict): continue
                    if str(ds.get('id')) == str(ds_id) or str(ds.get('name')) == str(ds_id):
                        matching_ds = ds
                        break
                
                if not matching_ds: continue
                
                t_name = tbl.get('table_name') or tbl.get('tableau_table_name') or tbl.get('bi_table_name') or ''
                if not t_name: continue
                
                t_name_norm = self._normalize_table_name(t_name)
                specific_key = f"{ds_id}_{t_name_norm}"
                
                target_conn = self._find_target_connection(matching_ds, tbl, t_name)
                if target_conn and isinstance(target_conn, dict):
                    conn_type = (target_conn.get('type') or matching_ds.get('connection_type') or '').lower()
                    conn_info = default_info.copy()
                    conn_info['type'] = conn_type
                    if target_conn.get('server'): conn_info['server'] = target_conn.get('server')
                    if target_conn.get('database'): conn_info['database'] = target_conn.get('database')
                    if target_conn.get('schema'): conn_info['schema'] = target_conn.get('schema')
                    elif conn_type == 'snowflake':
                        ds_name = matching_ds.get('name', '')
                        match = re.search(r'\(([^)]+)\)$', ds_name)
                        if match: conn_info['schema'] = match.group(1)
                    
                    if target_conn.get('google_cloud_project_id'):
                        conn_info['google_cloud_project_id'] = target_conn.get('google_cloud_project_id')
                    if target_conn.get('url'):
                        conn_info['url'] = target_conn.get('url')
                    
                    conn_info['mode'] = info['mode']
                    conn_info['connections'] = [target_conn]
                    
                    for name_val in [tbl.get('tableau_table_name'), tbl.get('table_name'), tbl.get('bi_table_name')]:
                        if not name_val: continue
                        norm = self._normalize_table_name(name_val)
                        if not norm: continue
                        if ds_id:
                            ds_map[f"{ds_id}_{norm}"] = conn_info
                        ds_name = matching_ds.get('name')
                        if ds_name:
                            ds_map[f"{ds_name}_{norm}"] = conn_info
        except Exception as e:
            log_error(f"Error building datasource map: {e}")
        
        return ds_map

    def _identify_primary_key(self, table_name: str, columns: List[Dict]) -> Optional[str]:
        t_upper = table_name.upper()
        candidate_1 = f"{t_upper}_ID"
        candidate_2 = None
        if t_upper.endswith('S'):
            candidate_2 = f"{t_upper[:-1]}_ID"

        first_col_id = None
        for idx, col in enumerate(columns):
            c_name = col.get('name', '').upper()
            if c_name == candidate_1: return col.get('name')
            if candidate_2 and c_name == candidate_2: return col.get('name')
            if idx == 0 and c_name.endswith("ID"): first_col_id = col.get('name')

        if first_col_id: return first_col_id
        return None

    def _generate_relationships_tmdl(self, relationships: List[Dict], valid_tables: List[str]) -> str:
        lines = []
        seen_relationships: Set[Tuple[str, str, str, str]] = set()
        active_table_pairs: Set[Tuple[str, str]] = set()

        for rel in relationships:
            try:
                # New API format: relationship has nested 'power_bi' sub-object
                if 'power_bi' in rel and isinstance(rel['power_bi'], dict):
                    rel = rel['power_bi']
                
                t_from = self._sanitize_name(rel.get('fromTable'))
                c_from = self._sanitize_name(rel.get('fromColumn'))
                t_to = self._sanitize_name(rel.get('toTable'))
                c_to = self._sanitize_name(rel.get('toColumn'))
                
                if t_from not in valid_tables or t_to not in valid_tables:
                    continue

                # Normalize cardinality string
                raw_cardinality = rel.get('cardinality', 'ManyToOne')
                cardinality = raw_cardinality.lower().replace('-', '') 
                
                final_from_tbl, final_from_col = t_from, c_from
                final_to_tbl, final_to_col = t_to, c_to

                # Swap direction for OneToMany to match standard TMDL format
                if cardinality == 'onetomany':
                    final_from_tbl, final_to_tbl = t_to, t_from
                    final_from_col, final_to_col = c_to, c_from

                rel_key = (final_from_tbl, final_from_col, final_to_tbl, final_to_col)
                if rel_key in seen_relationships: continue
                seen_relationships.add(rel_key)

                # --- AMBIGUITY CHECK ---
                table_pair = tuple(sorted((final_from_tbl, final_to_tbl)))
                should_be_active = rel.get('isActive', True)

                if table_pair in active_table_pairs:
                    if should_be_active:
                        should_be_active = False
                elif should_be_active:
                    active_table_pairs.add(table_pair)

                rel_name = str(uuid.uuid4())
                lines.append(f"relationship {rel_name}")
                lines.append(f"    fromColumn: '{final_from_tbl}'.'{final_from_col}'")
                lines.append(f"    toColumn: '{final_to_tbl}'.'{final_to_col}'")
                
                # --- Explicit Cardinality Handling ---
                if cardinality == 'manytomany':
                    lines.append("    fromCardinality: many")
                    lines.append("    toCardinality: many")
                elif cardinality == 'onetoone':
                    lines.append("    fromCardinality: one")
                    lines.append("    toCardinality: one")
                elif cardinality == 'onetomany_explicit':
                    lines.append("    fromCardinality: one")

                if not should_be_active:
                    lines.append("    isActive: false")

                cross_filter = rel.get('crossFilterDirection', 'Single').lower()
                if cross_filter == 'both':
                     lines.append("    crossFilteringBehavior: bothDirections")
                
                lines.append("") 
            except Exception as e:
                log_error(f"Failed to parse relationship: {rel} | Error: {e}")

        return "\n".join(lines)

    def _generate_model_tmdl(self, table_names: List[str], relationships_tmdl: str = "", has_calc_groups: bool = False, is_direct_lake: bool = False, lakehouse_info: dict = None) -> str:
        lines = [
            "model Model",
            "    culture: en-US",
            "    defaultPowerBIDataSourceVersion: powerBI_V3",
            "    sourceQueryCulture: en-US",
        ]

        if has_calc_groups:
            lines.append("    discourageImplicitMeasures: true")
            lines.append("")

        lines.append("    dataAccessOptions")
        lines.append("        legacyRedirects")
        lines.append("        returnErrorValuesAsNull")
        lines.append("")

        lines.append("annotation __PBI_TimeIntelligenceEnabled = 1")
        if is_direct_lake and lakehouse_info:
            lh_name = lakehouse_info.get('name', 'Lakehouse')
            lines.append(f'annotation PBI_QueryOrder = ["DirectLake - {lh_name}"]')
            lines.append('annotation PBI_ProTooling = ["DirectLakeOnOneLakeInWeb","WebModelingEdit"]')
        else:
            lines.append("annotation PBIDesktopVersion = 2.138.1004.0 (24.11)")
        lines.append("")
        
        # Explicit references required by Fabric compiler
        # NOTE: In TMDL, "ref table" MUST have zero indentation (root level)!
        for t in table_names:
            lines.append(f"ref table '{t}'")
            
        lines.append("")
        lines.append("ref cultureInfo en-US")
        lines.append("")
        
        if relationships_tmdl:
            lines.append(relationships_tmdl)
            
        return "\n".join(lines)

    def _generate_local_date_table_tmdl(self, parent_table: str, col_name: str,
                                        local_table_id: str, rel_id: str,
                                        col_lineage_tag: str) -> str:
        """Generate a LocalDateTable TMDL string for a Date-type column."""
        local_table_name = f"LocalDateTable_{local_table_id}"
        table_lineage = str(uuid.uuid4())
        lines = [
            f"table '{local_table_name}'",
            "    isHidden",
            "    showAsVariationsOnly",
            f"    lineageTag: {table_lineage}",
            "",
            "    column Date",
            "        dataType: dateTime",
            "        isHidden",
            "        formatString: General Date",
            f"        lineageTag: {str(uuid.uuid4())}",
            "        dataCategory: PaddedDateTableDates",
            "        summarizeBy: none",
            "        isNameInferred",
            "        sourceColumn: [Date]",
            "",
            "        annotation SummarizationSetBy = User",
            "",
            "    column Year = YEAR([Date])",
            "        dataType: int64",
            "        isHidden",
            "        formatString: 0",
            f"        lineageTag: {str(uuid.uuid4())}",
            "        dataCategory: Years",
            "        summarizeBy: none",
            "",
            "        annotation SummarizationSetBy = User",
            "",
            "        annotation TemplateId = Year",
            "",
            "    column MonthNo = MONTH([Date])",
            "        dataType: int64",
            "        isHidden",
            "        formatString: 0",
            f"        lineageTag: {str(uuid.uuid4())}",
            "        dataCategory: MonthOfYear",
            "        summarizeBy: none",
            "",
            "        annotation SummarizationSetBy = User",
            "",
            "        annotation TemplateId = MonthNumber",
            "",
            '    column Month = FORMAT([Date], "MMMM")',
            "        dataType: string",
            "        isHidden",
            f"        lineageTag: {str(uuid.uuid4())}",
            "        dataCategory: Months",
            "        summarizeBy: none",
            "        sortByColumn: MonthNo",
            "",
            "        annotation SummarizationSetBy = User",
            "",
            "        annotation TemplateId = Month",
            "",
            "    column QuarterNo = INT(([MonthNo] + 2) / 3)",
            "        dataType: int64",
            "        isHidden",
            "        formatString: 0",
            f"        lineageTag: {str(uuid.uuid4())}",
            "        dataCategory: QuarterOfYear",
            "        summarizeBy: none",
            "",
            "        annotation SummarizationSetBy = User",
            "",
            "        annotation TemplateId = QuarterNumber",
            "",
            '    column Quarter = "Qtr " & [QuarterNo]',
            "        dataType: string",
            "        isHidden",
            f"        lineageTag: {str(uuid.uuid4())}",
            "        dataCategory: Quarters",
            "        summarizeBy: none",
            "        sortByColumn: QuarterNo",
            "",
            "        annotation SummarizationSetBy = User",
            "",
            "        annotation TemplateId = Quarter",
            "",
            "    column Day = DAY([Date])",
            "        dataType: int64",
            "        isHidden",
            "        formatString: 0",
            f"        lineageTag: {str(uuid.uuid4())}",
            "        dataCategory: DayOfMonth",
            "        summarizeBy: none",
            "",
            "        annotation SummarizationSetBy = User",
            "",
            "        annotation TemplateId = Day",
            "",
            "    hierarchy 'Date Hierarchy'",
            f"        lineageTag: {str(uuid.uuid4())}",
            "",
            "        level Year",
            f"            lineageTag: {str(uuid.uuid4())}",
            "            column: Year",
            "",
            "        level Quarter",
            f"            lineageTag: {str(uuid.uuid4())}",
            "            column: Quarter",
            "",
            "        level Month",
            f"            lineageTag: {str(uuid.uuid4())}",
            "            column: Month",
            "",
            "        level Day",
            f"            lineageTag: {str(uuid.uuid4())}",
            "            column: Day",
            "",
            "        annotation TemplateId = DateHierarchy",
            "",
            f"    partition {local_table_name} = calculated",
            "        mode: import",
            f"        source = Calendar(Date(Year(MIN('{parent_table}'['{col_name}'])), 1, 1), Date(Year(MAX('{parent_table}'['{col_name}'])), 12, 31))",
            "",
            "    annotation __PBI_LocalDateTable = true",
            "",
        ]
        return "\n".join(lines)

    def _generate_database_tmdl(self) -> str:
        return "database Database\n    compatibilityLevel: 1604\n"

    def _generate_table_tmdl(self, table_def: Dict, connection_info: Dict, 
                             extra_measures: List[Dict] = None, 
                             extra_calcs: List[Dict] = None,
                             lakehouse_info: Dict = None,
                             table_to_cols: Dict[str, Set[str]] = None,
                             display_to_qualified: Dict[str, str] = None,
                             is_direct_lake: bool = False) -> tuple:
        t_name = self._sanitize_name(table_def.get('table_name', 'Unknown'))
        columns = table_def.get('columns', [])
        
        table_lineage_tag = str(uuid.uuid4())
        lines = [f"table '{t_name}'"]
        lines.append(f"    lineageTag: {table_lineage_tag}")
        if is_direct_lake and lakehouse_info and lakehouse_info.get('schema'):
            lh_schema = lakehouse_info.get('schema')
            lines.append(f"    sourceLineageTag: [{lh_schema}].[{t_name}]")
        lines.append("")

        seen_columns: Set[str] = set()
        local_date_tables = []
        
        # --- PRE-PROCESS CALCS: Split into measures and columns ---
        actual_calcs = []
        extra_measures_from_calcs = []
        
        forbidden_funcs = ["USERNAME(", "USERPRINCIPALNAME(", "CUSTOMDATA(", "USERCULTURE("]
        
        if extra_calcs:
            for calc in extra_calcs:
                if not isinstance(calc, dict): continue
                c_type = calc.get('type', 'dimension').lower()
                
                # Check for forced measure status (forbidden functions for columns)
                raw_formula = (calc.get('dax_formula') or calc.get('powerbi_formula') or '').upper()
                is_forced_measure = any(f in raw_formula for f in forbidden_funcs)
                
                if c_type == 'measure' or is_forced_measure:
                    extra_measures_from_calcs.append(calc)
                else:
                    actual_calcs.append(calc)

        # Merge promoted measures from calcs into extra_measures
        if extra_measures_from_calcs:
            if extra_measures is None: extra_measures = []
            extra_measures.extend(extra_measures_from_calcs)
            log_info(f"Promoted {len(extra_measures_from_calcs)} calculated fields to Measures due to type/security for table '{t_name}'")

        # --- HELPER: Map JSON Type to Power Query M Type ---
        def get_m_type(json_type: str) -> str:
            jt = json_type.lower()
            if jt in ['integer', 'int', 'int64', 'whole number']: return 'Int64.Type'
            if jt in ['real', 'decimal', 'float', 'double', 'number', 'decimal number']: return 'type number'
            if jt in ['boolean', 'bool', 'true/false']: return 'type logical'
            if jt in ['date']: return 'type date'
            if jt in ['datetime', 'date/time']: return 'type datetime'
            return 'type text'

        # 1. Standard Columns
        m_transform_list = []
        
        for col in columns:
            c_name = self._sanitize_name(col.get('name', 'Unknown'))
            if c_name in seen_columns: continue
            seen_columns.add(c_name)

            raw_type = col.get('datatype', 'string')
            dtype = self._map_data_type(raw_type)
            is_date_col = (dtype == 'dateTime' and raw_type.lower() == 'date')
            # In DirectLake mode, source table columns are physical lakehouse
            # columns — ignore any dax_formula so they emit as sourceColumn.
            dax_formula = None if is_direct_lake else col.get('dax_formula')
            
            if is_date_col:
                col_lineage_tag = str(uuid.uuid4())
                local_table_id_full = str(uuid.uuid4())
                rel_id = str(uuid.uuid4())
                if not is_direct_lake:
                    local_date_tables.append({
                        'parent_table': t_name,
                        'col_name': c_name,
                        'local_table_id': local_table_id_full,
                        'rel_id': rel_id,
                        'col_lineage_tag': col_lineage_tag,
                    })
                
                if dax_formula:
                    formula = self._clean_dax(dax_formula, is_measure=False, table_to_cols=table_to_cols, display_to_qualified=display_to_qualified)
                    lines.append(f"    column '{c_name}' = {formula}")
                else:
                    lines.append(f"    column '{c_name}'")
                    lines.append(f"        sourceColumn: {c_name}")
                
                lines.append(f"        dataType: {dtype}")
                lines.append("        formatString: Long Date")
                lines.append(f"        lineageTag: {col_lineage_tag}")
                lines.append("        summarizeBy: none")
                lines.append("")
                
                if not is_direct_lake:
                    lines.append(f"        variation Variation")
                    lines.append("            isDefault")
                    lines.append(f"            relationship: {rel_id}")
                    lines.append(f"            defaultHierarchy: LocalDateTable_{local_table_id_full}.'Date Hierarchy'")
                    lines.append("")
                    lines.append("        changedProperty = DataType")
                    lines.append("")
                    lines.append("        annotation UnderlyingDateTimeDataType = Date")
                    lines.append("")
            elif dax_formula:
                formula = self._clean_dax(dax_formula, is_measure=False, table_to_cols=table_to_cols, display_to_qualified=display_to_qualified)
                lines.append(f"    column '{c_name}' =")
                lines.append(indent_block(formula, 8))
                lines.append(f"        dataType: {dtype}")
                lines.append(f"        summarizeBy: none")
                lines.append("")
            else:
                col_lt = str(uuid.uuid4())
                lines.append(f"    column '{c_name}'")
                lines.append(f"        dataType: {dtype}")
                if is_direct_lake:
                    if dtype == 'int64':
                        lines.append("        formatString: 0")
                    elif dtype == 'boolean':
                        lines.append('        formatString: """TRUE"";""TRUE"";""FALSE"""')
                lines.append(f"        lineageTag: {col_lt}")
                if is_direct_lake:
                    lines.append(f"        sourceLineageTag: {c_name}")
                lines.append(f"        summarizeBy: none")
                lines.append(f"        sourceColumn: {c_name}")
                if is_direct_lake:
                    lines.append("")
                    lines.append("        annotation SummarizationSetBy = Automatic")
                    if dtype == 'double':
                        lines.append("")
                        lines.append('        annotation PBI_FormatHint = {"isGeneralNumber":true}')
                lines.append("")

        # 2. Calculated Columns
        if actual_calcs:
            for calc in actual_calcs:
                c_name = self._sanitize_name(calc.get('name'))
                
                raw_formula = calc.get('dax_formula') or calc.get('powerbi_formula') or calc.get('powerbi', {}).get('dax') or calc.get('dax') or calc.get('power_bi', {}).get('dax')
                formula = self._clean_dax(raw_formula, is_measure=False, table_to_cols=table_to_cols, display_to_qualified=display_to_qualified)
                
                # If DAX has "ColumnName = Expression" pattern, extract the column name and expression
                if formula and '=' in formula and not formula.lstrip().upper().startswith('VAR '):
                    parts = formula.split('=', 1)
                    left_side = parts[0].strip()
                    right_side = parts[1].strip()

                    # Try to extract name from brackets first
                    match = re.match(r"(?:'?[^'\[\]\n]+'?)?\[([^\]]+)\]", left_side)
                    if match:
                        potential_name = match.group(1)
                    else:
                        potential_name = left_side

                    if potential_name and '\n' not in left_side and '(' not in left_side and not any(op in left_side for op in ('+', '-', '*', '/', '<', '>', '{', '}')):
                        c_name = potential_name
                        formula = right_side
                
                if c_name in seen_columns: continue
                seen_columns.add(c_name)
                
                if not formula and calc.get('base_field'):
                    base = calc.get('base_field')
                    formula = f"[{base}]"
                
                if not formula:
                    formula = "\"Unmapped\""

                raw_calc_type = calc.get('data_type') or calc.get('power_bi_datatype') or calc.get('bi_datatype') or calc.get('tableau_datatype') or 'string'
                calc_dtype = self._map_data_type(raw_calc_type)
                formula_single = ' '.join(formula.strip().split())
                is_date_col = (calc_dtype == 'dateTime' and raw_calc_type.lower() == 'date')
                
                # --- DirectLake: calculated columns are materialized in
                #     the lakehouse, so emit them as plain source columns ---
                if is_direct_lake:
                    calc_col_lt = str(uuid.uuid4())
                    lines.append(f"    column '{c_name}'")
                    lines.append(f"        dataType: {calc_dtype}")
                    if is_date_col:
                        lines.append("        formatString: Long Date")
                    elif calc_dtype == 'int64':
                        lines.append("        formatString: 0")
                    elif calc_dtype == 'boolean':
                        lines.append('        formatString: """TRUE"";""TRUE"";""FALSE"""')
                    lines.append(f"        lineageTag: {calc_col_lt}")
                    lines.append(f"        sourceLineageTag: {c_name}")
                    lines.append("        summarizeBy: none")
                    lines.append(f"        sourceColumn: {c_name}")
                    lines.append("")
                    lines.append("        annotation SummarizationSetBy = Automatic")
                    if calc_dtype == 'double':
                        lines.append("")
                        lines.append('        annotation PBI_FormatHint = {"isGeneralNumber":true}')
                    lines.append("")
                    continue

                if is_date_col:
                    if not is_direct_lake:
                        col_lineage_tag = str(uuid.uuid4())
                        local_table_id_full = str(uuid.uuid4())
                        rel_id = str(uuid.uuid4())
                        local_date_tables.append({
                            'parent_table': t_name,
                            'col_name': c_name,
                            'local_table_id': local_table_id_full,
                            'rel_id': rel_id,
                            'col_lineage_tag': col_lineage_tag,
                        })
                    lines.append(f"    column '{c_name}' = {formula_single}")
                    lines.append(f"        dataType: {calc_dtype}")
                    lines.append("        formatString: Long Date")
                    if not is_direct_lake:
                        lines.append(f"        lineageTag: {col_lineage_tag}")
                    lines.append("        summarizeBy: none")
                    lines.append("")
                    
                    if not is_direct_lake:
                        lines.append(f"        variation Variation")
                        lines.append("            isDefault")
                        lines.append(f"            relationship: {rel_id}")
                        lines.append(f"            defaultHierarchy: LocalDateTable_{local_table_id_full}.'Date Hierarchy'")
                        lines.append("")
                        lines.append("        changedProperty = DataType")
                        lines.append("")
                        lines.append("        annotation UnderlyingDateTimeDataType = Date")
                        lines.append("")
                else:
                    lines.append(f"    column '{c_name}' = {formula_single}")
                    lines.append(f"        dataType: {calc_dtype}")
                    if calc_dtype == 'dateTime':
                        lines.append("        formatString: Long Date")
                    lines.append("")
                    lines.append("        summarizeBy: none")
                    lines.append("") 

        # 3. Measures (before M Query)
        if extra_measures:
            for meas in extra_measures:
                m_name = self._sanitize_name(meas.get('name'))
                raw_dax = meas.get('dax_formula') or meas.get('powerbi_formula')
                dax = self._clean_dax(raw_dax, is_measure=True, table_to_cols=table_to_cols, display_to_qualified=display_to_qualified) or "BLANK()"
                
                lines.append(f"    measure '{m_name}' =")
                lines.append(indent_block(dax, 8))
                lines.append("")
                lines.append("")

        # 4. Partition (M Script or Entity)
        t_name_safe = self._sanitize_name(t_name)
        mode = connection_info.get('mode', '')
        
        if (mode == 'extract' or is_direct_lake) and lakehouse_info:
            lakehouse_name = lakehouse_info.get('name', 'Lakehouse')
            lh_schema = lakehouse_info.get('schema')
            
            lines.append(f"    partition '{t_name_safe}' = entity")
            lines.append(f"        mode: directLake")
            lines.append(f"        source")
            lines.append(f"            entityName: {t_name_safe}")
            if lh_schema:
                lines.append(f"            schemaName: {lh_schema}")
            else:
                # [DEBUG] Fallback if schema is still missing
                log_error(f"TMDL Partition Failure: No schema found for DirectLake table '{t_name_safe}'")
            lines.append(f"            expressionSource: 'DirectLake - {lakehouse_name}'")
            lines.append("")
        else:
            lines.append(f"    partition '{t_name}' = m")
            lines.append(f"        mode: import")
            lines.append(f"        source =")
        
        conn_type = connection_info.get('type', '').lower()
        server = connection_info.get('server') or "<Server not found>"
        database = connection_info.get('database') or "<Database not found>"
        raw_schema = connection_info.get('schema')
        # Fallback: extract schema from table-level schema_name (e.g. "[Warehouse].[StockGroups]")
        if not raw_schema:
            schema_name_field = table_def.get('schema_name', '')
            if schema_name_field:
                schema_match = re.match(r'\[?([^\]\[]+)\]?\.\[?([^\]\[]+)\]?', schema_name_field)
                if schema_match:
                    raw_schema = schema_match.group(1)
        custom_query = table_def.get('powerbi_query') or table_def.get('query')

        dedup_column = self._identify_primary_key(t_name, columns)

        m_lines = ["let"]
        final_step_name = ""
        
        # --- Connection Logic with Custom SQL Support ---
        if conn_type in ['postgresql', 'postgres']:
            m_lines.append(f"    Source = PostgreSQL.Database(\"{server}\", \"{database}\", [CommandTimeout=#duration(0, 0, 10, 0)]),")
            
            if custom_query:
                safe_query = re.sub(r'--.*', '', custom_query)
                safe_query = safe_query.replace('"', '""').replace('\n', ' ')
                m_lines.append(f"    Custom_SQL = Value.NativeQuery(Source, \"{safe_query}\", null, [EnableFolding=true]),")
                step_name = "Custom_SQL"
            else:
                step_name = f"public_{t_name}"
                if raw_schema:
                     nav_step = f"Source{{[Schema=\"{raw_schema}\",Item=\"{t_name}\"]}}[Data]"
                else:
                     nav_step = f"Table.SelectRows(Source, each Text.Lower([Item]) = \"{t_name.lower()}\")[Data]{{0}}"
                m_lines.append(f"    {step_name} = {nav_step},")

            if dedup_column:
                m_lines.append(f"    #\"Removed Duplicates\" = Table.Distinct({step_name}, {{\"{dedup_column}\"}}),")
                final_step_name = "#\"Removed Duplicates\""
            else:
                final_step_name = step_name

        elif conn_type == 'snowflake':
            schema = raw_schema if raw_schema else "PUBLIC"
            m_lines.append(f"    Source = Snowflake.Databases(\"{server}\", \"\"),")
            m_lines.append(f"    Database = Source{{[Name=\"{database}\",Kind=\"Database\"]}}[Data],")
            m_lines.append(f"    Schema = Database{{[Name=\"{schema}\",Kind=\"Schema\"]}}[Data],")
            
            if custom_query:
                safe_query = re.sub(r'--.*', '', custom_query)
                safe_query = safe_query.replace('"', '""').replace('\n', ' ')
                m_lines.append(f"    Custom_SQL = Value.NativeQuery(Schema, \"{safe_query}\", null, [EnableFolding=true]),")
                step_name = "Custom_SQL"
            else:
                m_lines.append(f"    Table = Schema{{[Name=\"{t_name}\",Kind=\"Table\"]}}[Data],")
                step_name = "Table"

            if dedup_column:
                m_lines.append(f"    #\"Removed Duplicates\" = Table.Distinct({step_name}, {{\"{dedup_column}\"}}),")
                final_step_name = "#\"Removed Duplicates\""
            else:
                final_step_name = step_name

        elif conn_type == 'redshift':
            # Append port if not present
            if server and ':' not in server:
                server = f"{server}:5439"
            
            m_lines.append(f"    Source = AmazonRedshift.Database(\"{server}\", \"{database}\", [ProviderName = null, BatchSize = null]),")
            
            if custom_query:
                safe_query = re.sub(r'--.*', '', custom_query)
                safe_query = safe_query.replace('"', '""').replace('\n', ' ')
                m_lines.append(f"    Custom_SQL = Value.NativeQuery(Source, \"{safe_query}\", null, [EnableFolding=true]),")
                step_name = "Custom_SQL"
            else:
                schema = raw_schema or 'public'
                m_lines.append(f"    #\"Navigation 1\" = Source{{[Name = \"{schema}\"]}}[Data],")
                m_lines.append(f"    #\"Navigation 2\" = #\"Navigation 1\"{{[Name = \"{t_name}\"]}}[Data],")
                step_name = "#\"Navigation 2\""

            if dedup_column:
                m_lines.append(f"    #\"Removed Duplicates\" = Table.Distinct({step_name}, {{\"{dedup_column}\"}}),")
                final_step_name = "#\"Removed Duplicates\""
            else:
                final_step_name = step_name

        elif conn_type in ['azure_sql', 'azuresql', 'sqlserver', 'azure_sqldb', 'sql.proxy', 'sqlproxy']:
            m_lines.append(f"    Source = Sql.Database(\"{server}\", \"{database}\"),")
            
            if custom_query:
                safe_query = re.sub(r'--.*', '', custom_query)
                safe_query = safe_query.replace('"', '""').replace('\n', ' ')
                m_lines.append(f"    Custom_SQL = Value.NativeQuery(Source, \"{safe_query}\", null, [EnableFolding=true]),")
                step_name = "Custom_SQL"
            else:
                step_name = f"dbo_{t_name}"
                if raw_schema:
                     nav_step = f"Source{{[Schema=\"{raw_schema}\",Item=\"{t_name}\"]}}[Data]"
                else:
                     nav_step = f"Table.SelectRows(Source, each Text.Lower([Item]) = \"{t_name.lower()}\")[Data]{{0}}"
                m_lines.append(f"    {step_name} = {nav_step},")

            final_step_name = step_name

        elif conn_type == 'oracle':
            oracle_server = connection_info.get('friendly_name') or server
            schema = raw_schema if raw_schema else "ADMIN"
            oracle_service = connection_info.get('service') or ''
            oracle_port = connection_info.get('port') or '1521'
            
            if oracle_service:
                # Oracle Local: service is present, use direct query with server:port/service
                oracle_conn_str = f"{oracle_server}:{oracle_port}/{oracle_service}"
                if custom_query:
                    safe_query = re.sub(r'--.*', '', custom_query)
                    safe_query = safe_query.replace('"', '""').replace('\n', ' ')
                    m_lines.append(f"    Source = Oracle.Database(\"{oracle_conn_str}\", [Query = \"{safe_query}\"]),")
                else:
                    m_lines.append(f"    Source = Oracle.Database(\"{oracle_conn_str}\", [Query = \"SELECT * FROM {schema}.{t_name}\"]),")
                step_name = "Source"
            else:
                # Oracle Cloud: no service, use HierarchicalNavigation
                m_lines.append(f"    Source = Oracle.Database(\"{oracle_server}\", [HierarchicalNavigation = true]),")
                if custom_query:
                    safe_query = re.sub(r'--.*', '', custom_query)
                    safe_query = safe_query.replace('"', '""').replace('\n', ' ')
                    m_lines.append(f"    Custom_SQL = Value.NativeQuery(Source, \"{safe_query}\", null, [EnableFolding=true]),")
                    step_name = "Custom_SQL"
                else:
                    m_lines.append(f"    #\"Navigation 1\" = Source{{[Schema = \"{schema}\"]}}[Data],")
                    m_lines.append(f"    #\"Navigation 2\" = #\"Navigation 1\"{{[Name = \"{t_name}\"]}}[Data],")
                    step_name = "#\"Navigation 2\""

            if dedup_column:
                m_lines.append(f"    #\"Removed Duplicates\" = Table.Distinct({step_name}, {{\"{dedup_column}\"}}),")
                final_step_name = "#\"Removed Duplicates\""
            else:
                final_step_name = step_name

        else:
            m_lines.append(f"    // Error: Unsupported connection type detected: '{conn_type}'")
            m_lines.append(f"    Source = error \"Unsupported connection type: '{conn_type}'\",")
            final_step_name = "Source"

        if m_lines and m_lines[-1].endswith(","):
             m_lines[-1] = m_lines[-1].rstrip(',')

        m_lines.append("in")
        m_lines.append(f"    {final_step_name}")

        m_script = "\n".join(m_lines)
        lines.append(indent_block(m_script, spaces=12))
        lines.append("")

        return "\n".join(lines), local_date_tables

    def _generate_calculation_group_tmdl(self, group_name: str, calc_items: List[Dict]) -> str:
        """Generates a TMDL calculation group table from Calculation Item sets."""
        lines = [f"table '{group_name}'"]
        lines.append("")
        lines.append("    calculationGroup")
        lines.append("")

        for item in calc_items:
            item_name = self._sanitize_name(item.get('name', 'Unknown'))
            raw_dax = item.get('dax_formula') or item.get('powerbi', {}).get('dax') or item.get('powerbi_formula')
            dax = self._clean_dax(raw_dax, is_measure=True) if raw_dax else 'SELECTEDMEASURE()'

            lines.append(f"        calculationItem '{item_name}' =")
            lines.append(indent_block(dax, 12))
            lines.append("")

        # Name column
        lines.append(f"    column '{group_name}'")
        lines.append("        dataType: string")
        lines.append("        sourceColumn: Name")
        lines.append("        summarizeBy: none")
        lines.append("        sortByColumn: Ordinal")
        lines.append("")
        lines.append("        annotation SummarizationSetBy = Automatic")
        lines.append("")

        # Ordinal column
        lines.append("    column Ordinal")
        lines.append("        dataType: int64")
        lines.append("        formatString: 0")
        lines.append("        summarizeBy: sum")
        lines.append("        sourceColumn: Ordinal")
        lines.append("")
        lines.append("        annotation SummarizationSetBy = Automatic")
        lines.append("")

        return "\n".join(lines)

    async def generate_tmdl_structure(self, api_data: Dict, folder_path: str, app_name: str, is_direct_lake: bool = False):
        failed_files = []
        created_files = []

        try:
            if isinstance(api_data, list): api_data = api_data[0]
            
            datasource_map = self._build_datasource_map(api_data)
            
            # Extract lakehouse info for DirectLake
            lakehouse_info = None
            dl_results = api_data.get('data_layer_results', {})
            if isinstance(dl_results, dict):
                dl_payload = dl_results.get('payload', {})
                if not isinstance(dl_payload, dict):
                    dl_payload = dl_results
                
                proc_summary = dl_payload.get('processing_summary', [])
                if proc_summary and len(proc_summary) > 0:
                    summary_item = proc_summary[0]
                    lakehouse_info = summary_item.get('fabric_artifacts', {}).get('lakehouse', {}) or {}
                    
                    # Also extract lakehouse_schema from the processing summary root
                    if isinstance(lakehouse_info, dict):
                        lakehouse_info['schema'] = summary_item.get('lakehouse_schema')
                        log_info(f"[TMDL] Resolved lakehouse schema from summary: {lakehouse_info.get('schema')}")
            
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

            # Register calculation group table name so model.tmdl includes ref table
            calc_group_name = None
            if calc_item_sets:
                calc_group_name = "CalculationGroup"
                valid_table_names.append(calc_group_name)

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
            
            # Distribute dynamic sets as calculated columns
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

            table_to_cols: Dict[str, Set[str]] = {}
            display_to_qualified: Dict[str, str] = {}
            for tbl in all_tables:
                t_nm = self._sanitize_name(tbl['table_name'])
                table_to_cols[t_nm] = {self._sanitize_name(c.get('name')) for c in tbl.get('columns', []) if c.get('name')}
                
                # PK identification for CTD
                pk = self._identify_primary_key(t_nm, tbl.get('columns', []))
                for col in tbl.get('columns', []):
                    c_bi = self._sanitize_name(col.get('name'))
                    c_tb = str(col.get('tableau_name', col.get('name'))).upper()
                    if c_bi:
                        display_to_qualified[c_bi.upper()] = f"'{t_nm}'[{c_bi}]"
                        if pk and c_bi == pk:
                             display_to_qualified["CTD"] = f"'{t_nm}'[{pk}]"
                    if c_tb:
                        display_to_qualified[c_tb] = f"'{t_nm}'[{c_bi}]"

            # 3. Generate per-table TMDL files
            tables_created = 0
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
                
                # Append LOD expressions as DAX measures for this table (only if they are measures)
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


                tmd_content, ldt_list = self._generate_table_tmdl(tbl, table_conn_info, tbl_measures, tbl_specific_calcs, lakehouse_info, table_to_cols, display_to_qualified, is_direct_lake=is_direct_lake)
                table_path = f"{semantic_def_path}/tables/{t_name}.tmdl"
                table_ok = await self.file_agent.create_or_update_file(table_path, tmd_content, f"Create table {t_name}")
                if not table_ok:
                    failed_files.append(table_path)
                    log_error(f"Failed to push table file: {table_path}")
                    return {"success": False, "message": f"Failed to create table file: {t_name}.tmdl", "failed_files": failed_files}
                created_files.append(table_path)

                # Generate Local Date Tables
                for ldt in ldt_list:
                    ldt_content = self._generate_local_date_table_tmdl(
                        ldt['parent_table'], ldt['col_name'], 
                        ldt['local_table_id'], ldt['rel_id'], ldt['col_lineage_tag']
                    )
                    ldt_name = f"LocalDateTable_{ldt['local_table_id']}"
                    ldt_path = f"{semantic_def_path}/tables/{ldt_name}.tmdl"
                    await self.file_agent.create_or_update_file(ldt_path, ldt_content, f"Create LocalDateTable for {ldt['col_name']}")
                    created_files.append(ldt_path)
                    valid_table_names.append(ldt_name)
                    
                    # Add relationship
                    relationships.append({
                        "fromTable": ldt['parent_table'],
                        "fromColumn": ldt['col_name'],
                        "toTable": ldt_name,
                        "toColumn": "Date",
                        "cardinality": "ManyToOne",
                        "_override_rel_id": ldt['rel_id'],
                        "joinOnDateBehavior": "datePartOnly"
                    })

                tables_created += 1

            # Build the Model file after all tables and relationships are collected
            rels_tmdl = ""
            if relationships:
                rels_tmdl = self._generate_relationships_tmdl(relationships, valid_table_names)
            
            model_content = self._generate_model_tmdl(valid_table_names, rels_tmdl, has_calc_groups=bool(calc_item_sets), is_direct_lake=is_direct_lake, lakehouse_info=lakehouse_info)
            model_path = f"{semantic_def_path}/model.tmdl"
            model_ok = await self.file_agent.create_or_update_file(model_path, model_content, "Init model")
            if not model_ok:
                failed_files.append(model_path)
                log_error(f"Failed to push required file: {model_path}")
                return {"success": False, "message": f"Failed to create file: model.tmdl", "failed_files": failed_files}
            created_files.append(model_path)

            # 4. Generate Calculation Group table (if any Calculation Item sets exist)
            if calc_group_name and calc_item_sets:
                cg_content = self._generate_calculation_group_tmdl(calc_group_name, calc_item_sets)
                cg_path = f"{semantic_def_path}/tables/{calc_group_name}.tmdl"
                cg_ok = await self.file_agent.create_or_update_file(cg_path, cg_content, f"Create calculation group {calc_group_name}")
                if not cg_ok:
                    failed_files.append(cg_path)
                    log_error(f"Failed to push calculation group file: {cg_path}")
                    return {"success": False, "message": f"Failed to create calculation group: {calc_group_name}.tmdl", "failed_files": failed_files}
                created_files.append(cg_path)
                log_info(f"Created calculation group table '{calc_group_name}' with {len(calc_item_sets)} calculation items")

            # Validation: ensure at least one table was created when tables exist in the data
            if all_tables and tables_created == 0:
                msg = "No table .tmdl files were created despite tables being present in the source data"
                log_error(msg)
                return {"success": False, "message": msg, "failed_files": failed_files}
            
            log_info(f"Generated TMDL for {tables_created} tables and relationships. All {len(created_files)} files pushed successfully.")
            return {"success": True, "message": "TMDL Generated", "created_files": created_files}

        except Exception as e:
            log_error(f"TMDL Gen failed: {e}")
            return {"success": False, "message": str(e), "failed_files": failed_files}

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
            source_column_str = "[Value]"
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
            harvest_measure_block = f"""    measure '{harvest_name}'
        expression: SELECTEDVALUE('{name}'[{ref_column}], {default_val})
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
                        measure_blocks.append(f"""    measure '{measure_name}' =
        {indent_block(cleaned_meas_dax, 8).strip()}""")
                        log_info(f"[add_parameters] Using '{measure_name}' as measure for parameter '{name}'")

            # Add measures from top-level measures
            for m in mapped_measures:
                measure_name = self._sanitize_name(m.get("name"))
                if measure_name != harvest_name:
                    if not any(self._sanitize_name(tc.get("name")) == measure_name for tc in targeted_calcs):
                        raw_meas_dax = m.get("dax_formula") or m.get("powerbi_formula") or "1"
                        cleaned_meas_dax = self._clean_dax(raw_meas_dax)
                        measure_blocks.append(f"""    measure '{measure_name}' =
        {indent_block(cleaned_meas_dax, 8).strip()}""")
                        log_info(f"[add_parameters] Using '{measure_name}' as top-level measure for parameter '{name}'")

            measure_block = "\n\n".join(measure_blocks)

            tmdl = f"""table '{name}'

    column {ref_column}
        formatString: {format_str}
        summarizeBy: {summarize_by}
        isNameInferred
        sourceColumn: {source_column_str}
        annotation SummarizationSetBy = Automatic

    partition '{name}' = calculated
        mode: import
        source = {dax}

{measure_block}

    annotation PBI_Id = {str(uuid.uuid4()).replace("-", "")}
"""
            file_path = f"{def_path}/{name}.tmdl"
            ok = await self.file_agent.create_or_update_file(
                file_path,
                tmdl,
                f"Added parameter: {name}"
            )
            if not ok:
                failed_files.append(file_path)
                log_error(f"Failed to push parameter file: {file_path}")
                return {"success": False, "message": f"Failed to create parameter file: {name}.tmdl", "failed_files": failed_files}

        log_info(f"Added {len(parameters)} parameters")
        return {"success": True, "message": f"Added {len(parameters)} parameters"}
