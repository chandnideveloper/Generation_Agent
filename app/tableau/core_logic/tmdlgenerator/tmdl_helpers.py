import uuid
import json
import re
from typing import Dict, List, Optional, Set, Tuple
from app.tableau.core.logging_utils import log_info, log_error
from app.tableau.utils.path_utils import indent_block
from app.tableau.services.action_logger import ActionLogger
from .format_mappings import get_pbi_format_for_currency


class TmdlHelperMixin:
    def _find_target_connection(self, connection_info: Dict, table_def: Dict, t_name: str) -> Dict:
        all_conns = connection_info.get('connections', [])
        if not all_conns: return {}
        
        search_name = str(table_def.get('tableau_table_name') or table_def.get('table_name') or t_name).lower()
        
        # Build a cleaned qualified name from schema_name for deeper matching.
        schema_name_raw = table_def.get('schema_name', '')
        qualified_name = re.sub(r'[\[\]]', '', schema_name_raw).strip().lower() if schema_name_raw else ''
        
        # 1. Match by friendly_name
        for c in all_conns:
            if str(c.get('friendly_name', '')).lower() == search_name:
                return c
        
        # 2. Match by database filename (search for it in the path)
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

        # 4. Match by worksheets/tables list (exact or qualified-name suffix match)
        for c in all_conns:
            items = [str(w).lower() for w in (c.get('worksheets', []) + c.get('tables', []))]
            for item in items:
                if item == search_name or item.endswith('.' + search_name) or search_name.endswith('.' + item):
                    return c

        # 5. Match using the full qualified schema_name against connection tables
        if qualified_name:
            for c in all_conns:
                items = [str(w).lower() for w in (c.get('tables', []) + c.get('worksheets', []))]
                for item in items:
                    if item == qualified_name or item.endswith('.' + qualified_name) or qualified_name.endswith('.' + item):
                        return c
        
        # 6. Match by file type indicators (Excel vs CSV/Text)
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
        
        # 7. Fallback to first
        return all_conns[0]

    def _find_matching_worksheet(self, worksheets: List[str], table_def: Dict, t_name: str) -> Optional[str]:
        if not worksheets:
            return None
            
        # Build candidates to match worksheet names
        candidates = []
        for key in ['tableau_table_name', 'table_name']:
            val = table_def.get(key)
            if val:
                candidates.append(str(val))
        candidates.append(t_name)
        schema_name = table_def.get('schema_name', '')
        if schema_name:
            candidates.append(schema_name)
            
        def clean_name(s: str) -> str:
            if not s: return ""
            return re.sub(r"[\[\]\$'\"]", "", s).strip().lower()
            
        cleaned_worksheets = {clean_name(str(w)): str(w) for w in worksheets if w}
        
        # 1. Look for exact cleaned match
        for cand in candidates:
            cleaned_cand = clean_name(cand)
            if cleaned_cand in cleaned_worksheets:
                return cleaned_worksheets[cleaned_cand]
                
        # 2. Look for substring match
        for cand in candidates:
            cleaned_cand = clean_name(cand)
            if not cleaned_cand: continue
            for cw, orig_w in cleaned_worksheets.items():
                if cleaned_cand in cw or cw in cleaned_cand:
                    return orig_w
                    
        # 3. Fallback to the first worksheet
        return worksheets[0]

    def _clean_dax(self, formula: str, is_parameter: bool = False) -> str:
        if not formula: return ""
        clean = re.sub(r'```[a-zA-Z]*', '', formula).replace('```', '').strip()
        while clean.startswith('='): clean = clean[1:].strip()
        clean = re.sub(r'(?i)ISNULL\s*\(', 'ISBLANK(', clean)
        clean = re.sub(r'(?i)SUM\s*\(\s*INT\s*\(\s*(\[[^\]]+\])\s*\)\s*\)', r'SUM(\1)', clean)
        clean = re.sub(r'(?i)COUNT\s*\(\s*INT\s*\(\s*(\[[^\]]+\])\s*\)\s*\)', r'COUNT(\1)', clean)
        if is_parameter and "=" in clean and not clean.lstrip().upper().startswith("VAR "):
            parts = clean.split('=', 1)
            if '\n' not in parts[0] and len(parts[0]) < 100: clean = parts[1].strip()
        return clean

    def _generate_local_date_table_tmdl(self, parent_table: str, col_name: str,
                                        local_table_id: str, rel_id: str,
                                        col_lineage_tag: str) -> str:
        """Generate a LocalDateTable TMDL string for a Date-type calculated column."""
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
            f"        source = Calendar(Date(Year(MIN('{self._escape_tmdl_identifier(parent_table)}'[{self._escape_tmdl_identifier(col_name)}])), 1, 1), Date(Year(MAX('{self._escape_tmdl_identifier(parent_table)}'[{self._escape_tmdl_identifier(col_name)}])), 12, 31))",
            "",
            "    annotation __PBI_LocalDateTable = true",
            "",
        ]
        return "\n".join(lines)

    def _generate_table_tmdl(self, table_def: Dict, connection_info: Dict,
                             extra_measures: List[Dict] = None,
                             extra_calcs: List[Dict] = None,
                             lakehouse_info: Dict = None,
                             is_direct_lake: bool = False,
                             direct_lake_schema: str = None) -> tuple:
        t_name = self._sanitize_name(table_def.get('table_name', 'Unknown'))
        columns = table_def.get('columns', [])
        table_lineage_tag = str(uuid.uuid4())
        lines = [f"table '{self._escape_tmdl_identifier(t_name)}'"]
        lines.append(f"    lineageTag: {table_lineage_tag}")
        if is_direct_lake and direct_lake_schema:
            lines.append(f"    sourceLineageTag: [{direct_lake_schema}].[{t_name}]")
        lines.append("")

        seen_columns = set()
        local_date_tables = []

        def get_m_type(json_type: str) -> str:
            jt = json_type.lower()
            if jt in ['integer', 'int', 'int64', 'whole number']: return 'Int64.Type'
            if jt in ['real', 'decimal', 'float', 'double', 'number', 'decimal number']: return 'type number'
            if jt in ['boolean', 'bool', 'true/false']: return 'type logical'
            if jt in ['date']: return 'type date'
            if jt in ['datetime', 'date/time']: return 'type datetime'
            return 'type text'

        rename_mappings = []
        for col in columns:
            c_name = self._sanitize_name(col.get('name', 'Unknown'))
            raw_name = col.get('tableau_column_name') or col.get('original_column_name')
            if raw_name and c_name and raw_name != c_name:
                rename_mappings.append((raw_name, c_name))
            if c_name in seen_columns: continue
            seen_columns.add(c_name)
            raw_type = col.get('datatype', col.get('bi_datatype', 'string'))
            dtype = self._map_data_type(raw_type)
            is_date_col = (dtype == 'dateTime')
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
                    formula = self._clean_dax(dax_formula)
                    lines.append(f"    column '{self._escape_tmdl_identifier(c_name)}' = {formula}")
                else:
                    lines.append(f"    column '{self._escape_tmdl_identifier(c_name)}'")
                    lines.append(f"        sourceColumn: {c_name}")
                
                lines.append(f"        dataType: {dtype}")
                rt_lower = str(raw_type).lower()
                format_str = "General Date" if 'time' in rt_lower or 'datetime' in rt_lower else "Long Date"
                lines.append(f"        formatString: {format_str}")
                lines.append(f"        lineageTag: {col_lineage_tag}")
                if is_direct_lake:
                    lines.append(f"        sourceLineageTag: {c_name}")
                lines.append("        summarizeBy: none")
                lines.append("")

                if is_direct_lake:
                    lines.append(f"        annotation SummarizationSetBy = Automatic")
                    lines.append("")
                else:
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
                formula = self._clean_dax(dax_formula)
                lines.extend([f"    column '{self._escape_tmdl_identifier(c_name)}' =", indent_block(formula, 8), f"        dataType: {dtype}", "        summarizeBy: none", ""])
            else:
                # Standard source column
                col_lt = str(uuid.uuid4())
                lines.append(f"    column '{self._escape_tmdl_identifier(c_name)}'")
                lines.append(f"        dataType: {dtype}")
                if is_direct_lake:
                    # Add formatString for int64 and boolean in DirectLake
                    if dtype == 'int64':
                        lines.append("        formatString: 0")
                    elif dtype == 'boolean':
                        lines.append('        formatString: """TRUE"";""TRUE"";""FALSE"""')
                lines.append(f"        lineageTag: {col_lt}")
                if is_direct_lake:
                    lines.append(f"        sourceLineageTag: {c_name}")
                lines.append("        summarizeBy: none")
                lines.append(f"        sourceColumn: {c_name}")
                if is_direct_lake:
                    lines.append("")
                    lines.append("        annotation SummarizationSetBy = Automatic")
                    if dtype == 'double':
                        lines.append("")
                        lines.append('        annotation PBI_FormatHint = {"isGeneralNumber":true}')
                lines.append("")

        # Deduplicate rename pairs while preserving order.
        deduped_rename_mappings = []
        seen_rename_pairs = set()
        for raw_name, bi_name in rename_mappings:
            key = (str(raw_name), str(bi_name))
            if key in seen_rename_pairs:
                continue
            seen_rename_pairs.add(key)
            deduped_rename_mappings.append((raw_name, bi_name))

        actual_calcs = []
        extra_measures_from_calcs = []
        forbidden_funcs = ["USERNAME(", "USERPRINCIPALNAME(", "CUSTOMDATA(", "USERCULTURE("]
        if extra_calcs:
            for calc in extra_calcs:
                if not isinstance(calc, dict): continue
                raw_formula = (calc.get('dax_formula') or calc.get('powerbi_formula') or '').upper()
                if calc.get('type', 'dimension').lower() == 'measure' or any(f in raw_formula for f in forbidden_funcs):
                    extra_measures_from_calcs.append(calc)
                else: actual_calcs.append(calc)

        if extra_measures_from_calcs:
            if extra_measures is None: extra_measures = []
            extra_measures.extend(extra_measures_from_calcs)

        if actual_calcs:
            for calc in actual_calcs:
                c_name = self._sanitize_name(calc.get('name'))
                raw_formula = calc.get('dax_formula') or calc.get('powerbi_formula') or calc.get('dax') or calc.get('powerbi', {}).get('dax')
                formula = self._clean_dax(raw_formula)
                if formula and '=' in formula and not formula.lstrip().upper().startswith('VAR '):
                    parts = formula.split('=', 1)
                    left_side = parts[0].strip()
                    # If DAX has "ColumnName = Expression" pattern, extract
                    # Try bracket match first: 'Table'[Col] or [Col]
                    match = re.match(r"(?:'?[^'\[\]\n]+'?)?\[([^\]]+)\]", left_side)
                    if match:
                        potential_name = match.group(1)
                    else:
                        # Simple identifier like col_name = ...
                        potential_name = left_side

                    if potential_name and '\n' not in left_side and '(' not in left_side and not any(c in left_side for c in ('+', '-', '*', '/', '<', '>', '{', '}')):
                        c_name = potential_name
                        formula = parts[1].strip()

                if c_name in seen_columns: continue
                seen_columns.add(c_name)
                if not formula and calc.get('base_field'): formula = f"[{calc.get('base_field')}]"
                if not formula: formula = "\"Unmapped\""
                raw_calc_type = calc.get('data_type') or calc.get('power_bi_datatype') or calc.get('bi_datatype') or calc.get('tableau_datatype') or 'string'
                calc_dtype = self._map_data_type(raw_calc_type)
                formula_single = ' '.join(formula.strip().split())
                is_date_col = (calc_dtype == 'dateTime')

                # --- DirectLake: calculated columns are materialized in
                #     the lakehouse, so emit them as plain source columns ---
                if is_direct_lake:
                    calc_col_lt = str(uuid.uuid4())
                    lines.append(f"    column '{self._escape_tmdl_identifier(c_name)}'")
                    lines.append(f"        dataType: {calc_dtype}")
                    if is_date_col:
                        rct_lower = str(raw_calc_type).lower()
                        format_str = "General Date" if 'time' in rct_lower or 'datetime' in rct_lower else "Long Date"
                        lines.append(f"        formatString: {format_str}")
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
                        local_table_id = str(uuid.uuid4()).replace('-', '')[:32]
                        local_table_id_full = str(uuid.uuid4())
                        rel_id = str(uuid.uuid4())
                        local_date_tables.append({
                            'parent_table': t_name,
                            'col_name': c_name,
                            'local_table_id': local_table_id_full,
                            'rel_id': rel_id,
                            'col_lineage_tag': col_lineage_tag,
                        })
                    lines.append(f"    column '{self._escape_tmdl_identifier(c_name)}' = {formula_single}")
                    lines.append(f"        dataType: {calc_dtype}")
                    rct_lower = str(raw_calc_type).lower()
                    format_str = "General Date" if 'time' in rct_lower or 'datetime' in rct_lower else "Long Date"
                    lines.append(f"        formatString: {format_str}")
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
                    lines.append(f"    column '{self._escape_tmdl_identifier(c_name)}' = {formula_single}")
                    lines.append(f"        dataType: {calc_dtype}")
                    if calc_dtype == 'dateTime':
                        rct_lower = str(raw_calc_type).lower()
                        format_str = "General Date" if 'time' in rct_lower or 'datetime' in rct_lower else "Long Date"
                        lines.append(f"        formatString: {format_str}")
                    lines.extend(["", "        summarizeBy: none", ""])

        if extra_measures:
            for meas in extra_measures:
                m_name = self._sanitize_name(meas.get('name'))
                raw_dax = meas.get('dax_formula') or meas.get('powerbi_formula')
                dax = self._clean_dax(raw_dax) or "BLANK()"
                
                lines.append(f"    measure '{self._escape_tmdl_identifier(m_name)}' =")
                lines.append(indent_block(dax, 8))
                
                # Apply localized currency formatting if present
                fmt_obj = meas.get("format", {})
                if isinstance(fmt_obj, dict):
                    bi_fmt = fmt_obj.get("bi_format", {})
                    if bi_fmt and bi_fmt.get("format_style") == "currency":
                        symbol = bi_fmt.get("currency_symbol")
                        fmt_info = get_pbi_format_for_currency(symbol)
                        lines.append(f"        formatString: {fmt_info['formatString']}")
                        lines.append(f'        annotation PBI_FormatHint = {{"currencyCulture":"{fmt_info["culture"]}"}}')
                
                lines.append("")
                lines.append("")

        # 4. Partition (M Script or DirectLake)
        if is_direct_lake and lakehouse_info:
            lh_name = lakehouse_info.get('name', 'Lakehouse')
            lines.extend([
                f"    partition '{self._escape_tmdl_identifier(t_name)}' = entity",
                "        mode: directLake",
                "        source"
            ])
            lines.append(f"            entityName: {self._escape_tmdl_identifier(t_name)}")
            if direct_lake_schema:
                lines.append(f"            schemaName: {direct_lake_schema}")
            lines.append(f"            expressionSource: 'DirectLake - {lh_name}'")
            lines.append("")
        elif connection_info.get('mode', '').lower() == 'extract' and lakehouse_info and is_direct_lake: # Keep this check strict
            lh_name = lakehouse_info.get('name', 'Lakehouse')
            lines.extend([
                f"    partition '{self._escape_tmdl_identifier(t_name)}' = m",
                "        mode: import",
                "        source =",
                indent_block(f"            let\n                Source = #\"DirectLake - {lh_name}\",\n                #\"Navigate to Tables\" = Source{{[Name=\"Tables\"]}}[Content],\n                #\"Navigate to {t_name}\" = #\"Navigate to Tables\"{{[Name=\"{t_name}\"]}}[Content],\n                #\"Filter Parquet\" = Table.SelectRows(#\"Navigate to {t_name}\", each [Extension] = \".parquet\"),\n                #\"Get Content\" = #\"Filter Parquet\"{{0}}[Content],\n                #\"Imported Table\" = Parquet.Document(#\"Get Content\")\n            in\n                #\"Imported Table\"", 12),
                ""
            ])
        else:
            lines.extend([f"    partition '{self._escape_tmdl_identifier(t_name)}' = m", "        mode: import", "        source ="])
            m_query_metadata = table_def.get('m_query')
            
            if isinstance(m_query_metadata, list) and len(m_query_metadata) > 0:
                # Use pre-defined M query steps. Ensure commas are present between steps.
                m_lines = []
                for i, step in enumerate(m_query_metadata):
                    content = step.get('content', '').strip()
                    if not content: continue
                    # Add a comma if it's not the last step and doesn't have one, and doesn't contain the 'in' block
                    if i < len(m_query_metadata) - 1:
                        if not content.endswith(",") and " in " not in content:
                            content += ","
                    m_lines.append(content)
                m_script = "\n".join(m_lines)
                
                if deduped_rename_mappings:
                    mapping_str = "{" + ", ".join([f'{{"{r}", "{b}"}}' for r, b in deduped_rename_mappings]) + "}"
                    match = re.search(r'\s+in\s+([a-zA-Z0-9_#" ]+?)\s*$', m_script)
                    if match:
                        final_step = match.group(1).strip()
                        m_script = m_script[:match.start()]
                        if not m_script.strip().endswith(','):
                            m_script += ","
                        m_script += f"\n                #\"Renamed Columns\" = Table.RenameColumns({final_step}, {mapping_str})\n            in\n                #\"Renamed Columns\""
            else:
                target_conn_temp = self._find_target_connection(connection_info, table_def, t_name)
                conn_type = (target_conn_temp.get('type') or connection_info.get('type', '')).lower()
                server = connection_info.get('server') or "<Server not found>"
                database = connection_info.get('database') or "<Database not found>"
                warehouse = connection_info.get('warehouse') or ""
                raw_schema = connection_info.get('schema')
                # Fallback: extract schema from table-level schema_name (e.g. "[Warehouse].[StockGroups]")
                if not raw_schema:
                    schema_name_field = table_def.get('schema_name', '')
                    if schema_name_field:
                        schema_match = re.match(r'\[?([^\]\[]+)\]?\.\[?([^\]\[]+)\]?', schema_name_field)
                        if schema_match:
                            raw_schema = schema_match.group(1)
                custom_query = table_def.get('powerbi_query') or table_def.get('query')
                
                m_lines = ["let"]
                final_step_name = ""
                mapping_str = "{" + ", ".join([f'{{"{r}", "{b}"}}' for r, b in deduped_rename_mappings]) + "}"

                def add_process_steps(source_step, m_lines, mappings, columns=None):
                    current_step = source_step
                    # 1. Type Conversion Step (Apply before rename using raw names)
                    if columns:
                        type_items = []
                        seen_raw_names = set()
                        for col in columns:
                            raw_name = col.get('tableau_column_name') or col.get('original_column_name')
                            if not raw_name: continue
                            raw_key = str(raw_name).lower()
                            if raw_key in seen_raw_names:
                                continue
                            seen_raw_names.add(raw_key)
                            m_type = get_m_type(col.get('datatype', col.get('bi_datatype', 'string')))
                            type_items.append(f'{{"{raw_name}", {m_type}}}')
                        if type_items:
                            type_str = "{" + ", ".join(type_items) + "}"
                            m_lines.append(f"    #\"Changed Type\" = Table.TransformColumnTypes({current_step}, {type_str}),")
                            current_step = "#\"Changed Type\""

                    # 2. Rename Step
                    if mappings:
                        m_lines.append(f"    #\"Renamed Columns\" = Table.RenameColumns({current_step}, {mapping_str}),")
                        current_step = "#\"Renamed Columns\""
                    return current_step

                is_ai_fallback = False
                supported_types = [
                    'postgresql', 'postgres', 'oracle', 'snowflake', 'redshift',
                    'azure_sql', 'azuresql', 'sqlserver', 'azure_sqldb', 'sql.proxy', 'sqlproxy',
                    'hyper', 'bigquery', 'tableau_rest',
                    'googlesheets', 'excel-direct', 'textscan',
                    'cloudfile:onedrivesharepoint-excel-direct', 'cloudfile:onedrivesharepoint-textscan'
                ]
                if conn_type not in supported_types and not connection_info.get('mapping_url'):
                    is_ai_fallback = True

                if is_ai_fallback:
                    try:
                        from app.tableau.core.config import azure_client, Config
                        if azure_client:
                            # --- Resolve the target connection just like supported types do ---
                            target_conn = self._find_target_connection(connection_info, table_def, t_name)
                            ai_server = target_conn.get('server') or connection_info.get('server') or ''
                            ai_database = target_conn.get('database') or connection_info.get('database') or ''
                            ai_schema = target_conn.get('schema') or raw_schema or ''
                            ai_port = target_conn.get('port') or connection_info.get('port') or ''
                            ai_service = target_conn.get('service') or connection_info.get('service') or ''
                            ai_warehouse = target_conn.get('warehouse') or connection_info.get('warehouse') or ''
                            ai_url = target_conn.get('url') or target_conn.get('mapping_url') or connection_info.get('url') or connection_info.get('mapping_url') or ''
                            ai_friendly_name = target_conn.get('friendly_name') or connection_info.get('friendly_name') or ''
                            ai_custom_query = custom_query or ''
                            ai_conn_type_resolved = (target_conn.get('type') or conn_type).lower()

                            # --- Build structured column info ---
                            columns_info = []
                            for col in columns:
                                c_name = col.get('name', 'Unknown')
                                c_type = col.get('datatype', col.get('bi_datatype', 'string'))
                                raw_name = col.get('tableau_column_name') or col.get('original_column_name') or c_name
                                columns_info.append(f"- Column: \"{raw_name}\" -> BI Name: \"{c_name}\" (Type: {c_type})")
                            columns_info_str = "\n".join(columns_info)

                            # --- Build extracted connection details block ---
                            conn_details_parts = [f"Connection Type: {ai_conn_type_resolved}"]
                            if ai_server:
                                conn_details_parts.append(f"Server: {ai_server}")
                            if ai_database:
                                conn_details_parts.append(f"Database: {ai_database}")
                            if ai_schema:
                                conn_details_parts.append(f"Schema: {ai_schema}")
                            if ai_port:
                                conn_details_parts.append(f"Port: {ai_port}")
                            if ai_service:
                                conn_details_parts.append(f"Service: {ai_service}")
                            if ai_warehouse:
                                conn_details_parts.append(f"Warehouse: {ai_warehouse}")
                            if ai_url:
                                conn_details_parts.append(f"URL: {ai_url}")
                            if ai_friendly_name:
                                conn_details_parts.append(f"Friendly Name: {ai_friendly_name}")
                            if ai_custom_query:
                                conn_details_parts.append(f"Custom SQL Query: {ai_custom_query}")
                            conn_details_str = "\n".join(conn_details_parts)

                            system_prompt = (
                                "You are a Power Query M expert specializing in Power BI / Fabric semantic models.\n"
                                "Your job is to generate a valid, production-ready Power Query M script that connects to a data source and loads a specific table.\n\n"
                                "STRICT RULES:\n"
                                "1. Generate valid Power Query M code ONLY. No explanations, no markdown.\n"
                                "2. The output MUST start with 'let' and end with the expression name after 'in', e.g., 'let ... in FinalStep'.\n"
                                "3. Do NOT wrap the code in markdown code blocks (```powerquery, ```m, etc.). Return raw M code.\n"
                                "4. Include exactly ONE comment line (starting with //) at the top of the let block stating: the connection type, that this was AI-generated as a fallback, and what table it loads.\n"
                                "5. You MUST use the EXACT server, database, schema, port, warehouse, and URL values provided. Do NOT use placeholders like '<server>', 'your-server', or 'example.com'. Use the real values given.\n"
                                "6. If a server address is provided, it MUST appear in the Source step of the M query.\n"
                                "7. If a database name is provided, it MUST be used in the connection or navigation steps.\n"
                                "8. If a schema is provided, use it to navigate to the correct table.\n"
                                "9. If a custom SQL query is provided, use Value.NativeQuery() to execute it against the source.\n"
                                "10. Use the correct Power Query M connector function for the connection type. For example:\n"
                                "    - MySQL -> MySQL.Database(server, database)\n"
                                "    - MariaDB -> MySQL.Database(server, database)\n"
                                "    - MongoDB -> MongoDB.Database(server)\n"
                                "    - SAP HANA -> SapHana.Database(server)\n"
                                "    - Teradata -> Teradata.Database(server)\n"
                                "    - IBM DB2 -> DB2.Database(server, database)\n"
                                "    - Sybase -> Sybase.Database(server, database)\n"
                                "    - ODBC -> Odbc.DataSource(connectionString)\n"
                                "    - OData -> OData.Feed(url)\n"
                                "    - Web/REST API -> Json.Document(Web.Contents(url)) or Xml.Document(Web.Contents(url))\n"
                                "    - SharePoint -> SharePoint.Tables(url) or SharePoint.Files(url)\n"
                                "    - Salesforce -> Salesforce.Data() or Salesforce.Reports()\n"
                                "    - Dynamics 365 -> CommonDataService.Database(url)\n"
                                "    - Databricks -> Databricks.Catalogs(server, httpPath)\n"
                                "    - Spark -> Spark.Tables(server)\n"
                                "    - Hive -> Hive.Database(server)\n"
                                "    - Impala -> Impala.Database(server)\n"
                                "    - Vertica -> Vertica.Database(server, database)\n"
                                "    - Cosmos DB -> DocumentDB.Contents(url)\n"
                                "    - Azure Data Lake -> AzureDataLakeStore.Contents(url)\n"
                                "    - HTTP/CSV/JSON file -> Csv.Document(Web.Contents(url)) or Json.Document(Web.Contents(url))\n"
                                "    - For unknown types, pick the closest matching M connector or use Odbc.DataSource as a last resort.\n"
                                "11. After the Source step, add navigation steps to reach the specific table (e.g., Source{[Schema=\"schema\",Item=\"table\"]}[Data]).\n"
                                "12. The M query must be functional and load the exact table specified. Do NOT generate generic or placeholder code."
                            )

                            user_prompt = (
                                f"Generate a Power Query M script to load table '{t_name}' from this data source.\n\n"
                                f"=== EXTRACTED CONNECTION DETAILS ===\n{conn_details_str}\n\n"
                                f"=== COLUMN DEFINITIONS ({len(columns)} columns) ===\n{columns_info_str}\n\n"
                                f"=== FULL CONNECTION METADATA (for additional context) ===\n{json.dumps(target_conn, indent=2)}\n\n"
                                f"=== TABLE METADATA ===\n"
                                f"Table Name: {t_name}\n"
                                f"Schema Name: {table_def.get('schema_name', 'N/A')}\n"
                                f"Tableau Table Name: {table_def.get('tableau_table_name', 'N/A')}\n"
                                f"Relation Type: {table_def.get('relation_type', 'N/A')}\n\n"
                                f"Generate the M query now. Use the exact connection values above. "
                                f"The Source step MUST connect to the real server/URL. "
                                f"Navigate to the specific table '{t_name}' and return it."
                            )

                            log_info(f"Calling Azure OpenAI to generate M-query for unsupported connection type '{ai_conn_type_resolved}' on table '{t_name}' (server={ai_server}, database={ai_database})")
                            response = azure_client.chat.completions.create(
                                model=Config.AZURE_OPENAI_DEPLOYMENT_NAME,
                                messages=[
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt}
                                ],
                                temperature=0.2
                            )
                            ai_m_script = response.choices[0].message.content.strip()
                            # Sanitize code blocks
                            ai_m_script = re.sub(r'^```[a-zA-Z]*', '', ai_m_script, flags=re.MULTILINE)
                            ai_m_script = re.sub(r'```$', '', ai_m_script, flags=re.MULTILINE).strip()

                            # --- Validate: if server was provided, ensure it appears in the generated M code ---
                            if ai_server and ai_server not in ai_m_script:
                                log_info(f"AI-generated M-query missing server '{ai_server}', injecting as comment and re-checking.")
                                # Insert the server as a comment so it's at least visible for manual correction
                                ai_m_script = ai_m_script.replace(
                                    "let\n",
                                    f"let\n    // WARNING: Expected server \"{ai_server}\" was not found in the generated query. Please verify the Source step.\n",
                                    1
                                )
                                if "let\r\n" in ai_m_script and ai_server not in ai_m_script:
                                    ai_m_script = ai_m_script.replace(
                                        "let\r\n",
                                        f"let\r\n    // WARNING: Expected server \"{ai_server}\" was not found in the generated query. Please verify the Source step.\r\n",
                                        1
                                    )

                            m_script = ai_m_script
                        else:
                            log_error("Azure OpenAI client is not initialized. Using fallback error M-query.")
                            m_lines = ["let"]
                            m_lines.append(f"    // Error: Unsupported connection type detected: '{conn_type}' and AI fallback client not initialized")
                            m_lines.append(f"    Source = error \"Unsupported connection type: '{conn_type}'\",")
                            m_lines.extend(["in", "    Source"])
                            m_script = "\n".join(m_lines)
                    except Exception as ex:
                        log_error(f"Error generating M-query via Azure OpenAI: {ex}")
                        m_lines = ["let"]
                        m_lines.append(f"    // Error: Failed to generate M-query via AI for connection type '{conn_type}'. Details: {ex}")
                        m_lines.append(f"    Source = error \"Unsupported connection type: '{conn_type}'\",")
                        m_lines.extend(["in", "    Source"])
                        m_script = "\n".join(m_lines)
                else:
                    if conn_type in ['postgresql', 'postgres']:
                        m_lines.append(f"    Source = PostgreSQL.Database(\"{server}\", \"{database}\", [CommandTimeout=#duration(0, 0, 10, 0)]),")
                        if custom_query:
                            safe_query = " ".join(re.sub(r'--.*', '', custom_query).replace('"', '""').split())
                            m_lines.append(f"    NativeQuery = Value.NativeQuery(Source, \"{safe_query}\"),")
                            step_name = "NativeQuery"
                        else:
                            step_name = f"public_{t_name}"
                            if raw_schema:
                                nav_step = f"Source{{[Schema=\"{raw_schema}\",Item=\"{t_name}\"]}}[Data]"
                            else:
                                nav_step = f"Table.SelectRows(Source, each Text.Lower([Item]) = \"{t_name.lower()}\")[Data]{{0}}"
                            m_lines.append(f"    {step_name} = {nav_step},")
                        final_step_name = add_process_steps(step_name, m_lines, deduped_rename_mappings, columns)
                    
                    elif conn_type == 'oracle':
                        target_conn = self._find_target_connection(connection_info, table_def, t_name)
                        oracle_server = target_conn.get('friendly_name') or target_conn.get('server') or server
                        raw_schema = target_conn.get('schema') or raw_schema
                        oracle_service = target_conn.get('service') or ''
                        oracle_port = target_conn.get('port') or '1521'
                        
                        if oracle_service:
                            # Oracle Local: service is present, use direct query with server:port/service
                            schema = raw_schema or "ADMIN"
                            oracle_conn_str = f"{oracle_server}:{oracle_port}/{oracle_service}"
                            if custom_query:
                                safe_query = " ".join(re.sub(r'--.*', '', custom_query).replace('"', '""').split())
                                m_lines.append(f"    Source = Oracle.Database(\"{oracle_conn_str}\", [Query = \"{safe_query}\"]),")
                            else:
                                m_lines.append(f"    Source = Oracle.Database(\"{oracle_conn_str}\", [Query = \"SELECT * FROM {schema}.{t_name}\"]),")
                            step_name = "Source"
                        else:
                            # Oracle Cloud: no service, use HierarchicalNavigation
                            m_lines.append(f"    Source = Oracle.Database(\"{oracle_server}\", [HierarchicalNavigation = true]),")
                            if custom_query:
                                safe_query = " ".join(re.sub(r'--.*', '', custom_query).replace('"', '""').split())
                                m_lines.append(f"    NativeQuery = Value.NativeQuery(Source, \"{safe_query}\"),")
                                step_name = "NativeQuery"
                            else:
                                schema = raw_schema or "ADMIN"
                                m_lines.append(f"    #\"Navigation 1\" = Source{{[Schema = \"{schema}\"]}}[Data],")
                                m_lines.append(f"    #\"Navigation 2\" = #\"Navigation 1\"{{[Name = \"{t_name}\"]}}[Data],")
                                step_name = "#\"Navigation 2\""
                        final_step_name = add_process_steps(step_name, m_lines, deduped_rename_mappings, columns)

                    elif conn_type == 'snowflake':
                        target_conn = self._find_target_connection(connection_info, table_def, t_name)
                        server = target_conn.get('server') or server
                        database = target_conn.get('database') or database
                        warehouse = target_conn.get('warehouse') or warehouse
                        raw_schema = target_conn.get('schema') or raw_schema
                        
                        m_lines.append(f"    Source = Snowflake.Databases(\"{server}\", \"{warehouse}\"),")
                        m_lines.append(f"    DB = Source{{[Name=\"{database}\"]}}[Data],")
                        if custom_query:
                            safe_query = " ".join(re.sub(r'--.*', '', custom_query).replace('"', '""').split())
                            m_lines.append(f"    NativeQuery = Value.NativeQuery(DB, \"{safe_query}\"),")
                            step_name = "NativeQuery"
                        else:
                            schema = raw_schema or "PUBLIC"
                            m_lines.extend([f"    Schema = DB{{[Name=\"{schema}\"]}}[Data],", f"    Table = Schema{{[Name=\"{t_name}\"]}}[Data],"])
                            step_name = "Table"
                        final_step_name = add_process_steps(step_name, m_lines, deduped_rename_mappings, columns)

                    elif conn_type == 'redshift':
                        target_conn = self._find_target_connection(connection_info, table_def, t_name)
                        server = target_conn.get('server') or server
                        database = target_conn.get('database') or database
                        raw_schema = target_conn.get('schema') or raw_schema
                        
                        # Append port if not present
                        if server and ':' not in server:
                            server = f"{server}:5439"
                        
                        m_lines.append(f"    Source = AmazonRedshift.Database(\"{server}\", \"{database}\", [ProviderName = null, BatchSize = null]),")
                        if custom_query:
                            safe_query = " ".join(re.sub(r'--.*', '', custom_query).replace('"', '""').split())
                            m_lines.append(f"    NativeQuery = Value.NativeQuery(Source, \"{safe_query}\"),")
                            step_name = "NativeQuery"
                        else:
                            schema = raw_schema or 'public'
                            m_lines.append(f"    #\"Navigation 1\" = Source{{[Name = \"{schema}\"]}}[Data],")
                            m_lines.append(f"    #\"Navigation 2\" = #\"Navigation 1\"{{[Name = \"{t_name}\"]}}[Data],")
                            step_name = "#\"Navigation 2\""
                        final_step_name = add_process_steps(step_name, m_lines, deduped_rename_mappings, columns)

                    elif conn_type in ['azure_sql', 'azuresql', 'sqlserver', 'azure_sqldb', 'sql.proxy', 'sqlproxy']:
                        target_conn = self._find_target_connection(connection_info, table_def, t_name)
                        server = target_conn.get('server') or server
                        database = target_conn.get('database') or database
                        raw_schema = target_conn.get('schema') or raw_schema
                        
                        m_lines.append(f"    Source = Sql.Database(\"{server}\", \"{database}\"),")
                        if custom_query:
                            safe_query = " ".join(re.sub(r'--.*', '', custom_query).replace('"', '""').split())
                            m_lines.append(f"    NativeQuery = Value.NativeQuery(Source, \"{safe_query}\"),")
                            step_name = "NativeQuery"
                        else:
                            step_name = f"dbo_{t_name}"
                            if raw_schema:
                                nav_step = f"Source{{[Schema=\"{raw_schema}\",Item=\"{t_name}\"]}}[Data]"
                            else:
                                nav_step = f"Table.SelectRows(Source, each Text.Lower([Item]) = \"{t_name.lower()}\")[Data]{{0}}"
                            m_lines.append(f"    {step_name} = {nav_step},")
                        final_step_name = add_process_steps(step_name, m_lines, deduped_rename_mappings, columns)

                    elif conn_type == 'hyper':
                        target_conn = self._find_target_connection(connection_info, table_def, t_name)
                        hyper_path = target_conn.get('database') or database or ""
                        if hyper_path and not (hyper_path.startswith('C:') or hyper_path.startswith('\\') or hyper_path.startswith('/')):
                            hyper_path = f"C:/TableauData/{hyper_path}"

                        schema_name = target_conn.get('schema') or raw_schema or "Extract"
                        table_name = t_name
                        listed_tables = target_conn.get('tables', []) if isinstance(target_conn, dict) else []
                        if isinstance(listed_tables, list) and listed_tables:
                            first_tbl = str(listed_tables[0])
                            if '.' in first_tbl:
                                sch, tbl = first_tbl.split('.', 1)
                                if sch.strip():
                                    schema_name = sch.strip()
                                if tbl.strip():
                                    table_name = tbl.strip()
                            elif first_tbl.strip():
                                table_name = first_tbl.strip()

                        odbc_conn = f"Driver={{Tableau Hyper}};DBQ={hyper_path};"
                        m_lines.append(f"    Source = Odbc.DataSource(\"{odbc_conn}\", [HierarchicalNavigation=true]),")
                        m_lines.append(f"    Schema = Source{{[Name=\"{schema_name}\",Kind=\"Schema\"]}}[Data],")
                        m_lines.append(f"    Table = Schema{{[Name=\"{table_name}\",Kind=\"Table\"]}}[Data],")
                        step_name = "Table"
                        final_step_name = add_process_steps(step_name, m_lines, deduped_rename_mappings, columns)
                    
                    elif conn_type == 'bigquery':
                        target_conn = self._find_target_connection(connection_info, table_def, t_name)
                        gcp_project = target_conn.get('google_cloud_project_id') or connection_info.get('google_cloud_project_id') or '<gcp-project-id>'
                        schema = target_conn.get('schema') or raw_schema or 'default'
                        
                        m_lines.append(f"    Source = GoogleBigQuery.Database([BillingProject = null, UseStorageApi = null, ConnectionTimeout = null, CommandTimeout = null, ProjectId = null]),")
                        m_lines.append(f'    #"Navigation 1" = Source{{[Name = "{gcp_project}"]}}[Data],')
                        m_lines.append(f'    #"Navigation 2" = #"Navigation 1"{{[Name = "{schema}", Kind = "Schema"]}}[Data],')
                        
                        if custom_query:
                            safe_query = " ".join(re.sub(r'--.*', '', custom_query).replace('"', '""').split())
                            m_lines.append(f'    NativeQuery = Value.NativeQuery(#"Navigation 2", "{safe_query}"),')
                            step_name = "NativeQuery"
                        else:
                            m_lines.append(f'    #"Navigation 3" = #"Navigation 2"{{[Name = "{t_name}", Kind = "Table"]}}[Data],')
                            step_name = '#"Navigation 3"'
                        
                        final_step_name = add_process_steps(step_name, m_lines, deduped_rename_mappings, columns)

                    elif conn_type == 'tableau_rest':
                        server = connection_info.get('server') or ""
                        # Sanitize URL if it got mangled by loose HTML decoding of '&quotes' -> '"es'
                        if '"es=' in server:
                            server = server.replace('"es=', '&quotes=')
                        elif '%22es=' in server:
                            server = server.replace('%22es=', '&quotes=')
                            
                        m_lines.append(f'    Source = Json.Document(Web.Contents("{server}")),')
                        m_lines.append('    #"Converted to table" = Table.FromList(Source, Splitter.SplitByNothing(), null, null, ExtraValues.Error),')
                        
                        raw_names = []
                        seen_raw_names = set()
                        for col in columns:
                            raw_name = col.get('tableau_column_name') or col.get('original_column_name') or col.get('name')
                            if not raw_name: continue
                            if raw_name in seen_raw_names: continue
                            seen_raw_names.add(raw_name)
                            raw_names.append(raw_name)
                            
                        if raw_names:
                            cols_str = "{" + ", ".join([f'"{c}"' for c in raw_names]) + "}"
                            m_lines.append(f'    #"Expanded Column1" = Table.ExpandRecordColumn(#"Converted to table", "Column1", {cols_str}, {cols_str}),')
                            
                            type_items = []
                            for col in columns:
                                raw_name = col.get('tableau_column_name') or col.get('original_column_name') or col.get('name')
                                if not raw_name: continue
                                m_type = get_m_type(col.get('datatype', 'string'))
                                type_items.append(f'{{"{raw_name}", {m_type}}}')
                            if type_items:
                                type_str = "{" + ", ".join(type_items) + "}"
                                m_lines.append(f'    #"Changed column type" = Table.TransformColumnTypes(#"Expanded Column1", {type_str}),')
                                step_name = '#"Changed column type"'
                            else:
                                step_name = '#"Expanded Column1"'
                        else:
                            step_name = '#"Converted to table"'
                            
                        final_step_name = add_process_steps(step_name, m_lines, deduped_rename_mappings, columns=None)

                    elif conn_type in ['googlesheets', 'excel-direct', 'textscan', 'cloudfile:onedrivesharepoint-excel-direct', 'cloudfile:onedrivesharepoint-textscan'] or connection_info.get('mapping_url'):
                        target_conn = self._find_target_connection(connection_info, table_def, t_name)
                        
                        actual_conn_type = (target_conn.get('type') or conn_type).lower() if target_conn else conn_type
                        
                        if actual_conn_type == 'googlesheets' or connection_info.get('mapping_url'):
                            mapping_url = target_conn.get('mapping_url') if target_conn else ""
                            sheet_name = t_name
                            if target_conn and target_conn.get('worksheets'):
                                sheet_name = self._find_matching_worksheet(target_conn.get('worksheets'), table_def, t_name) or t_name
                            else:
                                schema_name = table_def.get('schema_name', '')
                                if schema_name:
                                    sheet_name = re.sub(r'[\[\]\$]', '', schema_name)
                                else:
                                    sheet_name = table_def.get('tableau_table_name') or table_def.get('table_name') or t_name
                                    
                            m_lines.append(f"    Source = GoogleSheets.Contents(\"{mapping_url}\"),")
                            m_lines.append(f"    #\"Navigation 1\" = Source{{[name = \"{sheet_name}\", ItemKind = \"Table\"]}}[Data],")
                            m_lines.append(f"    #\"Promoted Headers\" = Table.PromoteHeaders(#\"Navigation 1\", [PromoteAllScalars = true]),")
                        
                        elif actual_conn_type in ['excel-direct', 'cloudfile:onedrivesharepoint-excel-direct']:
                            filepath = target_conn.get('database') or target_conn.get('server') or ""
                            source_url = target_conn.get('url') or connection_info.get('url') or ""
                            is_cloud_excel = actual_conn_type == 'cloudfile:onedrivesharepoint-excel-direct'
                            if not is_cloud_excel and filepath and not (filepath.startswith('C:') or filepath.startswith('\\') or filepath.startswith('/')):
                                filepath = f"C:/TableauData/{filepath}"
                            
                            sheet_name = "Sheet1"
                            if target_conn and target_conn.get('worksheets'):
                                sheet_name = self._find_matching_worksheet(target_conn.get('worksheets'), table_def, t_name) or "Sheet1"
                            else:
                                schema_name = table_def.get('schema_name', '')
                                if schema_name:
                                    sheet_name = re.sub(r'[\[\]\$]', '', schema_name)
                                else:
                                    sheet_name = table_def.get('tableau_table_name') or table_def.get('table_name') or t_name or "Sheet1"

                            if is_cloud_excel and source_url:
                                m_lines.append(f"    Source_Raw = Excel.Workbook(Web.Contents(\"{source_url}\"), null, true),")
                            else:
                                m_lines.append(f"    Source_Raw = Excel.Workbook(File.Contents(\"{filepath}\"), null, true),")
                            m_lines.append(f"    #\"Navigation 1\" = Source_Raw{{[Item = \"{sheet_name}\", Kind = \"Sheet\"]}}[Data],")
                            m_lines.append(f"    Table_Raw = #\"Navigation 1\",")
                            m_lines.append(f"    #\"Promoted Headers\" = Table.PromoteHeaders(Table_Raw, [PromoteAllScalars = true]),")
                        
                        elif actual_conn_type in ['textscan', 'cloudfile:onedrivesharepoint-textscan']:
                            filepath = target_conn.get('database') or target_conn.get('server') or ""
                            source_url = target_conn.get('url') or connection_info.get('url') or ""
                            is_cloud_csv = actual_conn_type == 'cloudfile:onedrivesharepoint-textscan'
                            if not is_cloud_csv and filepath and not (filepath.startswith('C:') or filepath.startswith('\\') or filepath.startswith('/')):
                                filepath = f"C:/TableauData/{filepath}"
                            
                            if is_cloud_csv and source_url:
                                m_lines.append(f"    Source_Raw = Csv.Document(Web.Contents(\"{source_url}\"), [Delimiter = \",\", Encoding = 28591, QuoteStyle = QuoteStyle.None]),")
                            else:
                                m_lines.append(f"    Source_Raw = Csv.Document(File.Contents(\"{filepath}\"), [Delimiter=\",\", Encoding=28591, QuoteStyle=QuoteStyle.None]),")
                            m_lines.append(f"    #\"Promoted Headers\" = Table.PromoteHeaders(Source_Raw, [PromoteAllScalars = true]),")
                        
                        step_name = "#\"Promoted Headers\""
                        final_step_name = add_process_steps(step_name, m_lines, deduped_rename_mappings, columns)

                    if m_lines and m_lines[-1].endswith(","): m_lines[-1] = m_lines[-1].rstrip(',')
                    m_lines.extend(["in", f"    {final_step_name}"])
                    m_script = "\n".join(m_lines)

            lines.extend([indent_block(m_script, spaces=12), ""])
        return "\n".join(lines), local_date_tables

    def _map_data_type(self, json_type: str) -> str:
        jt = json_type.lower()
        if jt in ['string', 'text']: return 'string'
        if jt in ['integer', 'int', 'int64', 'whole number']: return 'int64'
        if jt in ['real', 'decimal', 'float', 'double', 'number', 'decimal number']: return 'double'
        if jt in ['boolean', 'bool', 'true/false']: return 'boolean'
        if jt in ['date', 'datetime', 'date/time']: return 'dateTime'
        return 'string'

    def _generate_model_tmdl(self, table_names: List[str], relationships_tmdl: str = "", has_calc_groups: bool = False, is_direct_lake: bool = False, lakehouse_info: Dict = None) -> str:
        lines = ["model Model", "    culture: en-US", "    defaultPowerBIDataSourceVersion: powerBI_V3", "    sourceQueryCulture: en-US"]
        if has_calc_groups: lines.append("    discourageImplicitMeasures: true")
        lines.extend(["    dataAccessOptions", "        legacyRedirects", "        returnErrorValuesAsNull", ""])
        lines.append("annotation __PBI_TimeIntelligenceEnabled = 1")
        if is_direct_lake and lakehouse_info:
            lh_name = lakehouse_info.get('name', 'Lakehouse')
            lines.append(f'annotation PBI_QueryOrder = ["DirectLake - {lh_name}"]')
            lines.append('annotation PBI_ProTooling = ["DirectLakeOnOneLakeInWeb","WebModelingEdit"]')
        else:
            lines.append("annotation PBIDesktopVersion = 2.138.1004.0 (24.11)")
        lines.append("")
        for t in table_names: lines.append(f"ref table '{self._escape_tmdl_identifier(t)}'")
        lines.extend(["", "ref cultureInfo en-US", ""])
        if relationships_tmdl: lines.append(relationships_tmdl)
        return "\n".join(lines)

    def _generate_database_tmdl(self) -> str: return "database Database\n    compatibilityLevel: 1604\n"

    def _generate_relationships_tmdl(self, relationships: List[Dict], valid_tables: List[str]) -> str:
        lines = []
        seen = set()
        active_pairs = set()
        for rel in relationships:
            try:
                pbi = rel.get('power_bi') or rel.get('powerbi')
                if pbi and isinstance(pbi, dict):
                    rel = pbi
                t_from, c_from = self._sanitize_name(rel.get('fromTable')), self._sanitize_name(rel.get('fromColumn'))
                t_to, c_to = self._sanitize_name(rel.get('toTable')), self._sanitize_name(rel.get('toColumn'))
                if t_from not in valid_tables or t_to not in valid_tables: continue
                card = rel.get('cardinality', 'ManyToOne').lower().replace('-', '')
                if card == 'onetomany': t_from, t_to, c_from, c_to = t_to, t_from, c_to, c_from
                key = (t_from, c_from, t_to, c_to)
                if key in seen: continue
                seen.add(key)
                pair = tuple(sorted((t_from, t_to)))
                active = rel.get('isActive', True)
                if pair in active_pairs and active: active = False
                elif active: active_pairs.add(pair)
                rel_uuid = rel.get('_override_rel_id') or str(uuid.uuid4())
                lines.extend([f"relationship {rel_uuid}", f"    fromColumn: '{self._escape_tmdl_identifier(t_from)}'.'{self._escape_tmdl_identifier(c_from)}'", f"    toColumn: '{self._escape_tmdl_identifier(t_to)}'.'{self._escape_tmdl_identifier(c_to)}'"])
                if card == 'manytomany': lines.extend(["    fromCardinality: many", "    toCardinality: many"])
                elif card == 'onetoone': lines.extend(["    fromCardinality: one", "    toCardinality: one"])
                if not active: lines.append("    isActive: false")
                if rel.get('joinOnDateBehavior'): lines.append(f"    joinOnDateBehavior: {rel['joinOnDateBehavior']}")
                if rel.get('crossFilterDirection', 'Single').lower() == 'both': lines.append("    crossFilteringBehavior: bothDirections")
                lines.append("")
            except Exception as e: log_error(f"Failed to parse relationship: {e}")
        return "\n".join(lines)

    def _generate_calculation_group_tmdl(self, group_name: str, calc_items: List[Dict]) -> str:
        lines = [f"table '{self._escape_tmdl_identifier(group_name)}'", "", "    calculationGroup", ""]
        for item in calc_items:
            raw = item.get('dax_formula') or item.get('powerbi', {}).get('dax') or item.get('powerbi_formula') or item.get('dax') or item.get('power_bi', {}).get('dax')
            lines.extend([f"        calculationItem '{self._escape_tmdl_identifier(self._sanitize_name(item.get('name')))}' =", indent_block(self._clean_dax(raw) if raw else 'SELECTEDMEASURE()', 12), ""])
        lines.append(f"    column '{self._escape_tmdl_identifier(group_name)}'\n        dataType: string\n        sourceColumn: Name\n        summarizeBy: none\n        sortByColumn: Ordinal\n\n        annotation SummarizationSetBy = Automatic\n")
        lines.append("    column Ordinal\n        dataType: int64\n        formatString: 0\n        summarizeBy: sum\n        sourceColumn: Ordinal\n\n        annotation SummarizationSetBy = Automatic\n")
        return "\n".join(lines)

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

    def _build_datasource_map(self, api_data: Dict) -> Dict[str, Dict]:
        ds_map = {}
        try:
            ds_list = api_data.get('datasources', [])
            if isinstance(ds_list, dict): 
                ds_list = ds_list.get('datasources', []) or [ds_list]
            
            if not isinstance(ds_list, list):
                ds_list = []

            for ds in ds_list:
                if not isinstance(ds, dict): continue
                connections = ds.get('connections', [])
                if not isinstance(connections, list): connections = []
                
                info = {
                    "type": None, 
                    "server": None, 
                    "database": None, 
                    "schema": None, 
                    "warehouse": None, 
                    "mode": str(ds.get('mode', '')).lower(), 
                    "mapping_url": None, 
                    "username": None,
                    "connections": connections
                }
                if connections:
                    conn = connections[0]
                    if isinstance(conn, dict):
                        c_type = (conn.get('type') or ds.get('connection_type') or '').lower()
                        info.update({
                            "type": c_type, 
                            "server": conn.get('server'), 
                            "database": conn.get('database'), 
                            "schema": conn.get('schema'), 
                            "warehouse": conn.get('warehouse'),
                            "mapping_url": conn.get('mapping_url'),
                            "username": conn.get('username'),
                            "google_cloud_project_id": conn.get('google_cloud_project_id'),
                            "url": conn.get('url')
                        })
                        if not info['schema'] and c_type == 'snowflake':
                            match = re.search(r'\(([^)]+)\)$', ds.get('name', ''))
                            if match: info['schema'] = match.group(1)
                
                ds_id = ds.get('id')
                if ds_id: ds_map[str(ds_id)] = info
                ds_name = ds.get('name')
                if ds_name: ds_map[str(ds_name)] = info

                # Table-specific connection mapping
                for conn in connections:
                    if not isinstance(conn, dict): continue
                    c_type = (conn.get('type') or ds.get('connection_type') or '').lower()
                    conn_info = {
                        "type": c_type, 
                        "server": conn.get('server'), 
                        "database": conn.get('database'), 
                        "schema": conn.get('schema'), 
                        "warehouse": conn.get('warehouse'),
                        "mapping_url": conn.get('mapping_url'),
                        "username": conn.get('username'),
                        "google_cloud_project_id": conn.get('google_cloud_project_id'),
                        "url": conn.get('url'),
                        "mode": str(ds.get('mode', '')).lower(),
                        "connections": [conn]
                    }
                    if not conn_info['schema'] and c_type == 'snowflake':
                        match = re.search(r'\(([^)]+)\)$', ds.get('name', ''))
                        if match: conn_info['schema'] = match.group(1)

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
                
                # Find matching datasource in ds_list
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
                    c_type = (target_conn.get('type') or matching_ds.get('connection_type') or '').lower()
                    conn_info = {
                        "type": c_type, 
                        "server": target_conn.get('server'), 
                        "database": target_conn.get('database'), 
                        "schema": target_conn.get('schema'), 
                        "warehouse": target_conn.get('warehouse'),
                        "mapping_url": target_conn.get('mapping_url'),
                        "username": target_conn.get('username'),
                        "google_cloud_project_id": target_conn.get('google_cloud_project_id'),
                        "url": target_conn.get('url'),
                        "mode": str(matching_ds.get('mode', '')).lower(),
                        "connections": [target_conn]
                    }
                    if not conn_info['schema'] and c_type == 'snowflake':
                        match = re.search(r'\(([^)]+)\)$', matching_ds.get('name', ''))
                        if match: conn_info['schema'] = match.group(1)
                        
                    # Register under various names to ensure lookup succeeds
                    for name_val in [tbl.get('tableau_table_name'), tbl.get('table_name'), tbl.get('bi_table_name')]:
                        if not name_val: continue
                        norm = self._normalize_table_name(name_val)
                        if not norm: continue
                        if ds_id:
                            ds_map[f"{ds_id}_{norm}"] = conn_info
                        ds_name = matching_ds.get('name')
                        if ds_name:
                            ds_map[f"{ds_name}_{norm}"] = conn_info
        except Exception as e: log_error(f"Error building datasource map: {e}")
        return ds_map

    def _identify_primary_key(self, table_name: str, columns: List[Dict]) -> Optional[str]:
        tu = table_name.upper()
        cands = [f"{tu}_ID", f"{tu[:-1]}_ID" if tu.endswith('S') else None]
        for idx, col in enumerate(columns):
            cn = col.get('name', '').upper()
            if cn in cands or (idx == 0 and cn.endswith("ID")): return col.get('name')
        return None

    def _normalize_tables(self, tables: List[Dict]) -> List[Dict]:
        for tbl in tables:
            if not isinstance(tbl, dict): continue
            if 'bi_table_name' in tbl: tbl['table_name'] = tbl['bi_table_name']
            for col in tbl.get('columns', []):
                if not isinstance(col, dict): continue
                if 'bi_column_name' in col: col['name'] = col['bi_column_name']
                if 'bi_datatype' in col: col['datatype'] = col['bi_datatype']
                if not col.get('datatype'): col['datatype'] = col.get('tableau_datatype')
        return tables

    def _sanitize_name(self, name):
        if not name: return "Unknown"
        name = name.strip()
        if name.startswith("[") and name.endswith("]"): name = name[1:-1].strip()
        return name.replace("'", "''").strip()

    def _escape_tmdl_identifier(self, name: str) -> str: return self._sanitize_name(name).replace("'", "''")
