"""Per-table TMDL: columns, partition and the M query behind it."""

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
) -> bool:
    name = _column_name(column)
    if extracted_exprs and name.lower() in extracted_exprs:
        qlik_expr = extracted_exprs[name.lower()]
        translated = _qlik_expr_to_dax(qlik_expr, table_name or "Table") if table_name else ""
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
    pattern = r"((?:If|Date|Date#|Timestamp|Month|MonthStart|MonthName|Year|Week|Num|ApplyMap|Upper|Lower|Trim|Text|Dual|Floor|Ceil|Round)\s*\([\s\S]+?\))\s+as\s+([A-Za-z0-9_#]+)"
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


def _qlik_expr_to_dax(expr: str, table_name: str) -> str:
    """Generic translator from Qlik expression to DAX calculated column expression."""
    if not expr:
        return ""
    cleaned = re.sub(r"\s+", " ", expr).strip()

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

    # 2. Nested If statement -> SWITCH(TRUE(), cond1, val1, cond2, val2, default)
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
    m_func = re.match(r"(date|month|year|week|num)\s*\(\s*([a-zA-Z0-9_]+)\s*\)", cleaned, re.IGNORECASE)
    if m_func:
        func, col = m_func.group(1).lower(), m_func.group(2)
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
    is_calc = _is_calculated(column, extracted_exprs, table_name=table)
    dax_expr = ""
    if is_calc:
        fabric = as_dict(column.get("fabric"))
        dax_expr = text(column.get("dax_expression") or fabric.get("dax_expression"))
        if not dax_expr or dax_expr.startswith("="):
            qlik_expr = column.get("qlik_expression") or (
                extracted_exprs.get(name.lower()) if extracted_exprs else None
            )
            if qlik_expr:
                dax_expr = _qlik_expr_to_dax(qlik_expr, table)

        if not dax_expr or dax_expr.strip().lower() in (
            f"'{table}'[{name}]".lower(), f"[{name}]".lower(), "blank()"
        ):
            is_calc = False

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


CONNECTOR_M_FUNCTIONS = {
    "redshift": ("AmazonRedshift.Database", lambda ep, db, sch, q: f'let\n    Source = AmazonRedshift.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}", null, [EnableFolding=false])\nin\n    Result'),
    "snowflake": ("Snowflake.Databases", lambda ep, db, sch, q: f'let\n    Source = Snowflake.Databases("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}")\nin\n    Result'),
    "postgres": ("PostgreSQL.Database", lambda ep, db, sch, q: f'let\n    Source = PostgreSQL.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}")\nin\n    Result'),
    "postgresql": ("PostgreSQL.Database", lambda ep, db, sch, q: f'let\n    Source = PostgreSQL.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}")\nin\n    Result'),
    "sqlserver": ("Sql.Database", lambda ep, db, sch, q: f'let\n    Source = Sql.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}")\nin\n    Result'),
    "mssql": ("Sql.Database", lambda ep, db, sch, q: f'let\n    Source = Sql.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}")\nin\n    Result'),
    "sql": ("Sql.Database", lambda ep, db, sch, q: f'let\n    Source = Sql.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}")\nin\n    Result'),
    "mysql": ("MySQL.Database", lambda ep, db, sch, q: f'let\n    Source = MySQL.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}")\nin\n    Result'),
    "oracle": ("Oracle.Database", lambda ep, db, sch, q: f'let\n    Source = Oracle.Database("{ep}"),\n    Result = Value.NativeQuery(Source, "{q}")\nin\n    Result'),
    "bigquery": ("GoogleBigQuery.Database", lambda ep, db, sch, q: f'let\n    Source = GoogleBigQuery.Database(),\n    Result = Value.NativeQuery(Source, "{q}")\nin\n    Result'),
    "databricks": ("Databricks.Catalogs", lambda ep, db, sch, q: f'let\n    Source = Databricks.Catalogs("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}")\nin\n    Result'),
    "teradata": ("Teradata.Database", lambda ep, db, sch, q: f'let\n    Source = Teradata.Database("{ep}"),\n    Result = Value.NativeQuery(Source, "{q}")\nin\n    Result'),
    "hana": ("SapHana.Database", lambda ep, db, sch, q: f'let\n    Source = SapHana.Database("{ep}"),\n    Result = Value.NativeQuery(Source, "{q}")\nin\n    Result'),
    "synapse": ("AzureSynapse.Database", lambda ep, db, sch, q: f'let\n    Source = AzureSynapse.Database("{ep}", "{db}"),\n    Result = Value.NativeQuery(Source, "{q}")\nin\n    Result'),
}


def _to_snake_case(name: str) -> str:
    s = re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s).lower()


def _format_m_steps(steps: List[str]) -> str:
    if not steps:
        return ""
    clean_steps = [s.strip() for s in steps if s and s.strip()]
    if not clean_steps:
        return ""

    final_identifier = None
    processed_steps = []

    for s in clean_steps:
        s_clean = s.strip()
        if s_clean.lower().startswith("let ") or s_clean.lower() == "let":
            s_clean = s_clean[3:].strip() if s_clean.lower().startswith("let ") else ""
        if " in " in s_clean.lower() or s_clean.lower().startswith("in "):
            parts = re.split(r"\s+in\s+", s_clean, flags=re.IGNORECASE)
            if len(parts) == 2:
                s_clean = parts[0].strip()
                final_identifier = parts[1].strip()
            elif s_clean.lower().startswith("in "):
                final_identifier = s_clean[3:].strip()
                s_clean = ""
        if s_clean:
            processed_steps.append(s_clean)

    if not processed_steps:
        return ""

    if not final_identifier:
        last_step_match = re.match(r"^([#\"a-zA-Z0-9_\s]+)\s*=", processed_steps[-1])
        final_identifier = last_step_match.group(1).strip() if last_step_match else processed_steps[-1].split("=")[0].strip()

    step_lines = []
    for i, step in enumerate(processed_steps):
        step_stripped = step.rstrip(",")
        lines = step_stripped.splitlines()
        indented_lines = []
        for j, l in enumerate(lines):
            indented_lines.append(f"    {l.strip()}" if j > 0 else f"    {l}")
        step_body = "\n".join(indented_lines)
        if i < len(processed_steps) - 1:
            step_lines.append(f"{step_body},")
        else:
            step_lines.append(step_body)

    return "let\n" + "\n".join(step_lines) + f"\nin\n    {final_identifier}"




def _extract_mquery_from_payload(table: Dict[str, Any]) -> Optional[str]:
    fabric = as_dict(table.get("fabric"))
    candidates = [
        table.get("m_query"),
        table.get("mquery"),
        fabric.get("m_query"),
        fabric.get("mquery"),
        table.get("power_query"),
        fabric.get("power_query"),
    ]
    for c in candidates:
        if isinstance(c, list) and c:
            step_contents = []
            for item in c:
                if isinstance(item, dict):
                    content = item.get("content") or item.get("step_content") or item.get("text")
                    if content:
                        step_contents.append(str(content).strip())
                elif isinstance(item, str) and item.strip():
                    step_contents.append(item.strip())
            if step_contents:
                formatted = _format_m_steps(step_contents)
                if formatted and not re.search(r"Table\.FromRows\(\s*\{\s*\}\s*,", formatted):
                    return _fix_relative_folder_paths(formatted, table)
        elif isinstance(c, str) and c.strip():
            raw_str = c.strip()
            if not re.search(r"Table\.FromRows\(\s*\{\s*\}\s*,", raw_str):
                if not raw_str.lower().startswith("let") and "=" in raw_str:
                    return _fix_relative_folder_paths(_format_m_steps(raw_str.splitlines()), table)
                return _fix_relative_folder_paths(raw_str, table)
    return None


SUPABASE_STORAGE_URL = "https://orcqwbokkvelxgmkagpz.supabase.co/storage/v1/object/public/migration-data-files"


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
        # QVD files migrated to cloud/Fabric are stored as CSV in Supabase Storage
        clean_file_name = re.sub(r"\.qvd$", ".csv", raw_file_name, flags=re.IGNORECASE)
        encoded_name = clean_file_name.replace(" ", "%20")
        supa_url = f"{SUPABASE_STORAGE_URL}/{encoded_name}"
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



def _mquery(table: Dict[str, Any], name: str) -> str:
    """The Power Query behind the partition.

    Dynamically uses mquery or custom SQL from the mapping document,
    or generates native connector queries for Redshift, SQL Server, Snowflake, etc.
    """
    # 1. Prioritize explicit M-query from mapping payload if present
    extracted_m = _extract_mquery_from_payload(table)
    if extracted_m:
        return extracted_m

    load_type = text(table.get("load_type") or table.get("source_type")).lower()
    qlik_query = text(table.get("qlik_query") or table.get("query_text"))
    fabric = as_dict(table.get("fabric"))
    if "mapping load" in qlik_query.lower() or (load_type == "resident" and "resident " in qlik_query.lower()):
        match = re.search(r"resident\s+([A-Za-z0-9_\-]+)", qlik_query, re.IGNORECASE)
        if match:
            upstream = _clean_table_name(match.group(1))
            if upstream != name:
                cols = [text(c.get("fabric_column_name") or c.get("name")) for c in as_list(table.get("columns")) if isinstance(c, dict)]
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
            endpoint = f"{server}:{port}" if port and port not in server else server
            if endpoint:
                return generator_fn(endpoint, database or "", schema or "", query_str)

    if server and database:
        endpoint = f"{server}:{port}" if port and port not in server else server
        return f'let\n    Source = Sql.Database("{endpoint}", "{database}"),\n    Result = Value.NativeQuery(Source, "{query_str}")\nin\n    Result'

    return f'let\n    Source = Table.FromRows({{}}, {{"{name}"}})\nin\n    Source'



def build_table(table: Dict[str, Any]) -> str:
    """A complete table.tmdl document."""
    raw_name = text(table.get("name") or table.get("table_name"), "Table")
    name = _clean_table_name(raw_name)
    columns = as_list(table.get("columns")) or as_list(table.get("fields"))
    qlik_query = text(table.get("qlik_query") or table.get("query_text"))
    extracted_exprs = _extract_column_expressions(qlik_query)

    lines = [f"table {quote_tmdl(name)}", f"{INDENT}lineageTag: {lineage_tag(f'table:{name}')}", ""]

    seen_columns = set()
    for column in columns:
        if isinstance(column, dict):
            col_name = _column_name(column)
            col_name_lower = col_name.lower()
            if col_name_lower in seen_columns:
                continue
            seen_columns.add(col_name_lower)
            lines.append(build_column(column, name, extracted_exprs))
            lines.append("")

    partition_query = _mquery(table, name)
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

    for clean_name, table in seen_clean_names.items():
        table_copy = dict(table)
        table_copy["name"] = clean_name
        files[clean_name] = build_table(table_copy)

    return files

