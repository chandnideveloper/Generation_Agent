from autogen import AssistantAgent
import zipfile
import xml.etree.ElementTree as ET
import os
from typing import Dict, Any

from app.tableau.logging_utils import log_info, log_error

class TableauParserAgent(AssistantAgent):
    def __init__(self, name="TableauParser"):
        super().__init__(name=name, code_execution_config={"use_docker": False})

    def parse_local_twbx(self, filename: str) -> Dict[str, Any]:
        """
        Extracts and parses a Tableau Packaged Workbook (.twbx) or Workbook (.twb)
        to extract semantic model and visual metadata.
        """
        log_info(f"[{self.name}] Initiating parse for file: {filename}")
        
        if not os.path.exists(filename):
            log_error(f"[{self.name}] File not found: {filename}")
            raise FileNotFoundError(f"Cannot find Tableau file: {filename}")

        xml_content = None

        # 1. Crack open the file
        if filename.endswith(".twbx"):
            # A .twbx is just a zip file containing a .twb and data extracts
            try:
                with zipfile.ZipFile(filename, 'r') as z:
                    for name in z.namelist():
                        if name.endswith('.twb'):
                            xml_content = z.read(name)
                            log_info(f"[{self.name}] Extracted XML '{name}' from .twbx archive.")
                            break
            except zipfile.BadZipFile:
                log_error(f"[{self.name}] Failed to unzip .twbx file. It may be corrupted.")
                raise
        elif filename.endswith(".twb"):
            # A .twb is raw XML
            with open(filename, 'rb') as f:
                xml_content = f.read()
        else:
            raise ValueError(f"Unsupported file format. Expected .twbx or .twb, got: {filename}")

        if not xml_content:
            raise ValueError("No valid .twb (XML) file found inside the provided archive.")

        # 2. Parse the XML
        return self._parse_tableau_xml(xml_content)

    def _parse_tableau_xml(self, xml_bytes: bytes) -> Dict[str, Any]:
        """
        Parses the raw XML of a Tableau workbook to extract connections, 
        tables, measures, and sheets.
        """
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            log_error(f"[{self.name}] XML Parse Error: {e}")
            raise

        parsed_data = {
            "datasources": [],
            "tables": [],
            "measures": [],
            "calculated_fields": [],
            "sheets_visuals": []
        }

        # --- A. Parse Datasources & Connections ---
        for ds in root.findall(".//datasource"):
            ds_name = ds.attrib.get('name', 'Unknown')
            ds_info = {"name": ds_name, "id": ds.attrib.get('caption', ds_name), "connections": []}
            
            # Find database connections
            for conn in ds.findall(".//connection"):
                conn_class = conn.attrib.get('class', '')
                if conn_class in ['snowflake', 'postgres', 'sqlserver']:
                    ds_info["connections"].append({
                        "type": conn_class,
                        "server": conn.attrib.get('server', ''),
                        "database": conn.attrib.get('dbname', ''),
                        "schema": conn.attrib.get('schema', 'PUBLIC')
                    })
            
            if ds_info["connections"]:
                parsed_data["datasources"].append(ds_info)

            # Find columns / calculated fields within the datasource
            for col in ds.findall(".//column"):
                col_name = col.attrib.get('name', '').strip('[]')
                datatype = col.attrib.get('datatype', 'string')
                formula_node = col.find('calculation')
                
                if formula_node is not None:
                    formula = formula_node.attrib.get('formula')
                    if formula:
                        parsed_data["calculated_fields"].append({
                            "name": col_name,
                            "datatype": datatype,
                            "powerbi_formula": formula # To be cleaned by TMDL Generator
                        })

        # --- B. Parse Worksheets (Visuals) ---
        for win in root.findall(".//window"):
            sheet_name = win.attrib.get('name')
            if not sheet_name:
                continue
            
            # This is a very basic extraction scaffold. 
            # Real Tableau XML parsing for rows/cols requires deep inspection of <worksheets> and <shelf>
            parsed_data["sheets_visuals"].append({
                "name": sheet_name,
                "mark_type": "Automatic", # Default
                "rows": [],      # Populated by looking at <shelf name='rows'>
                "columns": [],   # Populated by looking at <shelf name='columns'>
                "measures": [],
                "filters": []
            })

        log_info(f"[{self.name}] Successfully parsed Tableau XML. Found {len(parsed_data['datasources'])} datasources and {len(parsed_data['sheets_visuals'])} sheets.")
        return parsed_data