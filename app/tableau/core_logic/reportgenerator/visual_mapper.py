import json
import uuid
import re
from typing import Dict, Optional, List, Tuple
from app.tableau.core.logging_utils import log_info, log_error, log_warning
from app.tableau.core.config import Config
from app.tableau.core_logic.template_manager.template_manager import TemplateManager
from app.tableau.services.action_logger import ActionLogger


class VisualMapperMixin:
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
    "Matrix": "pivotTable",
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

    def _map_pbi_visual_type(self, power_bi_visual_type) -> str:
        """Maps power_bi_visual_type (string or dict) to PBI template filename (without .json)."""
        if isinstance(power_bi_visual_type, dict):
            # Extract basic string name from dictionary if possible
            power_bi_visual_type = power_bi_visual_type.get("power_bi_visual_type") or power_bi_visual_type.get("name") or power_bi_visual_type.get("value") or str(power_bi_visual_type)
        
        if not isinstance(power_bi_visual_type, str):
            power_bi_visual_type = str(power_bi_visual_type)
            
        return self.PBI_VISUAL_TYPE_MAP.get(power_bi_visual_type, "")

    def _resolve_visual_type(self, page_entry: Dict, measure_names: List[str] = None) -> str:
        """Resolves the final visual type: prefers power_bi_visual_type, falls back to mark_type."""
        pbi_type = page_entry.get("power_bi_visual_type", "")
        mark_type = page_entry.get("mark_type", "Automatic")
        
        # Ensure pbi_type is always a string for subsequent logic/lookups
        pbi_type_str = ""
        if isinstance(pbi_type, dict):
            # Try to extract from known keys in the dictionary format used in mapping.json
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
            # Prioritize Card and KPI types immediately
            if resolved in ["card", "multiRowCard"]:
                return resolved

        # --- Specific Handlers for KPI/Card based on markings ---
        # Explicit check for Card/KPI strings or Text mark type
        if pbi_type_str.lower() in ["card", "kpi", "multi-row card"] or mark_type == "Text":
            return "card"

        # Unified orientation logic for Bar vs Column
        # Tableau: Rows (Measure), Columns (Dim) -> Vertical (clusteredColumnChart)
        # Tableau: Rows (Dim), Columns (Measure) -> Horizontal (barChart)
        
        # Check if we are in a Bar/Column context
        is_bar_column_context = (
            "Bar" in pbi_type_str or "Column" in pbi_type_str or 
            mark_type in ["Bar", "Automatic"] or
            resolved in ["barChart", "clusteredBarChart", "clusteredColumnChart", "columnChart"]
        )

        if is_bar_column_context and measure_names:
            rows = page_entry.get("rows", [])
            # If a measure is in Rows, it should be a vertical (Column) chart
            has_measure_in_rows = any(self.clean_field(r) in measure_names for r in rows)
            
            # Use Clustered variant if specified in pbi_type or if resolved to a clustered type
            is_clustered = "Clustered" in pbi_type_str or (resolved and "clustered" in resolved.lower())
            
            if has_measure_in_rows:
                return "clusteredColumnChart" if is_clustered else "columnChart"
            else:
                return "clusteredBarChart" if is_clustered else "barChart"

        if resolved:
            return resolved

        return self._map_mark_type_to_visual_type(mark_type)

    @staticmethod
    def _is_none_field(f_name: str) -> bool:
        """Returns True if the field name represents an empty/None value that should be skipped."""
        if not f_name:
            return True
            
        # Extract string if dictionary
        if isinstance(f_name, dict):
            f_name = f_name.get("field") or f_name.get("name") or str(f_name)
            
        if not isinstance(f_name, str):
            return True
            
        stripped = f_name.strip()
        return stripped.lower() in ('none', '', 'null', 'n/a')

    def _resolve_field(self, f_name: str, display_to_bi_name: Dict, entity: str = None) -> str:
        """Resolves a display field name to its actual BI column name for PBI Property."""
        if not isinstance(f_name, str):
            return str(f_name)
            
        f_name_stripped = f_name.strip()
        resolved = None

        # Clean f_name_stripped for candidates
        cand_col = f_name_stripped
        if "." in f_name_stripped:
            cand_col = f_name_stripped.split(".", 1)[1].strip().strip("[]'\"")

        clean_col = f_name_stripped.strip("[]'\"")

        if entity:
            resolved = (
                display_to_bi_name.get(f"{entity}.{f_name}")
                or display_to_bi_name.get(f"{entity}.{f_name_stripped}")
                or display_to_bi_name.get(f"{entity}.{cand_col}")
                or display_to_bi_name.get(f"{entity}.{clean_col}")
            )
            if not resolved:
                norm_prefix = f"{entity.strip().lower()}."
                norm_map = {k.strip().lower(): v for k, v in display_to_bi_name.items() if isinstance(k, str)}
                resolved = (
                    norm_map.get(norm_prefix + f_name_stripped.lower())
                    or norm_map.get(norm_prefix + cand_col.lower())
                    or norm_map.get(norm_prefix + clean_col.lower())
                )

        if not resolved:
            resolved = (
                display_to_bi_name.get(cand_col)
                or display_to_bi_name.get(clean_col)
                or display_to_bi_name.get(f_name)
                or display_to_bi_name.get(f_name_stripped)
            )
            if not resolved:
                norm_map = {k.strip().lower(): v for k, v in display_to_bi_name.items() if isinstance(k, str)}
                resolved = (
                    norm_map.get(cand_col.lower())
                    or norm_map.get(clean_col.lower())
                    or norm_map.get(f_name_stripped.lower(), cand_col)
                )

        if isinstance(resolved, str):
            # If resolved still contains Table.Column, keep only the column part
            if "." in resolved and not (resolved.startswith("(") and resolved.endswith(")")):
                resolved = resolved.split(".", 1)[1].strip()
            # Remove any wrapping square brackets [FieldName] -> FieldName for BI model reference
            resolved = resolved.strip("[]'\"")

        return resolved

    @staticmethod
    def _normalize_for_matching(text: str) -> str:
        """Normalizes a string for matching by stripping, lowering, and standardizing dashes."""
        if not text: return ""
        # Standardize different dash characters to a normal hyphen
        normalized = text.strip().lower()
        normalized = normalized.replace("–", "-").replace("—", "-")
        return normalized

    @staticmethod
    def clean_field(f_name):
        """
        Cleans a field name by removing Tableau aggregations.
        Returns the cleaned name.
        """
        if isinstance(f_name, dict):
            # Recurse or walk to find the actual string name
            inner = f_name.get("field") or f_name.get("name") or f_name.get("caption") or f_name.get("column")
            
            # Handle Set sources by appending _In_Out
            if f_name.get("source") == "set" or f_name.get("mode") == "checklist":
                # If inner is still a dict, we need to clean it first to get the string
                clean_inner = VisualMapperMixin.clean_field(inner) if inner else ""
                if clean_inner and not str(clean_inner).endswith("_In_Out"):
                    return f"{str(clean_inner).replace(' ', '_')}_In_Out"
                return str(clean_inner)
            
            if inner:
                return VisualMapperMixin.clean_field(inner)
            return str(f_name)
        
        if not isinstance(f_name, str):
            return str(f_name)
            
        f_name = f_name.strip()
        # Detect aggregations like SUM(field), ATTR(field), etc.
        match = re.match(r'^([A-Z0-9_]+)\((.*)\)$', f_name, re.IGNORECASE)
        if match:
            agg_func = match.group(1).upper()
            inner_field = match.group(2).strip()
            # Date parts should be treated as dimensions/categories, not aggregated measures
            if agg_func in ("MONTH", "YEAR", "QUARTER", "DAY", "WEEK", "WEEKDAY"):
                f_name = inner_field
            else:
                f_name = inner_field
        
        return f_name

    @staticmethod
    def get_field_agg_info(f_name):
        """Returns (cleaned_name, agg_func_str) where agg_func_str is 'SUM', 'AVG' etc."""
        if isinstance(f_name, dict):
            f_name = f_name.get("field") or f_name.get("name") or f_name.get("caption") or f_name.get("column") or str(f_name)
            
        if not isinstance(f_name, str):
            return str(f_name), None
        
        f_name = f_name.strip()
        match = re.match(r'^([A-Z0-9_]+)\((.*)\)$', f_name, re.IGNORECASE)
        if match:
            agg_func = match.group(1).upper()
            inner_f = match.group(2).strip()
            # Special case for date functions which are dimensions
            if agg_func in ("MONTH", "YEAR", "QUARTER", "DAY", "WEEK", "WEEKDAY"):
                return inner_f, None
            return inner_f, agg_func
        return f_name, None

    @staticmethod
    def get_date_hierarchy_info(f_name):
        """Returns (inner_field, date_level) for date aggregations. date_level is capitalized."""
        if isinstance(f_name, dict):
            f_name = f_name.get("field") or f_name.get("name") or f_name.get("caption") or f_name.get("column") or str(f_name)
            
        if not isinstance(f_name, str):
            return "", None
        
        f_name = f_name.strip()
        match = re.match(r'^([A-Z0-9_]+)\((.*)\)$', f_name, re.IGNORECASE)
        if match:
            agg_func = match.group(1).upper()
            inner_f = match.group(2).strip()
            if agg_func in ("MONTH", "YEAR", "QUARTER", "DAY", "WEEK", "WEEKDAY"):
                if agg_func == "MONTH": level = "Month"
                elif agg_func == "YEAR": level = "Year"
                elif agg_func == "QUARTER": level = "Quarter"
                elif agg_func == "DAY": level = "Day"
                else: level = agg_func.capitalize()
                return inner_f, level
        return f_name, None

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

    @staticmethod
    def get_field_metadata(f_name) -> Tuple[str, bool]:
        """
        Cleans a field name and returns (cleaned_name, was_aggregated).
        """
        was_aggregated = False
        if isinstance(f_name, dict):
            # Recurse or walk to find the actual string name
            inner = f_name.get("field") or f_name.get("name") or f_name.get("caption") or f_name.get("column")
            
            # Handle Set sources by appending _In_Out
            if f_name.get("source") == "set" or f_name.get("mode") == "checklist":
                # If inner is still a dict, we need to clean it first to get the string
                clean_inner, _ = VisualMapperMixin.get_field_metadata(inner) if inner else ("", False)
                if clean_inner and not str(clean_inner).endswith("_In_Out"):
                    return f"{str(clean_inner).replace(' ', '_')}_In_Out", False
                return str(clean_inner), False
            
            if inner:
                return VisualMapperMixin.get_field_metadata(inner)
            return str(f_name), False
        
        if not isinstance(f_name, str):
            return str(f_name), False
            
        f_name = f_name.strip()
        match = re.match(r'^([A-Z0-9_]+)\((.*)\)$', f_name, re.IGNORECASE)
        if match:
            agg_func = match.group(1).upper()
            inner_field = match.group(2).strip()
            
            # Date parts are NOT measures in the PBI sense for Y axis
            if agg_func in ("MONTH", "YEAR", "QUARTER", "DAY", "WEEK", "WEEKDAY"):
                return inner_field, False
                
            was_aggregated = True
            return inner_field, was_aggregated
        return f_name, was_aggregated

    @staticmethod
    def _extract_entity(f_name: str, field_to_table: Dict, default_table: str) -> str:
        """
        Extracts the entity (table) name for a field.
        Logic:
        1. Direct lookup in field_to_table mapping.
        2. Clean the field name (strip aggregations) and retry.
        3. Try underscore-to-space variant.
        4. Try normalized match (case-insensitive & stripped).
        5. Fallback to default_table.
        """
        # Handle dictionaries
        if isinstance(f_name, dict):
            f_name = f_name.get("field") or f_name.get("name") or str(f_name)

        if not isinstance(f_name, str):
            return default_table

        f_strip = f_name.strip()

        # 1. Direct mapping
        entity = field_to_table.get(f_strip) or field_to_table.get(f"[{f_strip}]")
        if entity:
            return entity

        # 1b. Check explicit Table.Column or [Table].[Column] notation
        if "." in f_strip:
            parts = f_strip.split(".", 1)
            tbl_cand = parts[0].strip().strip("[]'\"")
            col_cand = parts[1].strip().strip("[]'\"")
            for kt in set(field_to_table.values()):
                if kt.lower() == tbl_cand.lower():
                    return kt
            # Also check if col_cand alone is in field_to_table
            if col_cand in field_to_table:
                return field_to_table[col_cand]

        # 2. Clean mapping (strip aggregation wrappers)
        clean_name, _ = VisualMapperMixin.get_field_metadata(f_name)
        entity = field_to_table.get(clean_name) or field_to_table.get(f"[{clean_name}]")
        if entity:
            return entity

        # 3. Try underscore-to-space variant
        spaced_name = clean_name.replace("_", " ")
        entity = field_to_table.get(spaced_name)
        if entity:
            return entity

        # 4. Try normalized match (case-insensitive & stripped)
        norm_field_to_table = {k.strip().lower(): v for k, v in field_to_table.items() if isinstance(k, str)}
        norm_clean = clean_name.strip().lower()
        if norm_clean in norm_field_to_table:
            return norm_field_to_table[norm_clean]

        # 4. Dynamic extraction from suffix
        match = re.search(r'\(([^()]+)\)(?:\s*\))*\s*$', clean_name)
        if match:
            suffix_candidate = match.group(1).strip()
            if suffix_candidate in field_to_table.values():
                return suffix_candidate
                
            # Fallback for PascalCase table names when field_to_table is limited
            if len(suffix_candidate) > 1 and suffix_candidate[0].isupper():
                if suffix_candidate not in ("Month", "Year", "Quarter", "Day", "Week", "Sum", "Avg", "Min", "Max", "Count", "Custom SQL Query"):
                    return suffix_candidate

        # 4b. Also try matching on original f_strip just in case
        match = re.search(r'\(([^()]+)\)(?:\s*\))*\s*$', f_strip)
        if match:
            suffix_candidate = match.group(1).strip()
            if suffix_candidate in field_to_table.values():
                return suffix_candidate
            if len(suffix_candidate) > 1 and suffix_candidate[0].isupper():
                if suffix_candidate not in ("Month", "Year", "Quarter", "Day", "Week", "Sum", "Avg", "Min", "Max", "Count", "Custom SQL Query"):
                    return suffix_candidate

        # 4. Fallback
        return default_table

