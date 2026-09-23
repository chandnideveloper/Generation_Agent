import re
from typing import Dict, List, Set, Any
from app.tableau.core.logging_utils import log_info

class CalculationGroupProcessorMixin:
    def _promote_visual_formulas_to_measures(self, api_data: Dict, pages: List[Dict], field_to_table: Dict, field_to_datatype: Dict, measure_set: Set[str], default_table: str, display_to_bi_name: Dict = None, known_source_columns: set = None, fields_with_dax: set = None):
        """
        Scans visuals for formulas in rows/columns and promotes them to explicit measures.
        Handles various Tableau formats:
           CNTD(Appointment Id)
           AGG(Retention Rate)
           CNTD(PATIENT_ID PATIENTS)
           DISTINCT(CustomerID (Fact_Sales))
        """
        import re
        agg_functions = ["SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "CNTD", "CTD", "CNT", "ATTR", "MEDIAN", "STDEV", "VAR", "DISTINCT", "AGG"]
        agg_regex = re.compile(r'^(' + '|'.join(agg_functions) + r')\s*\((.*)\)$', re.IGNORECASE)
        inner_paren_regex = re.compile(r'^(.*?)\s*\((.*?)\)$', re.IGNORECASE)
        
        all_visuals = []
        for page in pages:
            if page.get("is_dashboard"):
                all_visuals.extend(page.get("dashboard_visuals", []))
            else:
                all_visuals.append(page)

        existing_measures = {m.get("name") for m in api_data.get("measures", []) if isinstance(m, dict)}
        new_promoted_measures = {}
        bi_map = display_to_bi_name or {}

        for vis in all_visuals:
            if not isinstance(vis, dict): continue
            
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
                    
                    if agg_func == "AGG":
                        real_inner = bi_map.get(inner, inner)
                        fields[idx] = real_inner
                        continue

                    col_name = inner
                    table_name = default_table
                    
                    p_match = inner_paren_regex.match(inner)
                    if p_match:
                        col_name = p_match.group(1).strip()
                        table_name = p_match.group(2).strip()
                    else:
                        parts = inner.rsplit(None, 1)
                        if len(parts) == 2:
                            maybe_col, maybe_table = parts
                            if maybe_table in set(field_to_table.values()):
                                col_name = maybe_col.strip()
                                table_name = maybe_table.strip()
                    
                    real_col = bi_map.get(col_name, col_name)
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
                    
                    measure_name = f"{agg_func} of {col_name}"
                    if target_table and target_table != default_table:
                         measure_name += f" ({target_table})"
                    
                    # Look for custom axis title
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
                    
                    fields[idx] = measure_name
                    field_to_table[measure_name] = target_table
                    field_to_datatype[measure_name] = "real"
                    measure_set.add(measure_name)
                    
                    log_info(f"[CoordinatorAgent] Promoted formula '{field}' to measure '{measure_name}' on table '{target_table}'")

        if new_promoted_measures:
            if "measures" not in api_data:
                api_data["measures"] = []
            api_data["measures"].extend(new_promoted_measures.values())

    def _process_calculation_groups(self, api_data: Dict, pages: List[Dict], fields_with_dax: Set[str], field_to_table: Dict, field_to_datatype: Dict, measure_set: Set[str], default_table: str, display_to_bi_name: Dict, known_source_columns: set = None):
        """
        If calculation groups exist, Power BI disables implicit measures. 
        We must convert simple agg columns to explicit measures.
        """
        # Calculation Group support disabled as per new dynamic set logic
        return
