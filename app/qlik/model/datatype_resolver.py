"""Generic Datatype Resolver across Mapping, M Queries, and TMDL.

Reconciles:
    Mapping Datatype <---> M Partition Casts <---> TMDL Schema DataType

Ensures no validation mismatch between M Table.TransformColumnTypes and TMDL dataType,
without any hardcoded table or column names.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from app.qlik.model.datatypes import to_tmdl_type

M_TO_TMDL_TYPE_MAP = {
    "int64.type": "int64",
    "type text": "string",
    "text": "string",
    "type datetime": "dateTime",
    "datetime.type": "dateTime",
    "type date": "dateTime",
    "date.type": "dateTime",
    "type number": "double",
    "number": "double",
    "currency.type": "decimal",
    "percentage.type": "double",
    "type logical": "boolean",
    "logical.type": "boolean",
    "type binary": "binary",
    "binary.type": "binary",
}

TMDL_TO_M_TYPE_MAP = {
    "string": "type text",
    "dateTime": "type datetime",
    "int64": "Int64.Type",
    "double": "type number",
    "decimal": "Currency.Type",
    "boolean": "type logical",
    "binary": "type binary",
}


def extract_m_column_types(m_query: str) -> Dict[str, str]:
    """
    Parse column types from Table.TransformColumnTypes in M query.
    Returns: {column_name_lower: m_type_string}
    """
    if not m_query or "TransformColumnTypes" not in m_query:
        return {}

    types_dict: Dict[str, str] = {}
    # Matches patterns like: {"COL_NAME", Int64.Type} or {"COL_NAME", type text}
    # inside Table.TransformColumnTypes(..., {{...}, {...}})
    pattern = re.compile(r'\{\s*"([^"]+)"\s*,\s*([A-Za-z0-9_\.]+)\s*\}')
    for match in pattern.finditer(m_query):
        col_name = match.group(1).strip()
        m_type = match.group(2).strip()
        types_dict[col_name.lower()] = m_type

    return types_dict


def normalize_m_type_to_tmdl(m_type: str) -> Optional[str]:
    """Convert an M type token to TMDL data type."""
    if not m_type:
        return None
    cleaned = m_type.lower().strip()
    return M_TO_TMDL_TYPE_MAP.get(cleaned)


def resolve_column_datatype(
    col_name: str,
    column_meta: Dict[str, Any],
    m_types: Optional[Dict[str, str]] = None,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Dynamically resolve the authoritative datatype for a column.
    
    1. Read mapping's declared datatype (fabric_datatype, data_type, qlik_datatype).
    2. Read M query's cast type for this column (if present in TransformColumnTypes).
    3. Reconcile so TMDL matches M query partition output type, preventing Fabric validation errors.
    4. Return (resolved_tmdl_type, issue_or_none).
    """
    raw_mapping_type = (
        column_meta.get("fabric_datatype")
        or column_meta.get("data_type")
        or column_meta.get("qlik_datatype")
        or column_meta.get("dataType")
        or column_meta.get("type")
    )
    mapping_tmdl_type = to_tmdl_type(raw_mapping_type, col_name=col_name)

    m_cast = (m_types or {}).get(col_name.lower())
    m_tmdl_type = normalize_m_type_to_tmdl(m_cast) if m_cast else None

    # If M explicitly casts the column to a specific type, align TMDL's dataType to M's cast
    if m_tmdl_type and m_tmdl_type != mapping_tmdl_type:
        issue = {
            "category": "datatype",
            "column": col_name,
            "mapping_type": mapping_tmdl_type,
            "m_type": m_cast,
            "resolved_type": m_tmdl_type,
            "action": f"Aligned TMDL dataType to M partition cast '{m_tmdl_type}' (was mapping '{mapping_tmdl_type}').",
        }
        return m_tmdl_type, issue

    return mapping_tmdl_type, None


def reconcile_table_datatypes(
    columns: List[Dict[str, Any]],
    m_query: str,
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """
    Reconcile all columns in a table against its M query.
    Returns:
        resolved_types: {col_name_lower: resolved_tmdl_type}
        issues: list of conflicts resolved or detected
    """
    m_types = extract_m_column_types(m_query)
    resolved_types: Dict[str, str] = {}
    issues: List[Dict[str, Any]] = []

    for col in columns:
        if not isinstance(col, dict):
            continue
        cname = (
            col.get("fabric_column_name")
            or col.get("qlik_column_name")
            or col.get("name")
            or col.get("Name")
            or ""
        ).strip()
        if not cname:
            continue

        resolved_type, issue = resolve_column_datatype(cname, col, m_types)
        resolved_types[cname.lower()] = resolved_type
        if issue:
            issues.append(issue)

    return resolved_types, issues
