import json
import uuid
import re
from typing import Dict, Optional, List, Tuple
from app.tableau.core.logging_utils import log_info, log_error, log_warning
from app.tableau.core.config import Config
from app.tableau.core_logic.template_manager.template_manager import TemplateManager
from app.tableau.services.action_logger import ActionLogger


class SlicerBuilderMixin:
    def _build_slicer_visual(self, slicer_def, field_to_table: Dict, field_to_datatype: Dict,
                              measure_names: List, default_table: str, display_to_bi_name: Dict,
                              parameter_names: set = None, parameters: List[Dict] = None,
                              actions: List[Dict] = None, suppress_title: bool = False,
                              dashboard_title_formatting: Dict = None) -> Optional[Dict]:
        """Builds a PBI slicer visual from a Tableau slicer definition dict or string."""
        if parameter_names is None:
            parameter_names = set()
        if parameters is None:
            parameters = []
        if actions is None:
            actions = []
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
            field_name = self.clean_field(slicer_def)
            source = slicer_def.get("source", "").lower()
            slicer_type = slicer_def.get("tableau_type", "parameter" if (field_name in parameter_names or source == "parameter") else "quick")
        else:
            return None

        if not field_name or self._is_none_field(field_name):
            return None

        raw_field_name = slicer_def if isinstance(slicer_def, str) else slicer_def.get("tableau_original_name", slicer_def.get("field", ""))
        if not raw_field_name and isinstance(slicer_def, dict):
            name_val = slicer_def.get("name")
            if isinstance(name_val, dict):
                raw_field_name = name_val.get("name") or name_val.get("field") or ""
            elif isinstance(name_val, str):
                raw_field_name = name_val

        entity = self._extract_entity(raw_field_name, field_to_table, default_table)
        if entity == default_table and field_name:
            better_entity = self._extract_entity(field_name, field_to_table, default_table)
            if better_entity != default_table:
                entity = better_entity
            
        resolved = self._resolve_field(field_name, display_to_bi_name, entity)
        if field_to_table and resolved in field_to_table:
            entity = field_to_table[resolved]

        is_action_param = False
        param_current_value = None
        has_directlake_param_mapping = False
        if slicer_type == "parameter" or field_name in parameter_names:
            param_entity = field_name
            param_property = "Value"
            
            param_datatype = "string"
            for p in parameters:
                if p.get("name") == field_name:
                    p_bi = p.get("powerbi", {})
                    param_current_value = p_bi.get("current_value") or p.get("current_value")
                    param_datatype = p_bi.get("data_type") or p.get("tableau", {}).get("data_type") or "string"
                    
                    if param_current_value is None:
                        # Fallback: Parse first value from DAX DATATABLE
                        raw_dax = p_bi.get("dax") or p.get("dax") or ""
                        # Match first value in DATATABLE(... {{val}, ...})
                        match_val = re.search(r'\{\{([^,}]+)\}', raw_dax)
                        if match_val:
                            param_current_value = match_val.group(1).strip().replace('"', '').replace("'", "")
                    
                    # Check for Direct Lake parameter metadata (set by coordinator)
                    if "_directlake_column" in p and "_directlake_table" in p:
                        param_entity = p.get("_directlake_table")
                        param_property = p.get("_directlake_column")
                        has_directlake_param_mapping = True
                    break
                    
            for act in actions:
                pe = act.get("powerbi_equivalent", {})
                if pe.get("target_parameter") == field_name and "Parameter Action" in pe.get("implementation_type", ""):
                    # In Direct Lake, keep the synthesized parameter table name from metadata
                    # (e.g., pCustomer) instead of forcing the legacy _Table suffix.
                    if not has_directlake_param_mapping:
                        param_entity = f"{field_name}_Table"
                        param_property = pe.get("source_field", "Value")
                    is_action_param = True
                    break

            if not is_action_param and not has_directlake_param_mapping:
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
        
        # Add sync group and drillFilterOtherVisuals
        slicer_container["visual"]["syncGroup"] = {
            "groupName": field_name,
            "fieldChanges": True,
            "filterChanges": True
        }
        slicer_container["visual"]["drillFilterOtherVisuals"] = True
        
        if slicer_type == "parameter" or field_name in parameter_names:
            if "objects" not in slicer_container["visual"]:
                slicer_container["visual"]["objects"] = {}
                
            # User's manual fix shows NO visual-level filter in general properties, only in filterConfig
            if "data" not in slicer_container["visual"]["objects"]:
                slicer_container["visual"]["objects"]["data"] = [{"properties": {}}]
            slicer_container["visual"]["objects"]["data"][0]["properties"]["mode"] = {
                "expr": {"Literal": {"Value": "'Basic'"}}
            }
            
            if param_current_value is not None:
                # Format literal based on datatype
                formatted_val = param_current_value
                if param_datatype == "integer" and not str(formatted_val).endswith("L"):
                    formatted_val = f"{formatted_val}L"
                elif param_datatype == "real" and not str(formatted_val).endswith("D"):
                    formatted_val = f"{formatted_val}D"
                elif param_datatype == "string" and not str(formatted_val).startswith("'"):
                    formatted_val = f"'{formatted_val}'"
                
                # Construct visual-level filter for default selection
                filter_logic = self._create_parameter_filter_logic(param_entity, param_property, default_value=formatted_val)
                if filter_logic:
                    gen = slicer_container["visual"]["objects"].setdefault("general", [{"properties": {}}])
                    if not gen: gen.append({"properties": {}})
                    gen[0]["properties"]["filter"] = {"filter": filter_logic}
        else:
            # Handle Set-based slicers (Stock_Item_Name_Set_2_In_Out -> 'Out')
            if field_name.endswith("_In_Out"):
                if "objects" not in slicer_container["visual"]:
                    slicer_container["visual"]["objects"] = {}
                
                if "data" not in slicer_container["visual"]["objects"]:
                    slicer_container["visual"]["objects"]["data"] = [{"properties": {}}]
                slicer_container["visual"]["objects"]["data"][0]["properties"]["mode"] = {
                    "expr": {"Literal": {"Value": "'Basic'"}}
                }

        if slicer_type == "parameter" or field_name in parameter_names:
            filter_type = "Categorical" if is_action_param else "Advanced"
            slicer_container["filterConfig"] = {"filters": [{
                "name": str(uuid.uuid4()).replace("-", "")[:20],
                "field": proj_field,
                "type": filter_type
            }]}
        else:
            slicer_container["filterConfig"] = {"filters": [{
                "name": str(uuid.uuid4()).replace("-", "")[:20],
                "field": proj_field,
                "type": "Categorical"
            }]}

        if suppress_title:
            vco = slicer_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
            title_list = vco.setdefault("title", [{}])
            if not title_list: title_list.append({})
            t_props = title_list[0].setdefault("properties", {})
            t_props["show"] = {"expr": {"Literal": {"Value": "false"}}}
        else:
            # Explicitly show title if NOT suppressed
            vco = slicer_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
            title_list = vco.setdefault("title", [{}])
            if not title_list: title_list.append({})
            t_props = title_list[0].setdefault("properties", {})
            t_props["show"] = {"expr": {"Literal": {"Value": "true"}}}
            t_props["heading"] = {"expr": {"Literal": {"Value": "'Heading2'"}}}
            t_props["alignment"] = {"expr": {"Literal": {"Value": "'center'"}}}
            # Title text for slicers usually defaults to the field name in PBI, 
            # but we can set it explicitly to resolved field name
            t_props["text"] = {"expr": {"Literal": {"Value": f"'{field_name}'"}}}

        return slicer_container

