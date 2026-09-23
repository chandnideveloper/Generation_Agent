"""Structural validation of a generated package.

Not a JSON-Schema validator — it checks the things that actually stop Power BI
Desktop and Fabric from opening a project, each one learned from a real
failure:

  * a required file missing (`version.json`, the theme resource, `.platform`)
  * a wrong `$schema` version (report 1.0.0 instead of 3.2.0, and so on)
  * a `.platform` with an empty `logicalId`
  * a report resource package pointing at a file that was never written
  * a visual with no `visualType`, or positioned off the canvas
  * a relationship endpoint that resolves to nothing

Run it before shipping a package, and in the test suite.
"""

import json
import re
from typing import Any, Dict, List

from app.qlik.config import config
from app.qlik.report import pbir_schemas as S

# A pure Power BI number pattern (##0, #,##0.00, $#,##0, 0.00%, ...) vs a
# pure date/time pattern (YYYY-MM-DD, yyyy-mm-dd hh:nn:ss, General Date).
# Anything containing letters outside these small alphabets is a text label
# and shouldn't be judged against either shape.
_NUMBER_FORMAT_RE = re.compile(r"^[#0.,%$ -]+$")
_DATE_FORMAT_RE = re.compile(r"(?i)^[ydhmns:/\- ]+$|^general date$")

# Power Query type literal -> the TMDL dataType it corresponds to, for
# cross-checking a table's M partition against its own column declarations.
_M_TYPE_TO_TMDL = {
    "type datetime": "dateTime", "type date": "dateTime",
    "type number": "double", "Int64.Type": "int64",
    "type text": "string", "type logical": "boolean",
}

# path suffix -> the $schema it must declare
EXPECTED_SCHEMA = {
    "definition/report.json": S.REPORT,
    "definition/version.json": S.VERSION_METADATA,
    "definition/pages/pages.json": S.PAGES_METADATA,
    "definition.pbir": S.REPORT_DEFINITION_PROPERTIES,
    "definition.pbism": S.MODEL_DEFINITION_PROPERTIES,
}


def _load(package: Dict[str, str], path: str) -> Any:
    try:
        return json.loads(package[path])
    except (KeyError, ValueError):
        return None


def validate(package: Dict[str, str], app_name: str) -> Dict[str, Any]:
    """Return {"ok": bool, "errors": [...], "warnings": [...]}."""
    errors: List[str] = []
    warnings: List[str] = []

    model_root = f"{app_name}.SemanticModel"
    report_root = f"{app_name}.Report"
    has_report = any(path.startswith(report_root) for path in package)

    required = [
        f"{model_root}/definition/model.tmdl",
        f"{model_root}/definition/database.tmdl",
        f"{model_root}/definition.pbism",
        f"{model_root}/.platform",
    ]
    if has_report:
        required += [
            f"{report_root}/definition/report.json",
            f"{report_root}/definition/version.json",
            f"{report_root}/definition/pages/pages.json",
            f"{report_root}/definition.pbir",
            f"{report_root}/.platform",
            f"{app_name}.pbip",
        ]
    for path in required:
        if path not in package:
            errors.append(f"missing required file: {path}")

    # $schema versions
    for suffix, expected in EXPECTED_SCHEMA.items():
        for path in package:
            if not path.endswith(suffix):
                continue
            document = _load(package, path)
            if not isinstance(document, dict):
                continue
            declared = document.get("$schema")
            if declared != expected:
                errors.append(
                    f"{path}: $schema is {declared or 'missing'}, expected {expected}"
                )

    # .platform logical ids
    for path in package:
        if not path.endswith(".platform"):
            continue
        document = _load(package, path) or {}
        if not document.get("config", {}).get("logicalId"):
            errors.append(f"{path}: logicalId is empty")
        if not document.get("metadata", {}).get("displayName"):
            errors.append(f"{path}: displayName is empty")

    if has_report:
        errors.extend(_validate_report(package, report_root))

    # Relationships must never reference an unresolved endpoint.
    relationships = package.get(f"{model_root}/definition/relationships.tmdl", "")
    for line in relationships.splitlines():
        if "Column:" in line and ("''." in line or line.strip().endswith(".")):
            errors.append(f"relationships.tmdl: unresolved endpoint — {line.strip()}")

    # Every table needs a non-empty partition or the model will not load.
    for path, content in package.items():
        if "/definition/tables/" not in path:
            continue
        if "partition " not in content:
            errors.append(f"{path}: no partition")
        elif not content.split("source =", 1)[-1].strip():
            errors.append(f"{path}: empty partition source")

    errors.extend(_validate_column_types(package))

    return {"ok": not errors, "errors": errors, "warnings": warnings}


_COLUMN_BLOCK_RE = re.compile(
    r"\n\tcolumn (?P<name>\S+)\n(?P<body>(?:\t\t.*\n?)*)"
)


def _validate_column_types(package: Dict[str, str]) -> List[str]:
    """Catch a dataType that contradicts its own formatString or the M cast
    that loads it — the exact shape of bug that lets Power BI Desktop open a
    package fine but throw "Something's wrong with one or more fields" (or
    silently null out the column) the moment the query refreshes, which none
    of the structural checks above would ever see."""
    errors: List[str] = []
    for path, content in package.items():
        if "/definition/tables/" not in path or not path.endswith(".tmdl"):
            continue

        m_casts: Dict[str, str] = {}
        cast_match = re.search(r"Table\.TransformColumnTypes\([^,]+,\s*\{(.+?)\}\)", content, re.S)
        if cast_match:
            for col, m_type in re.findall(r'\{"([^"]+)",\s*([^}]+?)\}', cast_match.group(1)):
                m_casts[col] = m_type.strip()

        for block in _COLUMN_BLOCK_RE.finditer(content):
            name = block.group("name").strip("'")
            body = block.group("body")
            dtype_m = re.search(r"dataType:\s*(\S+)", body)
            fmt_m = re.search(r"formatString:\s*(.+)", body)
            data_type = dtype_m.group(1) if dtype_m else None
            fmt = fmt_m.group(1).strip() if fmt_m else None

            if data_type and fmt:
                if data_type == "dateTime" and _NUMBER_FORMAT_RE.match(fmt):
                    errors.append(
                        f"{path}: column {name} is dataType dateTime but formatString "
                        f"'{fmt}' is a number pattern — this column doesn't hold real dates"
                    )
                elif data_type in ("string", "boolean") and (_NUMBER_FORMAT_RE.match(fmt) or _DATE_FORMAT_RE.match(fmt)):
                    errors.append(
                        f"{path}: column {name} is dataType {data_type} but formatString "
                        f"'{fmt}' is a {'date' if _DATE_FORMAT_RE.match(fmt) else 'number'} pattern"
                    )

            m_type = m_casts.get(name)
            if m_type and data_type:
                expected = _M_TYPE_TO_TMDL.get(m_type)
                if expected and expected != data_type:
                    errors.append(
                        f"{path}: column {name} is dataType {data_type} in TMDL but the M "
                        f"partition casts it to {m_type} ({expected}) — refresh will corrupt this column"
                    )

    return errors


def _validate_report(package: Dict[str, str], report_root: str) -> List[str]:
    errors: List[str] = []

    version = _load(package, f"{report_root}/definition/version.json") or {}
    if version.get("version") != S.REPORT_VERSION:
        errors.append(
            f"version.json declares {version.get('version')}, expected {S.REPORT_VERSION}"
        )

    # A declared resource package must actually ship its file.
    report = _load(package, f"{report_root}/definition/report.json") or {}
    for resource in report.get("resourcePackages", []):
        for item in resource.get("items", []):
            expected = f"{report_root}/StaticResources/{resource.get('name')}/{item.get('path')}"
            if expected not in package:
                errors.append(f"report.json references a missing resource: {expected}")

    pages = _load(package, f"{report_root}/definition/pages/pages.json") or {}
    on_disk = {
        path.split("/pages/")[1].split("/")[0]
        for path in package
        if "/pages/" in path and path.endswith("page.json")
    }
    declared = set(pages.get("pageOrder") or [])
    if declared != on_disk:
        errors.append(f"pages.json lists {sorted(declared)}, on disk {sorted(on_disk)}")
    if on_disk and pages.get("activePageName") not in on_disk:
        errors.append("pages.json activePageName is not one of the pages")

    page_dims = {}
    for path in package:
        if "/pages/" in path and path.endswith("page.json"):
            page_name = path.split("/pages/")[1].split("/")[0]
            p_doc = _load(package, path) or {}
            pw = p_doc.get("width") or config.CANVAS_WIDTH
            ph = p_doc.get("height") or config.CANVAS_HEIGHT
            page_dims[page_name] = (pw, ph)

    for path, content in package.items():
        if not path.endswith("visual.json"):
            continue
        document = _load(package, path) or {}
        if document.get("$schema") != S.VISUAL_CONTAINER:
            errors.append(f"{path}: wrong visualContainer schema")
        if not document.get("visual", {}).get("visualType"):
            errors.append(f"{path}: no visualType")
        position = document.get("position") or {}
        page_name = path.split("/pages/")[1].split("/")[0] if "/pages/" in path else None
        max_w, max_h = page_dims.get(page_name, (config.CANVAS_WIDTH, config.CANVAS_HEIGHT))
        if position.get("x", 0) + position.get("width", 0) > max_w:
            errors.append(f"{path}: extends past the right edge of the canvas")
        if position.get("y", 0) + position.get("height", 0) > max_h:
            errors.append(f"{path}: extends past the bottom of the canvas")
        # `config` is the pre-PBIR form; emitting it alongside `visual` is invalid.
        if "config" in document:
            errors.append(f"{path}: carries a legacy `config` string")

    return errors
