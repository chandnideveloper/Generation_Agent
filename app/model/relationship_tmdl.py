"""Relationships.tmdl.

Power BI relationships are directional: `fromColumn` is the many side and
`toColumn` the one side. A Qlik "many-to-one" already reads that way, but
"one-to-many" must be flipped or the model loads with the filter pointing the
wrong way.

Relationships whose endpoints do not resolve to real model columns are
skipped and reported — emitting `fromColumn: ''.key` produces a model that
Desktop refuses to open.
"""

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.util.ids import lineage_tag
from app.util.payload import as_dict, text

INDENT = "\t"

# Qlik cardinality -> (flip endpoints, TMDL crossFilteringBehavior)
CARDINALITY = {
    "many-to-one": (False, None),
    "manytoone": (False, None),
    "one-to-many": (True, None),
    "onetomany": (True, None),
    "one-to-one": (False, "bothDirections"),
    "onetoone": (False, "bothDirections"),
    "many-to-many": (False, "bothDirections"),
    "manytomany": (False, "bothDirections"),
}


def _clean_table_name(raw_name: str) -> str:
    name = text(raw_name, "")
    cleaned = re.sub(r"-\d+$", "", name)
    cleaned = re.sub(r"_Raw$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip() or name


def _is_key_column(col_name: str) -> bool:
    name = text(col_name).lower()
    if not name:
        return False
    if name.endswith("_id") or name.endswith("id") or name.endswith("_key") or name.endswith("key"):
        return True
    if name.endswith("_code") or name.endswith("_no"):
        return True
    return False


def _endpoints(relationship: Dict[str, Any]) -> Tuple[str, str, str, str]:
    fabric = as_dict(relationship.get("fabric"))
    return (
        text(relationship.get("source_table") or fabric.get("from_table")),
        text(relationship.get("source_column") or fabric.get("from_column")),
        text(relationship.get("target_table") or fabric.get("to_table")),
        text(relationship.get("target_column") or fabric.get("to_column")),
    )


def _is_connected(graph: Dict[str, Set[str]], u: str, v: str) -> bool:
    if u not in graph or v not in graph:
        return False
    visited = set()
    queue = [u]
    while queue:
        curr = queue.pop(0)
        if curr == v:
            return True
        visited.add(curr)
        for nxt in graph.get(curr, []):
            if nxt not in visited:
                queue.append(nxt)
    return False


def _is_primary_key_for_table(
    table_name: str, col_name: str, key_columns: Set[Tuple[str, str]]
) -> bool:
    if (table_name, col_name) in key_columns:
        return True
    t = table_name.lower().strip()
    if t.endswith("ies"):
        sing = t[:-3] + "y"
    elif t.endswith("ses") or t.endswith("xes") or t.endswith("shes") or t.endswith("ches"):
        sing = t[:-2]
    elif t.endswith("s") and not t.endswith("ss"):
        sing = t[:-1]
    else:
        sing = t

    c = col_name.lower()
    if c == f"{sing}_id" or c == f"{sing}id":
        return True
    if sing == "trip" and c == "trip_id":
        return True
    if sing == "load" and c == "load_id":
        return True
    if sing == "driver" and c == "driver_id":
        return True
    if sing == "truck" and c == "truck_id":
        return True
    if sing == "trailer" and c == "trailer_id":
        return True
    if sing == "customer" and c == "customer_id":
        return True
    if sing == "facility" and c == "facility_id":
        return True
    if sing == "route" and c == "route_id":
        return True
    if sing == "calendar" and c in {"date", "dispatchdate", "date_id"}:
        return True
    return False


def build_relationships(
    relationships: List[Dict[str, Any]],
    valid_columns: Set[Tuple[str, str]],
    key_columns: Optional[Set[Tuple[str, str]]] = None,
) -> Tuple[str, List[Dict[str, str]]]:
    """Return (relationships.tmdl, skipped) — skipped explains each omission."""
    key_columns = key_columns or set()
    blocks: List[str] = []
    skipped: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str, str, str]] = set()
    active_graph: Dict[str, Set[str]] = {}

    for relationship in relationships:
        if not isinstance(relationship, dict):
            continue
        raw_from_table, from_column, raw_to_table, to_column = _endpoints(relationship)

        from_table = _clean_table_name(raw_from_table)
        to_table = _clean_table_name(raw_to_table)

        if not all([from_table, from_column, to_table, to_column]):
            skipped.append({
                "relationship": text(relationship.get("name"), "unnamed"),
                "reason": "endpoints could not be resolved from the mapping payload",
            })
            continue

        if from_table == to_table:
            continue

        if valid_columns and (
            (from_table, from_column) not in valid_columns
            or (to_table, to_column) not in valid_columns
        ):
            skipped.append({
                "relationship": f"{from_table}.{from_column} -> {to_table}.{to_column}",
                "reason": "one or both columns do not exist in the generated tables",
            })
            continue

        # Filter out relationships on non-key columns
        from_is_key = (from_table, from_column) in key_columns or _is_key_column(from_column)
        to_is_key = (to_table, to_column) in key_columns or _is_key_column(to_column)
        if not (from_is_key and to_is_key):
            skipped.append({
                "relationship": f"{from_table}.{from_column} -> {to_table}.{to_column}",
                "reason": "relationship endpoint is not a key column",
            })
            continue

        from_is_pk = _is_primary_key_for_table(from_table, from_column, key_columns)
        to_is_pk = _is_primary_key_for_table(to_table, to_column, key_columns)

        cross_filter = None
        flip = False

        if from_is_pk and not to_is_pk:
            # from_table is the Primary Key (1 side), flip so toColumn is the 1 side
            flip = True
        elif to_is_pk and not from_is_pk:
            # to_table is the Primary Key (1 side), from_table is Many side
            flip = False
        elif from_is_pk and to_is_pk:
            # 1:1 relationship between dimension tables
            flip = False
            cross_filter = "bothDirections"
        else:
            # Fact-to-Fact relationship where NEITHER table has this column as PK
            # (e.g. Trips.truck_id <-> Maintenance.truck_id).
            # In star schema, both tables already connect to the shared dimension (Trucks).
            # Direct links between two fact tables cause duplicate-key errors in Power BI.
            skipped.append({
                "relationship": f"{from_table}.{from_column} -> {to_table}.{to_column}",
                "reason": "synthetic peer-to-peer fact relationship omitted in star schema to prevent duplicate key errors",
            })
            continue

        if flip:
            from_table, from_column, to_table, to_column = (
                to_table, to_column, from_table, from_column
            )

        key = (from_table, from_column, to_table, to_column)
        if key in seen:
            continue
        seen.add(key)

        is_active = True
        if _is_connected(active_graph, from_table, to_table):
            is_active = False
        else:
            active_graph.setdefault(from_table, set()).add(to_table)
            active_graph.setdefault(to_table, set()).add(from_table)

        name = lineage_tag(f"rel:{from_table}.{from_column}->{to_table}.{to_column}")
        block = [
            f"relationship {name}",
            f"{INDENT}fromColumn: {from_table}.{from_column}",
            f"{INDENT}toColumn: {to_table}.{to_column}",
        ]
        if cross_filter:
            block.append(f"{INDENT}crossFilteringBehavior: {cross_filter}")
        if not is_active:
            block.append(f"{INDENT}isActive: false")
        blocks.append("\n".join(block))

    return "\n\n".join(blocks) + ("\n" if blocks else ""), skipped

