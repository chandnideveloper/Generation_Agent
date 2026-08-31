"""Shared datasource expressions.

Every table partition previously inlined its own connection string, so a
12-table model repeated the same Redshift host twelve times and changing the
server meant editing twelve partitions.

This emits one named expression per connection — the Power Query "parameter"
Desktop shows under Manage Parameters — so partitions can reference a single
shared source. The connection details come from the mapping payload's
`connections`, which is where the real server/database live.
"""

import re
from typing import Any, Dict, List, Tuple


from app.util.ids import lineage_tag, quote_tmdl
from app.util.payload import as_dict, text
from app.util import payload as P

INDENT = "\t"
SUPABASE_STORAGE_URL = "https://orcqwbokkvelxgmkagpz.supabase.co/storage/v1/object/public/migration-data-files"

# Qlik connector family -> the Power Query function that opens it.
CONNECTOR_FUNCTIONS = {
    "redshift": "AmazonRedshift.Database",
    "snowflake": "Snowflake.Databases",
    "postgres": "PostgreSQL.Database",
    "postgresql": "PostgreSQL.Database",
    "mysql": "MySQL.Database",
    "sqlserver": "Sql.Database",
    "mssql": "Sql.Database",
    "sql": "Sql.Database",
    "oracle": "Oracle.Database",
    "bigquery": "GoogleBigQuery.Database",
    "databricks": "Databricks.Catalogs",
    "teradata": "Teradata.Database",
    "hana": "SapHana.Database",
    "synapse": "AzureSynapse.Database",
}


def _connector(connection: Dict[str, Any]) -> str:
    details = as_dict(connection.get("connection_details"))
    haystack = " ".join(
        text(value).lower()
        for value in (
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
    """Return (expression name, TMDL block) for one connection.

    The mapping agent already resolves a complete M expression per connection
    under `fabric.m_expression` — for example
    `AmazonRedshift.Database("host:5439", "dev")`. That is authoritative and
    is used as-is; rebuilding it here from the individual fields was both
    redundant and, because the fields are flat rather than nested under
    `connection_details`, produced a `null` source.
    """
    name = text(connection.get("name") or connection.get("lib_name"), f"Source {index}")

    fabric = as_dict(connection.get("fabric"))
    source = text(fabric.get("m_expression"))

    if not source:
        # Fall back to assembling one from the connection's own fields.
        details = as_dict(connection.get("connection_details"))
        function = _connector(connection)
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
            # Nothing to open — leave a documented stub rather than a broken
            # function call that would fail at refresh time.
            source = 'null /* connection details unavailable — set the source here */'

    if "Folder.Files(" in source:
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

    blocks: List[str] = []
    names: List[str] = []
    for index, connection in enumerate(connections, start=1):
        name, block = build_expression(connection, index)
        if name in names:
            continue
        names.append(name)
        blocks.append(block)

    return "\n".join(blocks), names
