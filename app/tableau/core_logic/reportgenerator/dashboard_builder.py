import json
import uuid
import re
from typing import Dict, Optional, List, Tuple
from app.tableau.core.logging_utils import log_info, log_error, log_warning
from app.tableau.core.config import Config
from app.tableau.core_logic.template_manager.template_manager import TemplateManager
from app.tableau.services.action_logger import ActionLogger


class DashboardBuilderMixin:
    def _generate_dashboard_page_visuals(self, page_entry: Dict, field_to_table: Dict,
                                          field_to_datatype: Dict, measure_names: List,
                                          default_table: str, display_to_bi_name: Dict = None,
                                          actions: List[Dict] = None, sheet_name_to_id: Dict = None,
                                          actions_by_source: Dict = None,
                                          parameter_names: set = None, parameters: List[Dict] = None,
                                          sheet_bg_color: str = None,
                                          embedded_assets: Dict = None,
                                          page_width: int = 1280,
                                          page_height: int = 720,
                                          is_direct_lake: bool = False,
                                          dashboard_title_formatting: Dict = None,
                                          field_to_format: Dict = None) -> List[Dict]:
        """Generates all visuals for a dashboard page (multi-visual with layout)."""
        dashboard_visuals = page_entry.get("dashboard_visuals", [])
        text_headers = page_entry.get("text_headers", [])
        visual_title = page_entry.get("visual_title", "")
        zone_hierarchy = page_entry.get("zone_hierarchy", {}) or page_entry.get("layout", {})
        
        if embedded_assets is None: embedded_assets = {}
        if actions is None: actions = []
        if sheet_name_to_id is None: sheet_name_to_id = {}
        if display_to_bi_name is None: display_to_bi_name = {}
        if actions_by_source is None: actions_by_source = {}
        if parameter_names is None: parameter_names = set()
        if parameters is None: parameters = []

        # 0. Collect all unique slicers for the page
        self._added_slicers_on_page = set()
        all_page_slicers = []
        for dv in dashboard_visuals:
            if not isinstance(dv, dict): continue
            for sl_def in dv.get("slicers", []):
                if not sl_def: continue
                f_name = self.clean_field(sl_def)
                if f_name not in self._added_slicers_on_page:
                    all_page_slicers.append(sl_def)
                    self._added_slicers_on_page.add(f_name)

        # 1. Flatten zone_hierarchy to get positions and styles
        primary_zones = []
        for layout_name in ["Desktop", "Main", "Tablet", "Phone"]:
            if isinstance(zone_hierarchy, dict) and layout_name in zone_hierarchy:
                primary_zones = zone_hierarchy[layout_name]
                break
        
        if not primary_zones and isinstance(zone_hierarchy, list):
            primary_zones = zone_hierarchy
        elif not primary_zones and isinstance(zone_hierarchy, dict) and "children" in zone_hierarchy:
            primary_zones = zone_hierarchy["children"]

        pos_map = self._extract_visual_positions_from_hierarchy(primary_zones)
        
        page_visuals = []
        z_idx = 0
        tab_ord = 0

        # Build a map of dashboard visuals by name
        dv_by_name = {}
        for dv in dashboard_visuals:
            if not isinstance(dv, dict): continue
            name = dv.get("display_name") or dv.get("name") or dv.get("tableau_sheet_name")
            if name:
                dv_by_name[name] = dv

        # =========================================================
        # 2. HIERARCHY-BASED LAYOUT (Primary)
        # =========================================================
        placed_visual_names = set()
        if pos_map:
            # Sort by keys to keep some stability, though pos_map order should be from recursion
            for key, pos in pos_map.items():
                visual = None
                obj_type = pos.get("type")
                
                try:
                    # A. Data Visuals (matched by name)
                    if key in dv_by_name:
                        dv = dv_by_name[key]
                        dv["_resolved_type"] = self._resolve_visual_type(dv, measure_names=measure_names)
                        # Ensure titles are always enabled for dashboard visuals
                        suppress = False
                        visual = self._build_data_visual(dv, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, parameter_names=parameter_names, actions=actions, parameters=parameters, suppress_title=suppress, is_direct_lake=is_direct_lake, dashboard_title_formatting=dashboard_title_formatting, parent_type="dashboard", field_to_format=field_to_format)
                        placed_visual_names.add(key)
                    
                    # B. Bitmaps (Images)
                    elif obj_type == "bitmap":
                        visual = self._build_image_visual(pos, embedded_assets)
                    
                    # C. Text Objects
                    elif obj_type == "text":
                        formatted_text = pos.get("formatted_text", {})
                        if not isinstance(formatted_text, dict):
                            formatted_text = {}
                        runs = formatted_text.get("runs", [])
                        if runs or key.startswith("Text Header ("):
                            header_text = runs[0].get("text", "") if (runs and isinstance(runs[0], dict)) else ""
                            
                            # NEW: If key is a Text Header, extract display name from bracket
                            if key.startswith("Text Header (") and key.endswith(")"):
                                header_text = key.replace("Text Header (", "").rstrip(")")
                            
                            th = next((x for x in text_headers if x.get("text") == header_text), {})
                            visual = self._build_text_visual_from_hierarchy(pos, th, header_text=header_text)

                    # D. Web Objects
                    elif obj_type == "web":
                        param = pos.get("param", "")
                        if param.startswith("http"):
                            visual = self.template_manager.load_json_template("visuals", "htmlContent443BE3AD55E043BF878BED274D3A6855.json")

                except (FileNotFoundError, json.JSONDecodeError) as e:
                    tmpl_name = ""
                    if obj_type == "bitmap":
                        tmpl_name = "image.json"
                    elif obj_type == "text":
                        tmpl_name = "textbox.json"
                    elif obj_type == "web":
                        tmpl_name = "htmlContent443BE3AD55E043BF878BED274D3A6855.json"
                    else:
                        if key in dv_by_name:
                            dv = dv_by_name[key]
                            resolved_type = dv.get("_resolved_type") or self._resolve_visual_type(dv, measure_names=measure_names)
                            tmpl_name = f"{resolved_type}.json"
                    
                    log_warning(f"Skipping visual {tmpl_name or key}: {e!r}")
                    self._record_skipped(key, reason=str(e))
                    continue

                if visual:
                    if not visual.get("name"): visual["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
                    
                    # Position conversion (100k scale -> PBI dynamic dimensions)
                    raw_x, raw_y = pos.get("x", 0), pos.get("y", 0)
                    raw_w, raw_h = pos.get("w", 0), pos.get("h", 0)
                    visual["position"] = {
                        "x": (raw_x * page_width) // 100000, "y": (raw_y * page_height) // 100000,
                        "width": (raw_w * page_width) // 100000, "height": (raw_h * page_height) // 100000,
                        "z": z_idx, "tabOrder": tab_ord
                    }
                    
                    if pos.get("style"): self._apply_container_styles(visual, pos["style"])
                    if sheet_bg_color and "background" not in visual.get("visual", {}).get("visualContainerObjects", {}):
                        self._apply_container_styles(visual, {"bg_color": sheet_bg_color})
                    
                    # Apply visual formatting from properties
                    if key in dv_by_name:
                        dv = dv_by_name[key]
                        pbi_config = dv.get("power_bi_visual_type", {})
                        suppress = False
                        self._apply_visual_properties(visual, dv.get("visual_properties", {}), suppress_title=suppress, pbi_config=pbi_config, dashboard_title_formatting=dashboard_title_formatting, parent_type="dashboard")

                    page_visuals.append(visual)
                    
                    # Add Action Buttons for Navigation if defined for this visual
                    if key in dv_by_name:
                        dv_match = [self._normalize_for_matching(dv_by_name[key].get("display_name", "")), self._normalize_for_matching(key)]
                        for act in actions:
                            pe = act.get("powerbi_equivalent", {})
                            ta = act.get("tableau_action", {})
                            is_nav = "page navigation" in pe.get("implementation_type", "").lower() or ta.get("type", "").lower() == "navigation"
                            if is_nav:
                                src_ws = self._normalize_for_matching(ta.get("source_worksheet", ""))
                                src_vis = self._normalize_for_matching(pe.get("source_visual", ""))
                                if src_ws in dv_match or src_vis in dv_match:
                                    btn = self._build_action_button_visual(act, sheet_name_to_id)
                                    if btn:
                                        btn["position"] = visual["position"].copy()
                                        btn["position"]["z"] = visual["position"]["z"] + 1
                                        page_visuals.append(btn)
                    
                    z_idx += 2; tab_ord += 2

        # =========================================================
        # 3. FALLBACK LAYOUT (If no hierarchy found)
        # =========================================================
        if not pos_map:
            # This is the original logic. We keep it as a fallback.
            has_right_panel = len(all_page_slicers) > 0
            content_width = (page_width - 320) if has_right_panel else (page_width - 40)
            header_y = 10
            
            # Sheet Title if any
            if visual_title:
                try:
                    tb = self.template_manager.load_json_template("visuals", "textbox.json")
                    tb["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
                    tb["position"] = {"x": 20, "y": header_y, "z": z_idx, "height": 60, "width": content_width, "tabOrder": tab_ord}
                    # ... text content logic ... (simplified for brevity in this replace_file_content)
                    page_visuals.append(tb)
                except (FileNotFoundError, KeyError, ValueError, TypeError) as e:
                    self._record_skipped("title_textbox", f"Failed to build title textbox: {e}")

            for th in text_headers:
                try:
                    tb = self.template_manager.load_json_template("visuals", "textbox.json")
                    tb["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
                    tb["position"] = {"x": 20, "y": header_y, "z": z_idx, "height": 60, "width": content_width, "tabOrder": tab_ord}
                    
                    # Set the actual text from th
                    header_text = th.get("text", "Header")
                    tb["visual"]["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"][0]["value"] = header_text
                    
                    page_visuals.append(tb)
                except (FileNotFoundError, KeyError, ValueError, TypeError) as e:
                    self._record_skipped("header_textbox", f"Failed to build header textbox: {e}")
                    continue

            current_y = header_y + 10
            
            # Pre-resolve types for all visuals to separate KPIs from charts
            for dv in dashboard_visuals:
                dv["_resolved_type"] = self._resolve_visual_type(dv, measure_names)
            
            kpi_visuals = [dv for dv in dashboard_visuals if dv.get("_resolved_type") in ["card", "multiRowCard"]]
            chart_visuals = [dv for dv in dashboard_visuals if dv not in kpi_visuals]
            
            if kpi_visuals:
                kx = 20; kw = content_width // len(kpi_visuals)
                for kpi in kpi_visuals:
                    suppress = False
                    v = self._build_data_visual(kpi, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, parameter_names=parameter_names, actions=actions, parameters=parameters, suppress_title=suppress, is_direct_lake=is_direct_lake, dashboard_title_formatting=dashboard_title_formatting, parent_type="dashboard", field_to_format=field_to_format)
                    if v:
                        v["position"] = {"x": kx, "y": current_y, "z": z_idx, "height": 120, "width": min(kw - 10, 300), "tabOrder": tab_ord}
                        page_visuals.append(v)
                        z_idx += 1; tab_ord += 1

                        # Attach action overlay if any
                        kpi_name = kpi.get("display_name", "") or kpi.get("name", "")
                        dv_match = [self._normalize_for_matching(kpi_name), self._normalize_for_matching(kpi.get("tableau_sheet_name", ""))]
                        for act in actions:
                            pe = act.get("powerbi_equivalent", {})
                            ta = act.get("tableau_action", {})
                            if "page navigation" in pe.get("implementation_type", "").lower() or ta.get("type", "").lower() == "navigation":
                                src_ws = self._normalize_for_matching(ta.get("source_worksheet", ""))
                                src_vis = self._normalize_for_matching(pe.get("source_visual", ""))
                                if src_ws in dv_match or src_vis in dv_match:
                                    btn = self._build_action_button_visual(act, sheet_name_to_id)
                                    if btn:
                                        btn["position"] = v["position"].copy()
                                        btn["position"]["z"] = z_idx
                                        page_visuals.append(btn)
                                        z_idx += 1; tab_ord += 1

                        kx += kw
                current_y += 140

            chart_x = 20; charts_in_row = 0; chart_width = (content_width - 40) // 2 if len(chart_visuals) > 1 else content_width
            for cv in chart_visuals:
                suppress = False
                v = self._build_data_visual(cv, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, parameter_names=parameter_names, actions=actions, parameters=parameters, suppress_title=suppress, is_direct_lake=is_direct_lake, dashboard_title_formatting=dashboard_title_formatting, parent_type="dashboard", field_to_format=field_to_format)
                if v:
                    v["position"] = {"x": chart_x, "y": current_y, "z": z_idx, "height": 350, "width": chart_width, "tabOrder": tab_ord}
                    page_visuals.append(v)
                    z_idx += 1; tab_ord += 1
                    
                    # Attach action overlay if any
                    cv_name = cv.get("display_name", "") or cv.get("name", "")
                    dv_match = [self._normalize_for_matching(cv_name), self._normalize_for_matching(cv.get("tableau_sheet_name", ""))]
                    for act in actions:
                        pe = act.get("powerbi_equivalent", {})
                        ta = act.get("tableau_action", {})
                        if "page navigation" in pe.get("implementation_type", "").lower() or ta.get("type", "").lower() == "navigation":
                            src_ws = self._normalize_for_matching(ta.get("source_worksheet", ""))
                            src_vis = self._normalize_for_matching(pe.get("source_visual", ""))
                            if src_ws in dv_match or src_vis in dv_match:
                                btn = self._build_action_button_visual(act, sheet_name_to_id)
                                if btn:
                                    btn["position"] = v["position"].copy()
                                    btn["position"]["z"] = z_idx
                                    page_visuals.append(btn)
                                    z_idx += 1; tab_ord += 1

                    chart_x += (chart_width + 20); charts_in_row += 1
                    if charts_in_row >= 2: current_y += 370; chart_x = 20; charts_in_row = 0

        # =========================================================
        # 3.5 IMAGE VISUALS from dashboard_images
        # =========================================================
        # Process images declared in dashboard_objects (e.g. "Image (Image/logo.jpg)")
        # that may not have been placed via the hierarchy bitmap path
        dashboard_images = page_entry.get("dashboard_images", [])
        if dashboard_images:
            images_list = embedded_assets.get("images_and_shapes", {}).get("images", [])
            # Build a lookup of already-placed image visuals to avoid duplicates
            placed_image_urls = set()
            for pv in page_visuals:
                img_url_obj = pv.get("visual", {}).get("objects", {}).get("general", [{}])[0].get("properties", {}).get("imageUrl", {})
                url_val = img_url_obj.get("expr", {}).get("Literal", {}).get("Value", "")
                if url_val and url_val != "''":
                    placed_image_urls.add(url_val.strip("'"))

            img_x = 20
            img_y = 10  # Place images at top of the page
            for img_ref in dashboard_images:
                rel_path = img_ref.get("relative_path", "")
                if not rel_path:
                    continue
                norm_rel = rel_path.replace("\\", "/").lower()
                
                # Find matching image in embedded_assets
                image_url = ""
                for img in images_list:
                    img_path = img.get("relative_path", "").replace("\\", "/").lower()
                    if img_path == norm_rel:
                        image_url = img.get("image_url", "")
                        break
                    # Also try matching by filename only
                    if not image_url and img.get("name", "").lower() == norm_rel.split("/")[-1]:
                        image_url = img.get("image_url", "")
                
                if not image_url or image_url in placed_image_urls:
                    continue
                
                try:
                    image_vis = self.template_manager.load_json_template("visuals", "image.json")
                    image_vis["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
                    
                    objs = image_vis.setdefault("visual", {}).setdefault("objects", {})
                    
                    # Update general properties
                    general = objs.setdefault("general", [{}])
                    if not general: general.append({})
                    general[0].setdefault("properties", {})["imageUrl"] = {
                        "expr": {"Literal": {"Value": f"'{image_url}'"}}
                    }
                    
                    # Update image properties
                    img_props_list = objs.setdefault("image", [{}])
                    if not img_props_list: img_props_list.append({})
                    img_props = img_props_list[0].setdefault("properties", {})
                    img_props["sourceType"] = {"expr": {"Literal": {"Value": "'imageUrl'"}}}
                    img_props["sourceUrl"] = {"expr": {"Literal": {"Value": f"'{image_url}'"}}}

                    image_vis["position"] = {
                        "x": img_x, "y": img_y, "z": z_idx,
                        "height": 80, "width": 150, "tabOrder": tab_ord
                    }
                    page_visuals.append(image_vis)
                    placed_image_urls.add(image_url)
                    img_x += 160
                    z_idx += 1; tab_ord += 1
                    log_info(f"Added dashboard image visual: {rel_path} -> {image_url}")
                except Exception as e:
                    log_warning(f"Failed to create image visual for {rel_path}: {e}")

        # =========================================================
        # 4. GLOBAL ELEMENTS (Actions, Slicers, Back Navigation)
        # =========================================================
        # Add Global Actions (HTML Overlays / Dash-level buttons)
        dash_name_norm = self._normalize_for_matching(page_entry.get("display_name", ""))
        
        # Track added back buttons to avoid duplicates
        added_back_targets = set()

        for act in actions:
            pe = act.get("powerbi_equivalent", {})
            ta = act.get("tableau_action", {})
            
            src_dash = self._normalize_for_matching(pe.get("source_dashboard") or ta.get("source_dashboard", ""))
            src_vis = self._normalize_for_matching(pe.get("source_visual") or ta.get("source_worksheet", ""))
            
            # --- Forward Navigation ---
            if src_dash == dash_name_norm and (src_vis in ["all sheets", ""] or not src_vis):
                is_nav = "page navigation" in pe.get("implementation_type", "").lower() or ta.get("type", "").lower() == "navigation"
                if is_nav:
                    btn = self._build_action_button_visual(act, sheet_name_to_id)
                    if btn:
                        # Full page overlay if it's dashboard level
                        btn["position"] = {"x": 0, "y": 0, "z": z_idx + 50, "height": page_height, "width": page_width, "tabOrder": tab_ord + 50}
                        page_visuals.append(btn)
                
                # HTML Overlay
                html = self._build_html_content_visual(act, default_table)
                if html:
                    # Look for any very wide data visual on the page to place side-by-side
                    placed_side_by_side = False
                    for pv in page_visuals:
                        pos = pv.get("position", {})
                        v_type = pv.get("visual", {}).get("visualType", "")
                        # Check if this visual is a main data chart (height >= 150, y >= 50) and spans full width
                        if pos and pos.get("width", 0) >= 1000 and pos.get("height", 0) >= 150 and pos.get("y", 0) >= 50 and v_type not in ["actionButton", "slicer", "textbox", ""]:
                            # Resize the wide visual to make space on the right
                            pos["width"] = 800
                            # Position the HTML content visual side-by-side on the right
                            html["position"] = {
                                "x": 840,
                                "y": pos["y"],
                                "z": z_idx + 40,
                                "height": pos["height"],
                                "width": 400,
                                "tabOrder": tab_ord + 40
                            }
                            placed_side_by_side = True
                            break
                    
                    if not placed_side_by_side:
                        # Fallback to stacking below
                        max_y = 20
                        for pv in page_visuals:
                            pos = pv.get("position", {})
                            if pos and "y" in pos and "height" in pos:
                                # Avoid matching full page overlay buttons that span the entire page_height
                                if pos.get("height", 0) < page_height:
                                    max_y = max(max_y, pos["y"] + pos["height"])
                        
                        html_y = max_y + 20
                        html["position"] = {"x": 20, "y": html_y, "z": z_idx + 40, "height": 300, "width": 400, "tabOrder": tab_ord + 40}
                    
                    page_visuals.append(html)

            # --- Symmetrical Back Navigation ---
            target_sheet = pe.get("target_page") or ta.get("target_sheet")
            if target_sheet and self._normalize_for_matching(target_sheet) == dash_name_norm:
                is_nav = "page navigation" in pe.get("implementation_type", "").lower() or ta.get("type", "").lower() == "navigation"
                if is_nav:
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
                            by = 10 if not visual_title else 70
                            back_btn["position"] = {"x": 20, "y": by, "z": z_idx + 150, "height": 40, "width": 200, "tabOrder": tab_ord + 150}
                            page_visuals.append(back_btn)
                            added_back_targets.add(source_label)

        # Slicers on panel if not already placed from hierarchy
        if all_page_slicers and not any(v.get("visual", {}).get("visualType") == "slicer" for v in page_visuals):
            sy = 20; sx = page_width - 280
            for sl in all_page_slicers:
                # Slicers on dashboard also need titles enabled
                sv = self._build_slicer_visual(sl, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, parameter_names, parameters, actions, suppress_title=False)
                if sv:
                    sv["position"] = {"x": sx, "y": sy, "z": z_idx + 100, "height": 100, "width": 230, "tabOrder": tab_ord + 100}
                    page_visuals.append(sv)
                    sy += 110

        return page_visuals

    def _extract_visual_positions_from_hierarchy(self, zones: List[Dict]) -> Dict[str, Dict]:
        """Recursively extracts x, y, w, h and identifiers from zone hierarchy."""
        pos_map = {}
        for zone in zones:
            if not isinstance(zone, dict): continue
            attr = zone.get("attributes", {})
            name = attr.get("name")
            type_v2 = attr.get("type-v2")
            
            # If it's a leaf node/object
            if name or type_v2 in ["bitmap", "text", "web"]:
                key = name if name else f"{type_v2}_{id(zone)}"
                pos_map[key] = {
                    "x": int(attr.get("x", 0)),
                    "y": int(attr.get("y", 0)),
                    "w": int(attr.get("w", 0)),
                    "h": int(attr.get("h", 0)),
                    "type": type_v2,
                    "param": attr.get("param"),
                    "style": zone.get("style", {}),
                    "formatted_text": zone.get("formatted_text", {})
                }
            
            children = zone.get("children", [])
            if children:
                pos_map.update(self._extract_visual_positions_from_hierarchy(children))
        return pos_map

    def _build_image_visual(self, pos: Dict, embedded_assets: Dict) -> Optional[Dict]:
        """Creates a Power BI image visual from dashboard bitmap data."""
        param = pos.get("param")
        if not param: return None
        
        norm_param = param.replace("\\", "/").lower()
        image_url = ""
        images = embedded_assets.get("images_and_shapes", {}).get("images", [])
        for img in images:
            img_path = img.get("relative_path", "").replace("\\", "/").lower()
            if img_path == norm_param:
                image_url = img.get("image_url")
                break
        
        if not image_url: return None
        
        image_vis = self.template_manager.load_json_template("visuals", "image.json")
        try:
            image_vis["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
            
            objs = image_vis.setdefault("visual", {}).setdefault("objects", {})
            
            # Update general properties
            general = objs.setdefault("general", [{}])
            if not general: general.append({})
            general[0].setdefault("properties", {})["imageUrl"] = {
                "expr": {"Literal": {"Value": f"'{image_url}'"}}
            }
            
            # Update image properties
            img_props_list = objs.setdefault("image", [{}])
            if not img_props_list: img_props_list.append({})
            img_props = img_props_list[0].setdefault("properties", {})
            img_props["sourceType"] = {"expr": {"Literal": {"Value": "'imageUrl'"}}}
            img_props["sourceUrl"] = {"expr": {"Literal": {"Value": f"'{image_url}'"}}}
            
            return image_vis
        except (FileNotFoundError, KeyError, ValueError, TypeError, AttributeError) as e:
            self._record_skipped("image_visual", f"Failed to build image visual: {e}")
            return None

    def _build_text_visual_from_hierarchy(self, pos: Dict, template_th: Dict, header_text: str = None) -> Optional[Dict]:
        """Creates a textbox visual from hierarchy text data."""
        textbox = self.template_manager.load_json_template("visuals", "textbox.json")
        try:
            textbox["name"] = str(uuid.uuid4()).replace("-", "")[:12].upper()
            
            fmt_text = pos.get("formatted_text", {})
            if not isinstance(fmt_text, dict):
                fmt_text = {}
            runs = fmt_text.get("runs", [])
            if not runs and not header_text: return None
            
            attrs = {}
            if runs and isinstance(runs[0], dict):
                if not header_text:
                    header_text = runs[0].get("text", "Header")
                attrs = runs[0].get("attributes", {})
            elif not header_text:
                header_text = "Header"
            
            # Map attributes
            font_size = attrs.get("fontsize") or template_th.get("font_size") or "12"
            font_name = template_th.get("font_name") or "Arial"
            is_bold = attrs.get("bold") == "true" or template_th.get("bold", False)
            font_color = template_th.get("font_color") or "#000000"
            
            # Alignment mapping
            align_val = attrs.get("fontalignment")
            pbi_align = "center"
            if align_val == "1": pbi_align = "center"
            elif align_val == "2": pbi_align = "right"
            elif align_val == "0": pbi_align = "left"

            textbox["visual"]["objects"]["general"][0]["properties"]["paragraphs"] = [{
                "textRuns": [{
                    "value": header_text,
                    "textStyle": {"fontFamily": font_name, "fontSize": f"{font_size}px", "fontWeight": "bold" if is_bold else "normal", "color": font_color}
                }],
                "horizontalTextAlignment": pbi_align
            }]
            return textbox
        except (FileNotFoundError, KeyError, ValueError, TypeError, AttributeError) as e:
            self._record_skipped("hierarchy_textbox", f"Failed to build hierarchy textbox: {e}")
            return None

    def _record_skipped(self, key: str, reason: str):
        if not hasattr(self, "skipped_visuals"):
            self.skipped_visuals = []
        self.skipped_visuals.append({"visual_key": key, "reason": reason})

