import json
import uuid
import re
from typing import Dict, Optional, List, Tuple
from app.tableau.core.logging_utils import log_info, log_error, log_warning
from app.tableau.core.config import Config
from app.tableau.core_logic.template_manager.template_manager import TemplateManager

from app.tableau.services.action_logger import ActionLogger

class ReportGenerator:
    def __init__(self, file_agent, action_logger: Optional[ActionLogger] = None):
        self.file_agent = file_agent
        self.template_manager = TemplateManager()
        self.action_logger = action_logger or ActionLogger()
        self.skipped_visuals = []

    # =========================================================
    # VISUAL TYPE MAPPING
    # =========================================================
    
    # Maps the power_bi_visual_type string directly to a PBI visual type string
    PBI_VISUAL_TYPE_MAP = {
        "Clustered Bar Chart": "clusteredBarChart",
        "Stacked Bar Chart": "barChart",
        "100% Stacked Bar Chart": "barChart",
        "Clustered Column Chart": "clusteredColumnChart",
        "Stacked Column Chart": "columnChart",
        "Line Chart": "lineChart",
        "Area Chart": "areaChart",
        "Stacked Area Chart": "areaChart",
        "Line and Clustered Column Chart": "lineClusteredColumnComboChart",
        "Line and Stacked Column Chart": "lineStackedColumnComboChart",
        "Pie Chart": "pieChart",
        "Donut Chart": "donutChart",
        "Treemap": "treemap",
        "Funnel": "funnel",
        "Gauge": "gauge",
        "Card": "card",
        "Multi-Row Card": "multiRowCard",
        "Table": "tableEx",
        "Matrix": "tableEx",
        "Scatter Chart": "scatterChart",
        "Map": "map",
        "Filled Map": "filledMap",
        "Heatmap": "heatmap",
        "Heat Map": "heatmap",
        "Density": "heatmap",
        "Slicer": "slicer",
        "Waterfall Chart": "waterfallChart",
        "KPI": "card",
        "Decomposition Tree": "decompositionTreeVisual",
    }

    def _map_pbi_visual_type(self, power_bi_visual_type) -> str:
        """Maps power_bi_visual_type (string or dict) to PBI template filename (without .json)."""
        if isinstance(power_bi_visual_type, dict):
            # Extract basic string name from dictionary if possible
            power_bi_visual_type = (
                power_bi_visual_type.get("power_bi_visual_type") or 
                power_bi_visual_type.get("name") or 
                power_bi_visual_type.get("value") or 
                power_bi_visual_type.get("type", "")
            )
        
        if not isinstance(power_bi_visual_type, str):
            power_bi_visual_type = str(power_bi_visual_type)
            
        return self.PBI_VISUAL_TYPE_MAP.get(power_bi_visual_type, "")

    def _map_mark_type_to_visual_type(self, mark_type: str) -> str:
        """Fallback: Maps Tableau mark_type to PBI visual type."""
        mapping = {
            "Pie": "pieChart", "Area": "areaChart", "Square": "scatterChart",
            "Automatic": "barChart", "Line": "lineChart", "Bar": "barChart", 
            "Text": "card", "Shape": "scatterChart", "Circle": "scatterChart",
            "Map": "map", "Gantt Bar": "barChart", "Polygon": "filledMap",
            "Density": "heatmap",
        }
        return mapping.get(mark_type, "tableEx")

    def _resolve_visual_type(self, page_entry: Dict, measure_names: List[str] = None) -> str:
        """Resolves the final visual type: prefers power_bi_visual_type, falls back to mark_type."""
        pbi_type = page_entry.get("power_bi_visual_type", "")
        mark_type = page_entry.get("mark_type", "Automatic")
        
        # Ensure pbi_type is always a string for subsequent logic/lookups
        pbi_type_str = ""
        if isinstance(pbi_type, dict):
            pbi_type_str = (
                pbi_type.get("power_bi_visual_type") or 
                pbi_type.get("name") or 
                pbi_type.get("value") or 
                pbi_type.get("type", "")
            )
            if not pbi_type_str:
                pbi_type_str = str(pbi_type)
        else:
            pbi_type_str = str(pbi_type)

        resolved = ""
        if pbi_type_str:
            resolved = self._map_pbi_visual_type(pbi_type_str)
            if resolved:
                return resolved

        # --- Specific Handlers for KPI/Card based on markings ---
        # Explicit check for Card/KPI strings or Text mark type
        if pbi_type_str.lower() in ["card", "kpi", "multi-row card"] or mark_type == "Text":
            return "card"

        # --- Orientation Logic for Bar vs Column ---
        # Tableau: Rows (Measure), Columns (Dim) -> Vertical (clusteredColumnChart)
        # Tableau: Rows (Dim), Columns (Measure) -> Horizontal (barChart)
        is_bar_clustered = "Clustered Bar Chart" in pbi_type_str or mark_type == "Bar"
        
        if is_bar_clustered and measure_names:
            rows = page_entry.get("rows", [])
            has_measure_in_rows = any(self.clean_field(r) in measure_names for r in rows)
            if has_measure_in_rows:
                return "clusteredColumnChart"
            else:
                return "barChart"
        return self._map_mark_type_to_visual_type(mark_type)

    # =========================================================
    # FIELD CLEANER: Strips Tableau AGG(), SUM(), ATTR(), etc.
    # =========================================================
    @staticmethod
    def clean_field(f_name):
        if isinstance(f_name, dict):
            f_name = f_name.get("field") or f_name.get("name") or f_name.get("column") or str(f_name)
        if not isinstance(f_name, str):
            f_name = str(f_name)
        
        f_name = f_name.strip()
        agg_functions = ["SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "CNTD", "CTD", "ATTR", "MEDIAN", "STDEV", "VAR", "DISTINCT", "AGG", "DISTINCTCOUNT", "COUNTNONNULL"]
        agg_regex = re.compile(r'^(' + '|'.join(agg_functions) + r')\s*\((.*)\)$', re.IGNORECASE)
        match = agg_regex.match(f_name)
        if match:
            return match.group(2).strip()
        return f_name

    @staticmethod
    def _normalize_for_matching(text: str) -> str:
        """Normalizes a string for matching by stripping, lowering, and standardizing dashes."""
        if not text: return ""
        # Standardize different dash characters to a normal hyphen
        normalized = text.strip().lower()
        normalized = normalized.replace("–", "-").replace("—", "-")
        return normalized

    @staticmethod
    def _is_none_field(f_name: str) -> bool:
        """Returns True if the field name represents an empty/None value that should be skipped."""
        if not f_name or not isinstance(f_name, str):
            return True
        stripped = f_name.strip()
        return stripped.lower() in ('none', '', 'null', 'n/a')

    def _resolve_field(self, f_name: str, display_to_bi_name: Dict, entity: str = None) -> str:
        """Resolves a display field name to its actual BI column name for PBI Property."""
        # Simple lookup in display_to_bi_name
        res = None
        if entity:
            res = display_to_bi_name.get(f"{entity}.{f_name}")
        if not res:
            res = display_to_bi_name.get(f_name, f_name)
        
        # Suffix stripping disabled as per requirement to preserve (Custom SQL Query) and other entity markers
        # if entity and "(" in res and res.endswith(")"):
        #     if f"({entity})" in res:
        #         return res.replace(f"({entity})", "").strip()
        return res

    @staticmethod
    def map_agg_to_pbi(agg_func_str):
        """Maps Tableau/SQL aggregation function names to Power BI Function integer codes."""
        if not agg_func_str:
            return None
        mapping = {
            "SUM": 0,
            "AVG": 1,
            "AVERAGE": 1,
            "COUNTD": 2,
            "CNTD": 2,
            "DISTINCTCOUNT": 2,
            "MIN": 3,
            "MAX": 4,
            "COUNT": 5,
            "COUNTNONNULL": 5,
            "MEDIAN": 6,
            "STDEV": 7,
            "STANDARDDEVIATION": 7,
            "STDEVP": 7,
            "VARIANCE": 8,
            "VAR": 8,
        }
        return mapping.get(agg_func_str.upper())

    def _parse_agg_from_instruction(self, instruction: str) -> Optional[int]:
        if not instruction or not isinstance(instruction, str):
            return None
        instr_lower = instruction.lower()
        if "count (distinct)" in instr_lower or "distinct count" in instr_lower or "count distinct" in instr_lower or "cntd" in instr_lower:
            return 2 # CNTD / DISTINCTCOUNT
        elif "count" in instr_lower:
            return 5 # COUNT
        elif "sum" in instr_lower:
            return 0 # SUM
        elif "avg" in instr_lower or "average" in instr_lower:
            return 1 # AVG
        elif "min" in instr_lower:
            return 3 # MIN
        elif "max" in instr_lower:
            return 4 # MAX
        return None

    def _create_top_n_filter_logic(self, entity: str, property_name: str, by_value_property: str, top_n: int, is_by_value_measure: bool = False, by_val_entity: Optional[str] = None, agg_func: Optional[int] = None) -> Dict:
        """Creates the TopN filter logic (Version 2)."""
        by_val_field = "Measure" if is_by_value_measure else "Column"
        
        alias_entity = "d"
        alias_by_val = "d"
        
        from_list = [{"Name": "d", "Entity": entity, "Type": 0}]
        
        if by_val_entity and by_val_entity != entity:
            # Determine a unique alias for by_val_entity, e.g. "a" for APPOINTMENTS, or first letter, or "a" if same letter
            alias_by_val = by_val_entity[0].lower()
            if alias_by_val == alias_entity:
                alias_by_val = "a" if alias_entity != "a" else "b"
            
            from_list.append({"Name": alias_by_val, "Entity": by_val_entity, "Type": 0})
            
        order_by_expr = {
            by_val_field: {
                "Expression": {"SourceRef": {"Source": alias_by_val}},
                "Property": by_value_property
            }
        }
        
        # If it's a column (dimension), we need an aggregation (usually Min/Function 3 or distinctcount/Function 2, etc.)
        if not is_by_value_measure:
            func = agg_func if agg_func is not None else 3
            order_by_expr = {
                "Aggregation": {
                    "Expression": order_by_expr,
                    "Function": func
                }
            }

        return {
            "Version": 2,
            "From": [
                {
                    "Name": "subquery",
                    "Expression": {
                        "Subquery": {
                            "Query": {
                                "Version": 2,
                                "From": from_list,
                                "Select": [
                                    {
                                        "Column": {
                                            "Expression": {"SourceRef": {"Source": alias_entity}},
                                            "Property": property_name
                                        },
                                        "Name": "field"
                                    }
                                ],
                                "OrderBy": [
                                    {
                                        "Direction": 2,
                                        "Expression": order_by_expr
                                    }
                                ],
                                "Top": top_n
                            }
                        }
                    },
                    "Type": 2
                },
                {"Name": alias_entity, "Entity": entity, "Type": 0}
            ],
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [
                                {
                                    "Column": {
                                        "Expression": {"SourceRef": {"Source": alias_entity}},
                                        "Property": property_name
                                    }
                                }
                            ],
                            "Table": {"SourceRef": {"Source": "subquery"}}
                        }
                    }
                }
            ]
        }

    @staticmethod
    def _extract_entity(f_name: str, field_to_table: Dict, default_table: str) -> str:
        """Robustly extracts entity name for a field name string."""
        if not isinstance(f_name, str):
            return default_table
            
        f_strip = f_name.strip()
        
        # 1. Direct mapping
        if f_strip in field_to_table:
            return field_to_table[f_strip]
            
        # 2. Clean aggregation and retry
        agg_functions = ["SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "CNTD", "CTD", "ATTR", "MEDIAN", "STDEV", "VAR", "DISTINCT", "AGG", "DISTINCTCOUNT", "COUNTNONNULL"]
        agg_regex = re.compile(r'^(' + '|'.join(agg_functions) + r')\s*\((.*)\)$', re.IGNORECASE)
        match = agg_regex.match(f_strip)
        if match:
            inner = match.group(2).strip()
            if inner in field_to_table:
                return field_to_table[inner]
            f_strip = inner
            
        # 3. Try suffix extraction (e.g. "Field (Table)")
        match = re.search(r'\(([^()]+)\)(?:\s*\))*\s*$', f_strip)
        if match:
            suffix_candidate = match.group(1).strip()
            # If suffix matches a known table, use it
            if suffix_candidate in field_to_table.values():
                return suffix_candidate
            # Fallback for PascalCase table names (e.g. (Doctors))
            if len(suffix_candidate) > 1 and suffix_candidate[0].isupper() and suffix_candidate not in ("Month", "Year", "Quarter", "Day", "Week", "Sum", "Avg", "Min", "Max", "Count", "Custom SQL Query"):
                return suffix_candidate
                
        return default_table

    def _find_query_ref_for_field(self, projections: List[Dict], field_name: str) -> Optional[str]:
        """Finds the queryRef for a given field name by matching nativeQueryRef or field property."""
        if not field_name:
            return None
        
        # Aggressive normalization for robust matching
        def normalize(s):
            if not s: return ""
            s = str(s).lower().strip()
            # Remove "sum of", "count of", etc.
            s = re.sub(r'^(sum|avg|min|max|count|distinctcount|countnonnull|distinct count|count of|sum of|average of)\s+', '', s)
            # Remove aggregation wrappers clean_field style
            s = re.sub(r'^[a-z_0-9]+\((.*)\)$', r'\1', s)
            # Remove table prefixes (e.g. table.field)
            s = re.sub(r'^.*?\.', '', s)
            # Remove brackets and suffixes like (Custom SQL Query)
            s = re.sub(r'\(.*?\)', '', s).replace("[", "").replace("]", "")
            return s.replace(" ", "").strip()

        field_norm = normalize(field_name)
        
        for p in projections:
            # Match nativeQueryRef
            nqr = p.get("nativeQueryRef", "")
            if normalize(nqr) == field_norm:
                return p.get("queryRef")
            
            # Match queryRef
            qr = p.get("queryRef", "")
            if normalize(qr) == field_norm:
                return p.get("queryRef")
                
            # Match field property
            field_obj = p.get("field", {})
            for key in ["Column", "Measure", "Aggregation"]:
                if key in field_obj:
                    inner = field_obj[key]
                    if key == "Aggregation" and "Expression" in inner:
                        inner = inner["Expression"].get("Column", {}) or inner["Expression"].get("Measure", {})
                    
                    prop = inner.get("Property", "")
                    if normalize(prop) == field_norm:
                        return p.get("queryRef")
                    break
        return None

    def _build_pbi_selector(self, field_name: str, query_ref: str = None, entity: str = "f") -> Optional[Dict]:
        """Builds a PBI selector for a dataPoint color mapping."""
        # PRIORITIZE metadata selector if queryRef is provided
        if query_ref:
            return {"metadata": query_ref}
        
        # Fallback for category-based selectors if no queryRef is found
        return {
            "data": [
                {
                    "queryBy": {
                        "Entity": entity,
                        "Property": field_name
                    }
                }
            ]
        }

    # =========================================================
    # MAIN ENTRY: Generate and Push Report
    # =========================================================
    async def generate_and_push_report(self, destination_folder: str, app_name: str, report_data: Optional[Dict] = None, token: str = None) -> bool:
        self.skipped_visuals = []
        files_to_push = []
        log_info(f"Generating static project files for: {app_name}")
        
        # Get context from logging_utils
        from app.tableau.core.logging_utils import _project_id_ctx, _workbook_id_ctx, _run_id_ctx, get_log_token
        p_id = _project_id_ctx.get()
        w_id = _workbook_id_ctx.get()
        r_id = _run_id_ctx.get()
        # Prefer passed token over context token for git ops
        token = token or get_log_token()

        await self.action_logger.send_activity_to_api(
            p_id, w_id, r_id, 
            technical_message=f"Starting report generation for: {app_name}",
            token=token
        )

        report_id = str(uuid.uuid4())
        model_id = str(uuid.uuid4())
        
        replacements = {
            "app_name": app_name,
            "report_logical_id": report_id,
            "model_logical_id": model_id
        }

        static_files_map = {
            f"{app_name}.Report/definition.pbir": "definition.pbir.json",
            f"{app_name}.Report/.platform": "report_platform.json",
            f"{app_name}.Report/.pbi/localSettings.json": "report_localSettings.json",
            f"{app_name}.SemanticModel/definition.pbism": "definition.pbism.json",
            f"{app_name}.SemanticModel/.platform": "semantic_platform.json",
            f"{app_name}.SemanticModel/.pbi/localSettings.json": "semantic_localSettings.json",
            f"{app_name}.SemanticModel/.pbi/editorSettings.json": "editorSettings.json",
            f"{app_name}.SemanticModel/diagramLayout.json": "diagramLayout.json",
            f"{app_name}.Report/StaticResources/SharedResources/BaseThemes/CY24SU10.json": "CY24SU10.json"
        }

        for dest_path, template_file in static_files_map.items():
            full_dest = f"{destination_folder}/{dest_path.format(app_name=app_name)}"
            content_str = self.template_manager.load_and_replace_template("static", template_file, replacements)
            files_to_push.append((full_dest, content_str, "add"))

        files_to_push.append((f"{destination_folder}/readme", f"Project: {app_name}\nGenerated by Migration Agent", "add"))

        if report_data:
            report_files = await self._generate_dynamic_visuals(report_data, app_name)
            for rel_path, file_content in report_files:
                full_dest = f"{destination_folder}/{app_name}.Report/definition/{rel_path}"
                files_to_push.append((full_dest, file_content, "add"))
                log_info(f"Generated report file: {rel_path}")

        if not files_to_push:
            log_error("No files prepared for push")
            return False

        success = await self.file_agent.batch_update_files(files_to_push, repo=Config.REPO, token=token)
        if success:
            log_info(f"Pushed {len(files_to_push)} generated files successfully")
            await self.action_logger.send_activity_to_api(
                p_id, w_id, r_id, 
                technical_message=f"Successfully pushed {len(files_to_push)} report files to repository",
                token=token
            )
        else:
            await self.action_logger.send_error_to_api(
                p_id, w_id, r_id, 
                technical_message="Failed to push report files to repository",
                token=token
            )
        return success

    # =========================================================
    # DYNAMIC VISUAL GENERATION
    # =========================================================
    def _should_add_pcustomer_slicer(self, page_entry: Dict) -> bool:
        """Determines if the pCustomer slicer should be added to the page."""
        display_name = page_entry.get("display_name", "").lower()
        if "kpi order" in display_name or "customer lifetim value" in display_name:
            return True
        
        # Check if pCustomer is used in any field of the visuals or page metrics
        fields_to_check = []
        if page_entry.get("is_dashboard"):
            for dv in page_entry.get("dashboard_visuals", []):
                for key in ("rows", "columns", "measures", "marks_text", "filters", "slicers"):
                    fields_to_check.extend(dv.get(key, []))
        else:
            for key in ("rows", "columns", "measures", "marks_text", "filters", "slicers"):
                fields_to_check.extend(page_entry.get(key, []))
        
        for field in fields_to_check:
            if isinstance(field, str) and "pcustomer" in field.lower():
                return True
            if isinstance(field, dict):
                # Check dict values for pcustomer
                for v in field.values():
                    if isinstance(v, str) and "pcustomer" in v.lower():
                        return True
        return False

    async def _generate_dynamic_visuals(self, report_data: Dict, app_name: str) -> List[Tuple[str, str]]:
        files: List[Tuple[str, str]] = []

        report_root = self.template_manager.load_json_template("visuals", "report_root.json")
        root_version = self.template_manager.load_json_template("visuals", "version.json")
        
        # Update report_root with required theme and settings as per user requirement
        report_root["themeCollection"] = {
            "baseTheme": {
                "name": "CY24SU10",
                "reportVersionAtImport": {
                    "visual": "2.4.0",
                    "report": "3.0.0",
                    "page": "2.3.0"
                },
                "type": "SharedResources"
            }
        }
        report_root["objects"] = {
            "section": [{"properties": {"verticalAlignment": {"expr": {"Literal": {"Value": "'Top'"}}}}}]
        }
        report_root["settings"] = {
            "useStylableVisualContainerHeader": True,
            "exportDataMode": "AllowSummarized",
            "defaultDrillFilterOtherVisuals": True,
            "allowChangeFilterTypes": True,
            "useEnhancedTooltips": True,
            "useDefaultAggregateDisplayName": True
        }
        
        used_custom_visuals = set()
        
        # We will write report.json at the end once used_custom_visuals is populated
        files.append(("version.json", json.dumps(root_version, indent=2, ensure_ascii=False)))

        sheets = report_data.get("pages", [])
        if not sheets:
            sheets.append({
                "page_id": "Page1",
                "display_name": "Migration Summary",
                "page_json": {"displayOption": "FitToPage", "height": 720, "width": 1280},
                "mark_type": "Automatic",
                "rows": [], "columns": [], "measures": [], "filters": [], "visuals": [] 
            })

        page_order = []
        active_page = ""
        
        field_to_table = report_data.get("field_to_table", {})
        field_to_datatype = report_data.get("field_to_datatype", {})
        measure_names = report_data.get("measure_names", [])
        default_table = report_data.get("default_table", "UnknownTable")
        display_to_bi_name = report_data.get("display_to_bi_name", {})
        parameters = report_data.get("parameters", [])
        parameter_names = {p.get("name", "") for p in parameters if isinstance(p, dict)}
        is_direct_lake = report_data.get("is_direct_lake", False)

        actions = report_data.get("actions", [])
        actions_by_source = report_data.get("actions_by_source", {})
        sheet_name_to_id = {s.get("display_name", ""): s.get("page_id", f"Page{idx+1}") for idx, s in enumerate(sheets)}

        for idx, page_entry in enumerate(sheets):
            # Check if it is a Custom Visual that should be a blank page
            pbi_visual_config = page_entry.get("power_bi_visual_type", {})
            if isinstance(pbi_visual_config, dict):
                pbi_visual_type_str = pbi_visual_config.get("power_bi_visual_type", "")
                custom_visual_name = pbi_visual_config.get("custom_visual_name", "")
            else:
                pbi_visual_type_str = str(pbi_visual_config)
                custom_visual_name = ""

            is_blank_page = False
            if pbi_visual_type_str == "Custom Visual":
                visual_type = page_entry.get("_resolved_type", "")
                if not visual_type:
                    visual_type = self._resolve_visual_type(page_entry, measure_names)
                is_html_content = (
                    "htmlcontent" in str(custom_visual_name).lower() or 
                    "htmlcontent" in str(visual_type).lower()
                )
                if not is_html_content:
                    is_blank_page = True
                    log_info(f"Custom visual '{custom_visual_name}' of type 'Custom Visual' will generate a blank screen as per user request")

            page_id = page_entry.get("page_id", f"Page{idx + 1}")
            page_order.append(page_id)
            if not active_page: active_page = page_id

            page_file = self.template_manager.load_json_template("visuals", "page.json")
            page_file["name"] = page_id
            page_file["displayName"] = page_entry.get("display_name", "Unnamed Page")
            page_file["displayOption"] = page_entry.get("page_json", {}).get("displayOption", "FitToPage")
            page_file["height"] = page_entry.get("page_json", {}).get("height", 720)
            page_file["width"] = page_entry.get("page_json", {}).get("width", 1280)

            if is_blank_page:
                files.append((f"pages/{page_id}/page.json", json.dumps(page_file, indent=2, ensure_ascii=False)))
                continue
            
            # ── Inject pCustomer slicer if needed ──
            if self._should_add_pcustomer_slicer(page_entry):
                if page_entry.get("is_dashboard"):
                    dvs = page_entry.get("dashboard_visuals", [])
                    if dvs:
                        first_dv = dvs[0]
                        current_slicers = first_dv.get("slicers", [])
                        if not any(isinstance(s, str) and "pcustomer" in s.lower() or (isinstance(s, dict) and "pcustomer" in str(s).lower()) for s in current_slicers):
                            current_slicers.append("pCustomer")
                            first_dv["slicers"] = current_slicers
                else:
                    current_slicers = page_entry.get("slicers", [])
                    if not any(isinstance(s, str) and "pcustomer" in s.lower() or (isinstance(s, dict) and "pcustomer" in str(s).lower()) for s in current_slicers):
                        current_slicers.append("pCustomer")
                        page_entry["slicers"] = current_slicers
            
            actions = report_data.get("actions", [])  # full action list (for drillthrough)
            page_filters = []
            for action in actions:
                pe = action.get("powerbi_equivalent", {})
                if pe.get("implementation_type") == "drillthrough" and pe.get("target_page") == page_entry.get("display_name"):
                    cols = pe.get("drillthrough_fields", [])
                    for c in cols:
                        entity = self._extract_entity(c, field_to_table, default_table)
                        resolved = self._resolve_field(c, display_to_bi_name, entity)
                        page_filters.append({
                            "name": str(uuid.uuid4()).replace("-", "")[:20],
                            "type": "Categorical",
                            "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}},
                            "howCreated": "User"
                        })
            
            if page_filters:
                page_file["filterConfig"] = {
                    "filters": page_filters
                }
            
            files.append((f"pages/{page_id}/page.json", json.dumps(page_file, indent=2, ensure_ascii=False)))

            # =========================================================
            # DASHBOARD PAGE (multi-visual with layout)
            # =========================================================
            if page_entry.get("is_dashboard"):
                page_visuals = self._generate_dashboard_page_visuals(
                    page_entry, field_to_table, field_to_datatype, measure_names, default_table,
                    display_to_bi_name, actions, sheet_name_to_id,
                    actions_by_source=actions_by_source, parameter_names=parameter_names, parameters=parameters,
                    is_direct_lake=is_direct_lake
                )
                for v in page_visuals:
                    v_type = v.get("visual", {}).get("visualType", "")
                    if v_type.startswith("htmlContent"):
                        used_custom_visuals.add(v_type)
                    files.append((f"pages/{page_id}/visuals/{v['name']}/visual.json", json.dumps(v, indent=2, ensure_ascii=False)))
                continue

            # =========================================================
            # SINGLE-VISUAL PAGE (original flow)
            # =========================================================
            rows = page_entry.get("rows", [])
            columns = page_entry.get("columns", [])
            measures = page_entry.get("measures", [])
            filters = page_entry.get("filters", [])
            marks_text_raw = page_entry.get("marks_text", [])
            marks_text = []
            for mt in marks_text_raw:
                if isinstance(mt, dict):
                    field_val = mt.get("field") or mt.get("name")
                    if field_val:
                        marks_text.append(field_val)
                else:
                    marks_text.append(mt)
            slicers = page_entry.get("slicers", [])
            visual_properties = page_entry.get("visual_properties", {})
            
            if not rows and not columns and not measures and not marks_text:
                log_warning(f"Page '{page_entry.get('display_name')}' has no data fields — generating placeholder card")
                # Generate a placeholder card instead of skipping
                visual_type = "card"
                try:
                    visual_container = self.template_manager.load_json_template("visuals", "card.json")
                except FileNotFoundError:
                    visual_container = self.template_manager.load_json_template("visuals", "barChart.json")
                visual_container["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
                visual_container["visual"]["visualType"] = visual_type
                visual_container["visual"]["query"] = {"queryState": {}}
                try:
                    if "title" in visual_container["visual"]["objects"]:
                        del visual_container["visual"]["objects"]["title"]
                except KeyError:
                    pass
                visual_container["position"] = {"x": 20, "y": 20, "z": 0, "height": 680, "width": 1240, "tabOrder": 0}
                files.append((f"pages/{page_id}/visuals/{visual_container['name']}/visual.json", json.dumps(visual_container, indent=2, ensure_ascii=False)))
                continue

            visual_type = self._resolve_visual_type(page_entry, measure_names=measure_names)

            # Split Tableau rows/columns into PBI Category vs Y/Value fields.
            # Tableau semantics: rows shelf = row headers (categories for bar, values for line);
            # columns shelf = column headers (values for bar, time-axis for line).
            # Correct universal rule: whichever shelf contains a measure -> Y; dimensions -> Category.
            def _split_fields(r_fields, c_fields, m_fields):
                cat_out, y_out = [], []
                for rf in list(r_fields) + list(c_fields) + list(m_fields):
                    cf = self.clean_field(rf)
                    if not cf or self._is_none_field(cf):
                        continue
                    if cf in measure_names:
                        if cf not in y_out:
                            y_out.append(cf)
                    else:
                        if cf not in cat_out:
                            cat_out.append(cf)
                return cat_out, y_out

            # Determine if we should include marks_text in primary fields (e.g. for Cards or if Axes are empty)
            include_marks_in_primary = (not rows and not columns) or visual_type in ["card", "multiRowCard"]
            m_for_split = measures + marks_text if include_marks_in_primary else measures
            
            category_fields, y_fields = _split_fields(rows, columns, m_for_split)

            # Do not inject marks_text into y_fields, as Power BI visuals often reject non-value/axes fields in Y projections


            category_projections = self._build_category_projections(category_fields, field_to_table, default_table, display_to_bi_name, is_direct_lake=is_direct_lake)
            y_projections = self._build_y_projections(y_fields, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name)

            if not y_projections and category_fields:
                f = category_fields[0]
                entity = self._extract_entity(f, field_to_table, default_table)
                resolved_f = self._resolve_field(f, display_to_bi_name, entity)
                y_projections.append({
                    "field": {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved_f}}, "Function": 5}},
                    "queryRef": f"CountNonNull({entity}.{resolved_f})", "nativeQueryRef": resolved_f
                })

            if visual_type == "card":
                query_state = {"Values": {"projections": y_projections[:1]}}
            elif visual_type in ["map", "heatmap"]:
                # Map (bubble/heatmap): separate lat/long from general category
                lat_projs, long_projs, loc_projs = [], [], []
                for cp in category_projections:
                    prop = cp.get("nativeQueryRef", "").lower()
                    if any(kw in prop for kw in ["latitude", "lat"]):
                        lat_projs.append(cp)
                    elif any(kw in prop for kw in ["longitude", "long", "lng"]):
                        long_projs.append(cp)
                    else:
                        loc_projs.append(cp)
                query_state = {}
                if loc_projs: query_state["Category"] = {"projections": loc_projs}
                if lat_projs: query_state["X"] = {"projections": lat_projs}
                if long_projs: query_state["Y"] = {"projections": long_projs}
                if y_projections: query_state["Size"] = {"projections": y_projections}
            elif visual_type == "filledMap":
                # Filled Map: Category = location, Y = saturation value
                query_state = {}
                if category_projections: query_state["Category"] = {"projections": category_projections}
                if y_projections: query_state["Y"] = {"projections": y_projections}
            else:
                query_state = {}
                if category_projections:
                    query_state["Category"] = {"projections": category_projections}
                    
                if y_projections:
                    query_state["Y"] = {"projections": y_projections}

            sort_definition = None
            if True: # Always allow sort definition for all visuals now
                if visual_type == "lineChart" and category_projections:
                    sort_definition = {"sort": [{"field": category_projections[0]["field"], "direction": "Ascending"}], "isDefaultSort": True}
                elif y_projections:
                    sort_definition = {"sort": [{"field": y_projections[0]["field"], "direction": "Descending"}], "isDefaultSort": True}
                elif category_projections:
                    sort_definition = {"sort": [{"field": category_projections[0]["field"], "direction": "Ascending"}], "isDefaultSort": True}

            query_dict = {"queryState": query_state}

            # ── Tooltips: sheet_formatting + non-None marks_text ──
            tooltip_fields = []
            for t in page_entry.get("tooltip_formatting", []):
                for f in t.get("fields_used", []):
                    if f not in tooltip_fields:
                        tooltip_fields.append(f)
            for mt in marks_text:
                cleaned_mt = self.clean_field(mt)
                if cleaned_mt and not self._is_none_field(cleaned_mt) and cleaned_mt not in tooltip_fields:
                    tooltip_fields.append(cleaned_mt)
            
            tooltip_fields_clean = []
            if tooltip_fields:
                raw_clean = list(dict.fromkeys([self.clean_field(f) for f in tooltip_fields if f and not self._is_none_field(f)]))
                valid_keys = {k.lower() for k in field_to_table.keys()} | {m.lower() for m in measure_names}
                for rc in raw_clean:
                    check_f = rc.split(".", 1)[-1] if "." in rc else rc
                    if check_f.lower() in valid_keys:
                        tooltip_fields_clean.append(rc)
                
                tip_proj = self._build_y_projections(
                    tooltip_fields_clean, field_to_table, field_to_datatype,
                    measure_names, default_table, display_to_bi_name
                )
                if tip_proj and visual_type != "card":
                    query_state["Tooltips"] = {"projections": tip_proj}

            try:
                visual_container = self.template_manager.load_json_template("visuals", f"{visual_type}.json")
            except FileNotFoundError:
                log_warning(f"Template {visual_type}.json not found. Falling back to barChart.json")
                visual_container = self.template_manager.load_json_template("visuals", "barChart.json")

            visual_container["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
            # Heatmap uses the 'map' visualType with heatmap object enabled in template
            pbi_visual_type = "map" if visual_type == "heatmap" else visual_type
            visual_container["visual"]["visualType"] = pbi_visual_type
            visual_container["visual"]["query"] = query_dict
            if sort_definition:
                visual_container["visual"]["query"]["sortDefinition"] = sort_definition

            # Apply visual_properties (fonts, colors, axis titles) if present
            pbi_visual_config = page_entry.get("power_bi_visual_type", {})
            self._apply_visual_properties(visual_container, visual_properties, pbi_config=pbi_visual_config, parent_type="sheet")

            # Enable data labels by default if mark_type is present and not 'None'
            mark_type = page_entry.get("mark_type")
            if mark_type and not self._is_none_field(mark_type):
                objs = visual_container.setdefault("visual", {}).setdefault("objects", {})
                if "labels" not in objs:
                    objs["labels"] = [{
                        "properties": {
                            "show": {"expr": {"Literal": {"Value": "true"}}}
                        }
                    }]

            # ── filterConfig: auto from axis fields ──
            auto_filters = []
            for cf in category_fields:
                entity = self._extract_entity(cf, field_to_table, default_table)
                resolved = self._resolve_field(cf, display_to_bi_name, entity)
                auto_filters.append({
                    "name": str(uuid.uuid4()).replace("-", "")[:20],
                    "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}},
                    "type": "Categorical"
                })
            for yf in y_fields:
                entity = self._extract_entity(yf, field_to_table, default_table)
                resolved = self._resolve_field(yf, display_to_bi_name, entity)
                if yf in measure_names:
                    field_expr = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}
                else:
                    dt = field_to_datatype.get(yf, "string")
                    func = 0 if dt in ["integer", "real"] else 2
                    field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}, "Function": func}}
                auto_filters.append({
                    "name": str(uuid.uuid4()).replace("-", "")[:20],
                    "field": field_expr,
                    "type": "Advanced"
                })
            # Build set of clean category, y, and y2 fields to skip duplicate tooltip filters
            skip_filter_fields = set()
            for f in list(category_fields) + list(y_fields):
                skip_filter_fields.add(self.clean_field(f).lower())

            for tf in tooltip_fields_clean:
                clean_tf = self.clean_field(tf)
                if clean_tf.lower() in skip_filter_fields:
                    continue
                entity = self._extract_entity(tf, field_to_table, default_table)
                resolved = self._resolve_field(tf, display_to_bi_name, entity)
                if tf in measure_names:
                    field_expr = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}
                else:
                    dt = field_to_datatype.get(tf, "string")
                    func = 0 if dt in ["integer", "real"] else 2
                    field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}, "Function": func}}
                auto_filters.append({
                    "name": str(uuid.uuid4()).replace("-", "")[:20],
                    "field": field_expr,
                    "type": "Advanced"
                })
            visual_container["filterConfig"] = {"filters": auto_filters}

            # ── filterConfig: explicit sheet filters (tableau_type preserved) ──
            for filter_item in filters:
                raw_ff = filter_item.get("tableau_original_name", "") if isinstance(filter_item, dict) else str(filter_item)
                if not raw_ff or self._is_none_field(raw_ff):
                    continue
                ff = self.clean_field(raw_ff)
                if not ff or ff in ["Measure Names", "Measure Values"]:
                    continue
                entity = self._extract_entity(raw_ff, field_to_table, default_table)
                resolved_ff = self._resolve_field(ff, display_to_bi_name, entity)
                dt = field_to_datatype.get(ff, "string")
                is_numeric = dt in ["integer", "real"]

                if ff in measure_names:
                    field_expr = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved_ff}}
                    f_type = "Advanced"
                else:
                    was_agg = (raw_ff != ff and bool(re.match(r'^[A-Z0-9_]+\((.*)\)$', raw_ff, re.IGNORECASE)))
                    if was_agg:
                        func = 0 if is_numeric else 2
                        field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved_ff}}, "Function": func}}
                        f_type = "Advanced"
                    else:
                        field_expr = {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved_ff}}
                        f_type = "Categorical" if dt == "string" else "Advanced"

                # Apply default value 1 for param-based filters (e.g. Top N/Bottom N Filter)
                is_param = False
                if isinstance(filter_item, dict) and filter_item.get("tableau_type") == "parameter":
                    is_param = True
                elif parameter_names and ff in parameter_names:
                    is_param = True
                elif "top n filter" in ff.lower() or "bottom n filter" in ff.lower() or "top n filter" in raw_ff.lower() or "bottom n filter" in raw_ff.lower():
                    is_param = True

                filter_obj = {
                    "name": str(uuid.uuid4()).replace("-", "")[:20],
                    "field": field_expr,
                    "type": "Advanced" if is_param else f_type,
                    "howCreated": "User"
                }
                
                # Handle Top N filter type
                is_top_n = False
                f_type_val = ""
                if isinstance(filter_item, dict):
                    f_type_val = str(filter_item.get("filter_type", "") or filter_item.get("filterType", "") or "").strip().lower()
                    if f_type_val in ["top n", "topn"]:
                        is_top_n = True
                
                if is_top_n:
                    top_n_val = 10
                    try:
                        top_n_val = int(filter_item.get("value") or filter_item.get("top") or 10)
                    except (ValueError, TypeError):
                        pass
                    
                    by_val_raw = filter_item.get("By_value") or filter_item.get("by_value") or raw_ff
                    by_val_clean = self.clean_field(by_val_raw)
                    by_val_entity = self._extract_entity(by_val_raw, field_to_table, default_table)
                    by_val_resolved = self._resolve_field(by_val_clean, display_to_bi_name, by_val_entity)
                    is_by_val_measure = by_val_clean in measure_names or by_val_raw in measure_names
                    
                    # Determine aggregation function if any
                    agg_func = None
                    if isinstance(filter_item, dict):
                        # 1. Check if there's an explicit aggregation/agg/function in the filter_item itself
                        agg_val = filter_item.get("aggregation") or filter_item.get("agg") or filter_item.get("function")
                        if agg_val:
                            agg_func = self.map_agg_to_pbi(str(agg_val))
                        
                        # 2. Check power_bi_instruction
                        if agg_func is None:
                            instruction = filter_item.get("power_bi_instruction")
                            if instruction:
                                agg_func = self._parse_agg_from_instruction(instruction)
                                
                    # 3. Check page_entry's rows/columns nested sorts
                    if agg_func is None:
                        for row_or_col in list(page_entry.get("rows", [])) + list(page_entry.get("columns", [])):
                            if isinstance(row_or_col, dict) and "sort" in row_or_col:
                                s = row_or_col["sort"]
                                if isinstance(s, dict) and s.get("field_name") == by_val_raw:
                                    agg_val = s.get("aggregation")
                                    if agg_val:
                                        agg_func = self.map_agg_to_pbi(str(agg_val))
                                        if agg_func is not None:
                                            break

                    filter_obj["type"] = "TopN"
                    if "howCreated" in filter_obj:
                        del filter_obj["howCreated"]
                        
                    filter_obj["filter"] = self._create_top_n_filter_logic(
                        entity, resolved_ff, by_val_resolved, top_n_val, is_by_value_measure=is_by_val_measure, by_val_entity=by_val_entity, agg_func=agg_func
                    )
                elif is_param:
                    # If not in field_to_table, try to use the resolved field name as the entity (standard parameter table)
                    if not field_to_table.get(ff):
                        entity = resolved_ff
                    filter_logic = self._create_parameter_filter_logic(entity, resolved_ff, is_measure=(ff in measure_names))
                    if filter_logic:
                        filter_obj["filter"] = filter_logic

                visual_container["filterConfig"]["filters"].append(filter_obj)

            # =========================================================
            # LAYOUT: main visual (left) + slicers (right panel)
            # =========================================================
            page_visuals = []
            slicer_count = len([s for s in slicers if s])
            main_width = 960 if slicer_count > 0 else 1240

            visual_container["position"] = {
                "x": 20, "y": 20, "z": 0,
                "height": 680, "width": main_width, "tabOrder": 0
            }
            page_visuals.append(visual_container)

            # Slicer visuals stacked on the right
            if slicers:
                slicer_x = main_width + 30
                slicer_y = 20
                slicer_h = max(80, 680 // max(len(slicers), 1))
                for s_idx, slicer_def in enumerate(slicers):
                    if not slicer_def:
                        continue
                    sv = self._build_slicer_visual(
                        slicer_def, field_to_table, field_to_datatype, measure_names,
                        default_table, display_to_bi_name, parameter_names, parameters
                    )
                    if sv:
                        sv["position"] = {
                            "x": slicer_x, "y": slicer_y, "z": s_idx + 1,
                            "height": slicer_h - 10, "width": 230, "tabOrder": s_idx + 1
                        }
                        page_visuals.append(sv)
                        slicer_y += slicer_h

                # HTML Content Visuals via actions_by_source index
                for act in actions_by_source.get(page_entry.get("display_name", ""), []):
                    html_vis = self._build_html_content_visual(
                        act, default_table
                    )
                    if html_vis:
                        # Provide a default position stacked below
                        html_vis["position"] = {
                            "x": 20, "y": 720, "z": 0,
                            "height": 400, "width": 800, "tabOrder": 0
                        }
                        page_visuals.append(html_vis)
            
            # ── Action Buttons for Navigation (Single Visual Page) ──
            norm_page = self._normalize_for_matching(page_entry.get("display_name", ""))
            for act in actions:
                ta = act.get("tableau_action", {})
                pe = act.get("powerbi_equivalent", {})
                
                src_ws = self._normalize_for_matching(ta.get("source_worksheet", ""))
                src_vis = self._normalize_for_matching(pe.get("source_visual", ""))
                src_dash = self._normalize_for_matching(pe.get("source_dashboard", ta.get("source_dashboard", "")))
                
                is_nav = "page navigation" in pe.get("implementation_type", "").lower() or ta.get("type", "").lower() == "navigation"
                # For single-visual pages, we match on dashboard name or sheet name or "all sheets"
                if is_nav:
                    should_add = False
                    if src_ws == norm_page or src_vis == norm_page or src_dash == norm_page:
                        should_add = True
                    elif src_vis in ["all sheets", ""] and src_dash == norm_page:
                        should_add = True
                        
                    if should_add:
                        btn_vis = self._build_action_button_visual(act, sheet_name_to_id)
                        if btn_vis:
                            # If it's dashboard-level or matches page name, full page overlay might be better
                            # but for single visuals, we stick to visual container position
                            btn_vis["position"] = visual_container["position"].copy()
                            btn_vis["position"]["z"] = visual_container["position"].get("z", 0) + 1
                            page_visuals.append(btn_vis)

            # =========================================================

            for v in page_visuals:
                v_type = v.get("visual", {}).get("visualType", "")
                if v_type.startswith("htmlContent"):
                    used_custom_visuals.add(v_type)
                files.append((f"pages/{page_id}/visuals/{v['name']}/visual.json", json.dumps(v, indent=2, ensure_ascii=False)))

        # Register custom visuals and write report.json
        if used_custom_visuals:
            report_root["publicCustomVisuals"] = list(used_custom_visuals)
        files.append(("report.json", json.dumps(report_root, indent=2, ensure_ascii=False)))

        pages_metadata = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
            "pageOrder": page_order, "activePageName": active_page
        }
        files.append(("pages/pages.json", json.dumps(pages_metadata, indent=2, ensure_ascii=False)))
        files.append(("pages/version.json", json.dumps(root_version, indent=2, ensure_ascii=False)))

        return files

    # =========================================================
    # DASHBOARD PAGE: Generate Multiple Visuals per Page
    # =========================================================
    def _generate_dashboard_page_visuals(self, page_entry: Dict, field_to_table: Dict,
                                          field_to_datatype: Dict, measure_names: List,
                                          default_table: str, display_to_bi_name: Dict = None,
                                          actions: List[Dict] = None, sheet_name_to_id: Dict = None,
                                          actions_by_source: Dict = None,
                                          parameter_names: set = None, parameters: List[Dict] = None,
                                          is_direct_lake: bool = False) -> List[Dict]:
        """Generates all visuals for a dashboard page (multi-visual with layout)."""
        if actions is None: actions = []
        if sheet_name_to_id is None: sheet_name_to_id = {}
        if display_to_bi_name is None: display_to_bi_name = {}
        if actions_by_source is None: actions_by_source = {}
        if parameter_names is None: parameter_names = set()
        if parameters is None: parameters = []
        page_visuals = []
        dashboard_visuals = page_entry.get("dashboard_visuals", [])
        text_headers = page_entry.get("text_headers", [])

        # Flatten zone_hierarchy to get styles by visual name
        visual_styles = {}
        zone_hierarchy = page_entry.get("zone_hierarchy", {})
        # Prefer 'Desktop' layout for styles, fall back to others
        for layout_name in ["Desktop", "Main", "Tablet", "Phone"]:
            zones = zone_hierarchy.get(layout_name, [])
            if zones:
                visual_styles.update(self._flatten_zone_hierarchy(zones))
                if visual_styles: break # Found primary layout styles
        
        num_visuals = len(dashboard_visuals)
        z_idx = 0
        tab_ord = 0
        
        # =========================================================
        # 1. TEXT HEADERS (textbox visuals at the top)
        # =========================================================
        header_y = 10
        for th_idx, th in enumerate(text_headers):
            header_text = th.get("text", "Header")
            font_name = th.get("font_name", "Arial")
            font_size = th.get("font_size", "16")
            font_color = th.get("font_color", "#000000")
            is_bold = th.get("bold", True)
            
            try:
                textbox = self.template_manager.load_json_template("visuals", "textbox.json")
            except (FileNotFoundError, json.JSONDecodeError) as e:
                log_warning(f"textbox.json template not found, skipping header: {e!r}")
                self._record_skipped(header_text, reason=str(e))
                continue
                
            textbox["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
            textbox["position"] = {
                "x": 20, "y": header_y, "z": z_idx, 
                "height": 60, "width": 1240, "tabOrder": tab_ord
            }
            
            # Set the text content with formatting
            textbox["visual"]["objects"]["general"][0]["properties"]["paragraphs"] = [
                {
                    "textRuns": [
                        {
                            "value": header_text,
                            "textStyle": {
                                "fontFamily": font_name,
                                "fontSize": f"{font_size}px",
                                "fontWeight": "bold" if is_bold else "normal",
                                "color": font_color
                            }
                        }
                    ]
                }
            ]
            
            page_visuals.append(textbox)
            header_y += 70
            z_idx += 1
            tab_ord += 1

        # =========================================================
        # 2. DATA VISUALS (charts, cards, tables from sheet_visuals)
        # =========================================================
        visual_start_y = header_y + 10 if text_headers else 20
        
        # Calculate grid layout: arrange visuals in rows
        # KPI cards (card type) go in a row at the top, larger visuals below
        kpi_visuals = []
        chart_visuals = []
        
        for dv in dashboard_visuals:
            pbi_type = dv.get("power_bi_visual_type", "")
            mark_type = dv.get("mark_type", "Automatic")
            
            resolved_type = ""
            if pbi_type:
                resolved_type = self._map_pbi_visual_type(pbi_type)
            if not resolved_type:
                resolved_type = self._map_mark_type_to_visual_type(mark_type)
            
            dv["_resolved_type"] = self._resolve_visual_type(dv, measure_names=measure_names)
            
            if dv["_resolved_type"] in ["card", "multiRowCard"]:
                kpi_visuals.append(dv)
            else:
                chart_visuals.append(dv)
        
        # Handle "web" layout objects if present
        for obj in page_entry.get("layout", {}).get("children", []):
            attr = obj.get("attributes", {})
            if attr.get("type-v2") == "web":
                url = attr.get("param")
                if url:
                    # Treat as an HTML Content visual
                    try:
                        html_vis = self.template_manager.load_json_template("visuals", "htmlContent443BE3AD55E043BF878BED274D3A6855.json")
                        html_vis["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
                        # Position mapping based on layout attrs
                        raw_x = int(attr.get("x", 0))
                        raw_y = int(attr.get("y", 0))
                        raw_w = int(attr.get("w", 10000))
                        raw_h = int(attr.get("h", 10000))
                        # Scale factor: User json uses 0-100000 range for dashboard layout usually, 
                        # but PBI uses absolute pixels. 
                        # Migration logic mapping: 100000 -> 1280 (PBI page width)
                        html_vis["position"] = {
                            "x": (raw_x * 1280) // 100000,
                            "y": (raw_y * 720) // 100000,
                            "width": (raw_w * 1280) // 100000,
                            "height": (raw_h * 720) // 100000,
                            "z": z_idx, "tabOrder": tab_ord
                        }
                        page_visuals.append(html_vis)
                        z_idx += 1
                        tab_ord += 1
                    except (FileNotFoundError, json.JSONDecodeError) as e:
                        log_warning(f"htmlContent...json template missing for web layout object: {e!r}")
                        self._record_skipped(url, reason=str(e))
        
        current_y = visual_start_y
        
        # Layout KPI cards horizontally
        if kpi_visuals:
            kpi_width = max(200, 1240 // len(kpi_visuals)) if kpi_visuals else 250
            kpi_x = 20
            for kpi in kpi_visuals:
                visual = self._build_data_visual(kpi, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, parameter_names=parameter_names, is_direct_lake=is_direct_lake)
                if visual:
                    visual["position"] = {
                        "x": kpi_x, "y": current_y, "z": z_idx,
                        "height": 120, "width": min(kpi_width - 10, 300),
                        "tabOrder": tab_ord
                    }
                    
                    # Apply styles from zone_hierarchy if matched
                    kpi_name = kpi.get("display_name", kpi.get("name", ""))
                    style = visual_styles.get(kpi_name)
                    if not style:
                        # Fallback: normalized match
                        norm_kpi = kpi_name.strip().lower()
                        for k, v in visual_styles.items():
                            if k.strip().lower() == norm_kpi:
                                style = v
                                break
                    
                    if style:
                        self._apply_container_styles(visual, style)
                        
                    page_visuals.append(visual)

                    # Add Action Button for Navigation if defined
                    kpis_to_match = [
                        self._normalize_for_matching(kpi.get("display_name", "")),
                        self._normalize_for_matching(kpi.get("name", "")),
                        self._normalize_for_matching(kpi.get("tableau_sheet_name", ""))
                    ]
                    for act in actions:
                        ta = act.get("tableau_action", {})
                        pe = act.get("powerbi_equivalent", {})
                        
                        src_ws = self._normalize_for_matching(ta.get("source_worksheet", ""))
                        src_vis = self._normalize_for_matching(pe.get("source_visual", ""))
                        
                        is_nav = "page navigation" in pe.get("implementation_type", "").lower() or ta.get("type", "").lower() == "navigation"
                        
                        if is_nav and (src_ws in kpis_to_match or src_vis in kpis_to_match):
                            btn_vis = self._build_action_button_visual(act, sheet_name_to_id)
                            if btn_vis:
                                btn_vis["position"] = visual["position"].copy()
                                btn_vis["position"]["z"] = visual["position"].get("z", 0) + 1
                                page_visuals.append(btn_vis)

                    kpi_x += kpi_width
                    z_idx += 2
                    tab_ord += 2
            
            current_y += 140  # Move below KPI row
        
        # Layout chart visuals in rows of 2
        chart_x = 20
        charts_in_row = 0
        chart_width = 610 if len(chart_visuals) > 1 else 1240
        chart_height = 350
        
        for cv in chart_visuals:
            visual = self._build_data_visual(cv, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, parameter_names=parameter_names, is_direct_lake=is_direct_lake)
            if visual:
                pos = {
                    "x": chart_x, "y": current_y, "z": z_idx,
                    "height": chart_height, "width": chart_width,
                    "tabOrder": tab_ord
                }
                visual["position"] = pos.copy()
                
                # Apply styles from zone_hierarchy if matched
                cv_name = cv.get("display_name", cv.get("name", cv.get("tableau_sheet_name", "")))
                style = visual_styles.get(cv_name)
                if not style:
                    # Fallback: normalized match
                    norm_cv = cv_name.strip().lower()
                    for k, v in visual_styles.items():
                        if k.strip().lower() == norm_cv:
                            style = v
                            break
                
                if style:
                    self._apply_container_styles(visual, style)
                
                page_visuals.append(visual)

                # Add Action Button for Navigation if defined
                cv_to_match = [
                    self._normalize_for_matching(cv.get("display_name", "")),
                    self._normalize_for_matching(cv.get("name", "")),
                    self._normalize_for_matching(cv.get("tableau_sheet_name", ""))
                ]
                for act in actions:
                    ta = act.get("tableau_action", {})
                    pe = act.get("powerbi_equivalent", {})
                    
                    src_ws = self._normalize_for_matching(ta.get("source_worksheet", ""))
                    src_vis = self._normalize_for_matching(pe.get("source_visual", ""))
                    
                    is_nav = "page navigation" in pe.get("implementation_type", "").lower() or ta.get("type", "").lower() == "navigation"
                    
                    if is_nav and (src_ws in cv_to_match or src_vis in cv_to_match):
                        btn_vis = self._build_action_button_visual(act, sheet_name_to_id)
                        if btn_vis:
                            btn_vis["position"] = pos.copy()
                            btn_vis["position"]["z"] = pos.get("z", 0) + 1
                            page_visuals.append(btn_vis)

                # HTML Content Visual via actions_by_source
                for act in actions_by_source.get(cv_name, []):
                    html_vis = self._build_html_content_visual(act, default_table)
                    if html_vis:
                        # Stack HTML visual below the chart
                        html_vis["position"] = {
                            "x": pos["x"], "y": pos["y"] + pos["height"] + 20, "z": z_idx + 2,
                            "height": 300, "width": pos["width"], "tabOrder": tab_ord + 2
                        }
                        page_visuals.append(html_vis)

                # Slicer visuals for this chart (stacked below chart if any)
                cv_slicers = cv.get("slicers", [])
                if cv_slicers:
                    sl_x = chart_x
                    sl_y = current_y + chart_height + 10
                    sl_w = max(180, chart_width // max(len(cv_slicers), 1)) - 5
                    for sl_def in cv_slicers:
                        if not sl_def:
                            continue
                        sv = self._build_slicer_visual(
                            sl_def, field_to_table, field_to_datatype, measure_names,
                            default_table, display_to_bi_name, parameter_names, parameters
                        )
                        if sv:
                            sv["position"] = {
                                "x": sl_x, "y": sl_y, "z": z_idx + 1,
                                "height": 80, "width": sl_w, "tabOrder": tab_ord + 1
                            }
                            page_visuals.append(sv)
                            sl_x += sl_w + 5

                z_idx += 3
                tab_ord += 3
                charts_in_row += 1

                if charts_in_row >= 2:
                    current_y += chart_height + 20
                    chart_x = 20
                    charts_in_row = 0
                else:
                    chart_x += chart_width + 20

        if charts_in_row > 0:
            current_y += chart_height + 20

        # =========================================================
        # 3. DASHBOARD-LEVEL / ALL SHEETS ACTIONS
        # =========================================================
        dashboard_name = page_entry.get("display_name", "")
        norm_dash = self._normalize_for_matching(dashboard_name)
        added_global_actions = set()

        for act in actions:
            ta = act.get("tableau_action", {})
            pe = act.get("powerbi_equivalent", {})
            src_dash = self._normalize_for_matching(pe.get("source_dashboard") or ta.get("source_dashboard", ""))
            src_vis = self._normalize_for_matching(pe.get("source_visual") or ta.get("source_worksheet", ""))

            if src_dash == norm_dash and (src_vis in ["all sheets", ""] or not src_vis):
                act_id = act.get("action_id", str(id(act)))
                if act_id not in added_global_actions:
                    added_global_actions.add(act_id)
                    
                    # 1. HTML Content Visual
                    html_vis = self._build_html_content_visual(act, default_table)
                    if html_vis:
                        html_vis["position"] = {
                            "x": 20, "y": current_y, "z": z_idx,
                            "height": 400, "width": 800, "tabOrder": tab_ord
                        }
                        page_visuals.append(html_vis)
                        current_y += 420
                        z_idx += 1
                        tab_ord += 1

                    # 2. Action Button for Navigation (Full Page Overlay)
                    is_nav = "page navigation" in pe.get("implementation_type", "").lower() or ta.get("type", "").lower() == "navigation"
                    if is_nav:
                        btn_vis = self._build_action_button_visual(act, sheet_name_to_id)
                        if btn_vis:
                            pg_json = page_entry.get("page_json", {})
                            btn_vis["position"] = {
                                "x": 0, "y": 0, "z": z_idx + 10,
                                "height": pg_json.get("height", 720),
                                "width": pg_json.get("width", 1280),
                                "tabOrder": tab_ord
                            }
                            page_visuals.append(btn_vis)
                            z_idx += 1
                            tab_ord += 1

        
        return page_visuals


    # =========================================================
    # HELPER: Build HTML Content Visual for Iframe Embedding
    # =========================================================
    def _build_html_content_visual(self, action: Dict, default_table: str) -> Optional[Dict]:
        """Creates an HTML Content Visual if the action specifies Iframe Embedding."""
        if not action: return None
        
        pe = action.get("powerbi_equivalent", {})
        if pe.get("implementation_type") != "HTML Content Viewer (Iframe Embedding)":
            return None
            
        measure_name = pe.get("dax_measure_name")
        if not measure_name:
            # Need a measure to embed
            return None
            
        target_table = pe.get("target_table", default_table)
        
        try:
            html_vis = self.template_manager.load_json_template("visuals", "htmlContent443BE3AD55E043BF878BED274D3A6855.json")
        except FileNotFoundError:
            log_warning("htmlContent...json template missing, skipping HTML visual generation")
            return None
            
        html_vis["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
        
        # Populate projection for the HTML content 
        html_vis["visual"]["query"]["queryState"]["content"] = {
            "projections": [
                {
                    "field": {
                        "Measure": {
                            "Expression": {
                                "SourceRef": {
                                    "Entity": target_table
                                }
                            },
                            "Property": measure_name
                        }
                    },
                    "queryRef": f"{target_table}.{measure_name}",
                    "nativeQueryRef": measure_name
                }
            ]
        }
        
        return html_vis

    # =========================================================
    # HELPER: Build Action Button Visual for Navigation
    # =========================================================
    def _build_action_button_visual(self, action: Dict, sheet_name_to_id: Dict) -> Optional[Dict]:
        """Creates an actionButton visual for Page Navigation."""
        if not action: return None
        
        ta = action.get("tableau_action", {})
        pe = action.get("powerbi_equivalent", {})
        
        # Check for navigation type
        is_pbi_nav = "page navigation" in pe.get("implementation_type", "").lower()
        is_tab_nav = ta.get("type", "").lower() == "navigation"
        
        if not (is_pbi_nav or is_tab_nav):
            return None
            
        target_sheet = pe.get("target_page") or ta.get("target_sheet")
        if not target_sheet:
            return None
            
        # Robust target page resolution
        target_page_id = None
        norm_target = target_sheet.strip().lower()
        
        # 1. Exact or normalized mapping match
        target_page_id = sheet_name_to_id.get(target_sheet)
        if not target_page_id:
            for sheet_name, page_id in sheet_name_to_id.items():
                if sheet_name.strip().lower() == norm_target:
                    target_page_id = page_id
                    break
        
        # 2. Fuzzy match
        if not target_page_id:
            for sheet_name, page_id in sheet_name_to_id.items():
                if norm_target in sheet_name.lower():
                    target_page_id = page_id
                    break
        
        # 3. Default fallback
        if not target_page_id:
            target_page_id = "Page1"
                    
        try:
            btn_vis = self.template_manager.load_json_template("visuals", "actionButton.json")
        except FileNotFoundError:
            log_warning("actionButton.json template missing, skipping Action Button generation")
            return None
            
        btn_vis["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
        btn_vis["howCreated"] = "InsertVisualButton"
        
        # Configure navigation action
        vco = btn_vis["visual"].setdefault("visualContainerObjects", {})
        v_link = vco.setdefault("visualLink", [{}])
        if not v_link: v_link.append({})
        v_link_props = v_link[0].setdefault("properties", {})
        
        v_link_props["show"] = {"expr": {"Literal": {"Value": "true"}}}
        v_link_props["type"] = {"expr": {"Literal": {"Value": "'PageNavigation'"}}}
        v_link_props["navigationSection"] = {"expr": {"Literal": {"Value": f"'{target_page_id}'"}}}
        
        # Make the button transparent/blank as it will overlay a KPI/Chart
        objs = btn_vis["visual"].setdefault("objects", {})
        icon = objs.setdefault("icon", [{}])
        if not icon: icon.append({})
        icon[0].setdefault("properties", {})["shapeType"] = {"expr": {"Literal": {"Value": "'blank'"}}}
        
        # Hide text by default
        text = objs.setdefault("text", [{}])
        if not text: text.append({})
        text[0].setdefault("properties", {})["show"] = {"expr": {"Literal": {"Value": "false"}}}
        
        return btn_vis

    # =========================================================
    # HELPER: Build a single data visual (chart/card/table)
    # =========================================================
    def _build_data_visual(self, dv: Dict, field_to_table: Dict, field_to_datatype: Dict, 
                           measure_names: List, default_table: str, display_to_bi_name: Dict = None, parameter_names: set = None, is_direct_lake: bool = False) -> Optional[Dict]:
        """Builds a single data visual container from dashboard visual data."""
        import re
        agg_functions = ["SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "CNTD", "CTD", "CNT", "ATTR", "MEDIAN", "STDEV", "VAR", "DISTINCT", "AGG"]
        agg_regex = re.compile(r'^(' + '|'.join(agg_functions) + r')\s*\((.*)\)$', re.IGNORECASE)

        if display_to_bi_name is None:
            display_to_bi_name = {}
        if parameter_names is None:
            parameter_names = set()

        # Check if it is a Custom Visual that should be skipped
        pbi_visual_config = dv.get("power_bi_visual_type", {})
        if isinstance(pbi_visual_config, dict):
            pbi_visual_type_str = pbi_visual_config.get("power_bi_visual_type", "")
            custom_visual_name = pbi_visual_config.get("custom_visual_name", "")
        else:
            pbi_visual_type_str = str(pbi_visual_config)
            custom_visual_name = ""

        if pbi_visual_type_str == "Custom Visual":
            visual_type = dv.get("_resolved_type", "")
            if not visual_type:
                visual_type = self._resolve_visual_type(dv, measure_names)
            is_html_content = (
                "htmlcontent" in str(custom_visual_name).lower() or 
                "htmlcontent" in str(visual_type).lower()
            )
            if not is_html_content:
                log_info(f"Skipping custom visual '{custom_visual_name}' of type 'Custom Visual' as per user request")
                return None

        visual_type = dv.get("_resolved_type")
        if not visual_type:
            visual_type = self._resolve_visual_type(dv, measure_names)
        if not visual_type:
            visual_type = "barChart"
        display_name = dv.get("display_name", "Visual")
        
        rows = dv.get("rows", [])
        columns = dv.get("columns", [])
        measures = dv.get("measures", [])
        marks_text_raw = dv.get("marks_text", [])
        marks_text = []
        for mt in marks_text_raw:
            if isinstance(mt, dict):
                field_val = mt.get("field") or mt.get("name")
                if field_val:
                    marks_text.append(field_val)
            else:
                marks_text.append(mt)
        marks_color = dv.get("marks_color", [])
        filters = dv.get("filters", [])
        
        # Build field lists — correct: measures always go to Y, dimensions to Category
        def _split_fields(r_fields, c_fields, m_fields, pbi_config=None):
            cat_out, y_out = [], []
            
            # PRIORITIZE Power BI Config for field mapping
            if pbi_config:
                pbi_rows = pbi_config.get("rows", [])
                pbi_cols = pbi_config.get("columns", [])
                pbi_fields = pbi_config.get("fields", [])
                
                # For most visuals, rows/columns map to Category/Y
                # This handles explicit mapping from power_bi_visual_type
                for rf in pbi_rows:
                    cf = self.clean_field(rf)
                    if cf and not self._is_none_field(cf) and cf not in cat_out:
                        cat_out.append(cf)
                
                for cf_raw in pbi_cols:
                    cf = self.clean_field(cf_raw)
                    if cf and not self._is_none_field(cf) and cf not in y_out:
                        y_out.append(cf)
                
                for f in pbi_fields:
                    cf = self.clean_field(f)
                    if cf and not self._is_none_field(cf):
                        if cf in measure_names:
                            if cf not in y_out: y_out.append(cf)
                        elif cf not in cat_out:
                            cat_out.append(cf)
                
                if cat_out or y_out:
                    return cat_out, y_out

            # FALLBACK to default Tableau split logic
            for rf in list(r_fields) + list(c_fields) + list(m_fields):
                cf = self.clean_field(rf)
                if not cf or self._is_none_field(cf):
                    continue
                if cf in measure_names or (rf != cf and rf): # Measure or includes aggregation
                    if cf not in y_out:
                        y_out.append(cf)
                else:
                    if cf not in cat_out:
                        cat_out.append(cf)
            return cat_out, y_out

        # Load visual configuration if present
        pbi_config = dv.get("power_bi_visual_type")
        if isinstance(pbi_config, str): pbi_config = None # Handle cases where it's just a string name

        # Determine if we should include marks_text in primary fields (e.g. for Cards or if Axes are empty)
        include_marks_in_primary = (not rows and not columns) or visual_type in ["card", "multiRowCard"]
        m_for_split = measures + marks_text if include_marks_in_primary else measures
        
        category_fields, y_fields = _split_fields(rows, columns, m_for_split, pbi_config=pbi_config)
        
        # Do not inject marks_text into y_fields, as Power BI visuals often reject non-value/axes fields in Y projections
        
        if not category_fields and not y_fields:
            # No data fields — generate a placeholder card rather than skipping
            log_warning(f"Dashboard visual '{display_name}' has no resolvable fields — generating placeholder card")
            visual_type = "card"
            try:
                placeholder = self.template_manager.load_json_template("visuals", "card.json")
            except FileNotFoundError:
                placeholder = self.template_manager.load_json_template("visuals", "barChart.json")
            placeholder["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
            placeholder["visual"]["visualType"] = visual_type
            placeholder["visual"]["query"] = {"queryState": {}}
            try:
                if "title" in placeholder["visual"]["objects"]:
                    del placeholder["visual"]["objects"]["title"]
            except KeyError:
                pass
            return placeholder
        
        category_projections = self._build_category_projections(category_fields, field_to_table, default_table, display_to_bi_name, is_direct_lake=is_direct_lake)
        y_projections = self._build_y_projections(y_fields, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name)
        
        # If still no y_projections, create count of first category
        if not y_projections and category_fields:
            f = category_fields[0]
            entity = self._extract_entity(f, field_to_table, default_table)
            resolved_f = self._resolve_field(f, display_to_bi_name, entity)
            y_projections.append({
                "field": {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved_f}}, "Function": 2}},
                "queryRef": f"CountNonNull({entity}.{resolved_f})", "nativeQueryRef": resolved_f
            })
        
        # Build query state
        if visual_type in ["card", "multiRowCard"]:
            query_state = {"Values": {"projections": y_projections[:1] if y_projections else category_projections[:1]}}
        else:
            query_state = {}
            if category_projections:
                query_state["Category"] = {"projections": category_projections}
                
            if y_projections:
                query_state["Y"] = {"projections": y_projections}
        
        # Build sort
        sort_definition = None
        if visual_type == "lineChart" and category_projections:
            sort_definition = {"sort": [{"field": category_projections[0]["field"], "direction": "Ascending"}], "isDefaultSort": True}
        elif y_projections:
            sort_definition = {"sort": [{"field": y_projections[0]["field"], "direction": "Descending"}], "isDefaultSort": True}
        elif category_projections:
            sort_definition = {"sort": [{"field": category_projections[0]["field"], "direction": "Ascending"}], "isDefaultSort": True}
        
        query_dict = {"queryState": query_state}

        tooltip_formatting = dv.get("tooltip_formatting", [])
        tooltip_fields_clean = []
        if tooltip_formatting:
            tooltip_fields = []
            for t in tooltip_formatting:
                fields = t.get("fields_used", [])
                if isinstance(fields, list):
                    tooltip_fields.extend(fields)
            
            for mt in marks_text:
                cleaned_mt = self.clean_field(mt)
                if cleaned_mt and not self._is_none_field(cleaned_mt) and cleaned_mt not in tooltip_fields:
                    tooltip_fields.append(cleaned_mt)
            
            raw_clean = list(dict.fromkeys([self.clean_field(f) for f in tooltip_fields if f and not self._is_none_field(f)]))
            tooltip_fields_clean = []
            valid_keys = {k.lower() for k in field_to_table.keys()} | {m.lower() for m in measure_names}
            for rc in raw_clean:
                check_f = rc.split(".", 1)[-1] if "." in rc else rc
                if check_f.lower() in valid_keys:
                    tooltip_fields_clean.append(rc)
            
            tooltip_projections = self._build_y_projections(
                tooltip_fields_clean, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name
            )
            if tooltip_projections and visual_type not in ["card", "multiRowCard"]:
                query_state["Tooltips"] = {"projections": tooltip_projections}

        if sort_definition: query_dict["sortDefinition"] = sort_definition
        # Build legend from marks_color (skip None values)
        if marks_color and visual_type not in ["card", "multiRowCard", "tableEx"]:
            legend_projections = []
            for mc in marks_color:
                if self._is_none_field(mc):
                    continue
                cleaned = self.clean_field(mc)
                if cleaned and not self._is_none_field(cleaned) and cleaned not in ["Measure Names", "Measure Values"]:
                    entity = self._extract_entity(cleaned, field_to_table, default_table)
                    resolved = self._resolve_field(cleaned, display_to_bi_name, entity)
                    legend_projections.append({
                        "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}},
                        "queryRef": f"{entity}.{resolved}", "nativeQueryRef": resolved, "active": True
                    })
            if legend_projections:
                query_dict["queryState"]["Series"] = {"projections": legend_projections}
        
        # Load template
        try:
            visual_container = self.template_manager.load_json_template("visuals", f"{visual_type}.json")
        except FileNotFoundError:
            log_warning(f"Template {visual_type}.json not found. Falling back to barChart.json")
            visual_container = self.template_manager.load_json_template("visuals", "barChart.json")
        
        visual_container["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
        visual_container["visual"]["visualType"] = visual_type
        visual_container["visual"]["query"] = query_dict

        # Set visual title text globally if available in visualContainerObjects (schema 2.8.0 compliance)
        visual_title_text = dv.get("visual_title") or dv.get("display_name") or dv.get("name")
        if visual_title_text:
            vco = visual_container["visual"].setdefault("visualContainerObjects", {})
            title_list = vco.setdefault("title", [{}])
            if not title_list: title_list.append({})
            t_props = title_list[0].setdefault("properties", {})
            t_props["show"] = {"expr": {"Literal": {"Value": "true"}}}
            t_props["text"] = {"expr": {"Literal": {"Value": f"'{visual_title_text}'"}}}
            t_props["heading"] = {"expr": {"Literal": {"Value": "'Heading2'"}}}
            t_props["alignment"] = {"expr": {"Literal": {"Value": "'center'"}}}

        # Remove legacy title from visual.objects if it exists (standardize on visualContainerObjects)
        if "title" in visual_container["visual"].get("objects", {}):
            del visual_container["visual"]["objects"]["title"]

        # Apply visual_properties (fonts, colors, axis titles) if present
        self._apply_visual_properties(visual_container, dv.get("visual_properties", {}), pbi_config=pbi_config)
        
        # Auto-generate filterConfig.filters for all query fields
        auto_filters = []
        for cf in category_fields:
            entity = self._extract_entity(cf, field_to_table, default_table)
            
            # Suffix Preservation
            if entity and f"({entity})" in cf:
                resolved = cf
            else:
                resolved = self._resolve_field(cf, display_to_bi_name, entity)
                
            auto_filters.append({
                "name": str(uuid.uuid4()).replace("-", "")[:20],
                "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}},
                "type": "Categorical"
            })
        for yf in y_fields:
            agg_match = agg_regex.match(yf)
            is_agg_wrapper = False
            if agg_match and agg_match.group(1).upper() == "AGG":
                is_agg_wrapper = True
                inner_yf = agg_match.group(2).strip()
                entity = self._extract_entity(inner_yf, field_to_table, default_table)
                resolved = self._resolve_field(inner_yf, display_to_bi_name, entity)
            else:
                entity = self._extract_entity(yf, field_to_table, default_table)
                # Suffix Preservation
                if entity and f"({entity})" in yf:
                    resolved = yf
                else:
                    resolved = self._resolve_field(yf, display_to_bi_name, entity)
                
            norm_measure_names = {m.strip().lower() for m in measure_names if isinstance(m, str)}
            if is_agg_wrapper or yf.strip().lower() in norm_measure_names:
                field_expr = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}
            else:
                dt = field_to_datatype.get(yf, "string")
                func = 0 if dt in ["integer", "real"] else 2
                field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}, "Function": func}}
            auto_filters.append({
                "name": str(uuid.uuid4()).replace("-", "")[:20],
                "field": field_expr,
                "type": "Advanced"
            })
        
        # Build set of clean category, y, and y2 fields to skip duplicate tooltip filters
        skip_filter_fields = set()
        for f in list(category_fields) + list(y_fields):
            skip_filter_fields.add(self.clean_field(f).lower())

        for tf in tooltip_fields_clean:
            clean_tf = self.clean_field(tf)
            if clean_tf.lower() in skip_filter_fields:
                continue
            
            agg_match = agg_regex.match(tf)
            is_agg_wrapper = False
            if agg_match and agg_match.group(1).upper() == "AGG":
                is_agg_wrapper = True
                inner_tf = agg_match.group(2).strip()
                entity = self._extract_entity(inner_tf, field_to_table, default_table)
                resolved = self._resolve_field(inner_tf, display_to_bi_name, entity)
            else:
                entity = self._extract_entity(tf, field_to_table, default_table)
                # Suffix Preservation
                if entity and f"({entity})" in tf:
                    resolved = tf
                else:
                    resolved = self._resolve_field(tf, display_to_bi_name, entity)
                
            norm_measure_names = {m.strip().lower() for m in measure_names if isinstance(m, str)}
            if is_agg_wrapper or tf.strip().lower() in norm_measure_names:
                field_expr = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}
            else:
                dt = field_to_datatype.get(tf, "string")
                func = 0 if dt in ["integer", "real"] else 2
                field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}, "Function": func}}
            auto_filters.append({
                "name": str(uuid.uuid4()).replace("-", "")[:20],
                "field": field_expr,
                "type": "Advanced"
            })
        
        # Add explicit filters from the sheet data
        if filters:
            for filter_item in filters:
                raw_ff = filter_item.get("tableau_original_name", "") if isinstance(filter_item, dict) else str(filter_item)
                if not raw_ff or self._is_none_field(raw_ff): continue
                
                ff = self.clean_field(raw_ff)
                if not ff or ff in ["Measure Names", "Measure Values"]: continue

                entity = self._extract_entity(raw_ff, field_to_table, default_table)
                resolved_ff = self._resolve_field(ff, display_to_bi_name, entity)
                
                dt = field_to_datatype.get(ff, "string")
                is_numeric = dt in ["integer", "real"]
                
                norm_measure_names = {m.strip().lower() for m in measure_names if isinstance(m, str)}
                agg_match = re.match(r'^([A-Z0-9_]+)\((.*)\)$', raw_ff, re.IGNORECASE)
                is_agg = agg_match and agg_match.group(1).upper() == "AGG"
                
                if ff.strip().lower() in norm_measure_names or is_agg:
                    field_expr = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved_ff}}
                    f_type = "Advanced"
                else:
                    was_aggregated = (raw_ff != ff and bool(re.match(r'^[A-Z0-9_]+\((.*)\)$', raw_ff, re.IGNORECASE)))
                    if was_aggregated:
                        func = 0 if is_numeric else 2
                        field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved_ff}}, "Function": func}}
                        f_type = "Advanced"
                    else:
                        field_expr = {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved_ff}}
                        f_type = "Categorical" if dt == "string" else "Advanced"

                # Apply default value 1 for param-based filters (e.g. Top N/Bottom N Filter)
                is_param = False
                if isinstance(filter_item, dict) and filter_item.get("tableau_type") == "parameter":
                    is_param = True
                elif parameter_names and ff in parameter_names:
                    is_param = True
                elif "top n filter" in ff.lower() or "bottom n filter" in ff.lower() or "top n filter" in raw_ff.lower() or "bottom n filter" in raw_ff.lower():
                    is_param = True

                filter_obj = {
                    "name": str(uuid.uuid4()).replace("-", "")[:20],
                    "field": field_expr,
                    "type": "Advanced" if is_param else f_type,
                    "howCreated": "User"
                }

                # Handle Top N filter type
                is_top_n = False
                f_type_val = ""
                if isinstance(filter_item, dict):
                    f_type_val = str(filter_item.get("filter_type", "") or filter_item.get("filterType", "") or "").strip().lower()
                    if f_type_val in ["top n", "topn"]:
                        is_top_n = True
                
                if is_top_n:
                    top_n_val = 10
                    try:
                        top_n_val = int(filter_item.get("value") or filter_item.get("top") or 10)
                    except (ValueError, TypeError):
                        pass
                    
                    by_val_raw = filter_item.get("By_value") or filter_item.get("by_value") or raw_ff
                    by_val_clean = self.clean_field(by_val_raw)
                    by_val_entity = self._extract_entity(by_val_raw, field_to_table, default_table)
                    by_val_resolved = self._resolve_field(by_val_clean, display_to_bi_name, by_val_entity)
                    is_by_val_measure = by_val_clean in measure_names or by_val_raw in measure_names
                    
                    # Determine aggregation function if any
                    agg_func = None
                    if isinstance(filter_item, dict):
                        agg_val = filter_item.get("aggregation") or filter_item.get("agg") or filter_item.get("function")
                        if agg_val:
                            agg_func = self.map_agg_to_pbi(str(agg_val))
                        if agg_func is None:
                            instruction = filter_item.get("power_bi_instruction")
                            if instruction:
                                agg_func = self._parse_agg_from_instruction(instruction)
                    # Check dv's rows/columns nested sorts
                    if agg_func is None:
                        for row_or_col in list(dv.get("rows", [])) + list(dv.get("columns", [])):
                            if isinstance(row_or_col, dict) and "sort" in row_or_col:
                                s = row_or_col["sort"]
                                if isinstance(s, dict) and s.get("field_name") == by_val_raw:
                                    agg_val = s.get("aggregation")
                                    if agg_val:
                                        agg_func = self.map_agg_to_pbi(str(agg_val))
                                        if agg_func is not None:
                                            break

                    filter_obj["type"] = "TopN"
                    if "howCreated" in filter_obj:
                        del filter_obj["howCreated"]
                        
                    filter_obj["filter"] = self._create_top_n_filter_logic(
                        entity, resolved_ff, by_val_resolved, top_n_val, is_by_value_measure=is_by_val_measure, by_val_entity=by_val_entity, agg_func=agg_func
                    )
                elif is_param:
                    # If not in field_to_table, try to use the resolved field name as the entity (standard parameter table)
                    if not field_to_table.get(ff):
                        entity = resolved_ff
                    filter_logic = self._create_parameter_filter_logic(entity, resolved_ff, is_measure=(ff in measure_names))
                    if filter_logic:
                        filter_obj["filter"] = filter_logic

                auto_filters.append(filter_obj)
        
        visual_container["filterConfig"] = {"filters": auto_filters}
        
        return visual_container

    # =========================================================
    # PROJECTION BUILDERS
    # =========================================================
    def _build_category_projections(self, category_fields: List[str], field_to_table: Dict, default_table: str, display_to_bi_name: Dict = None, is_direct_lake: bool = False) -> List[Dict]:
        if display_to_bi_name is None:
            display_to_bi_name = {}
        projections = []
        for f in category_fields:
            if self._is_none_field(f):
                continue
            entity = self._extract_entity(f, field_to_table, default_table)
            
            # Suffix Preservation: If input already has (Entity) suffix, keep it
            if entity and f"({entity})" in f:
                resolved = f
            else:
                resolved = self._resolve_field(f, display_to_bi_name, entity)
            if field_to_table and resolved in field_to_table:
                entity = field_to_table[resolved]
            
            projections.append({
                "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}},
                "queryRef": f"{entity}.{resolved}", "nativeQueryRef": resolved, "active": True
            })
        return projections

    def _build_y_projections(self, y_fields: List[str], field_to_table: Dict, 
                             field_to_datatype: Dict, measure_names: List, default_table: str, display_to_bi_name: Dict = None) -> List[Dict]:
        if display_to_bi_name is None:
            display_to_bi_name = {}
        projections = []
        
        # Regex for extract aggregation: Func(Field)
        agg_functions = ["SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "CNTD", "CTD", "ATTR", "MEDIAN", "STDEV", "VAR", "DISTINCT", "AGG", "DISTINCTCOUNT", "COUNTNONNULL"]
        agg_regex = re.compile(r'^(' + '|'.join(agg_functions) + r')\s*\((.*)\)$', re.IGNORECASE)

        for f in y_fields:
            if self._is_none_field(f):
                continue
            
            # Check for explicit aggregation prefix
            agg_match = agg_regex.match(f)
            if agg_match:
                func_str = agg_match.group(1).upper()
                inner_f = agg_match.group(2).strip()
                
                if func_str == "AGG":
                    entity = self._extract_entity(inner_f, field_to_table, default_table)
                    if entity and f"({entity})" in inner_f:
                        resolved = inner_f
                    else:
                        resolved = self._resolve_field(inner_f, display_to_bi_name, entity)
                    if field_to_table and resolved in field_to_table:
                        entity = field_to_table[resolved]
                    field_expr = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}
                    query_ref = f"{entity}.{resolved}"
                    native_query_ref = resolved
                else:
                    # Mapping to PBI Function values
                    func_map = {
                        "SUM": 0, "AVG": 1, "COUNT": 5, "COUNTNONNULL": 5, 
                        "MIN": 3, "MAX": 4, "DISTINCTCOUNT": 2, "CNTD": 2, "COUNTD": 2,
                        "MEDIAN": 6, "STDEV": 7, "VAR": 8
                    }
                    func = func_map.get(func_str, 5) # Default to CountNonNull if unknown agg
                    entity = self._extract_entity(inner_f, field_to_table, default_table)
                    
                    # Suffix Preservation
                    if entity and f"({entity})" in inner_f:
                        resolved = inner_f
                    else:
                        resolved = self._resolve_field(inner_f, display_to_bi_name, entity)
                    if field_to_table and resolved in field_to_table:
                        entity = field_to_table[resolved]
                    
                    field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}, "Function": func}}
                    
                    # FORCE CountNonNull Label as requested for the target schema
                    func_name = "CountNonNull"
                    query_ref = f"{func_name}({entity}.{resolved})"
                    native_query_ref = f"{func_str.capitalize()} of {resolved}"
            else:
                entity = self._extract_entity(f, field_to_table, default_table)
                
                # Suffix Preservation
                if entity and f"({entity})" in f:
                    resolved = f
                else:
                    resolved = self._resolve_field(f, display_to_bi_name, entity)
                if field_to_table and resolved in field_to_table:
                    entity = field_to_table[resolved]
                
                if f in measure_names:
                    field_expr = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}
                    query_ref = f"CountNonNull({entity}.{resolved})"
                else:
                    dt = field_to_datatype.get(f, "string")
                    func = 0 if dt in ["integer", "real"] else 5 
                    field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}, "Function": func}}
                    query_ref = f"CountNonNull({entity}.{resolved})"

            projections.append({"field": field_expr, "queryRef": query_ref, "nativeQueryRef": native_query_ref if 'native_query_ref' in locals() else resolved})
        return projections

    def _create_parameter_filter_logic(self, entity: str, property_name: str, is_measure: bool = False) -> Dict:
        """Creates the advanced filter logic (Version 2) to set a default value of 1 for parameters."""
        field_type = "Measure" if is_measure else "Column"
        return {
            "Version": 2,
            "From": [{"Name": "f", "Entity": entity, "Type": 0}],
            "Where": [
                {
                    "Condition": {
                        "Comparison": {
                            "ComparisonKind": 0,
                            "Left": {
                                field_type: {
                                    "Expression": {"SourceRef": {"Source": "f"}},
                                    "Property": property_name
                                }
                            },
                            "Right": {"Literal": {"Value": "1L"}}
                        }
                    }
                }
            ]
        }

    # =========================================================
    # HELPER: Build a Slicer Visual from a slicer / parameter def
    # =========================================================
    def _build_slicer_visual(self, slicer_def, field_to_table: Dict, field_to_datatype: Dict,
                              measure_names: List, default_table: str, display_to_bi_name: Dict,
                              parameter_names: set = None, parameters: List[Dict] = None) -> Optional[Dict]:
        """Builds a PBI slicer visual from a Tableau slicer definition dict or string."""
        if parameter_names is None:
            parameter_names = set()
        if parameters is None:
            parameters = []
        try:
            slicer_container = self.template_manager.load_json_template("visuals", "slicer_container.json")
        except FileNotFoundError:
            log_warning("slicer_container.json template not found, skipping slicer")
            return None

        # Normalise: slicer_def can be a plain string (field name) or a dict
        if isinstance(slicer_def, str):
            field_name = self.clean_field(slicer_def)
            slicer_type = "parameter" if field_name in parameter_names else "quick"
        elif isinstance(slicer_def, dict):
            raw = (slicer_def.get("field_name") or slicer_def.get("name") or
                   slicer_def.get("caption") or slicer_def.get("column") or "")
            field_name = self.clean_field(raw)
            slicer_type = slicer_def.get("tableau_type", "parameter" if field_name in parameter_names else "quick")
        else:
            return None

        if not field_name or self._is_none_field(field_name):
            return None

        entity = self._extract_entity(field_name, field_to_table, default_table)
        if entity == default_table and field_name:
            better_entity = self._extract_entity(field_name, field_to_table, default_table)
            if better_entity != default_table:
                entity = better_entity
        resolved = self._resolve_field(field_name, display_to_bi_name, entity)

        if slicer_type == "parameter" or field_name in parameter_names:
            param_entity = f"{field_name}"
            param_property = "Value"
            for p in parameters:
                if p.get("name") == field_name:
                    raw_dax = p.get("powerbi", {}).get("dax") or p.get("dax") or ""
                    clean_dax = re.sub(r'```[a-zA-Z]*', '', str(raw_dax)).replace('```', '').strip()
                    match = re.search(r"'?([a-zA-Z0-9_ -]+)'?\[([a-zA-Z0-9_ -]+)\]", clean_dax)
                    if match:
                        param_entity = match.group(1)
                        param_property = match.group(2)
                    break

            proj_field = {"Column": {"Expression": {"SourceRef": {"Entity": param_entity}}, "Property": param_property}}
            query_ref = f"{param_entity}.{param_property}"
            resolved = param_property
            
        elif field_name in measure_names:
            proj_field = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}
            query_ref = f"{entity}.{resolved}"
        else:
            dt = field_to_datatype.get(field_name, "string")
            proj_field = {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}
            query_ref = f"{entity}.{resolved}"

        slicer_container["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
        slicer_container["visual"]["query"]["queryState"]["Values"]["projections"] = [{
            "field": proj_field,
            "queryRef": query_ref,
            "nativeQueryRef": resolved,
            "active": True
        }]
        
        if slicer_type == "parameter" or field_name in parameter_names:
            if "objects" not in slicer_container["visual"]:
                slicer_container["visual"]["objects"] = {}
            if "data" not in slicer_container["visual"]["objects"]:
                slicer_container["visual"]["objects"]["data"] = [{"properties": {}}]
            slicer_container["visual"]["objects"]["data"][0]["properties"]["mode"] = {
                "expr": {
                    "Literal": {
                        "Value": "'Basic'"
                    }
                }
            }

        if slicer_type == "parameter" or field_name in parameter_names:
            slicer_container["filterConfig"] = {"filters": [{
                "name": str(uuid.uuid4()).replace("-", "")[:20],
                "field": proj_field,
                "type": "Advanced"
            }]}
        else:
            slicer_container["filterConfig"] = {"filters": [{
                "name": str(uuid.uuid4()).replace("-", "")[:20],
                "field": proj_field,
                "type": "Categorical",
                "howCreated": "User"
            }]}

        # Set slicer title if available
        slicer_title = slicer_def.get("visual_title") or slicer_def.get("name") or field_name
        if slicer_title:
            objs = slicer_container.setdefault("visual", {}).setdefault("objects", {})
            # Slicers often use 'header' instead of 'title' for their default title, 
            # but they can also have a visual-level title.
            title_list = objs.setdefault("title", [{}])
            if not title_list: title_list.append({})
            t_props = title_list[0].setdefault("properties", {})
            t_props["show"] = {"expr": {"Literal": {"Value": "true"}}}
            title_expr = {"expr": {"Literal": {"Value": f"'{slicer_title}'"}}}
            t_props["title"] = title_expr
            t_props["text"] = title_expr

        return slicer_container

    # =========================================================
    # HELPER: Apply visual_properties (font / color / axes) to visual
    # =========================================================
    def _apply_visual_properties(self, visual_container: Dict, visual_properties: Dict, pbi_config: Dict = None) -> None:
        """Applies Tableau visual_properties AND Power BI specific config to the visual."""
        
        # 1. Apply standard Tableau formatting first as the baseline fallback
        if visual_properties and isinstance(visual_properties, dict):
            try:
                visual_type = visual_container.get("visual", {}).get("visualType", "")
                objects = visual_container.setdefault("visual", {}).setdefault("objects", {})

                # Apply fonts (title, axis, marks)
                fonts = visual_properties.get("fonts", [])
                for font_entry in fonts:
                    if not isinstance(font_entry, dict):
                        continue
                    applied_to = font_entry.get("applied_to", "").lower()
                    
                    # ── Title / Sheet Formatting ──
                    if "title" in applied_to or "sheet" in applied_to:
                        title_list = objects.setdefault("title", [{}])
                        if not title_list: title_list.append({})
                        props = title_list[0].setdefault("properties", {})
                        
                        # Ensure title is shown if there's formatting
                        props["show"] = {"expr": {"Literal": {"Value": "true"}}}
                        
                        if font_entry.get("font"):
                            props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{font_entry['font']}'"}}}
                        elif font_entry.get("font_name"):
                            props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{font_entry['font_name']}'"}}}
                            
                        sz = font_entry.get("size") or font_entry.get("font_size")
                        if sz:
                            # PBI often prefers fontSize as a numeric literal in some contexts, but '20' (string) or 20 (number) works
                            props["fontSize"] = {"expr": {"Literal": {"Value": str(sz)}}}
                            
                        if font_entry.get("bold") is True:
                            props["fontWeight"] = {"expr": {"Literal": {"Value": "'bold'"}}}
                            
                        if font_entry.get("alignment"):
                            align = font_entry["alignment"].lower()
                            props["alignment"] = {"expr": {"Literal": {"Value": f"'{align}'"}}}

                    # ── Mark Labels (Data Labels / Card Labels) ──
                    elif "mark labels" in applied_to:
                        label_key = "labels"
                        label_list = objects.setdefault(label_key, [{}])
                        if not label_list: label_list.append({})
                        props = label_list[0].setdefault("properties", {})
                        
                        props["show"] = {"expr": {"Literal": {"Value": "true"}}}
                        
                        if font_entry.get("font"):
                            props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{font_entry['font']}'"}}}
                        elif font_entry.get("font_name"):
                            props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{font_entry['font_name']}'"}}}
                            
                        sz = font_entry.get("size") or font_entry.get("font_size")
                        if sz:
                            props["fontSize"] = {"expr": {"Literal": {"Value": str(sz)}}}

                        if font_entry.get("bold") is True:
                            props["fontWeight"] = {"expr": {"Literal": {"Value": "'bold'"}}}

                # Apply colors
                colors = visual_properties.get("colors", [])
                for color_entry in colors:
                    if not isinstance(color_entry, dict):
                        continue
                    hex_val = color_entry.get("hex_value") or color_entry.get("color")
                    if not hex_val:
                        continue
                        
                    applied_to = color_entry.get("applied_to", "").lower()
                    
                    if "title" in applied_to or "sheet" in applied_to:
                        title_list = objects.setdefault("title", [{}])
                        if not title_list: title_list.append({})
                        props = title_list[0].setdefault("properties", {})
                        # In PBI, title color property can be 'fontColor' or 'color'
                        color_expr = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{hex_val}'"}}}} }
                        props["fontColor"] = color_expr
                        props["color"] = color_expr
                    elif "mark labels" in applied_to:
                        label_list = objects.setdefault("labels", [{}])
                        if not label_list: label_list.append({})
                        props = label_list[0].setdefault("properties", {})
                        props["color"] = {
                            "solid": {"color": {"expr": {"Literal": {"Value": f"'{hex_val}'"}}}}
                        }
                    elif "background" in applied_to:
                        bg_list = objects.setdefault("background", [{}])
                        if not bg_list: bg_list.append({})
                        props = bg_list[0].setdefault("properties", {})
                        props["show"] = {"expr": {"Literal": {"Value": "true"}}}
                        props["color"] = {
                            "solid": {"color": {"expr": {"Literal": {"Value": f"'{hex_val}'"}}}}
                        }

                # Apply axis titles
                axes = visual_properties.get("axes", [])
                for axis_entry in axes:
                    if not isinstance(axis_entry, dict):
                        continue
                    
                    # Tableau axis objects often use 'target' or 'axis'
                    axis_type = (axis_entry.get("target") or axis_entry.get("axis") or "").lower()
                    
                    # Extract title text
                    title_obj = axis_entry.get("axis_title", {})
                    
                    # Skip standard default axis titles
                    if isinstance(title_obj, dict) and (title_obj.get("custom") is False or str(title_obj.get("custom")).lower() == "false"):
                        continue
                        
                    if isinstance(title_obj, dict):
                        title_text = title_obj.get("text")
                    else:
                        title_text = axis_entry.get("title") or axis_entry.get("label")
                    
                    if not axis_type:
                        continue

                    pbi_axis_key = "categoryAxis" if "x" in axis_type or "col" in axis_type else "valueAxis"
                    axis_obj = objects.setdefault(pbi_axis_key, [{}])
                    if not axis_obj:
                        axis_obj.append({})
                    
                    props = axis_obj[0].setdefault("properties", {})
                    props["show"] = {"expr": {"Literal": {"Value": "true"}}}
                    
                    if title_text and title_text.lower() != "auto":
                        props["titleText"] = {
                            "expr": {"Literal": {"Value": f"'{title_text}'"}}
                        }
            except Exception as e:
                log_warning(f"_apply_visual_properties failed: {e}")

        # 2. Prioritize / Apply Power BI specific config formatting afterwards
        if pbi_config and isinstance(pbi_config, dict):
            self._apply_pbi_config_formatting(visual_container, pbi_config, visual_properties=visual_properties)
        


    def _apply_pbi_config_formatting(self, visual_container: Dict, pbi_config: Dict, visual_properties: Dict = None) -> None:
        """Applies formatting strictly from power_bi_visual_type mapping section."""
        objects = visual_container.setdefault("visual", {}).setdefault("objects", {})
        visual_type = visual_container.get("visual", {}).get("visualType", "")
        query_state = visual_container.get("visual", {}).get("query", {}).get("queryState", {})

        # 1. Title Formatting
        title_cfg = pbi_config.get("title", {})
        if title_cfg:
            title_list = objects.setdefault("title", [{}])
            if not title_list: title_list.append({})
            props = title_list[0].setdefault("properties", {})
            
            show = str(title_cfg.get("visible", "true")).lower() == "true"
            props["show"] = {"expr": {"Literal": {"Value": "true" if show else "false"}}}
            
            text = title_cfg.get("text")
            if text:
                props["text"] = {"expr": {"Literal": {"Value": f"'{text}'"}}}
            
            if title_cfg.get("font"):
                props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{title_cfg['font']}'"}}}
            if title_cfg.get("font_size"):
                props["fontSize"] = {"expr": {"Literal": {"Value": str(title_cfg['font_size'])}}}
            if title_cfg.get("font_color"):
                props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{title_cfg['font_color'].upper()}'"}}}}}

        # 2. Series / DataPoint Colors (Essential for Stacked Charts)
        series_cfg = pbi_config.get("series_colors") or pbi_config.get("bars") or pbi_config.get("slices")
        if series_cfg and isinstance(series_cfg, list):
            data_points = []
            
            # Collect all projections to match applied_to names
            all_projs = []
            for bucket in query_state.values():
                if isinstance(bucket, dict) and "projections" in bucket:
                    all_projs.extend(bucket["projections"])
            
            for entry in series_cfg:
                if not isinstance(entry, dict): continue
                applied_to = entry.get("applied_to") or entry.get("series")
                color = entry.get("color")
                if applied_to and color:
                    q_ref = self._find_query_ref_for_field(all_projs, applied_to)
                    selector = self._build_pbi_selector(applied_to, query_ref=q_ref)
                    if selector:
                        data_points.append({
                            "selector": selector,
                            "properties": {
                                "fill": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{color.upper()}'"}}}}}
                            }
                        })
            if data_points:
                objects["dataPoint"] = data_points

        # 3. Legend Formatting
        if "legends" in pbi_config and pbi_config["legends"] == []:
            objects.setdefault("legend", [{}])[0].setdefault("properties", {})["show"] = {"expr": {"Literal": {"Value": "false"}}}

        # 4. Axis Titles
        axes_title_cfg = pbi_config.get("axes_title", [])
        if isinstance(axes_title_cfg, list):
            for entry in axes_title_cfg:
                target = entry.get("target")
                text = entry.get("text")
                if not target or not text: continue
                
                # Skip standard default axis titles
                if entry.get("custom") is False or str(entry.get("custom")).lower() == "false":
                    continue
                
                # Map Tableau axis targets to PBI axis objects
                # Convention: x_axis (Tableau Dimension) -> categoryAxis (PBI Categorical)
                # y_axis (Tableau Measure) -> valueAxis (PBI Numerical)
                pbi_axis_key = "categoryAxis" if target == "x_axis" else "valueAxis"
                    
                axis_list = objects.setdefault(pbi_axis_key, [{}])
                if not axis_list: axis_list.append({})
                a_props = axis_list[0].setdefault("properties", {})
                a_props["show"] = {"expr": {"Literal": {"Value": "true"}}}
                
                # Use valueAxis for numerical, categoryAxis for categorical
                a_props["titleText"] = {"expr": {"Literal": {"Value": f"'{text}'"}}}

    # =========================================================
    # VISUAL FORMATTING HELPERS
    # =========================================================
    def _flatten_zone_hierarchy(self, zones: List[Dict]) -> Dict[str, Dict]:
        """Recursively flattens zone hierarchy to extract styles by visual name."""
        styles = {}
        for zone in zones:
            if not isinstance(zone, dict): continue
            attr = zone.get("attributes", {})
            name = attr.get("name")
            style = zone.get("style", {})
            if name and style:
                styles[name] = style
            
            # Recurse into children
            children = zone.get("children", [])
            if children:
                styles.update(self._flatten_zone_hierarchy(children))
        return styles

    def _apply_container_styles(self, visual_container: Dict, style: Dict) -> None:
        """Maps zone_hierarchy styles (border, etc.) to PBI visualContainerObjects."""
        if not style: return
        
        # Border mapping
        border_color = style.get("border-color")
        if border_color and border_color != "none":
            # Correct path is within the 'visual' object
            vco = visual_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
            vco["border"] = [{
                "properties": {
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "color": {
                        "solid": {
                            "color": {"expr": {"Literal": {"Value": f"'{border_color.upper()}'"}}}
                        }
                    }
                }
            }]

        # Data labels override if present in style
        if style.get("show-labels") == "true":
            objs = visual_container.setdefault("visual", {}).setdefault("objects", {})
            objs["labels"] = [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}}}]

    def _record_skipped(self, key: str, reason: str):
        if not hasattr(self, "skipped_visuals"):
            self.skipped_visuals = []
        self.skipped_visuals.append({"visual_key": key, "reason": reason})