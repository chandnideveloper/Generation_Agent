"""Assemble the complete semantic model folder from a mapping payload.

Produces the file map for `<name>.SemanticModel/`:

    definition/model.tmdl          model header + table references
    definition/database.tmdl       compatibility level
    definition/relationships.tmdl
    definition/tables/<T>.tmdl     one per table, with its measures inline
    definition/cultures/<c>.tmdl
    definition.pbism
    .platform
"""

import json
import uuid
from typing import Any, Dict, List, Set, Tuple

from app.config import config
from app.model import measure_tmdl
from app.model.expressions_tmdl import build_expressions
from app.report import pbir_schemas as S
from app.model.relationship_tmdl import build_relationships
from app.model.table_tmdl import _clean_table_name, build_tables
from app.util.ids import lineage_tag, quote_tmdl, safe_filename
from app.util.payload import as_dict, as_list, text
from app.util import payload as P

INDENT = "\t"


def _model_tmdl(app_name: str, table_names: List[str]) -> str:
    lines = [
        "model Model",
        f"{INDENT}culture: {config.TMDL_CULTURE}",
        f"{INDENT}defaultPowerBIDataSourceVersion: powerBI_V3",
        f"{INDENT}discourageImplicitMeasures",
        f"{INDENT}sourceQueryCulture: {config.TMDL_SOURCE_QUERY_CULTURE}",
        "",
        f"{INDENT}annotation PBI_QueryOrder = {json.dumps(table_names)}",
        f'{INDENT}annotation PBI_ProTooling = ["DevMode"]',
        "",
    ]
    for name in table_names:
        lines.append(f"ref table {quote_tmdl(name)}")
    lines.append("")
    lines.append(f"ref cultureInfo {config.TMDL_CULTURE}")
    lines.append("")
    return "\n".join(lines)


def _database_tmdl() -> str:
    return (
        "database\n"
        f"{INDENT}compatibilityLevel: {config.TMDL_COMPATIBILITY_LEVEL}\n"
    )


def _culture_tmdl() -> str:
    return (
        f"cultureInfo {config.TMDL_CULTURE}\n"
        f"{INDENT}linguisticMetadata =\n"
        f"{INDENT*3}{{\n"
        f'{INDENT*3}  "Version": "1.0.0",\n'
        f'{INDENT*3}  "Language": "{config.TMDL_CULTURE}"\n'
        f"{INDENT*3}}}\n"
        f"{INDENT*2}contentType: json\n"
    )


def _platform(display_name: str, kind: str) -> str:
    return json.dumps({
        "$schema": S.PLATFORM,
        "metadata": {"type": kind, "displayName": display_name},
        # An empty logicalId makes Fabric Git integration reject the item, and
        # a random one would change on every run — so it is derived from the
        # item identity and stays stable across regenerations.
        "config": {"version": "2.0", "logicalId": lineage_tag(f"{kind}:{display_name}")},
    }, indent=2)


def build_semantic_model(
    mapping: Dict[str, Any], app_name: str
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Return (files, report) for the semantic model folder."""
    tables = P.tables(mapping)
    measures = P.measures(mapping)
    dimensions = P.dimensions(mapping)
    relationships = P.relationships(mapping)

    table_files = build_tables(tables)
    table_names = list(table_files)

    # Collect valid physical columns from table definitions
    valid_columns: Set[Tuple[str, str]] = set()
    key_columns: Set[Tuple[str, str]] = set()
    for table in tables:
        raw_name = text(table.get("name") or table.get("table_name"))
        name = _clean_table_name(raw_name)
        if name not in table_files:
            continue
        for column in as_list(table.get("columns")) or as_list(table.get("fields")):
            column_name = text(
                as_dict(column).get("fabric_column_name")
                or as_dict(column).get("qlik_column_name")
                or as_dict(column).get("name")
            )
            if name and column_name:
                valid_columns.add((name, column_name))
                if as_dict(column).get("is_key") or as_dict(column).get("isKey"):
                    key_columns.add((name, column_name))

    # Attach measures and calculated columns to their host tables.
    dax_problems: List[Dict[str, str]] = []
    grouped = measure_tmdl.group_by_table(
        measures, dimensions, table_names, dax_problems, valid_columns=valid_columns
    )
    for table_name, blocks in grouped.items():
        if table_name in table_files:
            table_files[table_name] = measure_tmdl.inject_into_table(
                table_files[table_name], blocks
            )
        else:
            table_files[table_name] = measure_tmdl.build_orphan_table(blocks)

    table_names = list(table_files)

    relationships_tmdl, skipped = build_relationships(
        relationships, valid_columns, key_columns
    )

    files: Dict[str, str] = {
        "definition/model.tmdl": _model_tmdl(app_name, table_names),
        "definition/database.tmdl": _database_tmdl(),
        f"definition/cultures/{config.TMDL_CULTURE}.tmdl": _culture_tmdl(),
        "definition.pbism": json.dumps({
            "$schema": S.MODEL_DEFINITION_PROPERTIES,
            "version": "4.0",
            "settings": {},
        }, indent=2),
        ".pbi/localSettings.json": json.dumps({
            "$schema": S.MODEL_LOCAL_SETTINGS,
            "userConsent": {"compositeModel": True},
        }, indent=2),
        ".pbi/editorSettings.json": json.dumps({
            "$schema": S.MODEL_EDITOR_SETTINGS,
            "autodetectRelationships": True,
            "parallelQueryLoading": True,
            "typeDetectionEnabled": True,
            "relationshipImportEnabled": True,
            "shouldNotifyUserOfNameConflictResolution": True,
        }, indent=2),
        ".pbi/diagramLayout.json": json.dumps({
            "version": "1.0.0",
            "diagrams": [{
                "name": "All tables", "zoomValue": 100, "isDefault": True,
                "tables": [], "layout": {
                    "boundingBoxWidth": 0, "boundingBoxHeight": 0,
                    "boundingBoxPosition": {"x": 0, "y": 0}, "nodes": [],
                },
            }],
        }, indent=2),
        ".platform": _platform(app_name, "SemanticModel"),
    }
    if relationships_tmdl.strip():
        files["definition/relationships.tmdl"] = relationships_tmdl

    # One shared expression per connection, so the source lives in a single
    # place instead of being repeated in every partition.
    expressions_tmdl, expression_names = build_expressions(mapping)
    if expressions_tmdl.strip():
        files["definition/expressions.tmdl"] = expressions_tmdl

    for name, content in table_files.items():
        files[f"definition/tables/{safe_filename(name, 'Table')}.tmdl"] = content

    report = {
        "tables": len(table_files),
        "measures": len(measures),
        "calculated_columns": sum(len(b["columns"]) for b in grouped.values()),
        "relationships_written": relationships_tmdl.count("relationship "),
        "relationships_skipped": skipped,
        "orphan_measures_table": measure_tmdl.ORPHAN_TABLE in table_files,
        "shared_expressions": expression_names,
        "dax_needs_rewrite": len(dax_problems),
        "dax_problems": dax_problems,
    }
    return files, report
