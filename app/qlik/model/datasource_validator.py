"""Generic Datasource & Connection Validator.

Detects unresolved placeholder tokens (such as <<server>>, <<database>>,
<<schema>>, <<warehouse>>, {{...}}, etc.) in datasource connection metadata
and M queries before deployment.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

PLACEHOLDER_REGEX = re.compile(
    r"(<<\s*[A-Za-z0-9_\-]+\s*>>|\{\{\s*[A-Za-z0-9_\-]+\s*\}\}|<[A-Za-z0-9_\-]+>)",
    re.IGNORECASE,
)

SUSPICIOUS_TOKENS = {
    "<<server>>", "<<database>>", "<<schema>>", "<<warehouse>>",
    "<<endpoint>>", "<<host>>", "{{server}}", "{{database}}",
    "<server>", "<database>", "your_server_here", "replace_me",
}


def find_placeholders_in_text(content: str) -> List[str]:
    """Find any placeholder tokens like <<server>> or {{server}} in a string."""
    if not content:
        return []
    matches = [m.group(1).strip() for m in PLACEHOLDER_REGEX.finditer(content)]
    # Also check exact tokens
    content_lower = content.lower()
    for tok in SUSPICIOUS_TOKENS:
        if tok in content_lower and tok not in [m.lower() for m in matches]:
            matches.append(tok)
    return list(dict.fromkeys(matches))


def validate_m_query_placeholders(m_query: str, table_name: str = "") -> List[Dict[str, Any]]:
    """Scan M query for unresolved placeholders."""
    if not m_query:
        return []

    placeholders = find_placeholders_in_text(m_query)
    issues: List[Dict[str, Any]] = []
    for ph in placeholders:
        issues.append({
            "category": "datasource",
            "severity": "error",
            "status": "unresolved",
            "table": table_name,
            "object": f"Table: {table_name}" if table_name else "M Query",
            "location": f"M partition query ({table_name})" if table_name else "M Query",
            "reason": f"Unresolved placeholder '{ph}' in M query will break Fabric/Power BI connection",
            "details": f"M query contains unresolved placeholder '{ph}'",
            "placeholder": ph,
            "action": "Supply valid datasource connection parameters in the mapping source metadata",
        })
    return issues


def validate_connection_details(conn: Dict[str, Any], table_name: str = "") -> List[Dict[str, Any]]:
    """Scan connection dictionary for unresolved placeholders."""
    if not conn or not isinstance(conn, dict):
        return []

    issues: List[Dict[str, Any]] = []
    for key, val in conn.items():
        if isinstance(val, str):
            placeholders = find_placeholders_in_text(val)
            for ph in placeholders:
                issues.append({
                    "category": "datasource",
                    "severity": "error",
                    "status": "unresolved",
                    "table": table_name,
                    "object": f"Connection parameter '{key}'",
                    "location": f"table '{table_name}'.connection.{key}",
                    "reason": f"Datasource connection parameter '{key}' contains unresolved placeholder '{ph}'",
                    "details": f"Required datasource parameter '{key}' is unresolved: '{ph}'",
                    "placeholder": ph,
                    "action": f"Provide valid {key} in mapping connection details",
                })
    return issues


def validate_all_datasources(mapping: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]]]:
    """
    Validate all datasource metadata and M queries in the mapping payload.
    Returns: (is_valid, issues)
    """
    issues: List[Dict[str, Any]] = []
    tables = mapping.get("tables") or []
    if isinstance(tables, dict):
        tables = list(tables.values())

    for tbl in tables:
        if not isinstance(tbl, dict):
            continue
        tname = str(tbl.get("name") or tbl.get("table_name") or "Table")

        # 1. Validate connection details
        conn = tbl.get("connection_details") or tbl.get("connection") or {}
        if isinstance(conn, dict):
            issues.extend(validate_connection_details(conn, tname))

        # 2. Validate M query
        m_query = tbl.get("m_query") or tbl.get("mquery") or tbl.get("power_query")
        if isinstance(m_query, list):
            m_text = "\n".join(str(step.get("content") or step.get("text") or step) for step in m_query)
        elif isinstance(m_query, str):
            m_text = m_query
        else:
            m_text = ""

        if m_text:
            issues.extend(validate_m_query_placeholders(m_text, tname))

    has_errors = any(i.get("severity") == "error" for i in issues)
    return (not has_errors, issues)
