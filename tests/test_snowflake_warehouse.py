import json
import pytest
from app.qlik.util.payload import unwrap_mapping
from app.qlik.model.semantic_model import build_semantic_model
from app.qlik.model.table_tmdl import (
    _fix_snowflake_warehouse,
    _fix_table_navigation_target,
    _extract_mquery_from_payload,
)


def test_user_exact_snowflake_query_transformation():
    user_m = """let
    Source = Snowflake.Databases("gq37595.ap-southeast-7.aws.snowflakecomputing.com", null, [Role = null, CreateNavigationProperties = null, ConnectionTimeout = null, CommandTimeout = null, Implementation = "2.0"]),
    #"Navigation 1" = Source{[Name = "LEARNING_ANALYTICS", Kind = "Database"]}[Data],
    #"Navigation 2" = #"Navigation 1"{[Name = "PUBLIC", Kind = "Schema"]}[Data],
    #"Navigation 3" = #"Navigation 2"{[Name = "STUDENT_COURSES", Kind = "Table"]}[Data]
in
    #"Navigation 3" """

    table = {
        "name": "COURSES",
        "bi_table_name": "COURSES",
        "m_query": user_m,
        "warehouse": "COMPUTE_WH",
    }

    result = _extract_mquery_from_payload(table, "COURSES")
    assert 'Snowflake.Databases("gq37595.ap-southeast-7.aws.snowflakecomputing.com", "COMPUTE_WH", [Role = null' in result
    assert '#"Navigation 3" = #"Navigation 2"{[Name = "COURSES", Kind = "Table"]}[Data]' in result
    assert 'in\n    #"Navigation 3"' in result


def test_various_snowflake_warehouse_formats():
    c1 = 'Source = Snowflake.Databases("host.snowflake.com", null)'
    assert _fix_snowflake_warehouse(c1, "COMPUTE_WH") == 'Source = Snowflake.Databases("host.snowflake.com", "COMPUTE_WH")'

    c2 = 'Source = Snowflake.Databases("host.snowflake.com", "")'
    assert _fix_snowflake_warehouse(c2, "COMPUTE_WH") == 'Source = Snowflake.Databases("host.snowflake.com", "COMPUTE_WH")'

    c3 = 'Source = Snowflake.Databases("host.snowflake.com")'
    assert _fix_snowflake_warehouse(c3, "COMPUTE_WH") == 'Source = Snowflake.Databases("host.snowflake.com", "COMPUTE_WH")'

    c4 = 'Source = Snowflake.Databases("host.snowflake.com", [Implementation = "2.0"])'
    assert _fix_snowflake_warehouse(c4, "COMPUTE_WH") == 'Source = Snowflake.Databases("host.snowflake.com", "COMPUTE_WH", [Implementation = "2.0"])'


def test_navigation_target_variations():
    n1 = '#"Navigation 3" = #"Navigation 2"{[Name = "STUDENT_COURSES", Kind = "Table"]}[Data]'
    assert _fix_table_navigation_target(n1, "COURSES") == '#"Navigation 3" = #"Navigation 2"{[Name = "COURSES", Kind = "Table"]}[Data]'

    n2 = 'Navigation = Sch{[Name="LEARNING_ANALYTICS",Kind="Table"]}[Data]'
    assert _fix_table_navigation_target(n2, "COURSES") == 'Navigation = Sch{[Name="COURSES",Kind="Table"]}[Data]'

    n3 = '#"Navigation 3" = #"Navigation 2"{[Kind = "Table", Name = "STUDENT_COURSES"]}[Data]'
    assert _fix_table_navigation_target(n3, "COURSES") == '#"Navigation 3" = #"Navigation 2"{[Kind = "Table", Name = "COURSES"]}[Data]'


def test_end_to_end_mapping_json_tables():
    import os
    if not os.path.exists("response/mapping.json"):
        pytest.skip("response/mapping.json not present in repository checkout")

    with open("response/mapping.json", "r") as f:
        raw_mapping = json.load(f)

    mapping = unwrap_mapping(raw_mapping)
    files, report = build_semantic_model(mapping, "Learning Analytics")

    expected_tables = [
        "COURSES",
        "COURSE_INSTRUCTORS",
        "STUDENT_COURSES",
        "INSTRUCTORS",
        "GRADES",
        "STUDENTS",
    ]

    for tbl_name in expected_tables:
        tmdl_path = f"definition/tables/{tbl_name}.tmdl"
        assert tmdl_path in files, f"Missing table TMDL: {tmdl_path}"
        content = files[tmdl_path]

        assert '"COMPUTE_WH"' in content, f"Warehouse COMPUTE_WH missing in {tbl_name}"
        assert f'Name="{tbl_name}"' in content or f'Name = "{tbl_name}"' in content, f"Table navigation missing {tbl_name}"
