"""Static documents that make up a PBIR report item.

Separated from the page/visual assembly so the schema-bearing files —
`report.json`, `definition.pbir`, `.platform` — sit together and are easy
to check against a known-good project.
"""

import json

from app.report import pbir_schemas as S
from app.util.ids import lineage_tag


def _platform(display_name: str) -> str:
    return json.dumps({
        "$schema": S.PLATFORM,
        "metadata": {"type": "Report", "displayName": display_name},
        # A real logical id — an empty string makes Fabric Git reject the item.
        "config": {"version": "2.0", "logicalId": lineage_tag(f"report:{display_name}")},
    }, indent=2)


def _pbir(model_path: str) -> str:
    return json.dumps({
        "$schema": S.REPORT_DEFINITION_PROPERTIES,
        "version": "4.0",
        "datasetReference": {"byPath": {"path": model_path}},
    }, indent=2)


def _report_json() -> str:
    """The report root, including the theme resource it references."""
    return json.dumps({
        "$schema": S.REPORT,
        "themeCollection": {
            "baseTheme": {
                "name": S.BASE_THEME_NAME,
                "reportVersionAtImport": S.THEME_VERSIONS,
                "type": "SharedResources",
            }
        },
        "objects": {
            "section": [{
                "properties": {
                    "verticalAlignment": {"expr": {"Literal": {"Value": "'Top'"}}}
                }
            }]
        },
        "resourcePackages": [{
            "name": "SharedResources",
            "type": "SharedResources",
            "items": [{
                "name": S.BASE_THEME_NAME,
                "path": S.BASE_THEME_PATH,
                "type": "BaseTheme",
            }],
        }],
        "settings": {
            "useStylableVisualContainerHeader": True,
            "exportDataMode": "AllowSummarized",
            "defaultDrillFilterOtherVisuals": True,
            "allowChangeFilterTypes": True,
            "useEnhancedTooltips": True,
            "useDefaultAggregateDisplayName": True,
        },
    }, indent=2)
