"""
Build TMDL role files for Row-Level Security (RLS) in Power BI Semantic Model.
Ported and adapted from vl-q2f-mapping/src/converters/tmdl_builder.py and vl-q2f-report-generation.
"""
from typing import Any, Dict, List, Optional
import re

INDENT = "\t"


def _quote_tmdl_role_name(name: str) -> str:
    """Single-quote TMDL role or table names if they contain special characters."""
    text = str(name or "").strip()
    if re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]*$", text):
        return text
    safe = text.replace("'", "''")
    return f"'{safe}'"


def build_role_tmdl(role: Dict[str, Any]) -> Optional[str]:
    """
    Build a TMDL role block from a role dictionary.

    Expected input structure:
    {
        "name": "Country Security",
        "table_permissions": [
            {"table": "Sales", "filter": "'Sales'[Country] = USERPRINCIPALNAME()"}
        ],
        "members": ["user@domain.com"]  # optional
    }
    """
    if not isinstance(role, dict):
        return None

    name = role.get("name") or "Unnamed Role"
    table_permissions = role.get("table_permissions") or []
    members = role.get("members") or []

    lines = [
        f"role {_quote_tmdl_role_name(name)}",
        f"{INDENT}modelPermission: read",
    ]

    for perm in table_permissions:
        if isinstance(perm, dict):
            table = perm.get("table") or perm.get("table_name")
            expr = perm.get("filter") or perm.get("dax_filter")
            if table and expr:
                lines.append("")
                lines.append(f"{INDENT}tablePermission {_quote_tmdl_role_name(table)} = {expr}")

    for member in members:
        if member:
            lines.append("")
            lines.append(f"{INDENT}member {member}")

    return "\n".join(lines)


def build_roles_tmdl(roles: List[Dict[str, Any]]) -> Optional[str]:
    """Build a combined definition/roles.tmdl file string from a list of roles."""
    if not roles:
        return None

    blocks = []
    for r in roles:
        block = build_role_tmdl(r)
        if block:
            blocks.append(block)

    return "\n\n".join(blocks) if blocks else None
