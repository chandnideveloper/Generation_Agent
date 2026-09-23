import json
import uuid
import re
from typing import Dict, Optional, List, Tuple
from app.tableau.core.logging_utils import log_info, log_error, log_warning

class VisualDataBuilderMixin:
    def _apply_formats_to_query_state(self, query_state: Dict, field_to_format: Dict) -> None:
        """Final pass: attach format strings to projections in queryState.

        This is a defensive step to ensure formats are present even when field
        names differ across mapping variants (e.g., AGG(field), table.field).
        """
        if not isinstance(query_state, dict) or not isinstance(field_to_format, dict) or not field_to_format:
            return

        bucket_count = 0
        for bucket_name, bucket in query_state.items():
            if not isinstance(bucket, dict):
                continue
            bucket_count += 1
            projections = bucket.get("projections", [])
            if not isinstance(projections, list):
                continue

            for proj_idx, proj in enumerate(projections):
                if not isinstance(proj, dict) or proj.get("format"):
                    continue

                field_obj = proj.get("field", {})
                prop = ""

                if "Measure" in field_obj:
                    prop = field_obj["Measure"].get("Property", "")
                elif "Column" in field_obj:
                    prop = field_obj["Column"].get("Property", "")
                elif "Aggregation" in field_obj:
                    prop = (
                        field_obj["Aggregation"]
                        .get("Expression", {})
                        .get("Column", {})
                        .get("Property", "")
                    )

                candidates = []

                def _add(v):
                    if isinstance(v, str) and v and v not in candidates:
                        candidates.append(v)

                _add(prop)
                _add(proj.get("nativeQueryRef", ""))
                _add(proj.get("queryRef", ""))

                for c in list(candidates):
                    if "." in c:
                        _add(c.rsplit(".", 1)[-1])
                    clean_c = self.clean_field(c)
                    _add(clean_c)
                    _add(f"AGG({clean_c})")
                    _add(f"SUM({clean_c})")
                    _add(f"AVG({clean_c})")
                    _add(f"COUNT({clean_c})")
                    _add(f"COUNTD({clean_c})")
                
                for c in candidates:
                    fmt = self._resolve_projection_format(c, self.clean_field(c), c.rsplit(".", 1)[-1], field_to_format)
                    if fmt is not None:  # None means visual explicitly set to Automatic, skip format
                        proj["format"] = fmt
                        break

    def _find_field_sort_order(self, pbi_visual_config: Dict, property_name: str) -> Optional[str]:
        """Looks up if a sort order is explicitly specified for a property/field in power_bi_visual_type config."""
        if not isinstance(pbi_visual_config, dict):
            return None
        
        search_areas = [
            pbi_visual_config.get("columns", []),
            pbi_visual_config.get("rows", []),
            pbi_visual_config.get("fields", []),
            pbi_visual_config.get("legends", [])
        ]
        
        clean_prop = self.clean_field(property_name).lower().strip()
        if clean_prop.startswith('[') and clean_prop.endswith(']'):
            clean_prop = clean_prop[1:-1]
        clean_prop = clean_prop.replace('[', '').replace(']', '')
        
        for area in search_areas:
            items = area if isinstance(area, list) else [area]
            for item in items:
                if isinstance(item, dict):
                    field_val = item.get("field") or item.get("column") or item.get("name")
                    if field_val:
                        clean_field_val = self.clean_field(field_val).lower().strip()
                        if clean_field_val.startswith('[') and clean_field_val.endswith(']'):
                            clean_field_val = clean_field_val[1:-1]
                        clean_field_val = clean_field_val.replace('[', '').replace(']', '')
                        
                        if clean_prop == clean_field_val:
                            sort_order = item.get("sort_order") or item.get("sortOrder") or item.get("direction")
                            if not sort_order and isinstance(item.get("sort"), dict):
                                sort_order = item["sort"].get("sort_order") or item["sort"].get("sortOrder") or item["sort"].get("direction")
                            if sort_order:
                                val = str(sort_order).strip().lower()
                                if "desc" in val:
                                    return "Descending"
                                elif "asc" in val:
                                    return "Ascending"
                                return str(sort_order).capitalize()
        return None

    def _find_power_bi_sort_instruction(self, pbi_visual_config: Dict) -> Optional[str]:
        """Searches for power_bi_sort_instruction in visual config level or field level."""
        if not isinstance(pbi_visual_config, dict):
            return None
        
        # Check top level
        instr = pbi_visual_config.get("power_bi_sort_instruction")
        if instr:
            return instr
            
        # Check columns, rows, fields
        search_areas = [
            pbi_visual_config.get("columns", []),
            pbi_visual_config.get("rows", []),
            pbi_visual_config.get("fields", []),
            pbi_visual_config.get("legends", [])
        ]
        for area in search_areas:
            items = area if isinstance(area, list) else [area]
            for item in items:
                if isinstance(item, dict):
                    instr = item.get("power_bi_sort_instruction")
                    if instr:
                        return instr
        return None

    def _parse_sort_instruction(self, instruction: str) -> Optional[Tuple[str, str]]:
        """Parses the sort instruction (e.g. 'Sort by 'Revenue' Ascending') to extract field and direction."""
        if not instruction or not isinstance(instruction, str):
            return None
        match = re.search(r"sort\s+by\s+['\"]?([^'\"]+)['\"]?\s+(ascending|descending)", instruction, re.IGNORECASE)
        if match:
            field_name = match.group(1).strip()
            direction = match.group(2).strip().lower().capitalize()
            return field_name, direction
        return None

    def _create_parameter_filter_logic(self, entity: str, property_name: str, is_measure: bool = False, default_value: any = None) -> Dict:
        """Creates the advanced filter logic (Version 2) to set a default value for parameters."""
        field_type = "Measure" if is_measure else "Column"
        
        if default_value is not None:
            # Use "In" condition with Literal value for specific defaults
            val_str = str(default_value)
            
            # PBI Literal detection: 
            # 1. Already quoted ('Value')
            # 2. Pure numeric (123)
            # 3. Numeric with PBI suffix (123L or 123.45D)
            is_already_literal = (
                val_str.startswith("'") or 
                val_str.isnumeric() or 
                (val_str.endswith(("L", "D")) and val_str[:-1].replace(".", "").replace("-", "").isnumeric()) or
                (val_str.replace(".", "").replace("-", "").isnumeric())
            )

            if not is_already_literal:
                val_str = f"'{val_str}'"
                
            return {
                "Version": 2,
                "From": [{"Name": "f", "Entity": entity, "Type": 0}],
                "Where": [
                    {
                        "Condition": {
                            "In": {
                                "Expressions": [
                                    {
                                        field_type: {
                                            "Expression": {"SourceRef": {"Source": "f"}},
                                            "Property": property_name
                                        }
                                    }
                                ],
                                "Values": [[{"Literal": {"Value": val_str}}]]
                            }
                        }
                    }
                ]
            }
            
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
        html_vis["visual"]["query"] = {
            "queryState": {
                "content": {
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
            }
        }

        return html_vis

    def _build_data_visual(self, dv: Dict, field_to_table: Dict, field_to_datatype: Dict, 
                           measure_names: List, default_table: str, display_to_bi_name: Dict = None, 
                           parameter_names: set = None, actions: List[Dict] = None, 
                           parameters: List[Dict] = None, suppress_title: bool = False, is_direct_lake: bool = False,
                           dashboard_title_formatting: Dict = None, parent_type: str = "sheet", field_to_format: Dict = None) -> Optional[Dict]:
        """Builds a single data visual container from dashboard visual data."""
        if display_to_bi_name is None:
            display_to_bi_name = {}
        if parameter_names is None:
            parameter_names = set()
        if actions is None:
            actions = []
        if parameters is None:
            parameters = []
            
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
            is_html_content = (
                "htmlcontent" in str(custom_visual_name).lower() or 
                "htmlcontent" in str(visual_type).lower()
            )
            if not is_html_content:
                log_info(f"Skipping custom visual '{custom_visual_name}' of type 'Custom Visual' as per user request")
                return None

        # Calculate local formats for this specific visual
        visual_name = dv.get("display_name", dv.get("name", "unknown"))
        
        dv_rows = dv.get("rows", [])
        dv_cols = dv.get("columns", [])
        
        local_field_to_format = self._get_local_field_to_format(dv, field_to_format)
        
        visual_type = dv.get("_resolved_type", "barChart")
        # Standardize visual_type for line charts
        if "line" in str(visual_type).lower() and "combo" not in str(visual_type).lower():
             visual_type = "lineChart"
        display_name = dv.get("display_name", "Visual")

        # Build field lists — correct: measures always go to Y, dimensions to Category
        pbi_visual_config = dv.get("power_bi_visual_type", {})
        # Extract axis_mapping from pbi_visual_config or dv
        axis_mapping = dv.get("axis_mapping", {})
        
        # Consolidation: helper to extract field names from various PBI config formats (string, list, or dict with 'field')
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
             rows = _get_fields(pbi_visual_config.get("rows")) or dv.get("rows", [])
             columns = _get_fields(pbi_visual_config.get("columns")) or dv.get("columns", [])
             marks_color = _get_fields(pbi_visual_config.get("legends")) or dv.get("marks_color", [])
             pbi_fields = _get_fields(pbi_visual_config.get("fields"))
             pbi_legends = _get_fields(pbi_visual_config.get("legends"))
        else:
             rows = dv.get("rows", [])
             columns = dv.get("columns", [])
             marks_color = dv.get("marks_color", [])
             pbi_fields = []
             pbi_legends = []

        y2_fields = []
        size_fields = []
        if isinstance(axis_mapping, dict) and axis_mapping:
            y2_val = axis_mapping.get("secondary_y_axis")
            if y2_val:
                y2_fields = [y2_val] if isinstance(y2_val, str) else y2_val
            size_val = axis_mapping.get("size")
            if size_val:
                size_fields = [size_val] if isinstance(size_val, str) else size_val

        measures = dv.get("measures", [])
        filters = dv.get("filters", [])

        # Build field lists — correct: measures always go to Y, dimensions to Category
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
                if not cf:
                    continue
                
                if isinstance(cf, dict):
                    cf = cf.get("field") or cf.get("name") or str(cf)
                
                clean_f = self.clean_field(rf)
                _, agg = self.get_field_agg_info(rf)
                func = self.map_agg_to_pbi(agg)
                dedup_key = f"{clean_f}_{func}"
                
                # If it's aggregated in Tableau, it's definitely a measure for Power BI
                if was_agg or cf in measure_names:
                    if dedup_key not in seen_y:
                        y_out.append(rf) # Use the original field (with agg if any) for Y axis
                        seen_y.add(dedup_key)
                else:
                    if cf not in seen_cat:
                        cat_out.append(rf) # Use original rf to keep entity info for extraction
                        seen_cat.add(cf)
            return cat_out, y_out

        m_for_split = list(measures) + list(pbi_fields)
        
        # Selectively enable marks_text for Cards/KPIs/Matrix if they would otherwise be empty
        if visual_type in ["card", "multiRowCard", "pivotTable"] and not m_for_split:
            m_for_split = dv.get("marks_text", [])

        # For map visuals, include marks_detail and marks_size in field extraction
        if visual_type in ["map", "filledMap", "heatmap"]:
            m_for_split = m_for_split + list(dv.get("marks_detail", [])) + list(dv.get("marks_size", []))

        category_fields, y_fields = _split_fields(rows, columns, m_for_split)

        # Force axis buckets if axis_mapping is provided to ensure primary and secondary axes are strictly separated
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
                # Only override if it's a field name or list of strings
                if x_val:
                    f_list = _get_fields(x_val)
                    if f_list: category_fields = f_list
                
                y_val = axis_mapping.get("y_axis")
                if y_val:
                    f_list = _get_fields(y_val)
                    if f_list: y_fields = f_list
                
                # y2_fields already extracted above, but ensuring consistency here
                y2_val = axis_mapping.get("secondary_y_axis")
                if y2_val and isinstance(y2_val, (str, list)):
                    y2_fields = [y2_val] if isinstance(y2_val, str) else list(y2_val)

        # CRITICAL: Always ensure secondary measures are REMOVED from the primary Y bucket to prevent overlap
        # AND deduplicate primary fields while we are at it
        if y2_fields:
             y_fields = [f for f in y_fields if f not in y2_fields]
        if size_fields:
             y_fields = [f for f in y_fields if f not in size_fields]
        if x_fields_scatter:
             y_fields = [f for f in y_fields if f not in x_fields_scatter]
        
        y_fields = list(dict.fromkeys(y_fields)) # Deduplicate

        # For clusteredBarChart, force LineAmount to Y and others to Tooltips as per user "should be"
        if visual_type == "clusteredBarChart":
            # Identify LineAmount (or first available measure if not found)
            primary_y = [f for f in y_fields if "LineAmount" in f]
            if primary_y:
                y_fields = primary_y[:1]
            elif y_fields:
                y_fields = y_fields[:1]

        # Do not inject marks_text into y_fields, as Power BI visuals often reject non-value/axes fields in Y projections

        # For pie/donut charts, data comes from axis_mapping.values + legends, not rows/columns
        has_pie_data = (
            visual_type in ["pieChart", "donutChart"]
            and isinstance(axis_mapping, dict)
            and (axis_mapping.get("values") or pbi_legends)
        )
        if not category_fields and not y_fields and not y2_fields and not has_pie_data:
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

        category_projections = self._build_category_projections(category_fields, field_to_table, default_table, display_to_bi_name, is_direct_lake=is_direct_lake, field_to_format=local_field_to_format)
        y_projections = self._build_y_projections(y_fields, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, visual_type=visual_type, field_to_format=local_field_to_format)
        y2_projections = self._build_y_projections(y2_fields, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, visual_type=visual_type, field_to_format=local_field_to_format)
        x_projections_scatter = self._build_y_projections(x_fields_scatter, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, visual_type=visual_type, field_to_format=local_field_to_format)
        size_projections = self._build_y_projections(size_fields, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, visual_type=visual_type, field_to_format=local_field_to_format)

        # Strip 'active' from secondary buckets as per Power BI schema
        for p in y2_projections:
            p.pop("active", None)
        for p in size_projections:
            p.pop("active", None)

        # If still no y_projections and no y2_projections, fallback on first category field
        if not y_projections and not y2_projections and not x_projections_scatter and category_fields:
            f = category_fields[0]
            entity = field_to_table.get(f, default_table)
            resolved_f = self._resolve_field(f, display_to_bi_name, entity)
            dt = field_to_datatype.get(self.clean_field(f), "string")
            is_numeric = dt in ["integer", "real"]
            
            if visual_type == "card" and not is_numeric:
                func = 3
                q_ref = f"Min({entity}.{resolved_f})"
                nq_ref = f"First {resolved_f}"
            else:
                func = 5
                q_ref = f"CountNonNull({entity}.{resolved_f})"
                nq_ref = f"Count of {resolved_f}"

            y_projections.append({
                "field": {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved_f}}, "Function": func}},
                "queryRef": q_ref, "nativeQueryRef": nq_ref
            })

        # Build query state
        if visual_type in ["card", "multiRowCard"]:
            query_state = {"Values": {"projections": y_projections[:1] if y_projections else category_projections[:1]}}
        elif visual_type == "tableEx":
            # Tables use "Values" bucket for all columns, and they typically omit 'active'
            import copy
            values_projections = []
            for p in category_projections + y_projections + y2_projections + x_projections_scatter + size_projections:
                p_copy = copy.deepcopy(p)
                p_copy.pop("active", None)
                values_projections.append(p_copy)
            
            query_state = {
                "Values": {"projections": values_projections},
                "Category": {"projections": category_projections},
                "Y": {"projections": y_projections}
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
            pie_value_fields = list(dict.fromkeys(pie_value_fields))
            pie_y_projections = self._build_y_projections(
                pie_value_fields, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, visual_type=visual_type, field_to_format=local_field_to_format
            ) if pie_value_fields else y_projections
            # Build category projections from legends
            pie_cat_fields = [leg for leg in pbi_legends if leg and not self._is_none_field(leg)]
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

        # Build sort
        sort_definition = None
        if explicit_sort_items:
            if visual_type == "pivotTable":
                sort_definition = {"sort": explicit_sort_items, "isDefaultSort": True}
            else:
                sort_definition = {"sort": [explicit_sort_items[0]], "isDefaultSort": True}
        else:
            if visual_type == "lineChart" and category_projections:
                # For line charts, default to Ascending sort on Category (Trend)
                sort_definition = {"sort": [{"field": category_projections[0]["field"], "direction": "Ascending"}], "isDefaultSort": True}
            elif visual_type == "clusteredBarChart" and y_projections:
                # For clustered bar charts, sort by the primary measure (Revenue if available)
                sort_field = y_projections[0]["field"]
                for yp in y_projections:
                    if "Revenue" in yp.get("queryRef", ""):
                        sort_field = yp["field"]
                        break
                sort_definition = {"sort": [{"field": sort_field, "direction": "Descending"}], "isDefaultSort": True}
            elif visual_type == "pivotTable" and (row_projs or col_projs):
                # For Matrix visuals, default to sorting by all rows/columns in Ascending order
                sort_items = []
                for p in row_projs + col_projs:
                    sort_items.append({"field": p["field"], "direction": "Ascending"})
                if sort_items:
                    sort_definition = {"sort": sort_items, "isDefaultSort": True}
            elif y_projections:
                if visual_type == "card":
                    sort_definition = {"isDefaultSort": True}
                else:
                    sort_definition = {"sort": [{"field": y_projections[0]["field"], "direction": "Descending"}], "isDefaultSort": True}
            elif category_projections:
                sort_definition = {"sort": [{"field": category_projections[0]["field"], "direction": "Ascending"}], "isDefaultSort": True}

        query_dict = {"queryState": query_state}

        tooltip_formatting = dv.get("tooltip_formatting", [])
        tooltip_fields_clean = []
        tooltip_fields = []
        
        # Unify structure: ensure we have a list of dicts to iterate over
        tf_list = []
        if isinstance(tooltip_formatting, dict):
            tf_list = [tooltip_formatting]
        elif isinstance(tooltip_formatting, list):
            tf_list = tooltip_formatting

        # Collect tooltip fields from formatting
        if tf_list:
            for t in tf_list:
                if isinstance(t, dict):
                    fields = t.get("fields_used", [])
                    if isinstance(fields, list):
                        tooltip_fields.extend(fields)

        if tooltip_fields:
            raw_clean = list(dict.fromkeys([self.clean_field(f) for f in tooltip_fields if f and not self._is_none_field(f)]))
            tooltip_fields_clean = []
            valid_keys = {k.lower() for k in field_to_table.keys()} | {m.lower() for m in measure_names}
            for rc in raw_clean:
                check_f = rc.split(".", 1)[-1] if "." in rc else rc
                if check_f.lower() in valid_keys:
                    tooltip_fields_clean.append(rc)
            
            tooltip_projections = self._build_y_projections(
                tooltip_fields_clean, field_to_table, field_to_datatype, measure_names, default_table, display_to_bi_name, field_to_format=local_field_to_format
            )
            if tooltip_projections and visual_type not in ["card", "multiRowCard"]:
                query_state["Tooltips"] = {"projections": tooltip_projections}

        # Final safeguard: force-apply format strings on built projections.
        self._apply_formats_to_query_state(query_state, local_field_to_format)

        if sort_definition: query_dict["sortDefinition"] = sort_definition
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
                query_dict["queryState"]["Series"] = {"projections": legend_projections}

        # Load template
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

        # Remove legacy title from visual.objects if it exists (standardize on visualContainerObjects)
        if "title" in visual_container["visual"].get("objects", {}):
            del visual_container["visual"]["objects"]["title"]

        # Set visual title text globally if available (Visual Container Title)
        visual_title_text = dv.get("visual_title") or dv.get("display_name") or dv.get("name")
        if visual_type == "clusteredBarChart":
            # For clusteredBarChart on Top Products, user wants empty title centered
            # But the user might still want the visual_title if it's there
            if not dv.get("visual_title"):
                visual_title_text = "''"
            
        if visual_title_text and not suppress_title:
            # Set 'title' in visualContainerObjects
            vco = visual_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
            title_list = vco.setdefault("title", [{}])
            if not title_list: title_list.append({})
            t_props = title_list[0].setdefault("properties", {})
            t_props["show"] = {"expr": {"Literal": {"Value": "true"}}}
            
            # Heading level
            t_props["heading"] = {"expr": {"Literal": {"Value": "'Heading2'"}}}
            t_props["alignment"] = {"expr": {"Literal": {"Value": "'center'"}}}

            # Map to 'text' property
            if visual_title_text == "''":
                title_expr = {"expr": {"Literal": {"Value": "''"}}}
            else:
                title_expr = {"expr": {"Literal": {"Value": f"'{visual_title_text}'"}}}
                
            t_props["text"] = title_expr
            
            if visual_type == "clusteredBarChart":
                t_props["alignment"] = {"expr": {"Literal": {"Value": "'center'"}}}
        elif suppress_title:
            # Explicitly hide the title
            vco = visual_container.setdefault("visual", {}).setdefault("visualContainerObjects", {})
            title_list = vco.setdefault("title", [{}])
            if not title_list: title_list.append({})
            t_props = title_list[0].setdefault("properties", {})
            t_props["show"] = {"expr": {"Literal": {"Value": "false"}}}

        # Apply visual_properties (fonts, colors, axis titles) if present
        self._apply_visual_properties(visual_container, dv.get("visual_properties", {}), suppress_title=suppress_title, pbi_config=pbi_visual_config, dashboard_title_formatting=dashboard_title_formatting, parent_type=parent_type)
        
        # Inject default show properties for clusteredBarChart as per user "should be"
        if visual_type == "clusteredBarChart":
            objs = visual_container.setdefault("visual", {}).setdefault("objects", {})
            objs.setdefault("labels", [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}}}] )
            objs.setdefault("categoryAxis", [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}}}] )
            visual_container["visual"]["drillFilterOtherVisuals"] = True

        # Force Data Labels show: true for every visual EXCEPT tableEx and Card
        if visual_type not in ["tableEx", "card"]:
            objs = visual_container.setdefault("visual", {}).setdefault("objects", {})
            objs["labels"] = [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}}}]
            
        # specifically fix tableEx properties (strip chart properties like labels/legend)
        if visual_type == "tableEx":
            objs = visual_container.get("visual", {}).get("objects", {})
            if "labels" in objs:
                del objs["labels"]
            if "legend" in objs:
                del objs["legend"]

        # Identify explicit filter fields to skip auto-generation for them
        explicit_filter_fields = set()
        if filters:
            for f_item in filters:
                raw = f_item.get("tableau_original_name") if hasattr(f_item, "get") else str(f_item)
                if raw:
                    explicit_filter_fields.add(self._normalize_for_matching(self.clean_field(raw)))

        # Auto-generate filterConfig.filters for all query fields
        auto_filters = []
        for cf in category_fields:
            clean_cf = self.clean_field(cf)
            norm_cf = self._normalize_for_matching(clean_cf)
            if norm_cf in explicit_filter_fields:
                continue
            # Re-verify we don't have virtual fields here
            if clean_cf in ["Measure Names", "Measure Values"]: continue
            
            entity = self._extract_entity(cf, field_to_table, default_table)
            resolved = self._resolve_field(clean_cf, display_to_bi_name, entity)
            
            inner_f, date_level = self.get_date_hierarchy_info(cf)
            if date_level:
                field_expr = {
                    "HierarchyLevel": {
                        "Expression": {
                            "Hierarchy": {
                                "Expression": {
                                    "PropertyVariationSource": {
                                        "Expression": {
                                            "SourceRef": {
                                                "Entity": entity
                                            }
                                        },
                                        "Name": "Variation",
                                        "Property": resolved
                                    }
                                },
                                "Hierarchy": "Date Hierarchy"
                            }
                        },
                        "Level": date_level
                    }
                }
                f_type = "Categorical"
            else:
                field_expr = {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}
                f_type = "Categorical"
                
            auto_filters.append({
                "name": str(uuid.uuid4()).replace("-", "")[:20],
                "field": field_expr,
                "type": f_type,
                "howCreated": "User"
            })
        all_y_fields = list(dict.fromkeys(list(y_fields) + list(y2_fields) + list(x_fields_scatter) + list(size_fields)))
        for yf in all_y_fields:
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
                    is_numeric = dt in ["integer", "real"]
                    func = 0 if is_numeric else (3 if visual_type == "card" else 5)

                field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}, "Function": func}}
            auto_filters.append({
                "name": str(uuid.uuid4()).replace("-", "")[:20],
                "field": field_expr,
                "type": "Advanced",
                "howCreated": "User"
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
                    is_numeric = dt in ["integer", "real"]
                    func = 0 if is_numeric else (3 if visual_type == "card" else 5)

                field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}, "Function": func}}
            auto_filters.append({
                "name": str(uuid.uuid4()).replace("-", "")[:20],
                "field": field_expr,
                "type": "Advanced",
                "howCreated": "User"
            })

        # Add explicit filters from the sheet data
        if filters:
            for filter_item in filters:
                raw_ff = filter_item.get("tableau_original_name", "") if isinstance(filter_item, dict) else str(filter_item)
                if not raw_ff or self._is_none_field(raw_ff): continue

                ff = self.clean_field(raw_ff)
                if not ff or ff in ["Measure Names", "Measure Values"]: continue

                # Determine entity from suffix or field_to_table mapping
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
                    inner_ff, date_level = self.get_date_hierarchy_info(raw_ff)
                    if date_level:
                        field_expr = {
                            "HierarchyLevel": {
                                "Expression": {
                                    "Hierarchy": {
                                        "Expression": {
                                            "PropertyVariationSource": {
                                                "Expression": {
                                                    "SourceRef": {
                                                        "Entity": entity
                                                    }
                                                },
                                                "Name": "Variation",
                                                "Property": resolved_ff
                                            }
                                        },
                                        "Hierarchy": "Date Hierarchy"
                                    }
                                },
                                "Level": date_level
                            }
                        }
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
                                
                    # 3. Check sheet's rows/columns nested sorts
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
                    target_property = self._resolve_field(ff, display_to_bi_name, entity)
                    
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

                auto_filters.append(filter_obj)

        visual_container["filterConfig"] = {"filters": auto_filters}

        return visual_container
