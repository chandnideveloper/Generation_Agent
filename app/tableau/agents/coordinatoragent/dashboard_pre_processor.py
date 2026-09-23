from typing import Dict, List, Any

class DashboardPreProcessorMixin:
    def _extract_text_formatting(self, zone: Dict, text_headers: List[Dict]):
        """Helper to recursively find text headers and basic formatting"""
        zone_type = zone.get("type", "")
        # If the zone contains text data
        if zone_type == "text":
            content_list = zone.get("formatted_text", [])
            if not isinstance(content_list, list):
                content_list = []
            for item in content_list:
                if not isinstance(item, dict): continue
                run_text = item.get("run", "")
                if run_text and run_text.strip():
                    text_headers.append({
                        "text": run_text,
                        "font_family": item.get("font_name", "Segoe UI"),
                        "font_size": item.get("font_size", 12),
                        "font_color": item.get("font_color", "#000000"),
                        "bold": item.get("bold", False),
                        "italic": item.get("italic", False),
                        "alignment": item.get("alignment") or item.get("fontalignment")
                    })
        # Recursive check for sub-zones
        for child in zone.get("children", []):
            self._extract_text_formatting(child, text_headers)

    def _process_dashboards(self, dashboards: List[Dict], sheet_by_name: Dict[str, Dict], formatting_by_sheet: Dict[str, Dict], page_idx: int) -> tuple[List[Dict], List[str], int]:
        """
        Parses raw dashboard definitions into list of standardized Page dictionaries.
        """
        pages, page_order = [], []

        for dash in dashboards:
            page_idx += 1
            page_id = f"Page{page_idx}"
            page_order.append(page_id)

            dash_name = dash.get("dashboard_name", f"Dashboard {page_idx}")
            dash_objects = dash.get("dashboard_objects", [])

            # Collect all sheet visuals referenced in this dashboard
            dashboard_visuals = []
            for obj_name in dash_objects:
                if obj_name in sheet_by_name:
                    sv = sheet_by_name[obj_name]
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

            # Extract image references from dashboard_objects (those matching "Image (...)" pattern)
            dashboard_images = []
            for obj_name in dash_objects:
                if obj_name.startswith("Image (") and obj_name.endswith(")"):
                    # Extract relative path from "Image (Image/logo.jpg)"
                    image_relative_path = obj_name[len("Image ("):-1].strip()
                    if image_relative_path:
                        dashboard_images.append({"relative_path": image_relative_path})

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
                "dashboard_images": dashboard_images,
                "text_headers": text_headers,
                "buttons": buttons,
                "drill_throughs": clean_drills,
                "bookmarks": bookmarks,
                "filters": []
            })
            
        return pages, page_order, page_idx
