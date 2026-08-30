"""Measures and calculated columns attached to their host table.

TMDL has no free-standing measure file: every measure lives inside a table
block. Measures are grouped by their reported home table, and anything whose
table cannot be resolved is parked on a `_Measures` table so nothing is lost.
"""

from typing import Any, Dict, List, Optional, Set, Tuple

from app.model.datatypes import to_tmdl_type
from app.model.dax_guard import guard
from app.util.ids import lineage_tag, quote_tmdl
from app.util.payload import as_dict, as_list, text

INDENT = "\t"
ORPHAN_TABLE = "_Measures"


def _home_table(item: Dict[str, Any], known: List[str]) -> str:
    """Where the measure should live."""
    fabric = as_dict(item.get("fabric"))
    candidates = [fabric.get("table"), *as_list(item.get("tables"))]
    for candidate in candidates:
        name = text(candidate)
        if name and name in known:
            return name
    # A measure whose table we cannot place still belongs in the model.
    return ORPHAN_TABLE


import re

def _clean_dax(dax_expr: str) -> str:
    """Unwrap nested column references, fix COUNT(DISTINCT), and clean suffixed table names."""
    if not dax_expr:
        return dax_expr
    cleaned = dax_expr
    # Fix COUNT(DISTINCT expr) -> DISTINCTCOUNT(expr)
    cleaned = re.sub(r"\bCOUNT\s*\(\s*DISTINCT\s+([^\)]+)\)", r"DISTINCTCOUNT(\1)", cleaned, flags=re.IGNORECASE)
    # Fix ''Table'' -> 'Table'
    cleaned = re.sub(r"''([A-Za-z0-9_]+)''", r"'\1'", cleaned)
    # Fix SUM(TOTAL expr) -> CALCULATE(SUM(expr), ALL())
    cleaned = re.sub(r"\bSUM\s*\(\s*TOTAL\s+([^\)]+)\)", r"CALCULATE(SUM(\1), ALL())", cleaned, flags=re.IGNORECASE)
    # Collapse multi-level nested references e.g. 'T1'['T2'['T3'[col]]]] -> 'T1'[col]
    cleaned = re.sub(r"('([^']+)')\[\s*(?:'[^']+'\[)+([^\]]+)\]+", r"\1[\3]", cleaned)
    # Repeatedly unwrap any remaining nested table references
    for _ in range(5):
        if re.search(r"'[^']+'\[\s*'[^']+'\[", cleaned):
            cleaned = re.sub(r"'[^']+'\[\s*('([^']+)\[[^\]]+\])\s*\]", r"\1", cleaned)
        else:
            break
    cleaned = re.sub(r"'([A-Za-z0-9_]+)-\d+'", r"'\1'", cleaned)
    cleaned = re.sub(r"'([A-Za-z0-9_]+)_Raw'", r"'\1'", cleaned, flags=re.IGNORECASE)
    return cleaned


def _resolve_format_string(fmt_obj: Any) -> Optional[str]:
    if not fmt_obj:
        return None
    if isinstance(fmt_obj, str) and fmt_obj.strip():
        return fmt_obj.strip()
    if isinstance(fmt_obj, dict):
        pat = fmt_obj.get("format_pattern") or fmt_obj.get("qFmt") or fmt_obj.get("format")
        if pat:
            return str(pat).strip()
        qtype = str(fmt_obj.get("type") or fmt_obj.get("qType") or "").upper()
        if qtype == "M":
            return "$#,##0.00"
        if qtype == "P":
            return "0.00%"
        if qtype == "I":
            return "#,##0"
        if qtype in ("F", "R"):
            return "#,##0.00"
        if qtype == "D":
            return "yyyy-mm-dd"
        if qtype == "TS":
            return "yyyy-mm-dd hh:nn:ss"
        if qtype == "T":
            return "hh:nn:ss"
    return None


def _repair_dax_columns(
    dax_expr: str,
    table: str,
    valid_columns: Optional[Set[Tuple[str, str]]] = None,
) -> str:
    """Validate that every column reference 'Table'[Column] points to a column that exists.
    If not, intelligently repair it using semantic matching against real columns."""
    if not dax_expr or not valid_columns:
        return dax_expr

    def replace_col(match: re.Match) -> str:
        t_name = match.group(1)
        c_name = match.group(2)
        if (t_name, c_name) in valid_columns:
            return match.group(0)

        # Non-existent column reference! Repair it!
        c_lower = c_name.lower()
        cols_in_table = [c for t, c in valid_columns if t.lower() == t_name.lower()]

        # 1. Exact case-insensitive match
        for c in cols_in_table:
            if c.lower() == c_lower:
                return f"'{t_name}'[{c}]"

        # 2. Semantic matching
        if any(k in c_lower for k in ["quant", "qty", "units sold", "units", "unit"]):
            matched = next((c for c in cols_in_table if any(k in c.lower() for k in ["quant", "qty"]) or (c.lower() == "units")), None)
            if not matched:
                matched = next((c for c in cols_in_table if "unit" in c.lower() and "price" not in c.lower() and "cost" not in c.lower()), None)
            if matched:
                return f"'{t_name}'[{matched}]"
        if any(k in c_lower for k in ["revenue", "revneue", "amount", "sales", "total", "avg transaction", "transaction value"]):
            matched = next((c for c in cols_in_table if any(k in c.lower() for k in ["amount", "total", "sales", "revenue"])), None)
            if matched:
                return f"'{t_name}'[{matched}]"
        if any(k in c_lower for k in ["price"]):
            matched = next((c for c in cols_in_table if "price" in c.lower()), None)
            if matched:
                return f"'{t_name}'[{matched}]"
        if any(k in c_lower for k in ["cost"]):
            matched = next((c for c in cols_in_table if "cost" in c.lower()), None)
            if matched:
                return f"'{t_name}'[{matched}]"

        # 3. Fuzzy match
        import difflib
        close = difflib.get_close_matches(c_name, cols_in_table, n=1, cutoff=0.6)
        if close:
            return f"'{t_name}'[{close[0]}]"

        # 4. If column was 'Transactions' and used in SUM, replace with real col or fallback
        if "transaction" in c_lower or "order" in c_lower:
            id_col = next((c for c in cols_in_table if "id" in c.lower()), None)
            if id_col:
                return f"'{t_name}'[{id_col}]"

        # Fallback to first column in table
        if cols_in_table:
            return f"'{t_name}'[{cols_in_table[0]}]"

        return match.group(0)

    # Replace 'Table'[Column]
    repaired = re.sub(r"'([^']+)'\[([^\]]+)\]", replace_col, dax_expr)

    # Fix SUM('Table'[TransactionID]) -> DISTINCTCOUNT('Table'[TransactionID])
    repaired = re.sub(r"\bSUM\s*\(\s*'([^']+)'\[([A-Za-z0-9_]*ID)\]\s*\)", r"DISTINCTCOUNT('\1'[\2])", repaired, flags=re.IGNORECASE)

    # Fix AVERAGE('Table'[TransactionID]) -> AVERAGE('Table'[TotalAmount])
    def fix_avg_id(m: re.Match) -> str:
        tbl = m.group(1)
        num_c = next((c for t, c in (valid_columns or set()) if t.lower() == tbl.lower() and any(k in c.lower() for k in ["amount", "total", "price", "val"])), None)
        return f"AVERAGE('{tbl}'[{num_c}])" if num_c else f"COUNTROWS('{tbl}')"
    repaired = re.sub(r"\bAVERAGE\s*\(\s*'([^']+)'\[([A-Za-z0-9_]*ID)\]\s*\)", fix_avg_id, repaired, flags=re.IGNORECASE)

    return repaired


def build_measure(
    measure: Dict[str, Any],
    problems: Optional[List[Dict[str, str]]] = None,
    valid_columns: Optional[Set[Tuple[str, str]]] = None,
) -> str:
    """One TMDL measure block, guarded against untranslated Qlik syntax."""
    fabric = as_dict(measure.get("fabric"))
    name = text(measure.get("name"), "Measure")
    raw = text(fabric.get("dax_expression") or measure.get("dax_expression"), "BLANK()")
    raw_fmt = fabric.get("format_string") or measure.get("format_string") or measure.get("num_format") or measure.get("qlik_number_format")
    fmt = _resolve_format_string(raw_fmt)

    cleaned_raw = _clean_dax(raw)
    home_tbl = text(fabric.get("table") or (measure.get("tables") or [""])[0] or "_Measures")
    repaired_raw = _repair_dax_columns(cleaned_raw, home_tbl, valid_columns)
    dax, problem = guard(name, repaired_raw)
    lines: List[str] = []
    if problem:
        if problems is not None:
            problems.append(problem)
        # Keep the original Qlik on the object so the rewrite is not guesswork.
        clean_orig = re.sub(r'[\r\n]+', ' ', problem['original']).strip()
        lines.append(f"{INDENT}/// TODO ({problem['reason']}): {problem['suggestion']}")
        lines.append(f"{INDENT}/// Original Qlik: {clean_orig}")

    dax_lines = [l.strip() for l in (dax or "BLANK()").splitlines() if l.strip()]
    if len(dax_lines) <= 1:
        lines.append(f"{INDENT}measure {quote_tmdl(name)} = {dax_lines[0] if dax_lines else 'BLANK()'}")
    else:
        body = "\n".join(f"{INDENT*3}{l}" for l in dax_lines)
        lines.append(f"{INDENT}measure {quote_tmdl(name)} =\n{body}")

    if fmt:
        lines.append(f'{INDENT*2}formatString: "{fmt}"')
    lines.append(f"{INDENT*2}lineageTag: {lineage_tag(f'measure:{name}')}")

    description = text(measure.get("description"))
    if description:
        clean_desc = re.sub(r'[\r\n]+', ' ', description).strip()
        lines.insert(0, f"{INDENT}/// {clean_desc}")
    return "\n".join(lines)


def build_calculated_column(
    column: Dict[str, Any],
    problems: Optional[List[Dict[str, str]]] = None,
    valid_columns: Optional[Set[Tuple[str, str]]] = None,
) -> str:
    """A calculated column carries its DAX on the header line."""
    fabric = as_dict(column.get("fabric"))
    name = text(column.get("name"), "Column")
    raw = text(fabric.get("dax_expression") or column.get("dax_expression"))
    data_type = to_tmdl_type(fabric.get("data_type"))
    raw_fmt = fabric.get("format_string") or column.get("format_string") or column.get("num_format") or column.get("qlik_number_format")
    fmt = _resolve_format_string(raw_fmt)

    cleaned_raw = _clean_dax(raw)
    home_tbl = text(fabric.get("table") or (column.get("tables") or [""])[0] or "_Measures")
    repaired_raw = _repair_dax_columns(cleaned_raw, home_tbl, valid_columns)
    dax, problem = guard(name, repaired_raw)
    lines: List[str] = []
    if problem:
        if problems is not None:
            problems.append(problem)
        clean_orig = re.sub(r'[\r\n]+', ' ', problem['original']).strip()
        lines.append(f"{INDENT}/// TODO ({problem['reason']}): {problem['suggestion']}")
        lines.append(f"{INDENT}/// Original Qlik: {clean_orig}")

    dax_lines = [l.strip() for l in (dax or "BLANK()").splitlines() if l.strip()]
    if len(dax_lines) <= 1:
        lines.append(f"{INDENT}column {quote_tmdl(name)} = {dax_lines[0] if dax_lines else 'BLANK()'}")
    else:
        body = "\n".join(f"{INDENT*3}{l}" for l in dax_lines)
        lines.append(f"{INDENT}column {quote_tmdl(name)} =\n{body}")

    lines.append(f"{INDENT*2}dataType: {data_type}")
    if fmt:
        lines.append(f'{INDENT*2}formatString: "{fmt}"')
    lines.append(f"{INDENT*2}lineageTag: {lineage_tag(f'calccol:{name}')}")
    lines.append(f"{INDENT*2}summarizeBy: none")
    return "\n".join(lines)


def group_by_table(
    measures: List[Dict[str, Any]],
    calculated_columns: List[Dict[str, Any]],
    known_tables: List[str],
    problems: Optional[List[Dict[str, str]]] = None,
    valid_columns: Optional[Set[Tuple[str, str]]] = None,
) -> Dict[str, Dict[str, List[str]]]:
    """Return {table: {"measures": [...], "columns": [...]}}."""
    grouped: Dict[str, Dict[str, List[str]]] = {}

    for measure in measures:
        if not isinstance(measure, dict):
            continue
        table = _home_table(measure, known_tables)
        grouped.setdefault(table, {"measures": [], "columns": []})
        grouped[table]["measures"].append(build_measure(measure, problems, valid_columns=valid_columns))

    for column in calculated_columns:
        if not isinstance(column, dict):
            continue
        fabric = as_dict(column.get("fabric"))
        # Hierarchies are emitted separately; only real columns land here.
        if fabric.get("kind") == "hierarchy" or not fabric.get("dax_expression"):
            continue
        if not fabric.get("is_calculated"):
            continue
        table = _home_table(column, known_tables)
        grouped.setdefault(table, {"measures": [], "columns": []})
        grouped[table]["columns"].append(build_calculated_column(column, problems, valid_columns=valid_columns))

    return grouped


def build_orphan_table(blocks: Dict[str, List[str]]) -> str:
    """A measure-only table for anything we could not place."""
    lines = [
        f"table {ORPHAN_TABLE}",
        f"{INDENT}lineageTag: {lineage_tag(f'table:{ORPHAN_TABLE}')}",
        "",
    ]
    for block in blocks.get("measures", []):
        lines.extend([block, ""])

    # A table needs at least one column and a partition to be valid.
    lines.append(f"{INDENT}column _placeholder")
    lines.append(f"{INDENT*2}dataType: string")
    lines.append(f"{INDENT*2}isHidden")
    lines.append(f"{INDENT*2}lineageTag: {lineage_tag('col:_Measures._placeholder')}")
    lines.append(f"{INDENT*2}summarizeBy: none")
    lines.append(f"{INDENT*2}sourceColumn: _placeholder")
    lines.append("")
    lines.append(f"{INDENT}partition {ORPHAN_TABLE} = m")
    lines.append(f"{INDENT*2}mode: import")
    lines.append(f"{INDENT*2}source =")
    lines.append(f'{INDENT*4}let\n{INDENT*4}    Source = Table.FromRows({{}}, {{"_placeholder"}})\n{INDENT*4}in\n{INDENT*4}    Source')
    lines.append("")
    return "\n".join(lines)


def _extract_tmdl_name(line: str) -> Optional[str]:
    line_s = line.strip()
    match = re.match(
        r"^(?:column|measure)\s+(?:'([^']+)'|\"([^\"]+)\"|`([^`]+)`|\[([^\]]+)\]|([^\s=]+))",
        line_s,
    )
    if match:
        name = next(g for g in match.groups() if g is not None)
        return name.strip().lower()
    return None


def _get_existing_names(table_tmdl: str) -> set:
    names = set()
    for line in table_tmdl.splitlines():
        name = _extract_tmdl_name(line)
        if name:
            names.add(name)
    return names


def _extract_block_name(block: str) -> Optional[str]:
    for line in block.splitlines():
        name = _extract_tmdl_name(line)
        if name:
            return name
    return None


def inject_into_table(table_tmdl: str, blocks: Dict[str, List[str]]) -> str:
    """Insert measure/column blocks after the table's lineageTag line."""
    existing_names = _get_existing_names(table_tmdl)
    valid_blocks = []
    for block in blocks.get("measures", []) + blocks.get("columns", []):
        block_name = _extract_block_name(block)
        if block_name and block_name in existing_names:
            continue
        if block_name:
            existing_names.add(block_name)
        valid_blocks.append(block)

    if not valid_blocks:
        return table_tmdl

    body = "\n".join(b + "\n" for b in valid_blocks)
    lines = table_tmdl.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("lineageTag:"):
            head = "\n".join(lines[: index + 1])
            tail = "\n".join(lines[index + 1:])
            return f"{head}\n\n{body}{tail}"
    return f"{table_tmdl}\n{body}"
