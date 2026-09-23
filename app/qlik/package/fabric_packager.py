"""Fabric item definition parts.

The Fabric items API takes each file as a base64 `InlineBase64` part, with
paths relative to the *item* root — so the `<App>.SemanticModel/` and
`<App>.Report/` prefixes used on disk must be stripped, and the two items are
created separately.
"""

import base64
from typing import Any, Dict, List

from app.qlik.util.payload import text


def _part(path: str, content: str) -> Dict[str, str]:
    return {
        "path": path,
        "payload": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "payloadType": "InlineBase64",
    }


def to_parts(files: Dict[str, str]) -> List[Dict[str, str]]:
    """Item-relative definition parts, sorted for reproducibility."""
    return [_part(path, files[path]) for path in sorted(files)]


def split_items(package: Dict[str, str], app_name: str) -> Dict[str, List[Dict[str, str]]]:
    """Split a PBIP package into per-item part lists.

    Returns {"semantic_model": [...], "report": [...]} with the folder prefix
    removed from every path.
    """
    model_prefix = f"{app_name}.SemanticModel/"
    report_prefix = f"{app_name}.Report/"

    model: Dict[str, str] = {}
    report: Dict[str, str] = {}

    for path, content in package.items():
        if path.startswith(model_prefix):
            model[path[len(model_prefix):]] = content
        elif path.startswith(report_prefix):
            report[path[len(report_prefix):]] = content

    items: Dict[str, List[Dict[str, str]]] = {}
    if model:
        items["semantic_model"] = to_parts(model)
    if report:
        items["report"] = to_parts(report)
    return items


def report_definition_with_binding(
    parts: List[Dict[str, str]], dataset_id: str
) -> List[Dict[str, str]]:
    """Rewrite definition.pbir to bind the report to a deployed model by id.

    On disk the report points at the model by relative path, which is what
    Desktop needs. In a Fabric workspace the model is an item, so the binding
    has to be `byConnection`/`byPath` replaced with the created dataset id.
    """
    rebound: List[Dict[str, str]] = []
    for part in parts:
        if part["path"] != "definition.pbir":
            rebound.append(part)
            continue
        pbir = (
            '{\n  "version": "4.0",\n'
            '  "datasetReference": {\n'
            f'    "byConnection": {{\n'
            f'      "connectionString": null,\n'
            f'      "pbiServiceModelId": null,\n'
            f'      "pbiModelVirtualServerName": "sobe_wowvirtualserver",\n'
            f'      "pbiModelDatabaseName": "{text(dataset_id)}",\n'
            f'      "name": "EntityDataSource",\n'
            f'      "connectionType": "pbiServiceXmlaStyleLive"\n'
            "    }\n  }\n}"
        )
        rebound.append(_part("definition.pbir", pbir))
    return rebound


def summarize(items: Dict[str, List[Dict[str, str]]]) -> Dict[str, Any]:
    return {name: len(parts) for name, parts in items.items()}
