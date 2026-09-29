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

from app.qlik.util.ids import lineage_tag
from app.qlik.util.payload import as_dict, text

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
    if name.startswith("id_") or name.startswith("key_") or name.startswith("pk_") or name.startswith("fk_") or name.startswith("id") or name.startswith("key"):
        return True
    if name.endswith("_code") or name.endswith("_no") or name.startswith("code_") or name.startswith("no_"):
        return True
    if name.endswith("_date") or name.endswith("date") or name.startswith("date_"):
        return True
    return False


def _endpoints(relationship: Dict[str, Any]) -> Tuple[str, str, str, str]:
    pbi = as_dict(relationship.get("power_bi") or relationship.get("powerbi"))
    fabric = as_dict(relationship.get("fabric"))
    qlik = as_dict(relationship.get("qlik"))

    from_table = (
        relationship.get("from_table")
        or relationship.get("fromTable")
        or relationship.get("source_table")
        or relationship.get("sourceTable")
        or pbi.get("fromTable")
        or pbi.get("from_table")
        or pbi.get("sourceTable")
        or pbi.get("source_table")
        or fabric.get("from_table")
        or fabric.get("fromTable")
        or fabric.get("source_table")
        or fabric.get("sourceTable")
        or qlik.get("from_table")
        or qlik.get("fromTable")
        or qlik.get("source_table")
        or qlik.get("sourceTable")
    )
    from_column = (
        relationship.get("from_column")
        or relationship.get("fromColumn")
        or relationship.get("source_column")
        or relationship.get("sourceColumn")
        or pbi.get("fromColumn")
        or pbi.get("from_column")
        or pbi.get("sourceColumn")
        or pbi.get("source_column")
        or fabric.get("from_column")
        or fabric.get("fromColumn")
        or fabric.get("source_column")
        or fabric.get("sourceColumn")
        or qlik.get("from_column")
        or qlik.get("fromColumn")
        or qlik.get("source_column")
        or qlik.get("sourceColumn")
    )
    to_table = (
        relationship.get("to_table")
        or relationship.get("toTable")
        or relationship.get("target_table")
        or relationship.get("targetTable")
        or pbi.get("toTable")
        or pbi.get("to_table")
        or pbi.get("targetTable")
        or pbi.get("target_table")
        or fabric.get("to_table")
        or fabric.get("toTable")
        or fabric.get("target_table")
        or fabric.get("targetTable")
        or qlik.get("to_table")
        or qlik.get("toTable")
        or qlik.get("target_table")
        or qlik.get("targetTable")
    )
    to_column = (
        relationship.get("to_column")
        or relationship.get("toColumn")
        or relationship.get("target_column")
        or relationship.get("targetColumn")
        or pbi.get("toColumn")
        or pbi.get("to_column")
        or pbi.get("targetColumn")
        or pbi.get("target_column")
        or fabric.get("to_column")
        or fabric.get("toColumn")
        or fabric.get("target_column")
        or fabric.get("targetColumn")
        or qlik.get("to_column")
        or qlik.get("toColumn")
        or qlik.get("target_column")
        or qlik.get("targetColumn")
    )

    # Fallback to join_conditions if columns are still missing
    if not from_column or not to_column:
        joins = (
            relationship.get("join_conditions")
            or qlik.get("join_conditions")
            or pbi.get("join_conditions")
            or fabric.get("join_conditions")
            or []
        )
        if isinstance(joins, list) and joins and isinstance(joins[0], dict):
            left = str(joins[0].get("left", "")).strip()
            right = str(joins[0].get("right", "")).strip()
            if not from_table and "." in left:
                from_table = left.split(".")[0].strip()
            if not to_table and "." in right:
                to_table = right.split(".")[0].strip()
            if not from_column and left:
                from_column = left.split(".")[-1].strip()
            if not to_column and right:
                to_column = right.split(".")[-1].strip()

    # Clean qualified names if present (e.g. "COURSES.COURSE_ID" -> "COURSE_ID")
    if from_column and "." in str(from_column):
        from_column = str(from_column).split(".")[-1].strip()
    if to_column and "." in str(to_column):
        to_column = str(to_column).split(".")[-1].strip()

    return (
        text(from_table),
        text(from_column),
        text(to_table),
        text(to_column),
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


def _normalize_entity_name(raw_name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw_name)
    s = s.lower().strip()
    s = re.sub(r"^(tbl_|dim_|fact_|f_|d_|v_|vw_|stg_|ods_|hub_|sat_|raw_|clean_|mst_|lu_|ref_|lookup_|master_)", "", s)
    s = re.sub(r"(_dim|_fact|_tbl|_table|_raw|_staging|_v|_view|_mst|_lookup|_master|_ref)$", "", s)
    return s.strip("_")


def _get_word_roots(word: str) -> Set[str]:
    w = word.lower().strip()
    roots = {w}
    if w.endswith("ies") and len(w) > 3:
        roots.add(w[:-3] + "y")
    if w.endswith("ses") and len(w) > 3:
        roots.add(w[:-1])  # courses -> course, expenses -> expense
        roots.add(w[:-2])  # statuses -> status
    if w.endswith(("xes", "shes", "ches")) and len(w) > 3:
        roots.add(w[:-2])
    if w.endswith("s") and not w.endswith("ss") and len(w) > 1:
        roots.add(w[:-1])
    return roots


def _is_primary_key_for_table(
    table_name: str, col_name: str, key_columns: Set[Tuple[str, str]]
) -> bool:
    """Dynamically determine if col_name is the Primary Key of table_name.
    
    Generalized across any domain, schema, or reporting source:
    1. Explicit metadata annotations (`key_columns`).
    2. Single-entity tables: entity-name matching with full pluralization/singularization roots.
    3. Multi-entity / junction tables (e.g. Student_Courses, Course_Instructors): 
       individual entity foreign keys are NOT unique primary keys of this table.
    4. General PK column conventions ('id', 'key', 'code', 'date', 'no', 'num') on single-entity tables.
    """
    if (table_name, col_name) in key_columns:
        return True

    norm_table = _normalize_entity_name(table_name)
    parts = [p for p in re.split(r"[_\s-]+", norm_table) if p]
    if not parts:
        return False

    c = col_name.lower().strip()
    if "." in c:
        c = c.split(".")[-1].strip()

    is_multi_entity = len(parts) > 1

    if not is_multi_entity:
        entity = parts[0]
        roots = _get_word_roots(entity)
        candidate_keys = {"id", "key", "code", "date", "no", "number", "guid", "uuid"}
        for r in roots:
            candidate_keys.update({
                f"{r}_id", f"{r}id", f"id_{r}", f"id{r}",
                f"{r}_key", f"{r}key", f"key_{r}", f"key{r}",
                f"{r}_code", f"{r}code", f"code_{r}", f"code{r}",
                f"{r}_no", f"{r}no", f"no_{r}", f"no{r}",
                f"{r}_num", f"{r}num", f"num_{r}", f"num{r}",
                f"{r}_number", f"{r}number",
                f"{r}_date", f"{r}date", f"date_{r}", f"date{r}",
                f"{r}_guid", f"{r}guid",
            })
        if c in candidate_keys:
            return True

        if entity in ("calendar", "date", "dates", "dimdate", "time"):
            if c.endswith("date") or c in ("date", "datekey", "date_key", "day", "id", "key"):
                return True

        for r in roots:
            if (c.startswith(r) or c.endswith(r)) and any(s in c for s in ("id", "key", "code", "no", "num", "date")):
                return True
    else:
        import itertools
        all_part_roots = [list(_get_word_roots(p)) for p in parts]
        for combo in itertools.product(*all_part_roots):
            comb = "_".join(combo)
            comb_nospace = "".join(combo)
            for c_name in (comb, comb_nospace):
                for sfx in ("_id", "id", "_key", "key", "_code", "code"):
                    if c in (f"{c_name}{sfx}", f"id_{c_name}", f"key_{c_name}"):
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

        pbi = as_dict(relationship.get("power_bi") or relationship.get("powerbi"))
        fabric = as_dict(relationship.get("fabric"))
        qlik = as_dict(relationship.get("qlik"))

        explicit_cf = (
            pbi.get("crossFilterDirection")
            or pbi.get("crossFilteringBehavior")
            or fabric.get("crossFilterDirection")
            or fabric.get("crossFilteringBehavior")
            or relationship.get("crossFilterDirection")
            or relationship.get("crossFilteringBehavior")
        )
        explicit_card = (
            pbi.get("cardinality")
            or fabric.get("cardinality")
            or qlik.get("cardinality")
            or relationship.get("cardinality")
        )
        explicit_is_active = (
            pbi.get("isActive")
            if pbi.get("isActive") is not None
            else fabric.get("isActive")
            if fabric.get("isActive") is not None
            else relationship.get("isActive")
        )

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
        elif explicit_card and str(explicit_card).lower().replace("_", "").replace("-", "") in ("manytomany", "nn"):
            flip = False
            cross_filter = "bothDirections"
        else:
            # Fact-to-Fact relationship where NEITHER table has this column as PK
            # (e.g. COURSE_INSTRUCTORS.COURSE_ID <-> STUDENT_COURSES.COURSE_ID).
            # In star schema, both tables already connect to the shared dimension (COURSES).
            # Direct Many-to-One links between two fact tables cause duplicate-key errors in Power BI.
            skipped.append({
                "relationship": f"{from_table}.{from_column} -> {to_table}.{to_column}",
                "reason": "synthetic peer-to-peer fact relationship omitted in star schema to prevent duplicate key errors",
            })
            continue

        if explicit_cf:
            cf_clean = str(explicit_cf).lower()
            if cf_clean in ("both", "bothdirections"):
                cross_filter = "bothDirections"
            elif cf_clean in ("single", "onedirection"):
                cross_filter = None

        if flip:
            from_table, from_column, to_table, to_column = (
                to_table, to_column, from_table, from_column
            )

        key = (from_table, from_column, to_table, to_column)
        if key in seen:
            continue
        seen.add(key)

        is_active = True
        if explicit_is_active is False:
            is_active = False
        elif _is_connected(active_graph, from_table, to_table):
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

