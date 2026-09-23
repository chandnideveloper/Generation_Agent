import json
import os
from typing import Dict, Any

class TemplateManager:
    def __init__(self):
        # 1. Get the absolute path to the directory this file (template_manager.py) is in
        current_dir = os.path.dirname(os.path.abspath(__file__))
        
        # 2. Safely calculate the absolute path to the templates folder
        # Because template_manager is inside app/core_logic/template_manager/, we go up two levels
        self.base_dir = os.path.abspath(os.path.join(current_dir, "..", "..", "templates"))

    def load_json_template(self, sub_folder: str, filename: str) -> Dict[str, Any]:
        """Loads a pure JSON file as a Python Dictionary."""
        file_path = os.path.join(self.base_dir, sub_folder, filename)
        
        # DEBUG PRINT: This will show up in your terminal to tell you exactly where it is looking
        print(f"--- DEBUG: Looking for template at: {file_path} ---") 
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Template missing: {file_path}")

    def load_and_replace_template(self, sub_folder: str, filename: str, replacements: Dict[str, str]) -> str:
        """Loads a file, replaces placeholders like {app_name}, and returns a string."""
        file_path = os.path.join(self.base_dir, sub_folder, filename)
        
        # DEBUG PRINT
        print(f"--- DEBUG: Looking for template at: {file_path} ---")
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except FileNotFoundError:
             raise FileNotFoundError(f"Template missing: {file_path}")
            
        for key, value in replacements.items():
            content = content.replace(f"{{{key}}}", value)
            
        return content