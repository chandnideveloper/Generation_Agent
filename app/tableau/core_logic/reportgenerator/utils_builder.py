from typing import Dict, Optional, List, Any
import uuid
import re
from app.tableau.core.logging_utils import log_warning, log_info

class UtilsBuilderMixin:
    def _should_add_pcustomer_slicer(self, page_entry: Dict) -> bool:
        """Determines if the pCustomer slicer should be added to the page."""
        display_name = page_entry.get("display_name", "").lower()
        if "kpi order" in display_name or "customer lifetime value" in display_name:
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

    def _build_action_button_visual(self, action: Dict, sheet_name_to_id: Dict, label: Optional[str] = None) -> Optional[Dict]:
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

        # By default, buttons are blank/transparent overlays.
        # If a label is provided, we make it a visible "Back" button with icon and text.
        objs = btn_vis["visual"].setdefault("objects", {})
        icon = objs.setdefault("icon", [{}])
        if not icon: icon.append({})
        icon_props = icon[0].setdefault("properties", {})
        
        if label:
            icon_props["shapeType"] = {"expr": {"Literal": {"Value": "'backArrow'"}}}
        else:
            icon_props["shapeType"] = {"expr": {"Literal": {"Value": "'blank'"}}}

        # Configure text
        text = objs.setdefault("text", [{}])
        if not text: text.append({})
        text_props = text[0].setdefault("properties", {})
        
        if label:
            text_props["show"] = {"expr": {"Literal": {"Value": "true"}}}
            text_props["text"] = {"expr": {"Literal": {"Value": f"'{label}'"}}}
        else:
            text_props["show"] = {"expr": {"Literal": {"Value": "false"}}}

        return btn_vis

    def _map_format_to_pbi_string(self, f_obj: Any) -> Optional[str]:
        """Converts Tableau/Mapping format object to Power BI format string.
        (Copied from MetadataPreScannerMixin for local builder use)"""
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
            if "$" in symbol_raw or "US Dollar" in symbol_raw:
                return "\\$#,0.00;-\\$#,0.00;\\$#,0.00"
            if "€" in symbol_raw: 
                s_char = "€"
                return f'"{s_char}" #,0.00;-"{s_char}" #,0.00;"{s_char}" #,0.00'
            elif "₹" in symbol_raw or "Indian Rupee" in symbol_raw: 
                s_char = "₹" 
                if "16393" in str(f_obj.get("locale", "")):
                    s_char = "₹"
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

    def _get_local_field_to_format(self, entry: Dict, global_formats: Dict) -> Dict:
        """Extracts local formats from a page or visual entry and merges them with global formats."""
        if global_formats is None:
            global_formats = {}
        local_formats = global_formats.copy()

        # Helper to process a list of fields
        def _process_fields(fields, field_type=""):
            if not isinstance(fields, list): 
                return
            found_count = 0
            for f in fields:
                if isinstance(f, dict):
                    f_name = f.get("field") or f.get("name")
                    f_fmt = f.get("format")
                    
                    # Support measure_values expansion ALWAYS
                    m_vals = f.get("measure_values", [])
                    
                    pbi_fmt = None
                    if f_fmt:
                        # Visual-level Automatic/General with no explicit format_string
                        # must override global formats for this visual only.
                        fmt_type = str(f_fmt.get("format_type", "")).lower() if isinstance(f_fmt, dict) else ""
                        fmt_str = None
                        if isinstance(f_fmt, dict):
                            bi_f = f_fmt.get("bi_format", {})
                            if isinstance(bi_f, dict):
                                fmt_str = bi_f.get("format_string")

                        clean_name = f_name
                        if "(" in f_name and f_name.endswith(")"):
                            clean_name = f_name.split("(", 1)[1].rsplit(")", 1)[0]

                        if ("automatic" in fmt_type or "general" in fmt_type) and not fmt_str:
                            local_formats[f_name] = None
                            if clean_name != f_name:
                                local_formats[clean_name] = None
                            found_count += 1
                        else:
                            pbi_fmt = self._map_format_to_pbi_string(f_fmt)
                            if pbi_fmt and pbi_fmt != "G":
                                local_formats[f_name] = pbi_fmt
                                found_count += 1
                                if clean_name != f_name:
                                    local_formats[clean_name] = pbi_fmt
                                
                    if isinstance(m_vals, list) and m_vals:
                        for mv in m_vals:
                            if isinstance(mv, str):
                                clean_mv = mv
                                if "(" in mv and mv.endswith(")"):
                                    clean_mv = mv.split("(", 1)[1].rsplit(")", 1)[0]
                                local_formats[mv] = pbi_fmt
                                if clean_mv != mv:
                                    local_formats[clean_mv] = pbi_fmt
                                found_count += 1
                            elif isinstance(mv, dict):
                                mv_name = mv.get("field") or mv.get("name")
                                if mv_name:
                                    mv_fmt = mv.get("format")
                                    mv_pbi_fmt = self._map_format_to_pbi_string(mv_fmt) if mv_fmt else pbi_fmt
                                    
                                    mv_fmt_type = str(mv_fmt.get("format_type", "")).lower() if isinstance(mv_fmt, dict) else ""
                                    mv_fmt_str = None
                                    if isinstance(mv_fmt, dict):
                                        mv_bi_f = mv_fmt.get("bi_format", {})
                                        if isinstance(mv_bi_f, dict):
                                            mv_fmt_str = mv_bi_f.get("format_string")
                                            
                                    clean_mv = mv_name
                                    if "(" in mv_name and mv_name.endswith(")"):
                                        clean_mv = mv_name.split("(", 1)[1].rsplit(")", 1)[0]
                                        
                                    if ("automatic" in mv_fmt_type or "general" in mv_fmt_type) and not mv_fmt_str:
                                        local_formats[mv_name] = None
                                        if clean_mv != mv_name:
                                            local_formats[clean_mv] = None
                                    else:
                                        if mv_pbi_fmt and mv_pbi_fmt != "G":
                                            local_formats[mv_name] = mv_pbi_fmt
                                            if clean_mv != mv_name:
                                                local_formats[clean_mv] = mv_pbi_fmt
                                    found_count += 1
            return found_count

        # 1. Process standard fields in the entry
        for key in ["rows", "columns", "measures", "marks_text", "filters", "slicers"]:
            field_list = entry.get(key, [])
            _process_fields(field_list, field_type=key)

        # 2. Process tooltip_formatting fields
        tooltip_formatting = entry.get("tooltip_formatting", [])
        if isinstance(tooltip_formatting, dict):
            _process_fields(tooltip_formatting.get("fields_used", []), field_type="tooltip")
        elif isinstance(tooltip_formatting, list):
            for tf in tooltip_formatting:
                if isinstance(tf, dict):
                    _process_fields(tf.get("fields_used", []), field_type="tooltip")

        return local_formats
