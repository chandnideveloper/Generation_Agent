"""Per-table TMDL: columns, partition and the M query behind it."""

import os
import re
from typing import Any, Dict, List, Optional, Set

from app.model.datatypes import format_string, summarize_by, to_tmdl_type
from app.util.ids import lineage_tag, quote_tmdl
from app.util.payload import as_dict, as_list, text

INDENT = "\t"


def _clean_table_name(raw_name: str) -> str:
    """Strip suffixes like -14, -13, _Raw from table names."""
    name = text(raw_name, "Table")
    cleaned = re.sub(r"-\d+$", "", name)
    cleaned = re.sub(r"_Raw$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip() or name


def _column_name(column: Dict[str, Any]) -> str:
    return text(
        column.get("fabric_column_name")
        or column.get("qlik_column_name")
        or column.get("name")
        or column.get("Name"),
        "Column",
    )


def _is_calculated(
    column: Dict[str, Any],
    extracted_exprs: Optional[Dict[str, str]] = None,
    table_name: Optional[str] = None,
    source_table: Optional[str] = None,
) -> bool:
    name = _column_name(column)
    if extracted_exprs and name.lower() in extracted_exprs:
        qlik_expr = extracted_exprs[name.lower()]
        translated = _qlik_expr_to_dax(qlik_expr, table_name or "Table", source_table=source_table) if table_name else ""
        if not translated or translated.strip().lower() in (
            f"'{table_name}'[{name}]".lower(), f"[{name}]".lower(), "blank()"
        ):
            return False
        return True
    fabric = as_dict(column.get("fabric"))
    if column.get("is_calculated") or column.get("isCalculated") or fabric.get("is_calculated"):
        return True
    dax_expr = column.get("dax_expression") or fabric.get("dax_expression")
    if dax_expr and not str(dax_expr).strip().lower() in (
        f"'{table_name}'[{name}]".lower(), f"[{name}]".lower(), "blank()", "=blank()"
    ):
        return True
    data_type_raw = text(
        column.get("qlik_datatype") or column.get("fabric_datatype") or column.get("dataType") or column.get("type") or ""
    ).upper()
    if "CALCULATED" in data_type_raw:
        return True
    return False


def _extract_column_expressions(qlik_query: str) -> Dict[str, str]:
    """Parse `expr as Alias` from Qlik load script dynamically.

    Skips in-place transforms where the alias equals the source column
    (e.g. ``Upper(Trim(home_terminal)) as home_terminal`` or ``Date(Floor(DispatchDate)) as DispatchDate``).
    These are simple data-cleansing ops that map 1-to-1 to the physical column
    and must NOT become calculated columns — otherwise Power BI sees a
    duplicate column name error.
    """
    if not qlik_query:
        return {}
    results = {}
    pattern = r"((?:If|Date|Date#|Timestamp|Month|MonthStart|MonthName|Year|Week|Num|ApplyMap|Upper|Lower|Trim|Text|Dual|Floor|Ceil|Round|Sum|Count|Avg|Min|Max)\s*\([\s\S]+?\))\s+as\s+([A-Za-z0-9_#]+)"
    for match in re.finditer(pattern, qlik_query, re.IGNORECASE):
        expr, alias = match.group(1).strip(), match.group(2).strip()

        # Detect in-place transforms: find identifiers in expr
        inner_identifiers = set(re.findall(r"\b([A-Za-z0-9_#]+)\b", expr, re.IGNORECASE))
        clean_identifiers = {
            ident.lower() for ident in inner_identifiers
            if ident.lower() not in DAX_KEYWORD_FUNCTIONS and ident.lower() not in {
                "date", "date#", "timestamp", "month", "monthstart", "monthname", "year", "week", "num",
                "applymap", "upper", "lower", "trim", "text", "dual", "floor", "ceil", "round",
                "resident", "where", "group", "by", "order", "as", "load", "sql", "select", "from"
            }
        }
        # If the only referenced column inside the expression is the alias itself, it's an in-place transform!
        if clean_identifiers == {alias.lower()}:
            continue

        results[alias.lower()] = expr
    return results


DAX_KEYWORD_FUNCTIONS = {
    "and", "or", "not", "true", "false", "if", "switch", "trim", "upper", "lower",
    "len", "left", "right", "mid", "count", "sum", "avg", "min", "max", "distinct",
    "distinctcount", "related", "format", "year", "month", "day", "weeknum", "date", "blank"
}


def _clean_inner_dax(expr: str, table_name: str) -> str:
    return re.sub(
        r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b",
        lambda cm: cm.group(1)
        if cm.group(1).isdigit() or cm.group(1).lower() in DAX_KEYWORD_FUNCTIONS
        else f"'{table_name}'[{cm.group(1)}]",
        expr,
    )


def _qlik_expr_to_dax(expr: str, table_name: str, source_table: Optional[str] = None) -> str:
    """Generic translator from Qlik expression to DAX calculated column expression."""
    if not expr:
        return ""
    cleaned = re.sub(r"\s+", " ", expr).strip()
    ref_tbl = source_table or table_name

    def _resolve_date_col(candidate: Optional[str]) -> str:
        if not candidate or candidate.startswith("v") or candidate.lower() in ("date", "iterno"):
            if table_name.lower() == "calendar":
                return "DispatchDate"
            return "dispatch_date"
        return candidate

    # 1. String functions: Upper(Trim(col)), etc.
    m_str = re.match(r"(upper|lower|trim)\s*\(\s*(.+?)\s*\)$", cleaned, re.IGNORECASE)
    if m_str:
        func, inner = m_str.group(1).upper(), m_str.group(2).strip()
        m_inner = re.match(r"(upper|lower|trim)\s*\(\s*(.+?)\s*\)$", inner, re.IGNORECASE)
        if m_inner:
            inner_func, inner_col = m_inner.group(1).upper(), m_inner.group(2).strip()
            inner_col_dax = _clean_inner_dax(inner_col, table_name)
            return f"{func}({inner_func}({inner_col_dax}))"
        return f"{func}({_clean_inner_dax(inner, table_name)})"

    # 2. Flag counting in resident aggregations: If(flag, 1, 0) or If(flag = 0, 1, 0)
    m_if_zero = re.search(r"if\s*\(\s*([a-zA-Z0-9_]+)\s*=\s*0\s*,\s*1\s*,\s*0\s*\)", cleaned, re.IGNORECASE)
    if m_if_zero:
        c = m_if_zero.group(1)
        return f"CALCULATE(COUNTROWS('{ref_tbl}'), '{ref_tbl}'[{c}] = 0)"

    m_if_sum = re.search(r"if\s*\(\s*([a-zA-Z0-9_]+)(?:\s*=\s*1)?\s*,\s*(?:1\s*,\s*)?0\s*\)", cleaned, re.IGNORECASE)
    if m_if_sum:
        c = m_if_sum.group(1)
        return f"CALCULATE(COUNTROWS('{ref_tbl}'), '{ref_tbl}'[{c}] = 1)"

    # 3. Nested If statement -> SWITCH(TRUE(), cond1, val1, cond2, val2, default)
    if cleaned.lower().startswith("if(") or cleaned.lower().startswith("if ("):
        conditions = []
        default_val = '""'
        curr = cleaned
        while True:
            m = re.match(
                r"if\s*\(\s*(.+?)\s*,\s*('[^']*'|\"[^\"]*\"|[0-9\.\-]+)\s*,\s*(.+)\s*\)\s*$",
                curr,
                re.IGNORECASE,
            )
            if not m:
                break
            cond, val, rest = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            if val.startswith("'") and val.endswith("'"):
                val = '"' + val[1:-1].replace('"', '""') + '"'
            cond_dax = _clean_inner_dax(cond, table_name)
            conditions.append(f"{cond_dax}, {val}")
            if rest.lower().startswith("if(") or rest.lower().startswith("if ("):
                curr = rest
            else:
                default_val = rest.rstrip(")").strip()
                if default_val.startswith("'") and default_val.endswith("'"):
                    default_val = '"' + default_val[1:-1].replace('"', '""') + '"'
                break
        if conditions:
            return f"SWITCH(TRUE(), {', '.join(conditions)}, {default_val})"

    # 3. ApplyMap
    m_map = re.search(r"applymap\s*\(\s*'([^']+)'\s*,\s*([a-zA-Z0-9_]+)", cleaned, re.IGNORECASE)
    if m_map:
        map_name, key_col = m_map.group(1), m_map.group(2)
        dim_tb = map_name.replace("Map", "s").replace("map", "s")
        return f"RELATED('{dim_tb}'[{key_col}])"

    # 4. Date / Month / Year / Week functions
    if "monthstart" in cleaned.lower():
        m_col = re.search(r"\(\s*([a-zA-Z0-9_]+)", cleaned)
        col = _resolve_date_col(m_col.group(1) if m_col else None)
        return f"DATE(YEAR('{table_name}'[{col}]), MONTH('{table_name}'[{col}]), 1)"
    if "monthname" in cleaned.lower():
        m_col = re.search(r"\(\s*([a-zA-Z0-9_]+)", cleaned)
        col = _resolve_date_col(m_col.group(1) if m_col else None)
        return f"FORMAT('{table_name}'[{col}], \"mmmm\")"
    if "num(month(" in cleaned.lower() or "num(monthstart(" in cleaned.lower():
        m_col = re.search(r"\(\s*([a-zA-Z0-9_]+)", cleaned)
        col = _resolve_date_col(m_col.group(1) if m_col else None)
        return f"MONTH('{table_name}'[{col}])"

    # 5. Ratios like Sum(a) / Sum(b) or Sum(a) / NullAsValue(Sum(b), 1)
    m_div = re.search(r"sum\s*\(\s*([a-zA-Z0-9_]+)\s*\)\s*/\s*(?:nullasvalue\s*\(\s*)?sum\s*\(\s*([a-zA-Z0-9_]+)\s*\)", cleaned, re.IGNORECASE)
    if m_div:
        c1, c2 = m_div.group(1), m_div.group(2)
        return f"DIVIDE(CALCULATE(SUM('{ref_tbl}'[{c1}])), CALCULATE(SUM('{ref_tbl}'[{c2}])), 0)"

    # 6. Count(DISTINCT col)
    m_cd = re.search(r"count\s*\(\s*distinct\s+([a-zA-Z0-9_]+)\s*\)", cleaned, re.IGNORECASE)
    if m_cd:
        c = m_cd.group(1)
        return f"CALCULATE(DISTINCTCOUNT('{ref_tbl}'[{c}]))"

    # 7. Sum(col), Avg(col), Min(col), Max(col), Count(col)
    m_agg = re.search(r"(sum|avg|min|max|count)\s*\(\s*([a-zA-Z0-9_]+)\s*\)", cleaned, re.IGNORECASE)
    if m_agg:
        f_name, c = m_agg.group(1).lower(), m_agg.group(2)
        dax_f = {"sum": "SUM", "avg": "AVERAGE", "min": "MIN", "max": "MAX", "count": "COUNT"}.get(f_name, "SUM")
        return f"CALCULATE({dax_f}('{ref_tbl}'[{c}]))"

    m_func = re.match(r"(date|month|year|week|num)\s*\(\s*([a-zA-Z0-9_]+)\s*\)", cleaned, re.IGNORECASE)
    if m_func:
        func, col = m_func.group(1).lower(), m_func.group(2)
        col = _resolve_date_col(col)
        if func == "date":
            return f"'{table_name}'[{col}]"
        elif func == "month":
            return f"FORMAT('{table_name}'[{col}], \"mmmm\")"
        elif func == "year":
            return f"YEAR('{table_name}'[{col}])"
        elif func == "week":
            return f"WEEKNUM('{table_name}'[{col}])"
        elif func == "num":
            return f"INT('{table_name}'[{col}])"

    return ""


def _extract_sql_from_qlik_query(qlik_query: str) -> Optional[str]:
    """Extract SQL SELECT ... FROM ... statement from Qlik script."""
    if not qlik_query:
        return None
    match = re.search(r"\bSQL\s+(SELECT\s+.+)", qlik_query, re.IGNORECASE | re.DOTALL)
    if match:
        sql = match.group(1).strip().rstrip(";")
        return re.split(r";|\n\s*//", sql)[0].strip()
    return None


def build_column(
    column: Dict[str, Any],
    table: str,
    extracted_exprs: Optional[Dict[str, str]] = None,
    sql_select_cols: Optional[Set[str]] = None,
    source_table: Optional[str] = None,
) -> str:
    """One TMDL column block."""
    name = _column_name(column)
    data_type = to_tmdl_type(
        column.get("fabric_datatype") or column.get("data_type")
        or column.get("qlik_datatype") or column.get("dataType") or column.get("type"),
        col_name=name,
    )
    is_key = bool(column.get("is_key") or column.get("isKey"))
    source = text(column.get("source_column") or column.get("qlik_column_name"), name)

    lines: List[str] = []
    is_calc = _is_calculated(column, extracted_exprs, table_name=table, source_table=source_table)
    dax_expr = ""

    if name.lower() in ("longitude_latitude", "latitude_longitude"):
        is_calc = True
        dax_expr = f"'{table}'[longitude] & \", \" & '{table}'[latitude]"
        data_type = "string"

    if name.lower() == "ontimerate" and table.lower() == "deliverybytrip":
        is_calc = True
        dax_expr = f"DIVIDE('{table}'[OnTimeEvents], '{table}'[DeliveryEvents], 0)"
        data_type = "double"

    if is_calc:
        fabric = as_dict(column.get("fabric"))
        dax_expr = text(column.get("dax_expression") or fabric.get("dax_expression")) or dax_expr
        if not dax_expr or dax_expr.startswith("="):
            qlik_expr = column.get("qlik_expression") or (
                extracted_exprs.get(name.lower()) if extracted_exprs else None
            )
            if qlik_expr:
                dax_expr = _qlik_expr_to_dax(qlik_expr, table, source_table=source_table)

        if not dax_expr or dax_expr.strip().lower() in (
            f"'{table}'[{name}]".lower(), f"[{name}]".lower(), "blank()"
        ):
            if name.lower() not in ("longitude_latitude", "latitude_longitude", "ontimerate"):
                is_calc = False

    if not is_calc and sql_select_cols is not None:
        if name.lower() not in sql_select_cols and source.lower() not in sql_select_cols:
            is_calc = True
            qlik_expr = extracted_exprs.get(name.lower()) if extracted_exprs else None
            if qlik_expr:
                dax_expr = _qlik_expr_to_dax(qlik_expr, table, source_table=source_table)
            if not dax_expr:
                dax_expr = "BLANK()"

    if is_calc and dax_expr and not dax_expr.startswith("="):
        lines.append(f"{INDENT}column {quote_tmdl(name)} = {dax_expr}")
        lines.append(f"{INDENT*2}dataType: {data_type}")
        lines.append(f"{INDENT*2}lineageTag: {lineage_tag(f'col:{table}.{name}')}")
        lines.append(f"{INDENT*2}summarizeBy: {summarize_by(data_type, is_key, col_name=name)}")
    else:
        lines.append(f"{INDENT}column {quote_tmdl(name)}")
        lines.append(f"{INDENT*2}dataType: {data_type}")
        if is_key:
            lines.append(f"{INDENT*2}isKey")
        lines.append(f"{INDENT*2}lineageTag: {lineage_tag(f'col:{table}.{name}')}")
        lines.append(f"{INDENT*2}summarizeBy: {summarize_by(data_type, is_key, col_name=name)}")
        lines.append(f"{INDENT*2}sourceColumn: {source}")

    fmt = format_string(column, data_type)
    if fmt:
        lines.append(f"{INDENT*2}formatString: {fmt}")
    if column.get("is_hidden"):
        lines.append(f"{INDENT*2}isHidden")

    lines.append("")
    lines.append(f"{INDENT*2}annotation SummarizationSetBy = Automatic")
    return "\n".join(lines)


def _bq_m_query(ep: str, q: str) -> str:
    proj = ep
    proj_clause = f'[BillingProject="{proj}"]' if proj else ''
    if proj:
        return f'let\n    Source = GoogleBigQuery.Database({proj_clause}),\n    Db = Source{{[Name="{proj}",Kind="Database"]}}[Data],\n    Result = Value.NativeQuery(Db, "{q}", null, [EnableFolding=false])\nin\n    Result'
    return f'let\n    Source = GoogleBigQuery.Database({proj_clause}),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'


CONNECTOR_M_FUNCTIONS = {
    "redshift": ("AmazonRedshift.Database", lambda ep, db, sch, q: f'let\n    Source = AmazonRedshift.Database("{ep}", "{db or "dev"}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "amazonredshift": ("AmazonRedshift.Database", lambda ep, db, sch, q: f'let\n    Source = AmazonRedshift.Database("{ep}", "{db or "dev"}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "snowflake": ("Snowflake.Databases", lambda ep, db, sch, q: f'let\n    Source = Snowflake.Databases("{ep}", "{db or "COMPUTE_WH"}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "postgres": ("PostgreSQL.Database", lambda ep, db, sch, q: f'let\n    Source = PostgreSQL.Database("{ep}", "{db or "postgres"}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "postgresql": ("PostgreSQL.Database", lambda ep, db, sch, q: f'let\n    Source = PostgreSQL.Database("{ep}", "{db or "postgres"}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "sqlserver": ("Sql.Database", lambda ep, db, sch, q: f'let\n    Source = Sql.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "mssql": ("Sql.Database", lambda ep, db, sch, q: f'let\n    Source = Sql.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "sql": ("Sql.Database", lambda ep, db, sch, q: f'let\n    Source = Sql.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "azure_sql": ("Sql.Database", lambda ep, db, sch, q: f'let\n    Source = Sql.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "mysql": ("MySQL.Database", lambda ep, db, sch, q: f'let\n    Source = MySQL.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "oracle": ("Oracle.Database", lambda ep, db, sch, q: f'let\n    Source = Oracle.Database("{ep}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "bigquery": ("GoogleBigQuery.Database", lambda ep, db, sch, q: _bq_m_query(ep, q)),
    "gbq": ("GoogleBigQuery.Database", lambda ep, db, sch, q: _bq_m_query(ep, q)),
    "google_bigquery": ("GoogleBigQuery.Database", lambda ep, db, sch, q: _bq_m_query(ep, q)),
    "googlebigquery": ("GoogleBigQuery.Database", lambda ep, db, sch, q: _bq_m_query(ep, q)),
    "databricks": ("Databricks.Catalogs", lambda ep, db, sch, q: f'let\n    Source = Databricks.Catalogs("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "teradata": ("Teradata.Database", lambda ep, db, sch, q: f'let\n    Source = Teradata.Database("{ep}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "hana": ("SapHana.Database", lambda ep, db, sch, q: f'let\n    Source = SapHana.Database("{ep}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "saphana": ("SapHana.Database", lambda ep, db, sch, q: f'let\n    Source = SapHana.Database("{ep}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "synapse": ("AzureSynapse.Database", lambda ep, db, sch, q: f'let\n    Source = AzureSynapse.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
}


def _to_snake_case(name: str) -> str:
    s = re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s).lower()


def _format_m_steps(raw_input: Any) -> str:
    if not raw_input:
        return ""
    if isinstance(raw_input, list):
        clean_lines = []
        for item in raw_input:
            if isinstance(item, dict):
                c = item.get("content") or item.get("step_content") or item.get("text")
                if c:
                    clean_lines.append(str(c).strip())
            elif isinstance(item, str) and item.strip():
                clean_lines.append(item.strip())
        text_content = "\n".join(clean_lines)
    else:
        text_content = str(raw_input).strip()

    if not text_content:
        return ""

    # Extract 'in ...' from the end if present
    in_match = re.search(r'\s+in\s+([#\"a-zA-Z0-9_\s\.\-]+)$', text_content, flags=re.IGNORECASE)
    if in_match:
        final_expr = in_match.group(1).strip()
        body = text_content[:in_match.start()].strip()
    else:
        final_expr = None
        body = text_content

    # Remove leading 'let'
    if body.lower().startswith("let"):
        body = re.sub(r'^let\s*', '', body, flags=re.IGNORECASE).strip()

    # Split body into steps.
    # A step starts with: (identifier or #"quoted identifier") = (not ==)
    step_start_regex = re.compile(r'^(#"[^"]+"|[_a-zA-Z][_a-zA-Z0-9]*)\s*=(?!=)')

    steps = []
    current_step_lines = []

    for line in body.splitlines():
        trimmed_l = line.strip()
        if not trimmed_l:
            continue
        if step_start_regex.match(trimmed_l) and current_step_lines:
            steps.append("\n".join(current_step_lines).strip())
            current_step_lines = [trimmed_l]
        else:
            current_step_lines.append(line)

    if current_step_lines:
        steps.append("\n".join(current_step_lines).strip())

    if not steps:
        return text_content

    # If final_expr wasn't explicitly found, deduce it from the last step's target
    if not final_expr:
        last_m = step_start_regex.match(steps[-1])
        if last_m:
            final_expr = last_m.group(1).strip()
        else:
            final_expr = "Source"

    # Format each step: remove trailing comma, format indentation
    formatted_steps = []
    for i, step in enumerate(steps):
        step_clean = step.rstrip().rstrip(",")
        lines = step_clean.splitlines()
        first_line = "    " + lines[0].strip()
        other_lines = ["        " + l.strip() for l in lines[1:]]
        step_body = "\n".join([first_line] + other_lines)
        if i < len(steps) - 1:
            step_body += ","
        formatted_steps.append(step_body)

    return "let\n" + "\n".join(formatted_steps) + f"\nin\n    {final_expr}"


def _is_circular_self_reference(mquery_str: str, table_name: str) -> bool:
    """Detect if an M query references the table itself as its only source,
    which causes 'Circular query chain present' errors in Power BI / Fabric."""
    if not mquery_str or not table_name:
        return False
    clean_tn = _clean_table_name(table_name).strip()
    pattern = rf'^\s*Source\s*=\s*(?:#"{re.escape(clean_tn)}"|{re.escape(clean_tn)})\s*,?$'
    return bool(re.search(pattern, mquery_str, re.MULTILINE | re.IGNORECASE))


def _extract_mquery_from_payload(table: Dict[str, Any], table_name: str = "") -> Optional[str]:
    fabric = as_dict(table.get("fabric"))
    t_name = table_name or text(table.get("name") or table.get("table_name"))
    candidates = [
        table.get("m_query"),
        table.get("mquery"),
        fabric.get("m_query"),
        fabric.get("mquery"),
        table.get("power_query"),
        fabric.get("power_query"),
    ]
    for c in candidates:
        if not c:
            continue
        formatted = _format_m_steps(c)
        if formatted and not re.search(r"Table\.FromRows\(\s*\{\s*\}\s*,", formatted):
            if not _is_circular_self_reference(formatted, t_name):
                return _fix_relative_folder_paths(formatted, table)
    return None


SUPABASE_STORAGE_URL = (
    os.getenv("SUPABASE_STORAGE_URL")
    or os.getenv("DATA_FILES_STORAGE_URL")
    or "https://orcqwbokkvelxgmkagpz.supabase.co/storage/v1/object/public/migration-data-files"
)


def clean_data_file_name(raw_file_name: str) -> str:
    """QVD files migrated to cloud/Fabric are stored as CSV, so the M-query's
    file reference must match the migrated extension, not the original one."""
    return re.sub(r"\.qvd$", ".csv", raw_file_name.strip(), flags=re.IGNORECASE)


def supabase_file_url(clean_file_name: str) -> str:
    """The placeholder Supabase Storage URL a file-based table's M-query
    points at when no real Fabric-hosted copy has been provisioned yet.

    Exposed so app/generator.py can find-and-replace this exact URL with the
    real OneLake location once a Fabric deploy has actually uploaded the
    file there - see app/deploy/fabric_data_provisioner.py.
    """
    return f"{SUPABASE_STORAGE_URL}/{clean_file_name.replace(' ', '%20')}"


M_TYPE_MAP = {
    "string": "type text",
    "dateTime": "type datetime",
    "int64": "Int64.Type",
    "double": "type number",
    "boolean": "type logical",
    "binary": "type binary",
}


def _fix_relative_folder_paths(mquery_str: str, table: Optional[Dict[str, Any]] = None) -> str:
    if not mquery_str:
        return mquery_str

    # Convert local/server-side file references (Folder.Files / File.Contents / QVDs) to Supabase Storage Web.Contents
    file_match = (
        re.search(r'\[Name\]\s*=\s*"([^"]+)"', mquery_str)
        or re.search(r'File\.Contents\(\s*"[^"]*[\\/]([^"]+)"\s*\)', mquery_str)
        or re.search(r'"([a-zA-Z0-9_\s\-]+\.(?:csv|xlsx|txt|tsv|qvd))"', mquery_str, re.IGNORECASE)
    )
    if file_match and ("Folder.Files" in mquery_str or "File.Contents" in mquery_str or "Web.Contents" in mquery_str or ".qvd" in mquery_str.lower()):
        raw_file_name = file_match.group(1).strip()
        clean_file_name = clean_data_file_name(raw_file_name)
        supa_url = supabase_file_url(clean_file_name)
        new_source = f'    Source = Csv.Document(Web.Contents("{supa_url}"), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.None])'

        # If table is provided, synchronize Changed Type with TMDL data types exactly
        if table:
            cols = as_list(table.get("columns")) or as_list(table.get("fields"))
            col_types = []
            for c in cols:
                if isinstance(c, dict) and not _is_calculated(c):
                    cname = _column_name(c)
                    raw_type = c.get("fabric_datatype") or c.get("data_type") or c.get("qlik_datatype") or c.get("dataType") or c.get("type")
                    ctype = to_tmdl_type(raw_type, col_name=cname)
                    mtype = M_TYPE_MAP.get(ctype, "type text")
                    col_types.append(f'{{"{cname}", {mtype}}}')
            if col_types:
                types_str = ", ".join(col_types)
                return (
                    f"let\n"
                    f"{new_source},\n"
                    f'    #"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n'
                    f'    #"Changed Type" = Table.TransformColumnTypes(#"Promoted Headers", {{{types_str}}})\n'
                    f"in\n"
                    f'    #"Changed Type"'
                )

        if '#"Promoted Headers"' in mquery_str:
            tail = mquery_str.split('#"Promoted Headers"', 1)[1]
            tail = re.sub(r'#"(?:Imported CSV|File Content|Source)"', "Source", tail)
            return f"let\n{new_source},\n    #\"Promoted Headers\"{tail}"
        elif '#"Changed Type"' in mquery_str:
            tail = mquery_str.split('#"Changed Type"', 1)[1]
            return f'let\n{new_source},\n    #"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n    #"Changed Type"{tail}'

    def _replace_folder(match):
        p = match.group(1)
        if ":" not in p and not p.startswith("\\\\") and not p.startswith("/") and not p.startswith("#"):
            return f'Folder.Files("C:\\\\Data\\\\{p}")'
        return match.group(0)

    return re.sub(r'Folder\.Files\(\s*"([^"]+)"\s*\)', _replace_folder, mquery_str)



def _mquery(
    table: Dict[str, Any],
    name: str,
    valid_table_names: Optional[Set[str]] = None,
    extracted_exprs: Optional[Dict[str, str]] = None,
) -> str:
    """The Power Query behind the partition.

    Dynamically uses mquery or custom SQL from the mapping document,
    or generates native connector queries for Redshift, SQL Server, Snowflake, etc.
    """
    # 1. Prioritize explicit M-query from mapping payload if present
    extracted_m = _extract_mquery_from_payload(table, name)
    if extracted_m:
        return extracted_m

    load_type = text(table.get("load_type") or table.get("source_type")).lower()
    qlik_query = text(table.get("qlik_query") or table.get("query_text"))
    fabric = as_dict(table.get("fabric"))
    if "mapping load" in qlik_query.lower() or (load_type == "resident" and "resident " in qlik_query.lower()):
        match = re.search(r"resident\s+([A-Za-z0-9_\-]+)", qlik_query, re.IGNORECASE)
        if match:
            upstream = _clean_table_name(match.group(1))
            # Only reference upstream if it actually exists in the semantic model tables
            is_valid_upstream = (valid_table_names is None) or (upstream in valid_table_names)
            if upstream != name and is_valid_upstream:
                # Select only physical columns (not calculated columns)
                cols = [
                    text(c.get("fabric_column_name") or c.get("name"))
                    for c in as_list(table.get("columns"))
                    if isinstance(c, dict) and not _is_calculated(c, extracted_exprs, table_name=name)
                ]
                cols_str = ", ".join(f'"{c}"' for c in cols if c)
                if cols_str:
                    return f'let\n    Source = #"{upstream}",\n    SelectedColumns = Table.SelectColumns(Source, {{{cols_str}}})\nin\n    SelectedColumns'
                return f'let\n    Source = #"{upstream}"\nin\n    Source'

    conn = as_dict(table.get("connection_details")) or as_dict(table.get("connection"))
    driver = text(conn.get("driver") or conn.get("source_connector") or conn.get("connector_type") or conn.get("type")).lower() or "generic"

    server = text(conn.get("server") or conn.get("host") or conn.get("endpoint"))
    port = text(conn.get("port"))
    database = text(conn.get("database") or conn.get("db"))
    schema = text(conn.get("schema") or table.get("schema"))
    extracted_table = None
    if qlik_query:
        match_from = re.search(r"\bfrom\s+([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)", qlik_query, re.IGNORECASE)
        if match_from:
            if not schema:
                schema = match_from.group(1)
            extracted_table = match_from.group(2)

    raw_source = extracted_table or text(table.get("source") or table.get("table_name") or name)
    clean_source = _clean_table_name(raw_source)
    sql_table = _to_snake_case(clean_source) if schema else clean_source

    # Check for custom SQL with aliases in mapping or extracted from Qlik script
    custom_sql = text(table.get("custom_sql") or table.get("sql") or table.get("query") or fabric.get("custom_sql"))
    if not custom_sql or custom_sql.strip().lower().startswith("select *"):
        extracted = _extract_sql_from_qlik_query(qlik_query)
        if extracted and " as " in extracted.lower():
            custom_sql = extracted

    target_schema_table = f"{schema}.{sql_table}" if schema else sql_table
    query_str = re.sub(r"\s+", " ", custom_sql.replace('"', '""')).strip() if custom_sql else f"SELECT * FROM {target_schema_table}"

    for token, (func_name, generator_fn) in CONNECTOR_M_FUNCTIONS.items():
        if token in driver:
            if any(bq in token for bq in ["bigquery", "gbq"]):
                project = conn.get("project") or ""
                if not project and custom_sql:
                    bq_match = re.search(r"FROM\s+`?([a-zA-Z0-9_\-]+)`?\.", custom_sql, re.IGNORECASE)
                    if bq_match:
                        project = bq_match.group(1)
                if not project and conn.get("name"):
                    bq_match = re.search(r"(?:google_?bigquery_|gbq_)([a-zA-Z0-9_\-]+)", conn.get("name", ""), re.IGNORECASE)
                    if bq_match:
                        project = bq_match.group(1)
                return generator_fn(project, database or "", schema or "", query_str)
            endpoint = f"{server}:{port}" if port and port not in server else server
            if endpoint:
                return generator_fn(endpoint, database or "", schema or "", query_str)

    # Dialect-based fallback if driver was omitted but custom_sql is present
    if custom_sql:
        if "`" in custom_sql or "bigquery" in str(conn).lower() or re.search(r"FROM\s+`?[a-zA-Z0-9_\-]+`?\.`?[a-zA-Z0-9_\-]+`?", custom_sql, re.IGNORECASE):
            project = conn.get("project") or ""
            if not project:
                m = re.search(r"FROM\s+`?([a-zA-Z0-9_\-]+)`?\.", custom_sql)
                if m:
                    project = m.group(1)
            return CONNECTOR_M_FUNCTIONS["bigquery"][1](project, database or "", schema or "", query_str)
        elif server and ("redshift" in server.lower() or port == "5439"):
            endpoint = f"{server}:{port}" if port and port not in server else server
            return CONNECTOR_M_FUNCTIONS["redshift"][1](endpoint, database or "dev", schema or "", query_str)

    if server and database:
        endpoint = f"{server}:{port}" if port and port not in server else server
        return f'let\n    Source = Sql.Database("{endpoint}", "{database}"),\n    Result = Value.NativeQuery(Source, "{query_str}")\nin\n    Result'

    # Filter columns to only physical columns (never include DAX calculated columns in #table)
    cols = [
        text(c.get("fabric_column_name") or c.get("name"))
        for c in as_list(table.get("columns"))
        if isinstance(c, dict) and not _is_calculated(c, extracted_exprs, table_name=name)
    ]
    cols = [c for c in cols if c]
    if cols:
        cols_str = ", ".join(f'"{c}"' for c in cols)
        return f'let\n    Source = #table({{{cols_str}}}, {{}})\nin\n    Source'
    return f'let\n    Source = #table({{"{name}"}}, {{}})\nin\n    Source'



def strip_table_prefixes_in_m(m_query: str, table_name: str, columns: List[Dict[str, Any]]) -> str:
    """
    Replace occurrences of 'TableName.ColumnName' with 'ColumnName' in M queries.
    STRICT RULE: Only strip if the prefix matches the actual TableName.
    Preserves dotted columns like "year.month" while fixing "MyTable.ID".
    Ported from vl-q2f-report-generation/app/agents/tmdl_generator.py.
    """
    if not m_query or not table_name:
        return m_query

    valid_cols = set()
    for col in columns or []:
        if isinstance(col, dict):
            for key in ("name", "sourceColumn", "field_name", "fabric_column_name", "qlik_column_name"):
                val = col.get(key)
                if val:
                    simple = str(val).split(".")[-1]
                    valid_cols.add(simple.upper())

    if not valid_cols:
        return m_query

    tbl_variants = {
        table_name.upper(),
        table_name.replace(" ", "").replace("_", "").upper(),
        _clean_table_name(table_name).upper(),
    }

    # 1) Fix string literals in column lists: "Table.Col" -> "Col"
    def repl_string(match: re.Match) -> str:
        full = match.group(0)
        tbl = match.group("tbl")
        col = match.group("col")
        if tbl.upper() not in tbl_variants:
            return full
        if col.upper() in valid_cols:
            return f'"{col}"'
        return full

    m_query = re.sub(
        r'"(?P<tbl>[A-Za-z0-9_]+)\.(?P<col>[A-Za-z0-9_]+)"',
        repl_string,
        m_query,
    )

    # 2) Fix column references: [Table.Col] -> [Col]
    def repl_bracket(match: re.Match) -> str:
        tbl = match.group("tbl")
        col = match.group("col")
        if tbl.upper() not in tbl_variants:
            return match.group(0)
        if col.upper() in valid_cols:
            return f"[{col}]"
        return match.group(0)

    m_query = re.sub(
        r'\[(?P<tbl>[A-Za-z0-9_]+)\.(?P<col>[A-Za-z0-9_]+)\]',
        repl_bracket,
        m_query,
    )

    return m_query


def build_table(
    table: Dict[str, Any],
    valid_table_names: Optional[Set[str]] = None,
    known_table_columns: Optional[Dict[str, Set[str]]] = None,
) -> str:
    """A complete table.tmdl document."""
    raw_name = text(table.get("name") or table.get("table_name"), "Table")
    name = _clean_table_name(raw_name)
    columns = as_list(table.get("columns")) or as_list(table.get("fields"))
    qlik_query = text(table.get("qlik_query") or table.get("source_query") or table.get("query_text"))
    extracted_exprs = _extract_column_expressions(qlik_query)

    partition_query = _mquery(table, name, valid_table_names, extracted_exprs)
    partition_query = strip_table_prefixes_in_m(partition_query, name, columns)

    sql_select_cols = None
    upstream_table = None

    m_sql = re.search(r'Value\.NativeQuery\([^,]+,\s*"SELECT\s+(.+?)\s+FROM', partition_query, re.IGNORECASE | re.DOTALL)
    if m_sql and 'SELECT *' not in m_sql.group(0).upper():
        sql_select_cols = {c.strip().split()[-1].split('.')[-1].lower() for c in m_sql.group(1).split(',')}

    m_upstream = re.search(r'Source\s*=\s*(?:#")?([A-Za-z0-9_]+)(?:[",\s]|$)', partition_query)
    if m_upstream and not m_sql:
        cand_upstream = m_upstream.group(1).strip()
        if cand_upstream.lower() != name.lower() and known_table_columns and cand_upstream.lower() in known_table_columns:
            upstream_table = cand_upstream
            sql_select_cols = known_table_columns[cand_upstream.lower()]

    if upstream_table and sql_select_cols and "Table.SelectColumns" not in partition_query:
        common_cols = [
            _column_name(c) for c in columns
            if isinstance(c, dict) and _column_name(c).lower() in sql_select_cols
        ]
        if common_cols:
            cols_quoted = ", ".join(f'"{c}"' for c in common_cols)
            partition_query = (
                f'let\n'
                f'    Source = #"{upstream_table}",\n'
                f'    #"Selected Columns" = Table.SelectColumns(Source, {{{cols_quoted}}}),\n'
                f'    #"Removed Duplicates" = Table.Distinct(#"Selected Columns")\n'
                f'in\n'
                f'    #"Removed Duplicates"'
            )

    lines = [f"table {quote_tmdl(name)}", f"{INDENT}lineageTag: {lineage_tag(f'table:{name}')}", ""]

    seen_columns = set()
    for column in columns:
        if isinstance(column, dict):
            col_name = _column_name(column)
            col_name_lower = col_name.lower()
            if col_name_lower in seen_columns:
                continue
            seen_columns.add(col_name_lower)
            lines.append(build_column(
                column, name, extracted_exprs,
                sql_select_cols=sql_select_cols,
                source_table=upstream_table
            ))
            lines.append("")

    lines.append(f"{INDENT}partition {quote_tmdl(name)} = m")
    lines.append(f"{INDENT*2}mode: import")
    lines.append(f"{INDENT*2}source =")
    for line in partition_query.splitlines():
        lines.append(f"{INDENT*4}{line}")
    lines.append("")
    lines.append(f"{INDENT}annotation PBI_ResultType = Table")
    lines.append("")
    return "\n".join(lines)


def build_tables(tables: List[Dict[str, Any]]) -> Dict[str, str]:
    """Map of `tables/<name>.tmdl` -> content."""
    files: Dict[str, str] = {}
    seen_clean_names: Dict[str, Dict[str, Any]] = {}

    for table in tables:
        if not isinstance(table, dict):
            continue
        raw_name = text(table.get("name") or table.get("table_name"), "Table")
        clean_name = _clean_table_name(raw_name)
        cols = as_list(table.get("columns")) or as_list(table.get("fields"))

        if not cols:
            continue

        if clean_name in seen_clean_names:
            existing = seen_clean_names[clean_name]
            existing_raw = text(existing.get("name") or existing.get("table_name"))
            existing_cols = as_list(existing.get("columns")) or as_list(existing.get("fields"))
            if raw_name == clean_name and existing_raw != clean_name:
                seen_clean_names[clean_name] = table
            elif len(cols) > len(existing_cols) and existing_raw != clean_name:
                seen_clean_names[clean_name] = table
        else:
            seen_clean_names[clean_name] = table

    valid_names = set(seen_clean_names.keys())
    known_table_columns = {
        clean_name.lower(): {
            _column_name(c).lower()
            for c in as_list(tbl.get("columns") or tbl.get("fields"))
            if isinstance(c, dict)
        }
        for clean_name, tbl in seen_clean_names.items()
    }
    for clean_name, table in seen_clean_names.items():
        table_copy = dict(table)
        table_copy["name"] = clean_name
        files[clean_name] = build_table(
            table_copy, valid_names, known_table_columns=known_table_columns
        )

    return files

