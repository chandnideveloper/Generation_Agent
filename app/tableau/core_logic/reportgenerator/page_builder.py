import json
import uuid
import re
from typing import Dict, Optional, List, Tuple
from app.tableau.core.logging_utils import log_info, log_error, log_warning
from app.tableau.core.config import Config
from app.tableau.core_logic.template_manager.template_manager import TemplateManager
from app.tableau.services.action_logger import ActionLogger
from .filter_builder import FilterBuilderMixin
from .slicer_builder import SlicerBuilderMixin
from .projection_builder import ProjectionBuilderMixin
from .dashboard_builder import DashboardBuilderMixin
from .action_builder import ActionBuilderMixin
from .formatting_builder import FormattingBuilderMixin
from .visual_projections import VisualProjectionsMixin
from .visual_data_builder import VisualDataBuilderMixin
from .utils_builder import UtilsBuilderMixin

class PageBuilderMixin(FilterBuilderMixin, SlicerBuilderMixin, ProjectionBuilderMixin, DashboardBuilderMixin, ActionBuilderMixin, FormattingBuilderMixin, VisualProjectionsMixin, VisualDataBuilderMixin, UtilsBuilderMixin):

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

        sheets = report_data.get("pages", [])[:] # Copy list
        
        # Merge dashboards into sheets list and mark them
        dashboards = report_data.get("dashboards", [])
        for db in dashboards:
            if isinstance(db, dict):
                db_entry = db.copy()
                db_entry["is_dashboard"] = True
                if "dashboard_name" in db_entry and "display_name" not in db_entry:
                    db_entry["display_name"] = db_entry["dashboard_name"]
                sheets.append(db_entry)

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
        parameter_metadata = report_data.get("parameter_metadata", parameters)
        parameter_names = {p.get("name", "") for p in parameter_metadata if isinstance(p, dict)}
        is_direct_lake = report_data.get("is_direct_lake", False)
        field_to_format = report_data.get("field_to_format", {})

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

            # Calculate local formats for the entire page (sheets)
            local_field_to_format = self._get_local_field_to_format(page_entry, field_to_format)

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
                    if dvs and isinstance(dvs[0], dict):
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

            # ── Merge sheet-level parameters into scope for builders ──
            sheet_params = page_entry.get("parameters", [])
            for p in sheet_params:
                if isinstance(p, dict) and p.get("name") not in parameter_names:
                    parameter_names.add(p.get("name"))
                    parameters.append(p)

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

            # ── Sheet Background Color (captured for visuals) ──
            visual_props = page_entry.get("visual_properties", {})
            sheet_bg_color = None
            for c in visual_props.get("colors", []):
                applied_to = c.get("applied_to", "").lower()
                # Refine: Ignore colors applied to "Title" when looking for a background color
                if "background" in applied_to and "title" not in applied_to:
                    sheet_bg_color = c.get("color") or c.get("hex_value")
                    break
            
            # The user wants NO background on the sheet; they want it on the visuals.
            # So we remove the background object from page_file entirely.
            if "objects" in page_file and "background" in page_file["objects"]:
                del page_file["objects"]["background"]


            # =========================================================
            # DASHBOARD PAGE (multi-visual with layout)
            # =========================================================
            if page_entry.get("is_dashboard"):
                # Dynamically calculate height to avoid cutting visuals
                p_width = page_file.get("width", 1280)
                p_height = self._calculate_optimal_dashboard_height(page_entry)
                
                page_file["height"] = p_height
                page_file["width"] = p_width

                title_formatting = page_entry.get("title_formatting", {})
                
                page_visuals = self._generate_dashboard_page_visuals(
                    page_entry, field_to_table, field_to_datatype, measure_names, default_table,
                    display_to_bi_name, actions, sheet_name_to_id,
                    actions_by_source=actions_by_source, parameter_names=parameter_names, parameters=parameter_metadata,
                    sheet_bg_color=sheet_bg_color,
                    embedded_assets=report_data.get("embedded_assets", {}),
                    page_width=p_width,
                    page_height=p_height,
                    is_direct_lake=is_direct_lake,
                    dashboard_title_formatting=title_formatting,
                    field_to_format=field_to_format
                )
                max_v_bottom = p_height
                for v in page_visuals:
                    v_type = v.get("visual", {}).get("visualType", "")
                    if v_type.startswith("htmlContent"):
                        used_custom_visuals.add(v_type)
                    files.append((f"pages/{page_id}/visuals/{v['name']}/visual.json", json.dumps(v, indent=2, ensure_ascii=False)))
                    
                    pos = v.get("position", {})
                    if pos and "y" in pos and "height" in pos:
                        max_v_bottom = max(max_v_bottom, pos["y"] + pos["height"] + 40)
                page_file["height"] = max_v_bottom

                files.append((f"pages/{page_id}/page.json", json.dumps(page_file, indent=2, ensure_ascii=False)))
                continue

            # =========================================================
            # SINGLE-VISUAL PAGE (original flow)
            # =========================================================
            # Build field lists — correct: measures always go to Y, dimensions to Category
            pbi_visual_config = page_entry.get("power_bi_visual_type", {})
            if isinstance(pbi_visual_config, dict):
                if "legend_position" not in pbi_visual_config and "legend_position" in page_entry:
                    pbi_visual_config["legend_position"] = page_entry["legend_position"]
            
            # Extract axis_mapping from pbi_visual_config or page_entry
            axis_mapping = page_entry.get("axis_mapping", {})

            # Consolidation: helper to extract field names from various PBI config formats
            def _get_fields(val):
                if not val: return []
                if isinstance(val, str): return [val]
                if isinstance(val, list): return [v for v in val if v]
                if isinstance(val, dict):
                    f = val.get("field") or val.get("column")
                    if f: return [f]
                return []

            if not axis_mapping and isinstance(pbi_visual_config, dict):
                 axis_mapping = pbi_visual_config.get("axis_mapping", pbi_visual_config)
            
            if isinstance(pbi_visual_config, dict):
                 rows = _get_fields(pbi_visual_config.get("rows")) if "rows" in pbi_visual_config else page_entry.get("rows", [])
                 columns = _get_fields(pbi_visual_config.get("columns")) if "columns" in pbi_visual_config else page_entry.get("columns", [])
                 marks_color = _get_fields(pbi_visual_config.get("legends")) if "legends" in pbi_visual_config else page_entry.get("marks_color", [])
                 
                 # Look up values as list of fields (for matrix/pivotTable)
                 pbi_values = pbi_visual_config.get("values")
                 if isinstance(pbi_values, list):
                     pbi_fields = _get_fields(pbi_values)
                 else:
                     pbi_fields = _get_fields(pbi_visual_config.get("fields"))
                 pbi_legends = _get_fields(pbi_visual_config.get("legends"))
            else:
                 rows = page_entry.get("rows", [])
                 columns = page_entry.get("columns", [])
                 marks_color = page_entry.get("marks_color", [])
                 pbi_fields = []
                 pbi_legends = []

            # Extract secondary Y-axis fields from axis_mapping
            y2_fields = []
            size_fields = []
            if isinstance(axis_mapping, dict) and axis_mapping:
                y2_val = axis_mapping.get("secondary_y_axis")
                if y2_val:
                    y2_fields = [y2_val] if isinstance(y2_val, str) else list(y2_val)
                size_val = axis_mapping.get("size")
                if size_val:
                    size_fields = [size_val] if isinstance(size_val, str) else list(size_val)

            measures = page_entry.get("measures", [])
            filters = page_entry.get("filters", [])
            slicers = page_entry.get("slicers", [])
            visual_properties = page_entry.get("visual_properties", {})

            visual_type = self._resolve_visual_type(page_entry, measure_names=measure_names)

            # Build field lists
            # Include pbi_fields in measures and selectively enable marks_text fallback for Cards
            m_for_split = list(measures) + list(pbi_fields)
            if visual_type in ["card", "multiRowCard", "pivotTable"] and not m_for_split:
                 m_for_split = page_entry.get("marks_text", [])

            # Unpack "Measure Values" nested lists
            def _unpack_measure_fields(fields):
                out = []
                for item in fields:
                    if isinstance(item, dict):
                        f_name = item.get("field") or item.get("name") or ""
                        if f_name == "Measure Values" and "measure_values" in item:
                            out.extend(item["measure_values"])
                        else:
                            out.append(item)
                    elif isinstance(item, str) and item == "Measure Values":
                        out.append(item)
                    else:
                        out.append(item)
                return out

            rows = _unpack_measure_fields(rows)
            columns = _unpack_measure_fields(columns)
            m_for_split = _unpack_measure_fields(m_for_split)

            # For map visuals, include marks_detail and marks_size in field extraction
            if visual_type in ["map", "filledMap", "heatmap"]:
                m_for_split = m_for_split + list(page_entry.get("marks_detail", [])) + list(page_entry.get("marks_size", []))
            # For pie/donut charts, data comes from axis_mapping.values + legends, not rows/columns
            has_pie_data = (
                visual_type in ["pieChart", "donutChart"]
                and isinstance(axis_mapping, dict)
                and (axis_mapping.get("values") or pbi_legends)
            )
            if not rows and not columns and not m_for_split and not has_pie_data:
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
                 
                 # Ensure page.json is created even for placeholder visuals
                 files.append((f"pages/{page_id}/page.json", json.dumps(page_file, indent=2, ensure_ascii=False)))
                 continue

            # Split Tableau rows/columns into PBI Category vs Y/Value fields.
            # Tableau semantics: rows shelf = row headers (categories for bar, values for line);
            # columns shelf = column headers (values for bar, time-axis for line).
            # Correct universal rule: whichever shelf contains a measure -> Y; dimensions -> Category.
            def _split_fields(r_fields, c_fields, m_fields):
                cat_out, y_out = [], []
                seen_cat, seen_y = set(), set()
                for rf_raw in list(r_fields) + list(c_fields) + list(m_fields):
                    if isinstance(rf_raw, dict):
                        rf = rf_raw.get("field") or rf_raw.get("name") or rf_raw.get("column") or str(rf_raw)
                    else:
                        rf = rf_raw
                    if not rf or self._is_none_field(rf) or rf in ["Measure Names", "Measure Values"]:
                        continue
                    cf, was_agg = self.get_field_metadata(rf)
                    if not cf or self._is_none_field(cf):
                        continue
                    
                    if isinstance(cf, dict):
                        cf = cf.get("field") or cf.get("name") or str(cf)
                    
                    clean_f = self.clean_field(rf)
                    _, agg = self.get_field_agg_info(rf)
                    func = self.map_agg_to_pbi(agg)
                    dedup_key = f"{clean_f}_{func}"
                    
                    if was_agg or cf in measure_names:
                        if dedup_key not in seen_y:
                            y_out.append(rf) # Use original field for Y axis
                            seen_y.add(dedup_key)
                    else:
                        if cf not in seen_cat:
                            cat_out.append(rf)
                            seen_cat.add(cf)
                return cat_out, y_out


            category_fields, y_fields = _split_fields(rows, columns, m_for_split)

            # Force axis buckets if axis_mapping is provided
            x_fields_scatter = []
            if axis_mapping and isinstance(axis_mapping, dict):
                if visual_type == "scatterChart":
                    x_val = axis_mapping.get("x_axis")
                    if x_val:
                        x_fields_scatter = _get_fields(x_val)
                    y_val = axis_mapping.get("y_axis")
                    if y_val:
                        y_fields = _get_fields(y_val)
                    size_val = axis_mapping.get("size")
                    if size_val:
                        size_fields = _get_fields(size_val)
                    cat_val = axis_mapping.get("values")
                    if cat_val:
                        category_fields = _get_fields(cat_val)
                else:
                    x_val = axis_mapping.get("x_axis")
                    if x_val:
                        f_list = _get_fields(x_val)
                        if f_list: category_fields = f_list
                    y_val = axis_mapping.get("y_axis")
                    if y_val:
                        f_list = _get_fields(y_val)
                        if f_list: y_fields = f_list
                    y2_val = axis_mapping.get("secondary_y_axis")
                    if y2_val:
                        f_list = _get_fields(y2_val)
                        if f_list: y2_fields = f_list

            # CRITICAL: Always ensure secondary measures are REMOVED from the primary Y bucket
            if y2_fields:
                y_fields = [f for f in y_fields if f not in y2_fields]
            if size_fields:
                y_fields = [f for f in y_fields if f not in size_fields]
            if x_fields_scatter:
                y_fields = [f for f in y_fields if f not in x_fields_scatter]
            
            y_fields = list(dict.fromkeys(y_fields)) # Deduplicate

            # Do not inject marks_text into y_fields, as Power BI visuals often reject non-value/axes fields in Y projections


            category_projections = self._build_category_projections(category_fields, field_to_table, default_table, display_to_bi_name, is_direct_lake=is_direct_lake, field_to_format=local_field_to_format)
            y_projections = self._build_y_projections(y_fields, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, visual_type=visual_type, field_to_format=local_field_to_format)

            # Build secondary Y-axis projections
            y2_projections = []
            if y2_fields:
                y2_projections = self._build_y_projections(y2_fields, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, visual_type=visual_type, field_to_format=local_field_to_format)
                # Strip 'active' flag from Y2 projections per Power BI schema requirement
                for p in y2_projections:
                    p.pop("active", None)

            x_projections_scatter = []
            if x_fields_scatter:
                x_projections_scatter = self._build_y_projections(x_fields_scatter, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, visual_type=visual_type, field_to_format=local_field_to_format)

            size_projections = []
            if size_fields:
                size_projections = self._build_y_projections(size_fields, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, visual_type=visual_type, field_to_format=local_field_to_format)

            if not y_projections and category_fields:
                f = category_fields[0]
                entity = self._extract_entity(f, field_to_table, default_table)
                resolved_f = self._resolve_field(f, display_to_bi_name, entity)
                y_projections.append({
                    "field": {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved_f}}, "Function": 5}},
                    "queryRef": f"CountNonNull({entity}.{resolved_f})", "nativeQueryRef": f"Count of {resolved_f}"
                })

            if visual_type == "card":
                query_state = {"Values": {"projections": y_projections[:1]}}
            elif visual_type == "tableEx":
                import copy
                values_projections = []
                for p in category_projections + y_projections:
                    p_copy = copy.deepcopy(p)
                    p_copy.pop("active", None)
                    values_projections.append(p_copy)
                query_state = {
                    "Values": {"projections": values_projections}
                }
            elif visual_type == "pivotTable":
                row_projs = self._build_category_projections(rows, field_to_table, default_table, display_to_bi_name, is_direct_lake=is_direct_lake, field_to_format=local_field_to_format)
                col_projs = self._build_category_projections(columns, field_to_table, default_table, display_to_bi_name, is_direct_lake=is_direct_lake, field_to_format=local_field_to_format)
                val_projs = self._build_y_projections(m_for_split, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, field_to_format=local_field_to_format)
                
                query_state = {}
                if row_projs: query_state["Rows"] = {"projections": row_projs}
                if col_projs: query_state["Columns"] = {"projections": col_projs}
                if val_projs: query_state["Values"] = {"projections": val_projs}
            elif visual_type == "scatterChart":
                query_state = {}
                if category_projections: query_state["Category"] = {"projections": category_projections}
                if x_projections_scatter: query_state["X"] = {"projections": x_projections_scatter}
                if y_projections: query_state["Y"] = {"projections": y_projections}
                if size_projections: query_state["Size"] = {"projections": size_projections}
            elif visual_type in ["map", "heatmap"]:
                # Map (bubble/heatmap): Category = location dims, Size = value measures
                # Separate lat/long fields from general category dimensions
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
                # Size bucket: use explicit size_projections first, else measures (y_projections)
                map_size = size_projections if size_projections else y_projections
                if map_size: query_state["Size"] = {"projections": map_size}
            elif visual_type == "filledMap":
                # Filled Map: Category = geographic location, Y = saturation value
                query_state = {}
                if category_projections: query_state["Category"] = {"projections": category_projections}
                if y_projections: query_state["Y"] = {"projections": y_projections}
                if size_projections and not y_projections:
                    query_state["Y"] = {"projections": size_projections}
            elif visual_type in ["pieChart", "donutChart"]:
                # Pie/Donut: Category = legend dimension, Y = measure values
                pie_value_fields = []
                if isinstance(axis_mapping, dict):
                    val = axis_mapping.get("values")
                    if val:
                        pie_value_fields = [val] if isinstance(val, str) else list(val)
                # Deduplicate value fields
                pie_value_fields = list(dict.fromkeys(pie_value_fields))
                pie_y_projections = self._build_y_projections(
                    pie_value_fields, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, visual_type=visual_type, field_to_format=local_field_to_format
                ) if pie_value_fields else y_projections
                # Build category projections from legends
                pie_cat_fields = []
                for leg in pbi_legends:
                    if leg and not self._is_none_field(leg):
                        pie_cat_fields.append(leg)
                pie_cat_projections = self._build_category_projections(
                    pie_cat_fields, field_to_table, default_table, display_to_bi_name, is_direct_lake=is_direct_lake, field_to_format=local_field_to_format
                ) if pie_cat_fields else category_projections
                query_state = {}
                if pie_cat_projections:
                    query_state["Category"] = {"projections": pie_cat_projections}
                if pie_y_projections:
                    query_state["Y"] = {"projections": pie_y_projections}
            else:
                query_state = {}
                if category_projections:
                    query_state["Category"] = {"projections": category_projections}
                    
                if y_projections:
                    query_state["Y"] = {"projections": y_projections}
                if y2_projections:
                    query_state["Y2"] = {"projections": y2_projections}
            # Build legend from marks_color (skip None values)
            # Pie/Donut already handle their own Category from legends, skip Series injection
            # Matrix (pivotTable) does NOT have a Series bucket in Power BI
            # SKIP if legends is explicitly empty in mapping ([])
            if marks_color and visual_type not in ["card", "multiRowCard", "tableEx", "pieChart", "donutChart", "pivotTable", "map", "filledMap", "heatmap"] and pbi_visual_config.get("legends") != []:
                legend_projections = []
                for mc in marks_color:
                    if self._is_none_field(mc):
                        continue
                    cleaned = self.clean_field(mc)
                    if cleaned and not self._is_none_field(cleaned) and cleaned not in ["Measure Names", "Measure Values"]:
                        entity = self._extract_entity(mc, field_to_table, default_table)
                        resolved = self._resolve_field(cleaned, display_to_bi_name, entity)
                        legend_projections.append({
                            "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}},
                            "queryRef": f"{entity}.{resolved}", "nativeQueryRef": resolved, "active": True
                        })
                if legend_projections:
                    query_state["Series"] = {"projections": legend_projections}

            # Check if there is an explicit sort order specified in the config
            explicit_sort_items = []
            
            # Priority 1: Check for explicit power_bi_sort_instruction
            sort_instruction = self._find_power_bi_sort_instruction(pbi_visual_config)
            parsed_sort = None
            if sort_instruction:
                parsed_sort = self._parse_sort_instruction(sort_instruction)
                
            if parsed_sort:
                target_field, direction = parsed_sort
                clean_target = self.clean_field(target_field).lower().strip().replace('[', '').replace(']', '')
                
                def is_proj_match(proj):
                    f_obj = proj.get("field", {})
                    props_to_check = []
                    if "Column" in f_obj:
                        props_to_check.append(f_obj["Column"].get("Property", ""))
                    elif "Measure" in f_obj:
                        props_to_check.append(f_obj["Measure"].get("Property", ""))
                    elif "Aggregation" in f_obj:
                        props_to_check.append(f_obj["Aggregation"].get("Expression", {}).get("Column", {}).get("Property", ""))
                    
                    props_to_check.append(proj.get("nativeQueryRef", ""))
                    props_to_check.append(proj.get("queryRef", ""))
                    
                    for p in props_to_check:
                        if p:
                            clean_p = self.clean_field(p).lower().strip().replace('[', '').replace(']', '')
                            if clean_target == clean_p or clean_target in clean_p or clean_p in clean_target:
                                return True
                    return False

                found_proj = None
                for cp in category_projections + y_projections + y2_projections:
                    if is_proj_match(cp):
                        found_proj = cp
                        break
                
                if found_proj:
                    explicit_sort_items.append({"field": found_proj["field"], "direction": direction})

            # Priority 2: Standard field-level sort check if no instruction sort matched
            if not explicit_sort_items:
                # Helper to extract a matchable name from the projection's field object
                def _get_prop_from_proj(proj):
                    f_obj = proj.get("field", {})
                    if "Column" in f_obj:
                        return f_obj["Column"].get("Property", "")
                    elif "Measure" in f_obj:
                        return f_obj["Measure"].get("Property", "")
                    elif "Aggregation" in f_obj:
                        return f_obj["Aggregation"].get("Expression", {}).get("Column", {}).get("Property", "")
                    return proj.get("nativeQueryRef", "")
        
                for cp in category_projections:
                    p_name = _get_prop_from_proj(cp)
                    direction = self._find_field_sort_order(pbi_visual_config, p_name)
                    if direction:
                        explicit_sort_items.append({"field": cp["field"], "direction": direction})
        
                for yp in y_projections:
                    p_name = _get_prop_from_proj(yp)
                    direction = self._find_field_sort_order(pbi_visual_config, p_name)
                    if direction:
                        explicit_sort_items.append({"field": yp["field"], "direction": direction})
        
                for yp2 in y2_projections:
                    p_name = _get_prop_from_proj(yp2)
                    direction = self._find_field_sort_order(pbi_visual_config, p_name)
                    if direction:
                        explicit_sort_items.append({"field": yp2["field"], "direction": direction})

            sort_definition = None
            if explicit_sort_items:
                if visual_type == "pivotTable":
                    sort_definition = {"sort": explicit_sort_items, "isDefaultSort": True}
                else:
                    sort_definition = {"sort": [explicit_sort_items[0]], "isDefaultSort": True}
            else:
                if visual_type == "lineChart" and category_projections:
                    sort_definition = {"sort": [{"field": category_projections[0]["field"], "direction": "Ascending"}], "isDefaultSort": True}
                elif visual_type == "pivotTable":
                    # For Matrix visuals, default to sorting by all rows/columns in Ascending order
                    # Need to rebuild them here if not already available in this scope
                    r_p = self._build_category_projections(rows, field_to_table, default_table, display_to_bi_name, is_direct_lake=is_direct_lake, field_to_format=local_field_to_format)
                    c_p = self._build_category_projections(columns, field_to_table, default_table, display_to_bi_name, is_direct_lake=is_direct_lake, field_to_format=local_field_to_format)
                    sort_items = []
                    for p in r_p + c_p:
                        sort_items.append({"field": p["field"], "direction": "Ascending"})
                    if sort_items:
                        sort_definition = {"sort": sort_items, "isDefaultSort": True}
                elif y_projections:
                    sort_definition = {"sort": [{"field": y_projections[0]["field"], "direction": "Descending"}], "isDefaultSort": True}
                elif category_projections:
                    sort_definition = {"sort": [{"field": category_projections[0]["field"], "direction": "Ascending"}], "isDefaultSort": True}

            query_dict = {"queryState": query_state}

            # ── Tooltips: sheet_formatting ──
            tooltip_fields = []
            tf_data = page_entry.get("tooltip_formatting", [])
            
            # Unify structure: ensure we have a list of dicts to iterate over
            tf_list = []
            if isinstance(tf_data, dict):
                tf_list = [tf_data]
            elif isinstance(tf_data, list):
                tf_list = tf_data
            
            for t in tf_list:
                if isinstance(t, dict):
                    for f in t.get("fields_used", []):
                        if f not in tooltip_fields:
                            tooltip_fields.append(f)

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
                    measure_names, default_table, display_to_bi_name, visual_type=visual_type, field_to_format=local_field_to_format
                )
                if tip_proj and visual_type != "card":
                    query_state["Tooltips"] = {"projections": tip_proj}

            # Final safeguard: force-apply format strings on built projections.
            self._apply_formats_to_query_state(query_state, local_field_to_format)

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

            # Remove legacy title from visual.objects if it exists (standardize on visualContainerObjects)
            if "title" in visual_container["visual"].get("objects", {}):
                del visual_container["visual"]["objects"]["title"]

            # Apply visual_properties (fonts, colors, axis titles) if present
            self._apply_visual_properties(visual_container, visual_properties, pbi_config=pbi_visual_config, parent_type="sheet")

            # ── Visual Title Text (Visual Container Title) ──
            # SKIP legacy title logic if power_bi_visual_type is present (as per "no fallback" rule)
            if not pbi_visual_config:
                visual_title_text = page_entry.get("visual_title") or page_entry.get("display_name") or page_entry.get("name")
                if visual_title_text:
                    # Set 'title' in visualContainerObjects
                    vco = visual_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
                    title_list = vco.setdefault("title", [{}])
                    if not title_list: title_list.append({})
                    t_props = title_list[0].setdefault("properties", {})
                    t_props["show"] = {"expr": {"Literal": {"Value": "true"}}}
                    t_props["heading"] = {"expr": {"Literal": {"Value": "'Heading2'"}}}
                    t_props["alignment"] = {"expr": {"Literal": {"Value": "'center'"}}}

                    # Map to 'text' property
                    title_expr = {"expr": {"Literal": {"Value": f"'{visual_title_text}'"}}}
                    t_props["text"] = title_expr

            # Apply container styles (border, bg_color) from zone_hierarchy
            visual_styles = {}
            zone_hierarchy = page_entry.get("zone_hierarchy", {})
            for layout_name in ["Desktop", "Main", "Tablet", "Phone"]:
                zones = zone_hierarchy.get(layout_name, [])
                if zones:
                    visual_styles.update(self._flatten_zone_hierarchy(zones))
                    if visual_styles: break
            
            # Match style for the sheet
            sheet_name = page_entry.get("display_name", "")
            style = visual_styles.get(sheet_name)
            if not style:
                norm_sheet = sheet_name.strip().lower()
                for k, v in visual_styles.items():
                    if k.strip().lower() == norm_sheet:
                        style = v
                        break
            
            if style:
                self._apply_container_styles(visual_container, style)
            elif sheet_bg_color:
                # If no zone style, apply the sheet background to the visual container
                self._apply_container_styles(visual_container, {"bg_color": sheet_bg_color})

            # Force Data Labels show: true for every visual EXCEPT tableEx as per user requirement
            if visual_type != "tableEx":
                objs = visual_container.setdefault("visual", {}).setdefault("objects", {})
                objs["labels"] = [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}}}]

            # specifically fix tableEx properties (strip chart properties like labels/legend)
            if visual_type == "tableEx":
                objs = visual_container.get("visual", {}).get("objects", {})
                if "labels" in objs:
                    del objs["labels"]
                if "legend" in objs:
                    del objs["legend"]

            # ── filterConfig: auto from axis fields ──
            # Identify explicit visual-level filters to skip auto-generation for them
            visual_filters = page_entry.get("filters", [])
            explicit_filter_fields = set()
            if visual_filters:
                for f_item in visual_filters:
                    raw = f_item.get("tableau_original_name") if hasattr(f_item, "get") else str(f_item)
                    if raw:
                        explicit_filter_fields.add(self._normalize_for_matching(self.clean_field(raw)))

            auto_filters = []
            for cf in category_fields:
                clean_cf = self.clean_field(cf)
                norm_cf = self._normalize_for_matching(clean_cf)
                if norm_cf in explicit_filter_fields:
                    continue
                entity = self._extract_entity(cf, field_to_table, default_table)
                resolved = self._resolve_field(cf, display_to_bi_name, entity)
                auto_filters.append({
                    "name": str(uuid.uuid4()).replace("-", "")[:20],
                    "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}},
                    "type": "Categorical"
                })
            all_y_fields_for_filters = list(dict.fromkeys(list(y_fields) + list(y2_fields) + list(x_fields_scatter) + list(size_fields)))
            for yf in all_y_fields_for_filters:
                clean_yf, agg_func_str = self.get_field_agg_info(yf)
                # Re-clean the field name after extracting aggregation info
                clean_yf = self.clean_field(clean_yf)
                norm_yf = self._normalize_for_matching(clean_yf)
                
                if norm_yf in explicit_filter_fields:
                    continue
                entity = self._extract_entity(yf, field_to_table, default_table)
                resolved = self._resolve_field(clean_yf, display_to_bi_name, entity)
                
                pbi_func = self.map_agg_to_pbi(agg_func_str)
                norm_measure_names = {m.strip().lower() for m in measure_names if isinstance(m, str)}
                if yf.strip().lower() in norm_measure_names or clean_yf.strip().lower() in norm_measure_names or agg_func_str == 'AGG':
                    field_expr = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}
                else:
                    if pbi_func is not None:
                        func = pbi_func
                    else:
                        dt = field_to_datatype.get(clean_yf, "string")
                        func = 0 if dt in ["integer", "real"] else 5
                    field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}, "Function": func}}
                auto_filters.append({
                    "name": str(uuid.uuid4()).replace("-", "")[:20],
                    "field": field_expr,
                    "type": "Advanced"
                })
            # Build set of clean category, y, and y2 fields to skip duplicate tooltip filters
            skip_filter_fields = set()
            for f in list(category_fields) + list(y_fields) + list(y2_fields) + list(x_fields_scatter) + list(size_fields):
                skip_filter_fields.add(self.clean_field(f).lower())

            for tf in tooltip_fields_clean:
                clean_tf = self.clean_field(tf)
                if clean_tf.lower() in skip_filter_fields:
                    continue
                
                norm_tf = self._normalize_for_matching(tf)
                if norm_tf in explicit_filter_fields:
                    continue
                
                clean_tf, agg_func_str = self.get_field_agg_info(tf)
                entity = self._extract_entity(tf, field_to_table, default_table)
                resolved = self._resolve_field(clean_tf, display_to_bi_name, entity)
                
                pbi_func = self.map_agg_to_pbi(agg_func_str)
                norm_measure_names = {m.strip().lower() for m in measure_names if isinstance(m, str)}
                if tf.strip().lower() in norm_measure_names or clean_tf.strip().lower() in norm_measure_names or agg_func_str == 'AGG':
                    field_expr = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}
                else:
                    if pbi_func is not None:
                        func = pbi_func
                    else:
                        dt = field_to_datatype.get(clean_tf, "string")
                        func = 0 if dt in ["integer", "real"] else 5
                    field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}, "Function": func}}
                auto_filters.append({
                    "name": str(uuid.uuid4()).replace("-", "")[:20],
                    "field": field_expr,
                    "type": "Advanced"
                })
            visual_container["filterConfig"] = {"filters": auto_filters}

            # ── filterConfig: explicit visual filters ──
            if visual_filters:
                for filter_item in visual_filters:
                    raw_ff = filter_item.get("tableau_original_name", "") if hasattr(filter_item, "get") else str(filter_item)
                    if not raw_ff or self._is_none_field(raw_ff):
                        continue
                    ff = self.clean_field(raw_ff)
                    if not ff or ff in ["Measure Names", "Measure Values"]:
                        continue
                    entity = self._extract_entity(raw_ff, field_to_table, default_table)
                    resolved_ff = self._resolve_field(ff, display_to_bi_name, entity)
                    dt = field_to_datatype.get(ff, "string")
                    is_numeric = dt in ["integer", "real"]

                    clean_ff, agg_func_str = self.get_field_agg_info(raw_ff)
                    norm_measure_names = {m.strip().lower() for m in measure_names if isinstance(m, str)}
                    if ff.strip().lower() in norm_measure_names or agg_func_str == 'AGG':
                        field_expr = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved_ff}}
                        f_type = "Advanced"
                    else:
                        pbi_func = self.map_agg_to_pbi(agg_func_str)
                        
                        if pbi_func is not None:
                            func = pbi_func
                            field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved_ff}}, "Function": func}}
                            f_type = "Advanced"
                        else:
                            field_expr = {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved_ff}}
                            f_type = "Categorical" if dt == "string" else "Advanced"

                    # Apply default value 1 for param-based filters (e.g. Top N/Bottom N Filter)
                    is_param = False
                    if hasattr(filter_item, "get") and filter_item.get("tableau_type") == "parameter":
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
                            # Use 'value' or 'top' from mapping
                            top_n_val = int(filter_item.get("value") or filter_item.get("top") or 10)
                        except (ValueError, TypeError):
                            pass
                        
                        # Determine 'By_value' field - fallback to current field if not specified
                        by_val_raw = filter_item.get("By_value") or filter_item.get("by_value") or raw_ff
                        by_val_clean = self.clean_field(by_val_raw)
                        
                        # Extract entity for the By_value field
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
                        # TopN filters in PBI don't typically have howCreated: User
                        if "howCreated" in filter_obj:
                            del filter_obj["howCreated"]
                            
                        filter_obj["filter"] = self._create_top_n_filter_logic(
                            entity, resolved_ff, by_val_resolved, top_n_val, is_by_value_measure=is_by_val_measure, by_val_entity=by_val_entity, agg_func=agg_func
                        )
                    elif is_param:
                        param_current_value = None
                        is_action_param = False
                        
                        # Try to find current value among parameters
                        for p in parameters:
                            if p.get("name") == ff:
                                param_current_value = p.get("current_value")
                                break
                                
                        # Check for action-based target table and field
                        target_entity = entity
                        target_property = resolved_ff
                        
                        for act in actions:
                            pe = act.get("powerbi_equivalent", {})
                            if pe.get("target_parameter") == ff and "Parameter Action" in pe.get("implementation_type", ""):
                                target_entity = f"{ff}_Table"
                                target_property = pe.get("source_field", "Value")
                                is_action_param = True
                                # Update field_expr to use the action-based table/field
                                field_expr["Column"]["Expression"]["SourceRef"]["Entity"] = target_entity
                                field_expr["Column"]["Property"] = target_property
                                # If it's a categorical action param, use Categorical filter type
                                filter_obj["type"] = "Categorical"
                                break

                        if not is_action_param:
                            # Fallback for standard parameters: use {field}_Table if not in field_to_table
                            if not field_to_table.get(ff):
                                target_entity = resolved_ff
                                # Check if we should use p{Name}_Table pattern
                                for p in parameters:
                                    if p.get("name") == ff:
                                        raw_dax = p.get("powerbi", {}).get("dax") or p.get("dax") or ""
                                        clean_dax = re.sub(r'```[a-zA-Z]*', '', str(raw_dax)).replace('```', '').strip()
                                        match = re.search(r"'?([a-zA-Z0-9_ -]+)'?\[([a-zA-Z0-9_ -]+)\]", clean_dax)
                                        if match:
                                            target_entity = match.group(1)
                                            target_property = match.group(2)
                                        break
                        
                        filter_logic = self._create_parameter_filter_logic(
                            target_entity, target_property, 
                            is_measure=(ff in measure_names),
                            default_value=param_current_value if is_action_param else None
                        )
                        if filter_logic:
                            filter_obj["filter"] = filter_logic

                    visual_container["filterConfig"]["filters"].append(filter_obj)

            # =========================================================
            # LAYOUT: main visual (left) + slicers (right panel)
            # =========================================================
            page_visuals = []
            
            # ── Add Sheet Title Textbox if present ──
            visual_title = page_entry.get("visual_title", "")
            current_y = 10
            if visual_title:
                try:
                    title_textbox = self.template_manager.load_json_template("visuals", "textbox.json")
                    title_textbox["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
                    
                    # Extract formatting
                    title_formatting = {}
                    for f in visual_props.get("fonts", []):
                        if "title" in f.get("applied_to", "").lower() or "sheet" in f.get("applied_to", "").lower():
                            title_formatting = f
                            break
                    
                    title_color = "#000000"
                    for c in visual_props.get("colors", []):
                        if "title" in c.get("applied_to", "").lower() or "sheet" in c.get("applied_to", "").lower():
                            title_color = c.get("color") or c.get("hex_value") or "#000000"
                            break

                    font_name = title_formatting.get("font") or title_formatting.get("font_name") or "Arial"
                    font_size = title_formatting.get("size") or title_formatting.get("font_size") or "20"
                    is_bold = title_formatting.get("bold", True)
                    
                    align_val = title_formatting.get("alignment") or title_formatting.get("fontalignment") or "center"
                    pbi_align = "center"
                    if align_val:
                        str_val = str(align_val).strip().lower()
                        if str_val in ["1", "center"]: pbi_align = "center"
                        elif str_val in ["2", "right"]: pbi_align = "right"
                        elif str_val == "left": pbi_align = "left"

                    title_textbox["position"] = {
                        "x": 20, "y": current_y, "z": 0,
                        "height": 60, "width": 1240, "tabOrder": 0
                    }
                    
                    title_textbox["visual"]["objects"]["general"][0]["properties"]["paragraphs"] = [
                        {
                            "textRuns": [
                                {
                                    "value": visual_title,
                                    "textStyle": {
                                        "fontFamily": font_name,
                                        "fontSize": f"{font_size}px",
                                        "fontWeight": "bold" if is_bold else "normal",
                                        "color": title_color
                                    }
                                }
                            ],
                            "horizontalTextAlignment": pbi_align
                        }
                    ]
                    page_visuals.append(title_textbox)
                    current_y += 70
                except FileNotFoundError:
                    log_warning("textbox.json template not found, skipping page title")

            slicer_count = len([s for s in slicers if s])
            main_width = 960 if slicer_count > 0 else 1240

            visual_container["position"] = {
                "x": 20, "y": current_y, "z": 1 if visual_title else 0,
                "height": 700 - current_y, "width": main_width, "tabOrder": 1 if visual_title else 0
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
                        default_table, display_to_bi_name, parameter_names, parameter_metadata,
                        actions
                    )
                    if sv:
                        sv["position"] = {
                            "x": slicer_x, "y": slicer_y, "z": s_idx + 1,
                            "height": slicer_h - 10, "width": 230, "tabOrder": s_idx + 1
                        }
                        page_visuals.append(sv)
                        slicer_y += slicer_h

                # HTML Content Visuals via actions_by_source index
                for act in (actions_by_source or {}).get(page_entry.get("display_name", ""), []):
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
            
            # Track added back buttons to avoid duplicates
            added_back_targets = set()

            for act in actions:
                ta = act.get("tableau_action", {})
                pe = act.get("powerbi_equivalent", {})

                src_ws = self._normalize_for_matching(ta.get("source_worksheet", ""))
                src_vis = self._normalize_for_matching(pe.get("source_visual", ""))
                src_dash = self._normalize_for_matching(pe.get("source_dashboard", ta.get("source_dashboard", "")))

                is_nav = "page navigation" in pe.get("implementation_type", "").lower() or ta.get("type", "").lower() == "navigation"
                # For single-visual pages, we match on dashboard name or sheet name or "all sheets"
                if is_nav:
                    # --- Forward Navigation ---
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

                    # --- Symmetrical Back Navigation ---
                    target_sheet = pe.get("target_page") or ta.get("target_sheet")
                    if target_sheet and self._normalize_for_matching(target_sheet) == norm_page:
                        # Determine source page display name
                        source_label = pe.get("source_dashboard") or ta.get("source_dashboard") or ta.get("source_worksheet")
                        if source_label and source_label not in added_back_targets:
                            # Create a "Back" version of the action
                            back_act = {
                                "tableau_action": {"type": "navigation", "target_sheet": source_label},
                                "powerbi_equivalent": {"implementation_type": "page navigation", "target_page": source_label}
                            }
                            back_btn = self._build_action_button_visual(back_act, sheet_name_to_id, label=f"Back to {source_label}")
                            if back_btn:
                                # Place at top-left, slightly offset if there's a title
                                by = current_y if not visual_title else current_y - 60
                                back_btn["position"] = {"x": 20, "y": by, "z": 200, "height": 40, "width": 200, "tabOrder": 0}
                                page_visuals.append(back_btn)
                                added_back_targets.add(source_label)

            # =========================================================

            for v in page_visuals:
                v_type = v.get("visual", {}).get("visualType", "")
                if v_type.startswith("htmlContent"):
                    used_custom_visuals.add(v_type)
                files.append((f"pages/{page_id}/visuals/{v['name']}/visual.json", json.dumps(v, indent=2, ensure_ascii=False)))
            
            # Corrected: Write page.json at the end of the loop to capture dynamic height/width changes
            max_v_bottom = page_file.get("height", 720)
            for v in page_visuals:
                pos = v.get("position", {})
                if pos and "y" in pos and "height" in pos:
                    max_v_bottom = max(max_v_bottom, pos["y"] + pos["height"] + 40)
            page_file["height"] = max_v_bottom
            files.append((f"pages/{page_id}/page.json", json.dumps(page_file, indent=2, ensure_ascii=False)))

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

    def _calculate_optimal_dashboard_height(self, page_entry: Dict) -> int:
        """Calculates a dashboard height that ensures all visuals fit their 'natural' size."""
        # 1. Start with baseline default
        base_h = 720
        
        dashboard_visuals = page_entry.get("dashboard_visuals", [])
        zone_hierarchy = page_entry.get("zone_hierarchy", {}) or page_entry.get("layout", {})
        
        # 2. Extract positions and types from hierarchy
        primary_zones = []
        for layout_name in ["Desktop", "Main", "Tablet", "Phone"]:
            if isinstance(zone_hierarchy, dict) and layout_name in zone_hierarchy:
                primary_zones = zone_hierarchy[layout_name]
                break
        
        if not primary_zones and isinstance(zone_hierarchy, list):
            primary_zones = zone_hierarchy
        
        pos_map = self._extract_visual_positions_from_hierarchy(primary_zones)
        
        if not pos_map:
            # Fallback scaling: Estimate row count
            # Cards take ~120px, charts ~350px.
            dv_list = [v for v in dashboard_visuals if isinstance(v, dict)]
            if not dv_list: return base_h
            
            # Resolve types to estimate height
            kpi_count = 0
            chart_count = 0
            for dv in dv_list:
                v_type = self._resolve_visual_type(dv, measure_names=[])
                if v_type in ["card", "multiRowCard"]: kpi_count += 1
                else: chart_count += 1
            
            # Simple heuristic: 1 row of KPIs (max 4 per row), charts in 2 per row
            rows_h = 0
            if kpi_count > 0: rows_h += 140
            rows_h += ((chart_count + 1) // 2) * 380
            
            # Add margin for headers
            total_est = rows_h + 100
            return max(base_h, total_est)

        # 3. Hierarchy-based scaling
        # We want to find a scale factor 'S' such that for every visual 'v':
        # (v_h_percent * S) >= natural_h_of_v
        max_scale_factor = base_h
        
        # Build map of dashboard visual types for lookups
        dv_type_map = {}
        for dv in dashboard_visuals:
            if not isinstance(dv, dict): continue
            name = dv.get("display_name") or dv.get("name") or dv.get("tableau_sheet_name")
            if name:
                dv_type_map[name] = self._resolve_visual_type(dv)

        for key, pos in pos_map.items():
            raw_h_pct = pos.get("h", 0)
            if raw_h_pct <= 0: continue
            
            # Natural height based on type
            v_type = pos.get("type")
            if key in dv_type_map:
                v_type = dv_type_map[key]
            
            natural_h = 350 # Default for charts
            if v_type in ["card", "multiRowCard"]: natural_h = 130
            elif v_type == "title": natural_h = 60
            elif v_type == "text": natural_h = 50
            
            # (raw_h_pct / 100,000) * Scale >= natural_h
            # Scale >= (natural_h * 100,000) / raw_h_pct
            required_scale = (natural_h * 100000) // raw_h_pct
            if required_scale > max_scale_factor:
                max_scale_factor = required_scale
        
        # Cap the suggested height to avoid ridiculously long pages, but be generous
        # Usually 3000px is a safe upper bound for a single page report
        return min(3000, max(base_h, max_scale_factor))



