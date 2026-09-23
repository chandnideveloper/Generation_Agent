"""Turn the generated file map into a JSON-friendly artifact tree.

The API used to answer with counts only, so a caller had to read the output
folder from disk to see what was produced. This returns everything inline:
TMDL stays as text (it is not JSON), and every `.json`/`.pbip`/`.pbism`/
`.pbir`/`.platform` file is parsed so it appears as real JSON in the response
rather than an escaped string.
"""

import json
from typing import Any, Dict, Tuple

# Files whose content is JSON and should be embedded as objects, not strings.
JSON_SUFFIXES = (".json", ".pbip", ".pbism", ".pbir", ".platform")


def _decode(path: str, content: str) -> Any:
    if not path.endswith(JSON_SUFFIXES):
        return content
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        # Never lose content because it failed to parse.
        return content


def _split_prefix(path: str, app_name: str) -> Tuple[str, str]:
    """Classify a package path into (section, item-relative path)."""
    model_prefix = f"{app_name}.SemanticModel/"
    report_prefix = f"{app_name}.Report/"

    if path.startswith(model_prefix):
        return "semantic_model", path[len(model_prefix):]
    if path.startswith(report_prefix):
        return "report", path[len(report_prefix):]
    if path == f"{app_name}.pbip":
        return "pbip", path
    return "other", path


def build_tree(package: Dict[str, str], app_name: str) -> Dict[str, Any]:
    """Group the package by item, with JSON parsed and TMDL left as text."""
    tree: Dict[str, Any] = {
        "pbip": None,
        "semantic_model": {},
        "report": {},
        "other": {},
    }

    for path in sorted(package):
        section, relative = _split_prefix(path, app_name)
        decoded = _decode(path, package[path])
        if section == "pbip":
            tree["pbip"] = decoded
        else:
            tree[section][relative] = decoded

    return tree


def build_index(package: Dict[str, str], app_name: str) -> Dict[str, Any]:
    """A compact listing: every path with its size, for callers that only
    want to know what exists without carrying the whole payload."""
    index: Dict[str, Any] = {"semantic_model": [], "report": [], "other": []}
    for path in sorted(package):
        section, relative = _split_prefix(path, app_name)
        entry = {"path": relative, "bytes": len(package[path])}
        if section == "pbip":
            index.setdefault("pbip", []).append(entry)
        else:
            index[section].append(entry)
    return index


def measure(package: Dict[str, str]) -> Dict[str, int]:
    return {
        "file_count": len(package),
        "total_bytes": sum(len(content) for content in package.values()),
    }
