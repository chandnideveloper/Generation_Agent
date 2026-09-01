"""Shared datasource expressions.

Every table partition previously inlined its own connection string, so a
12-table model repeated the same Redshift host twelve times and changing the
server meant editing twelve partitions.

This emits one named expression per connection — the Power Query "parameter"
Desktop shows under Manage Parameters — so partitions can reference a single
shared source. The connection details come from the mapping payload's
`connections`, which is where the real server/database live.
"""

import os
import re
from typing import Any, Dict, List, Tuple


from app.util.ids import lineage_tag, quote_tmdl
from app.util.payload import as_dict, text
from app.util import payload as P

INDENT = "\t"
SUPABASE_STORAGE_URL = (
    os.getenv("SUPABASE_STORAGE_URL")
    or os.getenv("DATA_FILES_STORAGE_URL")
    or "https://orcqwbokkvelxgmkagpz.supabase.co/storage/v1/object/public/migration-data-files"
)

# Qlik connector family -> the Power Query function that opens it.
CONNECTOR_FUNCTIONS = {
    "redshift": "AmazonRedshift.Database",
    "amazonredshift": "AmazonRedshift.Database",
    "amazon_redshift": "AmazonRedshift.Database",
    "snowflake": "Snowflake.Databases",
    "postgres": "PostgreSQL.Database",
    "postgresql": "PostgreSQL.Database",
    "mysql": "MySQL.Database",
    "mariadb": "MySQL.Database",
    "sqlserver": "Sql.Database",
    "mssql": "Sql.Database",
    "sql": "Sql.Database",
    "azure_sql": "Sql.Database",
    "azuresql": "Sql.Database",
    "oracle": "Oracle.Database",
    "bigquery": "GoogleBigQuery.Database",
    "gbq": "GoogleBigQuery.Database",
    "google_bigquery": "GoogleBigQuery.Database",
    "googlebigquery": "GoogleBigQuery.Database",
    "databricks": "Databricks.Catalogs",
    "teradata": "Teradata.Database",
    "hana": "SapHana.Database",
    "saphana": "SapHana.Database",
    "sap_hana": "SapHana.Database",
    "synapse": "AzureSynapse.Database",
    "azure_synapse": "AzureSynapse.Database",
}


def _connector(connection: Dict[str, Any]) -> str:
    details = as_dict(connection.get("connection_details"))
    haystack = " ".join(
        text(value).lower()
        for value in (
            connection.get("name"), connection.get("lib_name"),
            connection.get("connector_type"), connection.get("connection_type"),
            connection.get("source_connector"), connection.get("driver"),
            details.get("driver"), details.get("source_connector"),
        )
    )
    for token, function in CONNECTOR_FUNCTIONS.items():
        if token in haystack:
            return function
    return ""


def build_expression(connection: Dict[str, Any], index: int) -> Tuple[str, str]:
    """Return (expression name, TMDL block) for one connection."""
    name = text(connection.get("name") or connection.get("lib_name"), f"Source {index}")

    fabric = as_dict(connection.get("fabric"))
    source = text(fabric.get("m_expression"))
    function = _connector(connection)

    # If this is a database connection (like BigQuery) but mapping gave Folder.Files or null:
    if function and ("Folder.Files" in source or not source):
        if "GoogleBigQuery" in function:
            proj = connection.get("project")
            if not proj:
                m = re.search(r"(?:google_?bigquery_|gbq_)([a-zA-Z0-9_\-]+)", name, re.IGNORECASE)
                if m:
                    proj = m.group(1)
            source = f'GoogleBigQuery.Database([BillingProject="{proj}"]' + ')' if proj else 'GoogleBigQuery.Database()'
        elif "AmazonRedshift" in function:
            server = text(connection.get("server"))
            port = text(connection.get("port") or "5439")
            db = text(connection.get("database") or "dev")
            host = f"{server}:{port}" if server and port else server
            source = f'AmazonRedshift.Database("{host}", "{db}")' if host else 'AmazonRedshift.Database()'
        elif "Snowflake" in function:
            server = text(connection.get("server"))
            wh = text(connection.get("warehouse") or "COMPUTE_WH")
            source = f'Snowflake.Databases("{server}", "{wh}")' if server else 'Snowflake.Databases()'
        elif "PostgreSQL" in function:
            server = text(connection.get("server"))
            db = text(connection.get("database") or "postgres")
            source = f'PostgreSQL.Database("{server}", "{db}")' if server else 'PostgreSQL.Database()'
        elif "Sql.Database" in function:
            server = text(connection.get("server"))
            db = text(connection.get("database") or "master")
            source = f'Sql.Database("{server}", "{db}")' if server else 'Sql.Database()'
        elif "Oracle" in function:
            server = text(connection.get("server"))
            source = f'Oracle.Database("{server}")' if server else 'Oracle.Database()'

    if not source:
        # Fall back to assembling one from the connection's own fields.
        details = as_dict(connection.get("connection_details"))
        server = text(connection.get("server") or details.get("server"))
        port = text(connection.get("port") or details.get("port"))
        database = text(connection.get("database") or details.get("database"))
        path = text(connection.get("path") or details.get("path"))
        host = f"{server}:{port}" if server and port else server

        if function and host:
            source = (
                f'{function}("{host}", "{database}")' if database
                else f'{function}("{host}")'
            )
        elif path:
            source = f'Folder.Files("{path}")'
        else:
            source = 'null /* connection details unavailable — set the source here */'

    if "Folder.Files(" in source and not function:
        # Avoid broken local C:\Data\... references in shared expressions; point to Supabase Storage Web.Contents
        source = f'Web.Contents("{SUPABASE_STORAGE_URL}")'

    if source.strip().lower().startswith("let"):
        source_lines = [f"{INDENT*2}{line}" for line in source.strip().splitlines()]
        expr_body = "\n".join(source_lines)

    else:
        expr_body = f"{INDENT*2}let\n{INDENT*2}    Source = {source}\n{INDENT*2}in\n{INDENT*2}    Source"

    block = [
        f"expression {quote_tmdl(name)} =",
        expr_body,
        f"{INDENT}lineageTag: {lineage_tag(f'expression:{name}')}",
        "",
        f"{INDENT}annotation PBI_NavigationStepName = Navigation",
        "",
        f"{INDENT}annotation PBI_ResultType = Table",
        "",
    ]
    return name, "\n".join(block)



def build_expressions(mapping: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Return (expressions.tmdl content, expression names)."""
    connections = P.connections(mapping)
    if not connections:
        return "", []

    tables = P.tables(mapping)
    # Collect all connection identifiers used by the actual tables
    used_conn_identifiers = set()
    for t in tables:
        if not isinstance(t, dict):
            continue
        c = t.get("connection") or t.get("connection_details") or {}
        if isinstance(c, dict):
            for k in ["name", "lib_name", "server", "id", "datasource_id"]:
                if c.get(k):
                    used_conn_identifiers.add(str(c[k]).strip().lower())
        elif isinstance(c, str) and c.strip():
            used_conn_identifiers.add(c.strip().lower())
        qlik_q = str(t.get("qlik_query") or t.get("query_text") or "")
        for match in re.finditer(r"lib://([^/\]]+)", qlik_q, re.IGNORECASE):
            used_conn_identifiers.add(match.group(1).strip().lower())

    # If NO tables actually use any external connection, do not emit unneeded expressions
    if not used_conn_identifiers:
        return "", []

    blocks: List[str] = []
    names: List[str] = []
    for index, connection in enumerate(connections, start=1):
        c_name = str(connection.get("name") or connection.get("lib_name") or "").lower()
        c_id = str(connection.get("id") or "").lower()
        c_server = str(connection.get("server") or "").lower()
        c_details = as_dict(connection.get("connection_details"))
        c_det_server = str(c_details.get("server") or "").lower()

        # Ensure this connection is actually used by at least one table in the model
        is_used = any(
            ident in used_conn_identifiers
            for ident in [c_name, c_id, c_server, c_det_server]
            if ident
        )
        if not is_used and not connection.get("used_in_script"):
            continue

        name, block = build_expression(connection, index)
        if name in names:
            continue
        names.append(name)
        blocks.append(block)

    return "\n".join(blocks), names
