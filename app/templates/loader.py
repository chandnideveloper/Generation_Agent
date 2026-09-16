"""Template loader for Power BI / Fabric PBIR and TMDL generation.

Loads static and visual templates from `app/templates/` with in-memory caching.
Ensures generation follows the schema-compliant structure of modern Fabric / Power BI projects.
"""

import copy
import json
import os
from typing import Any, Dict, Optional

TEMPLATES_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(TEMPLATES_DIR, "static")
VISUALS_DIR = os.path.join(TEMPLATES_DIR, "visuals")

_STATIC_CACHE: Dict[str, str] = {}
_VISUAL_CACHE: Dict[str, Dict[str, Any]] = {}


def get_static_template(filename: str) -> str:
    """Load raw text content of a static template file (e.g. CY24SU10.json, expressions.tmdl)."""
    if filename in _STATIC_CACHE:
        return _STATIC_CACHE[filename]

    filepath = os.path.join(STATIC_DIR, filename)
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"Static template not found: {filepath}")

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    _STATIC_CACHE[filename] = content
    return content


def get_static_json(filename: str) -> Dict[str, Any]:
    """Load and parse a JSON file from templates/static/."""
    content = get_static_template(filename)
    return json.loads(content)


def get_visual_template(visual_type: str) -> Dict[str, Any]:
    """Load a visualContainer JSON template by visual type.
    
    Falls back to `visual_container.json` if a specific visual template does not exist.
    Returns a deep copy so callers can mutate freely.
    """
    candidate = f"{visual_type}.json"
    if visual_type not in _VISUAL_CACHE:
        filepath = os.path.join(VISUALS_DIR, candidate)
        if not os.path.isfile(filepath):
            # Fallback to generic visual container
            filepath = os.path.join(VISUALS_DIR, "visual_container.json")
            if not os.path.isfile(filepath):
                raise FileNotFoundError(f"Base visual container template not found: {filepath}")

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        _VISUAL_CACHE[visual_type] = data

    return copy.deepcopy(_VISUAL_CACHE[visual_type])


def get_report_root_template() -> Dict[str, Any]:
    """Load templates/visuals/report_root.json template (schema 3.2.0)."""
    return get_visual_template("report_root")


def get_page_template() -> Dict[str, Any]:
    """Load templates/visuals/page.json template (schema 2.0.0)."""
    return get_visual_template("page")


def get_base_theme_json() -> str:
    """Return raw CY24SU10.json content from templates/static/."""
    return get_static_template("CY24SU10.json")
