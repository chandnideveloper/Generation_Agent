import asyncio
from typing import Dict, Any
from app.tableau.core.logging_utils import log_info, log_error

class MetadataExporter:
    @staticmethod
    def extract_metadata(api_data: Dict[str, Any], report_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extracts semantic model and visuals metadata into a structured JSON dictionary.
        This aggregates all structural properties from the memory dictionaries.
        """
        try:
            # 1. Compile Semantic Model Data
            semantic_model = {}
            
            tables = api_data.get("tables", [])
            custom_sql = api_data.get("custom_sql", [])
            
            all_tables = []
            if isinstance(tables, list):
                all_tables.extend(tables)
            if isinstance(custom_sql, list):
                all_tables.extend(custom_sql)
            
            semantic_model["tables"] = all_tables
            semantic_model["relationships"] = api_data.get("relationships", [])
            semantic_model["measures"] = api_data.get("measures", [])
            semantic_model["calculated_fields"] = api_data.get("calculated_fields", [])
            semantic_model["sets"] = api_data.get("sets", [])
            semantic_model["parameters"] = api_data.get("parameters", [])
            semantic_model["calculated_fields_lods"] = api_data.get("Calculated Fields & LODs", [])
            
            # 2. Compile Visuals Data
            visuals = {}
            if report_data:
                # `pages` from report_data already contains all the rich visual structures and properties
                visuals["pages"] = report_data.get("pages", [])
            
            payload = {
                "semantic_model": semantic_model,
                "visuals": visuals
            }
            return payload
            
        except Exception as e:
            log_error(f"Failed to extract semantic model and visuals metadata: {e}")
            # Ensure it returns empty structures rather than throwing exceptions so it fails gracefully
            return {"semantic_model": {}, "visuals": {}}

