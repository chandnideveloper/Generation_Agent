"""Tests for fine-grained Qlik Cloud to Fabric/PBIR mappings in Generation Agent."""

import json
from app.report.visual_builder import build_visual
from app.report.filters import build_filter_config
from app.model.measure_tmdl import build_measure
from app.model.table_tmdl import build_column


def test_visual_styling_and_custom_coloring():
    visual = {
        "qlik_source": {
            "qlik_type": "barchart",
            "title": "Revenue Analysis",
            "style": {
                "title_color": "#112233",
                "font_size": "16pt",
                "background_color": "#F0F0F0"
            },
            "custom_coloring": {
                "single_color": "#34D399"
            },
            "reference_lines": [
                {
                    "label": "Threshold",
                    "expression": "5000",
                    "color": "#FF5500",
                    "line_type": "dashed",
                    "show_label": True
                }
            ],
            "trend_lines": [
                {
                    "type": "linear",
                    "line_type": "solid"
                }
            ],
            "dimensions": [{"name": "region_name"}],
            "measures": [{"name": "Total Revenue"}]
        }
    }
    doc, note, applied = build_visual(
        visual,
        {"x": 0, "y": 0, "width": 400, "height": 300},
        1,
        {"Total Revenue": "Sales"},
        {"region_name": "Regions"}
    )

    objects = doc["visual"]["objects"]

    # 1. Title styling
    assert "title" in objects
    title_props = objects["title"][0]["properties"]
    assert title_props["fontColor"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#112233'"
    assert title_props["fontSize"]["expr"]["Literal"]["Value"] == "16.0D"

    # 2. Background styling
    assert "background" in objects
    bg_props = objects["background"][0]["properties"]
    assert bg_props["show"]["expr"]["Literal"]["Value"] == "true"
    assert bg_props["color"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#F0F0F0'"

    # 3. Single color dataPoint fill
    assert "dataPoint" in objects
    dp_props = objects["dataPoint"][0]["properties"]
    assert dp_props["fill"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#34D399'"

    # 4. Reference lines
    assert "valueAxisReferenceLine" in objects
    ref_props = objects["valueAxisReferenceLine"][0]["properties"]
    assert ref_props["displayName"]["expr"]["Literal"]["Value"] == "'Threshold'"
    assert ref_props["lineColor"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#FF5500'"
    assert ref_props["lineStyle"]["expr"]["Literal"]["Value"] == "'dashed'"
    assert ref_props["value"]["expr"]["Literal"]["Value"] == "5000.0D"

    # 5. Trend lines
    assert "trendLine" in objects
    trend_props = objects["trendLine"][0]["properties"]
    assert trend_props["show"]["expr"]["Literal"]["Value"] == "true"
    assert trend_props["style"]["expr"]["Literal"]["Value"] == "'solid'"


def test_dimension_limitations_and_null_suppression():
    visual = {
        "qlik_source": {
            "measures": [{"name": "Sales"}],
            "dimensions": [
                {
                    "name": "customer_name",
                    "limitations": {
                        "limitation_type": "Fixed number",
                        "direction": "Bottom",
                        "limit_value": 5,
                        "suppress_null": True
                    }
                }
            ]
        }
    }
    config = build_filter_config(visual, {"customer_name": "Customers"}, {"Sales": "Orders"})
    filters = config["filters"]
    assert len(filters) == 2

    # TopN / BottomN filter
    top_n = next(f for f in filters if f["type"] == "TopN")
    cond = top_n["filter"]["Where"][0]["Condition"]["Not"]["Expression"]["TopN"]
    assert cond["Top"] == 5
    assert cond["OrderBy"][0]["Direction"] == 1  # 1 for Ascending (Bottom)

    # Not-blank null suppression filter
    not_blank = next(f for f in filters if f["type"] == "Categorical")
    nb_cond = not_blank["filter"]["Where"][0]["Condition"]["Not"]["Expression"]["Comparison"]
    assert nb_cond["ComparisonKind"] == 0
    assert nb_cond["Right"]["Literal"]["Value"] == "null"


def test_measure_and_column_num_format_translation():
    # Measure with Qlik currency num_format
    measure = {
        "name": "Margin",
        "fabric": {
            "dax_expression": "SUM('Sales'[margin])"
        },
        "num_format": {
            "type": "M",
            "format_pattern": "$#,##0.00"
        }
    }
    tmdl = build_measure(measure)
    assert 'formatString: "$#,##0.00"' in tmdl

    # Measure with Qlik percentage num_format
    measure_pct = {
        "name": "Margin Rate",
        "fabric": {
            "dax_expression": "DIVIDE([Margin], [Revenue], 0)"
        },
        "num_format": {
            "type": "P"
        }
    }
    tmdl_pct = build_measure(measure_pct)
    assert 'formatString: "0.00%"' in tmdl_pct

    # Column with num_format
    col = {
        "name": "unit_price",
        "fabric_datatype": "double",
        "num_format": {
            "type": "M"
        }
    }
    col_tmdl = build_column(col, "Sales")
    assert "formatString: $#,##0.00" in col_tmdl
