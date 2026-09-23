import json
import uuid
import re
from typing import Dict, Optional, List, Tuple
from app.tableau.core.logging_utils import log_info, log_error, log_warning
from app.tableau.core.config import Config
from app.tableau.core_logic.template_manager.template_manager import TemplateManager
from app.tableau.services.action_logger import ActionLogger


class FormattingBuilderMixin:
    def _apply_container_styles(self, visual_container: Dict, style: Dict) -> None:
        """Maps zone_hierarchy styles (border, bg_color, etc.) to PBI visualContainerObjects."""
        if not style: return

        # Border mapping
        border_color = style.get("border-color")
        border_style = style.get("border-style", "solid").lower()
        border_width = style.get("border-width")

        if border_color and border_color != "none":
            vco = visual_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
            border_props = {
                "show": {"expr": {"Literal": {"Value": "true"}}},
                "color": {
                    "solid": {
                        "color": {"expr": {"Literal": {"Value": f"'{border_color.upper()}'"}}}
                    }
                }
            }
            
            # Map border width if present
            if border_width:
                try:
                    # Power BI border width is typically an integer
                    width_val = int(re.search(r'\d+', str(border_width)).group())
                    border_props["width"] = {"expr": {"Literal": {"Value": str(width_val)}}}
                except (ValueError, AttributeError):
                    pass

            # Map border style - User wants solid border everywhere
            pbi_style = "Solid"
            
            border_props["lineStyle"] = {"expr": {"Literal": {"Value": f"'{pbi_style}'"}}}
            
            vco["border"] = [{"properties": border_props}]

        # Background color mapping
        bg_color = style.get("bg_color") or style.get("background-color")
        if bg_color and bg_color != "none":
            vco = visual_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
            vco["background"] = [{
                "properties": {
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "color": {
                        "solid": {
                            "color": {"expr": {"Literal": {"Value": f"'{bg_color.upper()}'"}}}
                        }
                    }
                }
            }]

        # Margin/Padding (limited mapping to PBI padding if available in template)
        # Most layout objects in PBI use position (x, y, width, height) which are handled in dashboard_builder.
        # But we can log or adjust if needed.

        # Data labels override if present in style
        if style.get("show-labels") == "true":
            objs = visual_container.setdefault("visual", {}).setdefault("objects", {})
            objs["labels"] = [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}}}]

    def _apply_visual_properties(self, visual_container: Dict, visual_properties: Dict, suppress_title: bool = False, pbi_config: Dict = None, dashboard_title_formatting: Dict = None, parent_type: str = "sheet") -> None:
        """Applies Tableau visual_properties OR Power BI specific config to the visual."""
        
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

                    # ── Title / Sheet Formatting (Visual Container Title) ──
                    if "title" in applied_to or "sheet" in applied_to:
                        # Clear any existing title in 'objects' to ensure it's ONLY in container
                        if "title" in objects:
                            del objects["title"]

                        vco = visual_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
                        title_list = vco.setdefault("title", [{}])
                        if not title_list: title_list.append({})
                        props = title_list[0].setdefault("properties", {})

                        # Ensure title is shown if there's formatting, UNLESS suppressed
                        if not suppress_title:
                            props["show"] = {"expr": {"Literal": {"Value": "true"}}}
                        else:
                            props["show"] = {"expr": {"Literal": {"Value": "false"}}}
                        
                        # Heading level
                        props["heading"] = {"expr": {"Literal": {"Value": "'Heading2'"}}}

                        if font_entry.get("font"):
                            props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{font_entry['font']}'"}}}
                        elif font_entry.get("font_name"):
                            props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{font_entry['font_name']}'"}}}

                        sz = font_entry.get("size") or font_entry.get("font_size")
                        if sz:
                            # Append 'D' for decimal/double as requested for visualContainer titles
                            props["fontSize"] = {"expr": {"Literal": {"Value": f"{sz}D"}}}

                        if font_entry.get("bold") is True:
                            props["bold"] = {"expr": {"Literal": {"Value": "true"}}}

                        align_val = font_entry.get("alignment") or font_entry.get("fontalignment")
                        if not align_val:
                            fmt_text = font_entry.get("formatted_text", {})
                            if isinstance(fmt_text, dict):
                                runs = fmt_text.get("runs", [])
                                if runs and isinstance(runs[0], dict):
                                    attrs = runs[0].get("attributes", {})
                                    align_val = attrs.get("fontalignment") or attrs.get("alignment")

                        pbi_align = "center" # Change default to center
                        if align_val is not None:
                            str_val = str(align_val).strip().lower()
                            if str_val in ["1", "center"]:
                                pbi_align = "center"
                            elif str_val in ["2", "right"]:
                                pbi_align = "right"
                            elif str_val in ["0", "left"]:
                                pbi_align = "left"
                            
                            props["alignment"] = {"expr": {"Literal": {"Value": f"'{pbi_align}'"}}}

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
                        vco = visual_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
                        title_list = vco.setdefault("title", [{}])
                        if not title_list: title_list.append({})
                        props = title_list[0].setdefault("properties", {})
                        # In visualContainerObjects, it's 'fontColor'
                        color_expr = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{hex_val.upper()}'"}}}} }
                        props["fontColor"] = color_expr
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

            # ── Apply Global Dashboard Title Formatting (Overrides) ──
            if dashboard_title_formatting:
                vco = visual_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
                title_list = vco.setdefault("title", [{}])
                if not title_list: title_list.append({})
                props = title_list[0].setdefault("properties", {})
                
                # Force show if formatting is provided
                props["show"] = {"expr": {"Literal": {"Value": "true"}}}
                
                # Apply Style Properties
                if dashboard_title_formatting.get("font"):
                    props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{dashboard_title_formatting['font']}'"}}}
                
                sz = dashboard_title_formatting.get("size")
                if sz:
                    sz_val = re.search(r'\d+', str(sz)).group() if re.search(r'\d+', str(sz)) else "12"
                    props["fontSize"] = {"expr": {"Literal": {"Value": f"{sz_val}D"}}}
                
                if dashboard_title_formatting.get("bold") is True:
                    props["bold"] = {"expr": {"Literal": {"Value": "true"}}}
                
                align = dashboard_title_formatting.get("alignment", "center")
                props["alignment"] = {"expr": {"Literal": {"Value": f"'{align}'"}}}
                
                color = dashboard_title_formatting.get("color")
                if color:
                    props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{color.upper()}'"}}}}}
                
                bg_color = dashboard_title_formatting.get("bg_color")
                if bg_color:
                    props["background"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{bg_color.upper()}'"}}}}}

        # 2. Apply Power BI specific config formatting afterwards (as override/supplement)
        if pbi_config and isinstance(pbi_config, dict):
            self._apply_pbi_config_formatting(visual_container, pbi_config, suppress_title=suppress_title, visual_properties=visual_properties, dashboard_title_formatting=dashboard_title_formatting, parent_type=parent_type)

    def _apply_pbi_config_formatting(self, visual_container: Dict, pbi_config: Dict, suppress_title: bool = False, visual_properties: Dict = None, dashboard_title_formatting: Dict = None, parent_type: str = "sheet") -> None:
        """Applies formatting strictly from power_bi_visual_type."""
        
        objects = visual_container.setdefault("visual", {}).setdefault("objects", {})
        visual_type = visual_container.get("visual", {}).get("visualType", "")

        # 1. Title Formatting (Visual Container)
        title_cfg = pbi_config.get("title", {})
        if title_cfg:
            vco = visual_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
            title_list = vco.setdefault("title", [{}])
            if not title_list: title_list.append({})
            props = title_list[0].setdefault("properties", {})

            # Visibility
            show_title = str(title_cfg.get("visible", "true")).lower() == "true" and not suppress_title
            props["show"] = {"expr": {"Literal": {"Value": "true" if show_title else "false"}}}

            # Text & Heading
            title_text = title_cfg.get("text", "")
            if title_text:
                props["text"] = {"expr": {"Literal": {"Value": f"'{title_text}'"}}}
            
            props["heading"] = {"expr": {"Literal": {"Value": "'Heading2'"}}}

            # Font Family
            font = title_cfg.get("font")
            if font:
                props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{font}'"}}}

        # 1b. Legend Position Mapping
        legend_pos = pbi_config.get("legend_position")
        if legend_pos:
            l_map = {
                "top right stacked": "Right",
                "top right": "TopRight",
                "top center": "Top",
                "top left": "TopLeft",
                "bottom right": "BottomRight",
                "bottom center": "Bottom",
                "bottom left": "BottomLeft",
                "right center": "RightCenter",
                "left center": "LeftCenter",
                "right": "Right",
                "left": "Left"
            }
            pbi_pos = l_map.get(str(legend_pos).lower().strip(), "Right")
            
            legend_list = objects.setdefault("legend", [{}])
            if not legend_list: legend_list.append({})
            l_props = legend_list[0].setdefault("properties", {})
            
            # Set show to true if position is defined
            l_props["show"] = {"expr": {"Literal": {"Value": "true"}}}
            l_props["position"] = {"expr": {"Literal": {"Value": f"'{pbi_pos}'"}}}

        # 1c. Title Font Size
        if title_cfg:
            vco = visual_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
            title_list = vco.setdefault("title", [{}])
            props = title_list[0].setdefault("properties", {})
            # Font Size
            sz = title_cfg.get("font_size")
            if sz:
                sz_val = re.search(r'\d+', str(sz)).group() if re.search(r'\d+', str(sz)) else "12"
                props["fontSize"] = {"expr": {"Literal": {"Value": f"{sz_val}D"}}}

            # Bold
            if title_cfg.get("bold") is True:
                props["bold"] = {"expr": {"Literal": {"Value": "true"}}}

            # Alignment
            align = title_cfg.get("alignment", "center")
            props["alignment"] = {"expr": {"Literal": {"Value": f"'{align}'"}}}

            # Colors
            if title_cfg.get("font_color"):
                props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{title_cfg['font_color'].upper()}'"}}}}}
            if title_cfg.get("bg_color"):
                props["background"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{title_cfg['bg_color'].upper()}'"}}}}}

            # 1b. Hide internal visual title to avoid duplication
            if visual_type != "card" and "title" in objects:
                del objects["title"]

        # ── Apply Global Dashboard Title Formatting (Overrides) ──
        if dashboard_title_formatting:
            vco = visual_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
            title_list = vco.setdefault("title", [{}])
            if not title_list: title_list.append({})
            props = title_list[0].setdefault("properties", {})
            
            # Force show if formatting is provided
            props["show"] = {"expr": {"Literal": {"Value": "true"}}}
            
            # Apply Style Properties
            if dashboard_title_formatting.get("font"):
                props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{dashboard_title_formatting['font']}'"}}}
            
            sz = dashboard_title_formatting.get("size")
            if sz:
                sz_val = re.search(r'\d+', str(sz)).group() if re.search(r'\d+', str(sz)) else "12"
                props["fontSize"] = {"expr": {"Literal": {"Value": f"{sz_val}D"}}}
            
            if dashboard_title_formatting.get("bold") is True:
                props["bold"] = {"expr": {"Literal": {"Value": "true"}}}
            
            align = dashboard_title_formatting.get("alignment", "center")
            props["alignment"] = {"expr": {"Literal": {"Value": f"'{align}'"}}}
            
            color = dashboard_title_formatting.get("color")
            if color:
                props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{color.upper()}'"}}}}}
            
            bg_color = dashboard_title_formatting.get("bg_color")
            if bg_color:
                props["background"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{bg_color.upper()}'"}}}}}

        # 2. Legend Formatting
        show_leg_cfg = pbi_config.get("legends")
        legend_cfg = pbi_config.get("legend") or pbi_config.get("legends_text_formatting")
        
        # Handle explicit legend visibility
        if show_leg_cfg == []:
            leg_list = objects.setdefault("legend", [{}])
            if not leg_list: leg_list.append({})
            leg_list[0].setdefault("properties", {})["show"] = {"expr": {"Literal": {"Value": "false"}}}
        elif legend_cfg:
            leg_list = objects.setdefault("legend", [{}])
            if not leg_list: leg_list.append({})
            l_props = leg_list[0].setdefault("properties", {})
            
            # Show legend (default to true if config exists)
            l_props["show"] = {"expr": {"Literal": {"Value": "true"}}}
            
            # Handle list versus dict for legend config
            l_item = legend_cfg[0] if isinstance(legend_cfg, list) else legend_cfg
            
            if l_item.get("font"):
                l_props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{l_item['font']}'"}}}
            
            sz = l_item.get("font_size") or l_item.get("size")
            if sz:
                sz_val = re.search(r'\d+', str(sz)).group() if re.search(r'\d+', str(sz)) else "12"
                l_props["fontSize"] = {"expr": {"Literal": {"Value": str(sz_val)}}}
            
            if l_item.get("font_color") or l_item.get("color"):
                color = l_item.get("font_color") or l_item.get("color")
                l_props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{color.upper()}'"}}}}}
            
            if l_item.get("bold") is True:
                l_props["fontWeight"] = {"expr": {"Literal": {"Value": "'bold'"}}}

        # 3. Data Labels (labels / detailLabels)
        label_cfg = pbi_config.get("detail_labels") or pbi_config.get("data_labels")
        if label_cfg:
            label_key = "labels"
            label_list = objects.setdefault(label_key, [{}])
            if not label_list: label_list.append({})
            props = label_list[0].setdefault("properties", {})

            # Handle list versus dict for label config
            l_item = label_cfg[0] if isinstance(label_cfg, list) else label_cfg

            if not isinstance(l_item, dict):
                l_item = {}

            # Show - Forced to true for all visuals as per user requirement
            props["show"] = {"expr": {"Literal": {"Value": "true"}}}

            # Font properties
            if l_item.get("font"):
                props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{l_item['font']}'"}}}
            
            sz = l_item.get("font_size") or l_item.get("size")
            if sz:
                sz_val = re.search(r'\d+', str(sz)).group() if re.search(r'\d+', str(sz)) else "10"
                props["fontSize"] = {"expr": {"Literal": {"Value": str(sz_val)}}}
            
            if l_item.get("font_color") or l_item.get("color"):
                color = l_item.get("font_color") or l_item.get("color")
                props["color"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{color.upper()}'"}}}}}

        # 4. Series Colors (Slices / Data Points / Lines)
        series_cfg = pbi_config.get("series_colors")
        if not series_cfg:
            series_cfg = pbi_config.get("lines_color")
        if not series_cfg:
            slices_cfg = pbi_config.get("slices", {})
            series_cfg = slices_cfg.get("series") if isinstance(slices_cfg, dict) else None
        if not series_cfg:
            series_cfg = pbi_config.get("series")
        if not series_cfg:
            series_cfg = pbi_config.get("bars")
        
        if series_cfg and isinstance(series_cfg, list):
            data_points = []
            query_state = visual_container.get("visual", {}).get("query", {}).get("queryState", {})
            
            # Collect all projections once for matching measures
            all_projs = []
            for b_name, bucket in query_state.items():
                if isinstance(bucket, dict) and "projections" in bucket:
                    all_projs.extend(bucket["projections"])

            # Find primary entity from projections - prioritize Series for legend colors
            primary_entity = "f"
            projs_for_entity = query_state.get("Series", {}).get("projections", []) or \
                               query_state.get("Category", {}).get("projections", []) or \
                               query_state.get("Y", {}).get("projections", []) or \
                               query_state.get("Y2", {}).get("projections", []) or \
                               query_state.get("Tooltips", {}).get("projections", [])
            
            if projs_for_entity:
                field_obj = projs_for_entity[0].get("field", {})
                inner = field_obj.get("Column") or field_obj.get("Measure") or field_obj.get("Aggregation", {}).get("Expression", {}).get("Column")
                if inner and "SourceRef" in inner.get("Expression", {}):
                    primary_entity = inner["Expression"]["SourceRef"].get("Entity", "f")

            for entry in series_cfg:
                if not isinstance(entry, dict): continue
                
                # Check for Gradient Formatting
                if entry.get("format_style") == "gradient":
                    based_on = entry.get("basedOn")
                    min_c = entry.get("minColor")
                    max_c = entry.get("maxColor")
                    
                    if based_on and min_c and max_c:
                        # Find the field expression from existing projections
                        proj = self._find_projection_for_field(all_projs, based_on)
                        if proj and "field" in proj:
                            field_expr = proj["field"]
                            
                            # Build Gradient Object (New structure for conditional formatting)
                            # PBI FillRule Input expects field expression (Property nested inside Measure/Column)
                            gradient_fill = {
                                "solid": {
                                    "color": {
                                        "expr": {
                                            "FillRule": {
                                                "Input": field_expr,
                                                "FillRule": {
                                                    "linearGradient2": {
                                                        "min": {
                                                            "color": { "Literal": { "Value": f"'{min_c.upper()}'" } }
                                                        },
                                                        "max": {
                                                            "color": { "Literal": { "Value": f"'{max_c.upper()}'" } }
                                                        },
                                                        "nullColoringStrategy": {
                                                            "strategy": { "Literal": { "Value": "'asZero'" } }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            
                            # Apply to dataPoint with selector and dummy first entry
                            objects["dataPoint"] = [
                                {"properties": {}},
                                {
                                    "properties": {
                                        "fill": gradient_fill
                                    },
                                    "selector": {
                                        "data": [
                                            {
                                                "dataViewWildcard": {
                                                    "matchingOption": 1
                                                }
                                            }
                                        ]
                                    }
                                }
                            ]
                            # Break after applying gradient to whole visual
                            break

                applied_to = entry.get("applied_to") or entry.get("series")
                color = entry.get("color")
                if applied_to and color:
                    # Try to resolve to a queryRef (Measure Match) first
                    q_ref = self._find_query_ref_for_field(all_projs, applied_to)
                    if q_ref:
                        selector = self._build_pbi_selector(applied_to, query_ref=q_ref)
                    else:
                        selector = self._build_pbi_selector(applied_to, entity=primary_entity)
                    
                    if selector:
                        data_points.append({
                            "selector": selector,
                            "properties": {
                                "fill": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{color.upper()}'"}}}}}
                            }
                        })
            if data_points:
                objects["dataPoint"] = data_points
                # Area Chart specific: set area styling
                if visual_type == "areaChart":
                    ls_list = objects.setdefault("lineStyles", [{}])
                    if not ls_list: ls_list.append({})
                    ls_props = ls_list[0].setdefault("properties", {})
                    ls_props["areaMatchStrokeColor"] = {"expr": {"Literal": {"Value": "false"}}}
                    ls_props["showMarker"] = {"expr": {"Literal": {"Value": "true"}}}
                    # Set areaColor to match the first series color if available
                    first_c = None
                    for dp in data_points:
                        if dp.get("properties", {}).get("fill", {}).get("solid", {}).get("color", {}).get("expr", {}).get("Literal", {}).get("Value"):
                            first_c = dp["properties"]["fill"]["solid"]["color"]["expr"]["Literal"]["Value"]
                            break
                    
                    if first_c:
                        ls_props["areaColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": first_c}}}}}
                    
                    # For Area Charts, markers should follow the worksheet theme color (#F28E2B)
                    # while the area uses the color from mapping (lines_color/series_colors).
                    ws_color = "#F28E2B"
                    formatting = visual_properties.get("formatting", [])
                    if isinstance(formatting, list):
                        for f_item in formatting:
                            if "Worksheet" in str(f_item.get("applied_to", "")):
                                ws_color = f_item.get("font_color") or "#F28E2B"
                                break
                    
                    for dp in data_points:
                        dp["properties"]["fill"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{ws_color.upper()}'"}}}}}
            
        # 4b. Fallback: Apply worksheet font_color to dataPoint fill ONLY if no points defined
        if ("dataPoint" not in objects or not objects["dataPoint"]) and visual_properties:
            ws_color = None
            for fmt in visual_properties.get("formatting", []):
                if fmt.get("applied_to") == "Worksheet" and fmt.get("font_color"):
                    ws_color = fmt.get("font_color")
                    break
            
            if ws_color:
                objects["dataPoint"] = [{
                    "properties": {
                        "fill": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{ws_color.upper()}'"}}}}}
                    }
                }]


        # 4c. Line Styling (interpolation, lineStyle)
        line_styling = pbi_config.get("line_styling")
        if line_styling and visual_type in ["lineChart", "areaChart"]:
            ls_list = objects.setdefault("lineStyles", [{}])
            if not ls_list: ls_list.append({})
            ls_props = ls_list[0].setdefault("properties", {})
            
            # line_style mapping (dotted, dashed, solid)
            l_style = line_styling.get("line_style")
            if l_style:
                ls_props["lineStyle"] = {"expr": {"Literal": {"Value": f"'{l_style}'"}}}
            
            # interpolation_type mapping (linear, smooth, step)
            # Omitted strokeType to prevent Power BI Desktop visual validation error and "Fix this" dialog
            pass

        # 5. Visual Background
        bg_cfg = pbi_config.get("visual_bg") or pbi_config.get("bg_color")
        if isinstance(bg_cfg, str): bg_cfg = {"color": bg_cfg}
        
        if bg_cfg and bg_cfg.get("color"):
            vco = visual_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
            vco["background"] = [{
                "properties": {
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "color": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{bg_cfg['color'].upper()}'"}}}}}
                }
            }]

        # 6. Axis Titles (from power_bi_visual_type)
        axes_title_cfg = pbi_config.get("axes_title", [])
        if axes_title_cfg and isinstance(axes_title_cfg, list):
            for entry in axes_title_cfg:
                target = entry.get("target")
                text = entry.get("text")
                if not target or not text:
                    continue
                
                # Skip standard default axis titles
                if entry.get("custom") is False or str(entry.get("custom")).lower() == "false":
                    continue
                
                # Map target to PBI axis key based on visual type
                is_horizontal = (visual_type == "barChart" or visual_type == "clusteredBarChart")
                pbi_axis_key = None
                if target == "x_axis":
                    pbi_axis_key = "valueAxis" if is_horizontal else "categoryAxis"
                elif target == "y_axis" or target == "secondary_y_axis":
                    pbi_axis_key = "categoryAxis" if is_horizontal else "valueAxis"
                
                if pbi_axis_key:
                    axis_list = objects.setdefault(pbi_axis_key, [{}])
                    if not axis_list: axis_list.append({})
                    a_props = axis_list[0].setdefault("properties", {})
                    
                    # Ensure axis is shown
                    a_props["show"] = {"expr": {"Literal": {"Value": "true"}}}
                    
                    # Set the title
                    a_props["titleText"] = {"expr": {"Literal": {"Value": f"'{text}'"}}}

        # 6.2 Axis Value Colors (Mapped from x_axis/y_axis configs)
        for ax_key, pbi_key in [("x_axis", "categoryAxis"), ("y_axis", "valueAxis"), ("secondary_y_axis", "valueAxis")]:
            ax_cfg = pbi_config.get(ax_key, {})
            if isinstance(ax_cfg, dict) and "values" in ax_cfg:
                f_color = ax_cfg["values"].get("font_color")
                if f_color:
                    axis_list = objects.setdefault(pbi_key, [{}])
                    if not axis_list: axis_list.append({})
                    a_props = axis_list[0].setdefault("properties", {})
                    # Standard PBI uses labelColor and titleColor
                    a_props["labelColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{f_color.upper()}'"}}}}}
                    a_props["titleColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{f_color.upper()}'"}}}}}

        # 6b. Category Labels (mapped to categoryAxis show or labels show for Cards)
        cat_labels = pbi_config.get("category_labels") or pbi_config.get("category_label")
        if cat_labels:
            show_val = False
            if isinstance(cat_labels, dict):
                show_val = str(cat_labels.get("show", "false")).lower() in ["on", "true"]
            else:
                show_val = str(cat_labels).lower() in ["on", "true"]
            
            pbi_key = "labels" if visual_type == "card" else "categoryAxis"
            axis_list = objects.setdefault(pbi_key, [{}])
            if not axis_list: axis_list.append({})
            a_props = axis_list[0].setdefault("properties", {})
            a_props["show"] = {"expr": {"Literal": {"Value": "true" if show_val else "false"}}}

        # 6c. Total Labels (for stacked charts)
        totals_enabled = pbi_config.get("total_labels_enabled")
        if totals_enabled:
            totals_list = objects.setdefault("totals", [{}])
            if not totals_list: totals_list.append({})
            t_props = totals_list[0].setdefault("properties", {})
            show_val = str(totals_enabled).lower() in ["on", "true"]
            t_props["show"] = {"expr": {"Literal": {"Value": "true" if show_val else "false"}}}

        # 7. Matrix Specific Formatting (pivotTable)
        if visual_type == "pivotTable":
            self._apply_matrix_formatting(visual_container, pbi_config)

        # 8. Table Specific Formatting (tableEx)
        if visual_type == "tableEx":
            self._apply_table_formatting(visual_container, pbi_config)

    def _apply_table_formatting(self, visual_container: Dict, pbi_config: Dict) -> None:
        """Applies advanced table formatting including specific column styles."""
        objects = visual_container.setdefault("visual", {}).setdefault("objects", {})
        query_state = visual_container.get("visual", {}).get("query", {}).get("queryState", {})
        projections = query_state.get("Values", {}).get("projections", [])
        
        columns_cfg = pbi_config.get("columns", [])
        if not columns_cfg:
            return

        column_formatting = []
        header_bg = None
        header_font_color = None
        val_font_family = None

        for col in columns_cfg:
            field_name = col.get("field")
            specific = col.get("specific_column", {})
            values_cfg = col.get("values", {})
            
            if not field_name:
                continue

            # Try to find the matching projection to get queryRef
            query_ref = self._find_query_ref_for_field(projections, field_name)
            if not query_ref:
                continue

            # A. Column Formatting (fontColor, backColor)
            f_color = specific.get("font_color")
            b_color = specific.get("bg_color")
            if f_color or b_color:
                props = {}
                if f_color:
                    props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{f_color.upper()}'"}}}}}
                    header_font_color = f_color # Use for global header as fallback
                if b_color:
                    props["backColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{b_color.upper()}'"}}}}}
                    header_bg = b_color # Use for global header as fallback
                
                column_formatting.append({
                    "properties": props,
                    "selector": {"metadata": query_ref}
                })

            # B. Values Formatting (fontFamily)
            if values_cfg.get("font"):
                val_font_family = values_cfg.get("font")

        if column_formatting:
            objects["columnFormatting"] = column_formatting

        # C. Global Column Headers
        if header_bg:
            objects["columnHeaders"] = [{
                "properties": {
                    "backColor": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{header_bg.upper()}'"}}}}}
                }
            }]
            if header_font_color:
                objects["columnHeaders"][0]["properties"]["fontColor"] = {
                    "solid": {"color": {"expr": {"Literal": {"Value": f"'{header_font_color.upper()}'"}}}}
                }

        # D. Global Values (Font)
        if val_font_family:
            objects["values"] = [{
                "properties": {
                    "fontFamily": {"expr": {"Literal": {"Value": f"'{val_font_family}'"}}}
                }
            }]

        # E. Grid Settings
        objects["grid"] = [{
            "properties": {
                "gridVertical": {"expr": {"Literal": {"Value": "true"}}},
                "gridHorizontal": {"expr": {"Literal": {"Value": "true"}}}
            }
        }]

    def _apply_matrix_formatting(self, visual_container: Dict, pbi_config: Dict) -> None:
        """Applies advanced matrix (pivotTable) formatting."""
        objects = visual_container.setdefault("visual", {}).setdefault("objects", {})
        query_state = visual_container.get("visual", {}).get("query", {}).get("queryState", {})
        
        # 1. Column Headers
        ch_cfg = pbi_config.get("column_headers", {})
        if isinstance(ch_cfg, dict) and ch_cfg:
            ch_list = objects.setdefault("columnHeaders", [{}])
            if not ch_list: ch_list.append({})
            props = ch_list[0].setdefault("properties", {})
            if ch_cfg.get("font_color"):
                props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{ch_cfg['font_color'].upper()}'"}}}}}
            if ch_cfg.get("bg_color"):
                props["backColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{ch_cfg['bg_color'].upper()}'"}}}}}
            if ch_cfg.get("font_size"):
                sz = re.search(r'\d+', str(ch_cfg["font_size"])).group() if re.search(r'\d+', str(ch_cfg["font_size"])) else "10"
                props["fontSize"] = {"expr": {"Literal": {"Value": str(sz)}}}
            if ch_cfg.get("font"):
                props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{ch_cfg['font']}'"}}}
            if ch_cfg.get("alignment"):
                props["alignment"] = {"expr": {"Literal": {"Value": f"'{ch_cfg['alignment']}'"}}}

        # 2. Row Headers
        rh_cfg = pbi_config.get("row_headers", {})
        if isinstance(rh_cfg, dict) and rh_cfg:
            rh_list = objects.setdefault("rowHeaders", [{}])
            if not rh_list: rh_list.append({})
            props = rh_list[0].setdefault("properties", {})
            if rh_cfg.get("font_color"):
                props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{rh_cfg['font_color'].upper()}'"}}}}}
            if rh_cfg.get("bg_color"):
                props["backColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{rh_cfg['bg_color'].upper()}'"}}}}}
            if rh_cfg.get("font_size"):
                sz = re.search(r'\d+', str(rh_cfg["font_size"])).group() if re.search(r'\d+', str(rh_cfg["font_size"])) else "10"
                props["fontSize"] = {"expr": {"Literal": {"Value": str(sz)}}}
            if rh_cfg.get("font"):
                props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{rh_cfg['font']}'"}}}
            if rh_cfg.get("alignment"):
                props["alignment"] = {"expr": {"Literal": {"Value": f"'{rh_cfg['alignment']}'"}}}

        # 3. Values (Main Data Area)
        val_cfg = pbi_config.get("values_formatting") or pbi_config.get("values", {})
        if isinstance(val_cfg, dict) and val_cfg:
            val_list = objects.setdefault("values", [{}])
            if not val_list: val_list.append({})
            props = val_list[0].setdefault("properties", {})
            if val_cfg.get("text_color"):
                props["fontColorPrimary"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{val_cfg['text_color'].upper()}'"}}}}}
            if val_cfg.get("alternate_text_color"):
                props["fontColorSecondary"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{val_cfg['alternate_text_color'].upper()}'"}}}}}
            if val_cfg.get("text_bg_color"):
                props["backColorPrimary"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{val_cfg['text_bg_color'].upper()}'"}}}}}
            if val_cfg.get("alternate_bg_color"):
                props["backColorSecondary"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{val_cfg['alternate_bg_color'].upper()}'"}}}}}
            if val_cfg.get("font_size"):
                sz = re.search(r'\d+', str(val_cfg["font_size"])).group() if re.search(r'\d+', str(val_cfg["font_size"])) else "10"
                props["fontSize"] = {"expr": {"Literal": {"Value": str(sz)}}}
            if val_cfg.get("font"):
                props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{val_cfg['font']}'"}}}

        # 4. Column Subtotals/Totals
        ct_cfg = pbi_config.get("column_total")
        if isinstance(ct_cfg, dict) and ct_cfg:
            ct_list = objects.setdefault("columnTotal", [{}])
            if not ct_list: ct_list.append({})
            props = ct_list[0].setdefault("properties", {})
            if ct_cfg.get("font_color"):
                props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{ct_cfg['font_color'].upper()}'"}}}}}
            if ct_cfg.get("bg_color"):
                props["backColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{ct_cfg['bg_color'].upper()}'"}}}}}

        # 5. Row Subtotals/Totals
        rt_cfg = pbi_config.get("row_total")
        if isinstance(rt_cfg, dict) and rt_cfg:
            rt_list = objects.setdefault("rowTotal", [{}])
            if not rt_list: rt_list.append({})
            props = rt_list[0].setdefault("properties", {})
            if rt_cfg.get("font_color"):
                props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{rt_cfg['font_color'].upper()}'"}}}}}

        # 6. Specific Column Formatting (Selectors)
        # In Matrix, specific column formatting often applies to the measure values
        col_fmt_list = pbi_config.get("columnFormatting", [])
        if col_fmt_list:
            matrix_col_formatting = []
            projections = query_state.get("Values", {}).get("projections", [])
            for item in col_fmt_list:
                field_name = item.get("field")
                fmt_props = item.get("properties", {})
                if not field_name: continue
                
                query_ref = self._find_query_ref_for_field(projections, field_name)
                if query_ref:
                    pbi_props = {}
                    if fmt_props.get("font_color"):
                        pbi_props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{fmt_props['font_color'].upper()}'"}}}}}
                    if fmt_props.get("bg_color"):
                        pbi_props["backColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{fmt_props['bg_color'].upper()}'"}}}}}
                    
                    if pbi_props:
                        matrix_col_formatting.append({
                            "properties": pbi_props,
                            "selector": {"metadata": query_ref}
                        })
            if matrix_col_formatting:
                objects["columnFormatting"] = matrix_col_formatting

    def _find_query_ref_for_field(self, projections: List[Dict], field_name: str) -> Optional[str]:
        """
        Finds the queryRef for a given field name by matching nativeQueryRef or field property.
        Uses a robust normalized cleaning strategy to handle table prefixes and aggregation syntax.
        """
        if not field_name:
            return None

        # 1. Standard Case-Insensitive Normalization
        field_norm = str(field_name).strip().lower()
        
        # 2. Aggressive Cleaning (for measures/table prefixes)
        field_clean = self._clean_field_for_match(field_name)
        
        for p in projections:
            # A. Native Query Ref Match (e.g. "Distinct Count of Account Id")
            nqr = p.get("nativeQueryRef", "").strip().lower()
            if nqr == field_norm or self._clean_field_for_match(nqr) == field_clean:
                return p.get("queryRef")
            
            # B. Query Ref Direct Match (e.g. "district.District Name")
            qr = p.get("queryRef", "").strip().lower()
            if qr == field_norm or self._clean_field_for_match(qr) == field_clean:
                return p.get("queryRef")
            
            # C. Field Property Match (Column/Measure/Aggregation)
            field_obj = p.get("field", {})
            for key in ["Column", "Measure", "Aggregation"]:
                if key in field_obj:
                    inner = field_obj[key]
                    if key == "Aggregation" and "Expression" in inner:
                        inner = inner["Expression"].get("Column", {}) or inner["Expression"].get("Measure", {})
                    
                    prop = inner.get("Property", "").strip().lower()
                    if prop == field_norm or self._clean_field_for_match(prop) == field_clean:
                        return p.get("queryRef")
                    break

        return None

    def _find_projection_for_field(self, projections: List[Dict], field_name: str) -> Optional[Dict]:
        """
        Finds the full projection dictionary for a given field name.
        Uses the same cleaning logic as _find_query_ref_for_field.
        """
        if not field_name:
            return None

        field_norm = str(field_name).strip().lower()
        field_clean = self._clean_field_for_match(field_name)
        
        for p in projections:
            nqr = p.get("nativeQueryRef", "").strip().lower()
            if nqr == field_norm or self._clean_field_for_match(nqr) == field_clean:
                return p
            
            qr = p.get("queryRef", "").strip().lower()
            if qr == field_norm or self._clean_field_for_match(qr) == field_clean:
                return p
            
            field_obj = p.get("field", {})
            for key in ["Column", "Measure", "Aggregation"]:
                if key in field_obj:
                    inner = field_obj[key]
                    if key == "Aggregation" and "Expression" in inner:
                        inner = inner["Expression"].get("Column", {}) or inner["Expression"].get("Measure", {})
                    
                    prop = inner.get("Property", "").strip().lower()
                    if prop == field_norm or self._clean_field_for_match(prop) == field_clean:
                        return p
                    break

        return None

    def _clean_field_for_match(self, s: str) -> str:
        """
        Cleans a field name string for robust matching:
        - Lowercases
        - Removes common aggregation/Hierarchy wrappers (e.g. 'AGG(', 'SUM(', 'YEAR(')
        - Removes table prefixes (e.g. 'table.' or 'table[')
        - Removes spaces, dots, and brackets
        """
        if not s:
            return ""
        
        # Lowercase
        s = s.lower()
        
        # Strip common tableau/dax function wrappers (e.g. agg(Total Loan) -> Total Loan)
        # matches: word(field) 
        s = re.sub(r'^[a-z_0-9]+\((.*)\)$', r'\1', s)
        # cases like SUM(table[field]) or [table].[field]
        
        # Remove table dots (e.g. 'disp.' in 'DistinctCount(disp.Account Id)')
        s = re.sub(r'([a-z0-9_]+)\.', r'', s)
        
        # Remove brackets (PBI often uses [Field])
        s = s.replace("[", "").replace("]", "")
        
        # Remove spaces
        s = s.replace(" ", "")
        
        # Final pass: if it still has a trailing parenthesis, strip it (for nested or missed patterns)
        s = s.rstrip(")")
        
        return s

    def _build_pbi_selector(self, applied_to: str, entity: str = None, query_ref: str = None) -> Dict:
        """Parses 'Field: Value' strings into PBI dataPoint selectors or creates metadata selectors."""
        if query_ref:
            return {"metadata": query_ref}

        if ":" not in applied_to:
            return {}
        
        parts = applied_to.split(":", 1)
        field_prop = parts[0].strip()
        val_str = parts[1].strip()
        
        # Determine value type for literal
        if val_str.lower() == "true":
            literal = {"Value": "true"}
        elif val_str.lower() == "false":
            literal = {"Value": "false"}
        elif val_str.isdigit():
            literal = {"Value": f"{val_str}L"}
        else:
            literal = {"Value": f"'{val_str}'"}
            
        source_ref = {"Entity": entity} if entity else {"Source": "f"}
            
        return {
            "data": [
                {
                    "scopeId": {
                        "Comparison": {
                            "ComparisonKind": 0,
                            "Left": {
                                "Column": {
                                    "Expression": {"SourceRef": source_ref},
                                    "Property": field_prop
                                }
                            },
                            "Right": {"Literal": literal}
                        }
                    }
                }
            ]
        }


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

