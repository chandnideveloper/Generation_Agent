import re
from typing import Dict, List
from app.tableau.core.logging_utils import log_info

class VisualProjectionsMixin:
    def _resolve_projection_format(self, raw_field: str, clean_field: str, resolved_field: str, field_to_format: Dict = None) -> str:
        """Find format string using robust field-name variants.

        Supports keys like:
        - AGG(Total Loan)
        - Total Loan
        - loan.Total Loan
        """
        if not field_to_format or not isinstance(field_to_format, dict):
            return None

        candidates = []

        def _add(v):
            if isinstance(v, str) and v and v not in candidates:
                candidates.append(v)

        _add(raw_field)
        _add(clean_field)
        _add(resolved_field)

        # Add aggregation-stripped variant from the raw field
        try:
            inner_f, _ = self.get_field_agg_info(raw_field)
            _add(inner_f)
            _add(self.clean_field(inner_f))
        except (AttributeError, TypeError, ValueError):
            pass

        # Add de-entity variants
        for c in list(candidates):
            if "." in c:
                _add(c.rsplit(".", 1)[-1])

        # Add common aggregation wrappers around clean names
        for c in list(candidates):
            _add(f"AGG({c})")
            _add(f"SUM({c})")
            _add(f"AVG({c})")
            _add(f"COUNT({c})")
            _add(f"COUNTD({c})")

        # Check if any candidate has explicit None (visual-level Automatic format override)
        for k in candidates:
            if k in field_to_format and field_to_format[k] is None:
                return None  # Skip format injection - visual explicitly set to Automatic

        # 1) Exact key match
        for k in candidates:
            v = field_to_format.get(k)
            if v and v != "G":
                return v

        # 2) Case-insensitive normalized match
        norm_map = {
            str(k).strip().lower(): v
            for k, v in field_to_format.items()
            if isinstance(k, str) and v and v != "G"
        }
        for k in candidates:
            v = norm_map.get(k.strip().lower())
            if v:
                return v

        return None

    def _build_category_projections(self, category_fields: List[str], field_to_table: Dict, default_table: str, display_to_bi_name: Dict = None, is_direct_lake: bool = False, field_to_format: Dict = None) -> List[Dict]:
        if display_to_bi_name is None:
            display_to_bi_name = {}
        projections = []
        for f in category_fields:
            if self._is_none_field(f):
                continue
            
            clean_f = self.clean_field(f)
            if not clean_f or clean_f in ("Measure Names", "Measure Values"):
                continue

            entity = self._extract_entity(f, field_to_table, default_table)
            
            entity = entity or default_table

            resolved = self._resolve_field(clean_f, display_to_bi_name, entity)
            if field_to_table and resolved in field_to_table:
                entity = field_to_table[resolved]
            
            inner_f, date_level = self.get_date_hierarchy_info(f)
            # For Direct Lake connections, skip hierarchy creation and use simple Column references
            if date_level and not is_direct_lake:
                proj = {
                    "field": {
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
                    },
                    "queryRef": f"{entity}.{resolved}.Variation.Date Hierarchy.{date_level}",
                    "nativeQueryRef": f"{resolved} {date_level}",
                    "active": True
                }
                p_fmt = self._resolve_projection_format(f, clean_f, resolved, field_to_format)
                if p_fmt:
                    proj["format"] = p_fmt
                projections.append(proj)
            else:
                proj = {
                    "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}},
                    "queryRef": f"{entity}.{resolved}", "nativeQueryRef": resolved, "active": True
                }
                p_fmt = self._resolve_projection_format(f, clean_f, resolved, field_to_format)
                if p_fmt:
                    proj["format"] = p_fmt
                projections.append(proj)
        return projections

    def _build_y_projections(self, y_fields: List[str], field_to_table: Dict, 
                             field_to_datatype: Dict, measure_names: List, default_table: str, 
                             display_to_bi_name: Dict = None, visual_type: str = None, field_to_format: Dict = None) -> List[Dict]:
        if display_to_bi_name is None:
            display_to_bi_name = {}
        projections = []
        for f in y_fields:
            if self._is_none_field(f):
                continue
            
            clean_f = self.clean_field(f)
            if not clean_f or clean_f in ("Measure Names", "Measure Values"):
                continue

            entity = self._extract_entity(f, field_to_table, default_table)
            
            entity = entity or default_table

            # Use aggregation logic to correctly map SUM, AVG, etc. from Tableau/SQL
            _, agg_func_str = self.get_field_agg_info(f)
            resolved = self._resolve_field(clean_f, display_to_bi_name, entity)
            if field_to_table and resolved in field_to_table:
                entity = field_to_table[resolved]
            
            # Check if this field should be treated as a DAX measure or a column aggregation
            norm_measure_names = {m.strip().lower() for m in measure_names if isinstance(m, str)}
            is_measure = (
                clean_f.strip().lower() in norm_measure_names 
                or f.strip().lower() in norm_measure_names 
                or agg_func_str == 'AGG'
            )
            
            force_aggregation = False

            if is_measure and not force_aggregation:
                field_expr = {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}
                query_ref = f"{entity}.{resolved}"
                native_query_ref = resolved
            else:
                # Determine aggregation function
                dt = field_to_datatype.get(clean_f, "string")
                is_numeric = dt in ["integer", "real"]

                pbi_func = self.map_agg_to_pbi(agg_func_str)
                if pbi_func is not None:
                    func = pbi_func
                else:
                    # For string fields, use Min (3) for cards, otherwise CountNonNull (5)
                    func = 0 if is_numeric else (3 if visual_type == "card" else 5)
                
                # Mapping based on user testing: 0:Sum, 1:Avg, 2:CountNonNull, 3:Min, 4:Max, 5:CountNonNull, 6:Median, 7:StandardDeviation, 8:Variance
                func_names = {0: "Sum", 1: "Avg", 2: "CountNonNull", 3: "Min", 4: "Max", 5: "CountNonNull", 6: "Median", 7: "StandardDeviation", 8: "Variance"}
                func_name = func_names.get(func, "Sum")
                
                field_expr = {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": resolved}}, "Function": func}}
                
                # Correctly assign queryRef based on mapping strings
                query_ref = f"{func_name}({entity}.{resolved})"
                
                # nativeQueryRef logic: 
                # 5: Count [Field] (Sometimes "Count of [Field]")
                # 3: First [Field] (for strings)
                # 2: Count of [Field]
                if func == 5 or func == 2:
                    native_query_ref = f"Count of {resolved}"
                elif func == 3 and not is_numeric:
                    native_query_ref = f"First {resolved}"
                elif func == 4 and not is_numeric:
                    native_query_ref = f"Last {resolved}"
                else:
                    native_query_ref = f"{func_name} of {resolved}"

            proj = {"field": field_expr, "queryRef": query_ref, "nativeQueryRef": native_query_ref}
            p_fmt = self._resolve_projection_format(f, clean_f, resolved, field_to_format)
            if p_fmt is not None:  # None means visual explicitly set to Automatic, skip format
                proj["format"] = p_fmt
            projections.append(proj)
        return projections
