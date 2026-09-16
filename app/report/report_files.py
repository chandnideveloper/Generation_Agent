"""Static documents that make up a PBIR report item.

Separated from the page/visual assembly so the schema-bearing files —
`report.json`, `definition.pbir`, `.platform` — sit together and are easy
to check against a known-good project.
"""

import json

from app.report import pbir_schemas as S
from app.templates.loader import get_report_root_template, get_static_template
from app.util.ids import lineage_tag


def _platform(display_name: str) -> str:
    template = get_static_template("report_platform.json")
    tag = lineage_tag(f"report:{display_name}")
    content = template.replace("{app_name}", display_name).replace("{report_logical_id}", tag)
    return content


def _pbir(model_path: str) -> str:
    # Ensure model path is properly referenced
    return json.dumps({
        "$schema": S.REPORT_DEFINITION_PROPERTIES,
        "version": "4.0",
        "datasetReference": {"byPath": {"path": model_path}},
    }, indent=2)


def _report_json() -> str:
    """The report root, loaded directly from templates/visuals/report_root.json with dual-theme configuration."""
    report_doc = get_report_root_template()
    return json.dumps(report_doc, indent=2)

