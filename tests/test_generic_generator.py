"""16 Generic Unit/Integration Tests for the Generation Agent.

Verifies that the generator operates dynamically on arbitrary mapping payloads,
with NO report-specific assumptions or hardcoded table/column names.
"""

import pytest
from app.generator import generate
from app.model.datatype_resolver import reconcile_table_datatypes, resolve_column_datatype
from app.model.dax_guard import guard, validate as validate_dax
from app.model.datasource_validator import validate_all_datasources, validate_m_query_placeholders
from app.model.qlik_dax_converter import transform_qlik_expression
from app.package.comprehensive_validator import run_comprehensive_validation
from app.schemas import Deploy, GenerateRequest, Target


# TEST 1: Small report (few tables, few measures, few visuals)
def test_scenario_01_small_report():
    mapping = {
        "app_name": "Small_Synthetic_App",
        "tables": [
            {
                "name": "Categories",
                "columns": [
                    {"name": "CategoryID", "fabric_datatype": "int64", "is_key": True},
                    {"name": "CategoryName", "fabric_datatype": "string"},
                ],
                "m_query": "let Source = #table({\"CategoryID\", \"CategoryName\"}, {}) in Source",
            }
        ],
        "measures": [
            {
                "name": "Total Categories",
                "table": "Categories",
                "dax_expression": "COUNTROWS('Categories')",
            }
        ],
        "app_layout": {
            "sheets": [
                {
                    "title": "Overview",
                    "visuals": [
                        {
                            "id": "v1",
                            "chart_type": "card",
                            "measures": [{"name": "Total Categories"}],
                        }
                    ],
                }
            ]
        },
    }
    res = generate(mapping, GenerateRequest(write_to_disk=False, deploy=Deploy.NONE))
    assert res["generation_status"] == "success"
    assert res["generation"]["tables"] == 1
    assert res["generation"]["measures"] == 1
    assert res["generation"]["pages"] == 1
    assert res["generation"]["visuals"] == 1


# TEST 2: Multiple tables, multiple relationships, multiple pages
def test_scenario_02_multiple_tables_relationships_pages():
    mapping = {
        "app_name": "Multi_Table_App",
        "tables": [
            {
                "name": "DimA",
                "columns": [{"name": "ID_A", "fabric_datatype": "int64", "is_key": True}],
                "m_query": "let Source = #table({\"ID_A\"}, {}) in Source",
            },
            {
                "name": "DimB",
                "columns": [{"name": "ID_B", "fabric_datatype": "int64", "is_key": True}],
                "m_query": "let Source = #table({\"ID_B\"}, {}) in Source",
            },
            {
                "name": "FactT",
                "columns": [
                    {"name": "FactID", "fabric_datatype": "int64", "is_key": True},
                    {"name": "ID_A", "fabric_datatype": "int64"},
                    {"name": "ID_B", "fabric_datatype": "int64"},
                    {"name": "Amount", "fabric_datatype": "double"},
                ],
                "m_query": "let Source = #table({\"FactID\", \"ID_A\", \"ID_B\", \"Amount\"}, {}) in Source",
            },
        ],
        "relationships": [
            {"from_table": "FactT", "from_column": "ID_A", "to_table": "DimA", "to_column": "ID_A", "cardinality": "many_to_one"},
            {"from_table": "FactT", "from_column": "ID_B", "to_table": "DimB", "to_column": "ID_B", "cardinality": "many_to_one"},
        ],
        "app_layout": {
            "sheets": [
                {"title": "Summary Page", "visuals": []},
                {"title": "Detail Page", "visuals": []},
            ]
        },
    }
    res = generate(mapping, GenerateRequest(write_to_disk=False, deploy=Deploy.NONE))
    assert res["generation_status"] == "success"
    assert res["generation"]["tables"] == 3
    assert res["generation"]["relationships"] == 2
    assert res["generation"]["pages"] == 2


# TEST 3: Different datatypes (string, integer, decimal, date, datetime, boolean)
def test_scenario_03_different_datatypes():
    cols = [
        {"name": "C_Str", "fabric_datatype": "string"},
        {"name": "C_Int", "fabric_datatype": "int64"},
        {"name": "C_Dec", "fabric_datatype": "decimal"},
        {"name": "C_Date", "fabric_datatype": "date"},
        {"name": "C_DTime", "fabric_datatype": "dateTime"},
        {"name": "C_Bool", "fabric_datatype": "boolean"},
    ]
    mapping = {
        "app_name": "Datatypes_App",
        "tables": [{"name": "AllTypes", "columns": cols, "m_query": "let Source = #table({\"C_Str\"}, {}) in Source"}],
    }
    res = generate(mapping, GenerateRequest(write_to_disk=False, deploy=Deploy.NONE, include_artifacts=True))
    assert res["generation_status"] == "success"
    tmdl = res["artifacts"]["semantic_model"]["definition/tables/AllTypes.tmdl"]
    assert "dataType: string" in tmdl
    assert "dataType: int64" in tmdl
    assert "dataType: decimal" in tmdl
    assert "dataType: dateTime" in tmdl
    assert "dataType: boolean" in tmdl


# TEST 4: Different visual types (bar, line, pie, card, table)
def test_scenario_04_different_visual_types():
    mapping = {
        "app_name": "Visual_Types_App",
        "tables": [{"name": "Data", "columns": [{"name": "Cat", "fabric_datatype": "string"}, {"name": "Val", "fabric_datatype": "double"}]}],
        "measures": [{"name": "SumVal", "table": "Data", "dax_expression": "SUM('Data'[Val])"}],
        "app_layout": {
            "sheets": [
                {
                    "title": "Charts",
                    "visuals": [
                        {"id": "v1", "chart_type": "barchart", "dimensions": [{"name": "Cat"}], "measures": [{"name": "SumVal"}]},
                        {"id": "v2", "chart_type": "linechart", "dimensions": [{"name": "Cat"}], "measures": [{"name": "SumVal"}]},
                        {"id": "v3", "chart_type": "piechart", "dimensions": [{"name": "Cat"}], "measures": [{"name": "SumVal"}]},
                        {"id": "v4", "chart_type": "card", "measures": [{"name": "SumVal"}]},
                        {"id": "v5", "chart_type": "table", "dimensions": [{"name": "Cat"}], "measures": [{"name": "SumVal"}]},
                    ],
                }
            ]
        },
    }
    res = generate(mapping, GenerateRequest(write_to_disk=False, deploy=Deploy.NONE))
    assert res["generation_status"] == "success"
    assert res["generation"]["visuals"] == 5


# TEST 5: Visual with multiple fields
def test_scenario_05_visual_with_multiple_fields():
    mapping = {
        "app_name": "Multi_Field_App",
        "tables": [{"name": "Sales", "columns": [{"name": "Region", "fabric_datatype": "string"}, {"name": "Country", "fabric_datatype": "string"}, {"name": "Revenue", "fabric_datatype": "double"}]}],
        "measures": [{"name": "Total Rev", "table": "Sales", "dax_expression": "SUM('Sales'[Revenue])"}],
        "app_layout": {
            "sheets": [
                {
                    "title": "Analysis",
                    "visuals": [
                        {"id": "v1", "chart_type": "tableEx", "dimensions": [{"name": "Region"}, {"name": "Country"}], "measures": [{"name": "Total Rev"}]},
                    ],
                }
            ]
        },
    }
    res = generate(mapping, GenerateRequest(write_to_disk=False, deploy=Deploy.NONE))
    assert res["generation_status"] == "success"
    vis_val = res["validation"]["visuals"]
    assert vis_val["total"] == 1
    assert vis_val["valid"] == 1


# TEST 6: Visual with unresolved field binding
def test_scenario_06_visual_with_unresolved_field():
    mapping = {
        "app_name": "Unresolved_Visual_App",
        "tables": [{"name": "T1", "columns": [{"name": "Col1", "fabric_datatype": "string"}]}],
        "app_layout": {
            "sheets": [
                {
                    "title": "Page",
                    "visuals": [
                        {"id": "v1", "chart_type": "barchart", "dimensions": [{"name": "NonExistentColumn"}]},
                    ],
                }
            ]
        },
    }
    res = generate(mapping, GenerateRequest(write_to_disk=False, deploy=Deploy.NONE))
    vis_val = res["validation"]["visuals"]
    assert vis_val["unresolved_bindings"] >= 1
    assert any(w.get("category") == "visual_binding" for w in res["warnings"])


# TEST 7: Filter with valid target
def test_scenario_07_filter_with_valid_target():
    mapping = {
        "app_name": "Valid_Filter_App",
        "tables": [{"name": "Orders", "columns": [{"name": "Status", "fabric_datatype": "string"}]}],
        "app_layout": {
            "filters": [
                {"table": "Orders", "column": "Status", "filter_type": "categorical", "values": ["Shipped"]}
            ],
            "sheets": [{"title": "P1", "visuals": []}],
        },
    }
    res = generate(mapping, GenerateRequest(write_to_disk=False, deploy=Deploy.NONE))
    flt_val = res["validation"]["filters"]
    assert flt_val["valid"] >= 1
    assert flt_val["unresolved"] == 0


# TEST 8: Filter with unresolved target
def test_scenario_08_filter_with_unresolved_target():
    mapping = {
        "app_name": "Unresolved_Filter_App",
        "tables": [{"name": "Orders", "columns": [{"name": "Status", "fabric_datatype": "string"}]}],
        "app_layout": {
            "filters": [
                {"table": "NonExistentTable", "column": "NonExistentColumn", "filter_type": "categorical", "values": ["X"]}
            ],
            "sheets": [{"title": "P1", "visuals": []}],
        },
    }
    res = generate(mapping, GenerateRequest(write_to_disk=False, deploy=Deploy.NONE))
    flt_val = res["validation"]["filters"]
    assert flt_val["unresolved"] >= 1


# TEST 9: Valid datasource
def test_scenario_09_valid_datasource():
    mapping = {
        "app_name": "Valid_Datasource_App",
        "tables": [
            {
                "name": "DimCustomer",
                "columns": [{"name": "CustID", "fabric_datatype": "int64"}],
                "connection": {"server": "sql-prod.database.windows.net", "database": "AnalyticsDB"},
                "m_query": "let Source = Sql.Database(\"sql-prod.database.windows.net\", \"AnalyticsDB\") in Source",
            }
        ],
    }
    ok, issues = validate_all_datasources(mapping)
    assert ok is True
    assert len(issues) == 0


# TEST 10: Unresolved datasource (placeholders like <<server>>, <<database>>)
def test_scenario_10_unresolved_datasource_blocks_deploy():
    mapping = {
        "app_name": "Unresolved_Datasource_App",
        "tables": [
            {
                "name": "Instructors",
                "columns": [{"name": "ID", "fabric_datatype": "int64"}],
                "m_query": "let Source = Sql.Database(\"<<server>>\", \"<<database>>\") in Source",
            }
        ],
    }
    ok, issues = validate_all_datasources(mapping)
    assert ok is False
    assert any(i.get("placeholder") in ("<<server>>", "<<database>>") for i in issues)

    res = generate(mapping, GenerateRequest(write_to_disk=False, deploy=Deploy.GITHUB, git_pat="dummy"))
    assert res["validation_status"] == "failed"
    assert res["deployment_status"] == "blocked"
    assert any(e.get("category") == "datasource" for e in res["errors"])


# TEST 11: Simple Qlik expression safely converted
def test_scenario_11_simple_qlik_expression():
    expr = "AVERAGE(SWITCH(Match('T'[Grade], 'A', 'B'), 1, 4.0, 2, 3.0, BLANK()))"
    res, status, reason = transform_qlik_expression(expr)
    assert status == "safely_transformed"
    assert "AVERAGEX('T', SWITCH('T'[Grade], \"A\", 4.0, \"B\", 3.0, BLANK()))" == res


# TEST 12: Qlik expression requiring rewrite (AGGR / Set Analysis)
def test_scenario_12_qlik_expression_requiring_rewrite():
    expr = "Sum(Aggr(Max(Score), StudentID))"
    res, status, reason = transform_qlik_expression(expr)
    assert status == "rewrite_required"
    assert "AGGR" in reason

    safe_dax, problem = guard("ComplexMeasure", expr)
    assert safe_dax == "BLANK()"
    assert problem is not None
    assert problem["status"] == "rewrite_required"


# TEST 13: Datatype conflict between mapping / M / TMDL
def test_scenario_13_datatype_conflict_reconciled():
    cols = [{"name": "UserID", "fabric_datatype": "string"}]
    m_query = 'let Source = Table.TransformColumnTypes(Source, {{"UserID", Int64.Type}}) in Source'
    resolved, issues = reconcile_table_datatypes(cols, m_query)
    assert resolved["userid"] == "int64"
    assert len(issues) == 1
    assert issues[0]["resolved_type"] == "int64"


# TEST 14: Invalid generated DAX (syntax / unbalanced parenthesis)
def test_scenario_14_invalid_dax_detected():
    bad_dax = "CALCULATE(SUM('Sales'[Amount]), 'Sales'[Year] = 2024"  # missing closing paren
    problem = validate_dax(bad_dax)
    assert problem is not None
    assert "unbalanced" in problem["reason"]


# TEST 15: Valid complete report
def test_scenario_15_valid_complete_report():
    mapping = {
        "app_name": "Complete_Valid_Report",
        "tables": [
            {
                "name": "Products",
                "columns": [
                    {"name": "ProductID", "fabric_datatype": "int64", "is_key": True},
                    {"name": "Price", "fabric_datatype": "double"},
                ],
                "m_query": "let Source = #table({\"ProductID\", \"Price\"}, {}) in Source",
            }
        ],
        "measures": [
            {"name": "AvgPrice", "table": "Products", "dax_expression": "AVERAGE('Products'[Price])"}
        ],
        "app_layout": {
            "sheets": [
                {
                    "title": "Pricing",
                    "visuals": [
                        {"id": "v1", "chart_type": "card", "measures": [{"name": "AvgPrice"}]}
                    ],
                }
            ]
        },
    }
    res = generate(mapping, GenerateRequest(write_to_disk=False, deploy=Deploy.NONE))
    assert res["generation_status"] == "success"
    assert res["validation_status"] == "success"
    assert len(res["errors"]) == 0


# TEST 16: Incomplete mapping handled gracefully
def test_scenario_16_incomplete_mapping_graceful():
    mapping = {
        "app_name": "Incomplete_App",
        # Empty tables and empty sheets
        "tables": [],
        "app_layout": {},
    }
    res = generate(mapping, GenerateRequest(write_to_disk=False, deploy=Deploy.NONE))
    assert res["generation_status"] == "success"
    assert res["generation"]["tables"] == 0
