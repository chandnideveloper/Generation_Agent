import json
import uuid
import re
from typing import Dict, Optional, List, Tuple
from app.tableau.core.logging_utils import log_info, log_error, log_warning
from app.tableau.core.config import Config
from app.tableau.core_logic.template_manager.template_manager import TemplateManager
from app.tableau.services.action_logger import ActionLogger


class ActionBuilderMixin:
    def _get_hardened_drill_throughs(self, page_entry: Dict) -> List[Dict]:
        """Returns a list of drill_throughs guaranteed to be dictionaries."""
        drills = page_entry.get("drill_throughs", [])
        if not isinstance(drills, list):
            return []
        
        valid_drills = []
        for dt in drills:
            if isinstance(dt, dict) and dt.get("drill_name"):
                valid_drills.append(dt)
        return valid_drills
