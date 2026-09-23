from autogen import AssistantAgent
import aiohttp
import httpx
import re
import datetime
from app.tableau.core.config import Config
from app.tableau.core.logging_utils import log_info, log_error, log_warning, log_action_to_api
from app.tableau.services.action_logger import ActionLogger
from app.tableau.core_logic.metadata_exporter.metadata_exporter import MetadataExporter
import json
from .memory_file_agent import MemoryFileAgent


class MetadataExtractorMixin:
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

    def _promote_visual_formulas_to_measures(self, api_data: dict, pages: list, field_to_table: dict, field_to_datatype: dict, measure_set: set, default_table: str, display_to_bi_name: dict = None, known_source_columns: set = None, fields_with_dax: set = None, field_to_format: dict = None):
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
                    field_obj = None
                    if isinstance(field, dict):
                        field_obj = field
                        field = field_obj.get("field", "")
                    
                    if not isinstance(field, str): continue

                    cleaned_field = field.strip()
                    match = agg_regex.match(cleaned_field)
                    if not match: continue

                    agg_func = match.group(1).upper()
                    inner = match.group(2).strip()

                    # 1. Handle AGG(Measure) -> Strip AGG as it means field is already aggregated in Tableau
                    if agg_func == "AGG":
                        real_inner = bi_map.get(inner, inner)
                        if field_obj is not None:
                            # Preserve metadata (e.g., format, dual_axis) on visual field dict entries
                            field_obj["field"] = real_inner
                            fields[idx] = field_obj
                        else:
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
                        m_obj = {
                            "name": measure_name,
                            "dax_formula": f"{dax_func}('{target_table}'[{real_col}])",
                            "table": target_table
                        }
                        if field_obj and "format" in field_obj:
                            m_obj["format"] = field_obj["format"]
                        
                        new_promoted_measures[measure_name] = m_obj

                    # Update the visual to use the new measure name
                    if field_obj is not None:
                        # Preserve metadata (e.g., format) while rewriting the field reference
                        field_obj["field"] = measure_name
                        fields[idx] = field_obj
                    else:
                        fields[idx] = measure_name

                    # Register in field mappings
                    field_to_table[measure_name] = target_table
                    field_to_datatype[measure_name] = "real"
                    measure_set.add(measure_name)

                    log_info(f"[CoordinatorAgent] Promoted formula '{field}' to measure '{measure_name}' on table '{target_table}'")
                    if field_obj and "format" in field_obj and field_to_format is not None:
                        field_to_format[measure_name] = self._map_format_to_pbi_string(field_obj["format"])

        if new_promoted_measures:
            if "measures" not in api_data:
                api_data["measures"] = []
            api_data["measures"].extend(new_promoted_measures.values())
            log_info(f"[CoordinatorAgent] Created {len(new_promoted_measures)} new promoted measures from visual formulas.")

