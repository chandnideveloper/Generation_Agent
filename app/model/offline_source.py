"""Inline M partitions, so the model opens with no database connection.

Every generated table normally points at the real source — for the test app
that is Amazon Redshift. Opening the PBIP therefore prompts for credentials,
and without them nothing loads and the report cannot be reviewed at all.

Offline mode swaps each partition for a literal `Table.FromRows` carrying the
same column names and types, so Power BI Desktop opens the file, builds the
model, and renders every page immediately. The layout, field bindings,
measures and relationships are all exercised; only the values are synthetic.

Switch it off (the default) to emit the real connection.
"""

from typing import Any, Dict, List, Tuple

from app.model.datatypes import to_tmdl_type
from app.util.payload import as_dict, as_list, text

# One representative row per type, so visuals have something to draw.
SAMPLE_VALUES: Dict[str, List[str]] = {
    "string": ['"Sample A"', '"Sample B"', '"Sample C"'],
    "int64": ["1", "2", "3"],
    "double": ["10.5", "20.25", "30.75"],
    "decimal": ["10.5", "20.25", "30.75"],
    "dateTime": [
        "#datetime(2024, 1, 1, 0, 0, 0)",
        "#datetime(2024, 6, 15, 0, 0, 0)",
        "#datetime(2024, 12, 31, 0, 0, 0)",
    ],
    "boolean": ["true", "false", "true"],
    "binary": ["null", "null", "null"],
}

# Power Query type literals for Table.FromRows' optional type argument.
M_TYPES = {
    "string": "type text",
    "int64": "Int64.Type",
    "double": "type number",
    "decimal": "type number",
    "dateTime": "type datetime",
    "boolean": "type logical",
    "binary": "type binary",
}

def _get_smart_sample_value(col_name: str, data_type: str, row_idx: int) -> str:
    """Generate realistic, distinct values based on column name hints and TMDL data type."""
    name_l = col_name.lower()

    if data_type == "dateTime":
        dates = [
            "#datetime(2024, 1, 15, 9, 30, 0)",
            "#datetime(2024, 2, 20, 11, 45, 0)",
            "#datetime(2024, 3, 10, 14, 15, 0)",
            "#datetime(2024, 4, 5, 16, 20, 0)",
            "#datetime(2024, 5, 18, 18, 50, 0)",
        ]
        return dates[row_idx % len(dates)]

    if data_type in ("double", "decimal", "int64"):
        if any(k in name_l for k in ("discount", "rate", "pct", "percent")):
            vals = ["5.0", "10.0", "15.5", "8.0", "12.5"]
        elif any(k in name_l for k in ("amount", "sales", "revenue", "order", "price", "total", "cost")):
            vals = ["120.50", "245.00", "380.75", "195.25", "450.00"]
        elif any(k in name_l for k in ("qty", "quantity", "num", "count", "items", "units")):
            vals = ["2", "5", "1", "4", "3"]
        elif "hour" in name_l:
            vals = ["9", "11", "14", "16", "18"]
        else:
            vals = ["10.5", "25.0", "42.5", "18.0", "35.5"]
        return vals[row_idx % len(vals)]

    if data_type == "boolean":
        return "true" if row_idx % 2 == 0 else "false"

    # String type hints
    if "state" in name_l:
        states = ['"California"', '"New York"', '"Texas"', '"Florida"', '"Illinois"']
        return states[row_idx % len(states)]
    if "city" in name_l:
        cities = ['"Los Angeles"', '"New York"', '"Houston"', '"Miami"', '"Chicago"']
        return cities[row_idx % len(cities)]
    if "member" in name_l or "flag" in name_l:
        flags = ['"Yes"', '"No"', '"Yes"', '"Yes"', '"No"']
        return flags[row_idx % len(flags)]
    if "hour" in name_l:
        hours = ['"09:00 AM"', '"11:00 AM"', '"02:00 PM"', '"04:00 PM"', '"06:00 PM"']
        return hours[row_idx % len(hours)]
    if "number" in name_l or "id" in name_l or "code" in name_l:
        ids = [f'"ID-{1001 + row_idx}"' for _ in range(5)]
        return ids[row_idx % len(ids)]
    if "name" in name_l or "customer" in name_l:
        names = ['"Acme Corp"', '"Global Tech"', '"Apex Retail"', '"Pinnacle Inc"', '"Vertex Co"']
        return names[row_idx % len(names)]

    defaults = [f'"{col_name} {chr(65 + row_idx)}"' for _ in range(5)]
    return defaults[row_idx % len(defaults)]


ROW_COUNT = 5


def _columns(table: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(column name, TMDL type) pairs for one table."""
    resolved: List[Tuple[str, str]] = []
    seen = set()
    for column in as_list(table.get("columns")) or as_list(table.get("fields")):
        column = as_dict(column)
        name = text(
            column.get("fabric_column_name")
            or column.get("qlik_column_name")
            or column.get("name")
            or column.get("Name")
        )
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        data_type = to_tmdl_type(
            column.get("fabric_datatype") or column.get("data_type")
            or column.get("qlik_datatype") or column.get("dataType")
            or column.get("type")
        )
        resolved.append((name, data_type))
    return resolved


def build_partition(table: Dict[str, Any], table_name: str) -> str:
    """A self-contained M expression with no external dependency."""
    columns = _columns(table)
    if not columns:
        return (
            'let\n    Source = Table.FromRows({}, {"Placeholder"})\nin\n    Source'
        )

    header = ", ".join(f'"{name}"' for name, _ in columns)
    types = ", ".join(
        f'{{"{name}", {M_TYPES.get(data_type, "text")}}}' for name, data_type in columns
    )

    rows = []
    for index in range(ROW_COUNT):
        values = [
            _get_smart_sample_value(col_name, data_type, index)
            for col_name, data_type in columns
        ]
        rows.append("{" + ", ".join(values) + "}")
    body = ", ".join(rows)

    return (
        "let\n"
        f"    Source = Table.FromRows({{{body}}}, {{{header}}}),\n"
        f"    Typed = Table.TransformColumnTypes(Source, {{{types}}})\n"
        "in\n"
        "    Typed"
    )


def apply(tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return copies of `tables` whose M query is inline sample data."""
    offline: List[Dict[str, Any]] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        copied = dict(table)
        name = text(copied.get("name") or copied.get("table_name"), "Table")
        partition_code = build_partition(copied, name)
        copied["mquery"] = partition_code
        copied["m_query"] = partition_code
        fabric = dict(as_dict(copied.get("fabric")))
        fabric["mquery"] = partition_code
        fabric["m_query"] = partition_code
        copied["fabric"] = fabric
        offline.append(copied)
    return offline

