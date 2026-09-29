import pytest
from app.qlik.model.relationship_tmdl import (
    _endpoints,
    _is_primary_key_for_table,
    build_relationships,
)


def test_endpoints_power_bi_format():
    rel = {
        "power_bi": {
            "fromTable": "COURSE_INSTRUCTORS",
            "fromColumn": "COURSE_ID",
            "toTable": "COURSES",
            "toColumn": "COURSE_ID",
            "cardinality": "ManyToOne",
            "crossFilterDirection": "Single",
            "isActive": True,
        }
    }
    ft, fc, tt, tc = _endpoints(rel)
    assert ft == "COURSE_INSTRUCTORS"
    assert fc == "COURSE_ID"
    assert tt == "COURSES"
    assert tc == "COURSE_ID"


def test_endpoints_qlik_join_conditions():
    rel = {
        "qlik": {
            "from_table": "COURSES",
            "to_table": "COURSE_INSTRUCTORS",
            "cardinality": "many-to-one",
            "join_conditions": [
                {"left": "COURSES.COURSE_ID", "right": "COURSE_INSTRUCTORS.COURSE_ID"}
            ],
        }
    }
    ft, fc, tt, tc = _endpoints(rel)
    assert ft == "COURSES"
    assert fc == "COURSE_ID"
    assert tt == "COURSE_INSTRUCTORS"
    assert tc == "COURSE_ID"


def test_generalized_primary_key_detection():
    # Single entity dimensions across domains
    assert _is_primary_key_for_table("COURSES", "COURSE_ID", set()) is True
    assert _is_primary_key_for_table("STUDENTS", "STUDENT_ID", set()) is True
    assert _is_primary_key_for_table("INSTRUCTORS", "INSTRUCTOR_ID", set()) is True
    assert _is_primary_key_for_table("Dim_Customers", "CustomerID", set()) is True
    assert _is_primary_key_for_table("tbl_Products", "Product_Code", set()) is True
    assert _is_primary_key_for_table("Expenses", "Expense_ID", set()) is True
    assert _is_primary_key_for_table("Dim_Date", "DateKey", set()) is True

    # Multi-entity bridge / junction / fact tables are NOT PK for individual foreign keys
    assert _is_primary_key_for_table("STUDENT_COURSES", "COURSE_ID", set()) is False
    assert _is_primary_key_for_table("STUDENT_COURSES", "STUDENT_ID", set()) is False
    assert _is_primary_key_for_table("COURSE_INSTRUCTORS", "COURSE_ID", set()) is False
    assert _is_primary_key_for_table("COURSE_INSTRUCTORS", "INSTRUCTOR_ID", set()) is False
    assert _is_primary_key_for_table("Order_Details", "OrderID", set()) is False
    assert _is_primary_key_for_table("Order_Details", "ProductID", set()) is False


def test_build_relationships_flips_and_skips_peer_facts():
    """Verify in a general model that:
    1. Dimension -> Fact is flipped so fromColumn is Many and toColumn is One (PK).
    2. Fact -> Fact peer links are skipped so Fabric doesn't fail on duplicate keys.
    """
    relationships = [
        # Inverted mapping: Dimension -> Fact
        {
            "power_bi": {
                "fromTable": "Dim_Customers",
                "fromColumn": "CustomerID",
                "toTable": "Fact_Orders",
                "toColumn": "CustomerID",
                "cardinality": "ManyToOne",
            }
        },
        # Fact -> Fact link hallucinated by mapping
        {
            "power_bi": {
                "fromTable": "Fact_Orders",
                "fromColumn": "ProductID",
                "toTable": "Fact_Inventory",
                "toColumn": "ProductID",
                "cardinality": "ManyToOne",
            }
        },
    ]

    valid_columns = {
        ("Dim_Customers", "CustomerID"),
        ("Fact_Orders", "CustomerID"),
        ("Fact_Orders", "ProductID"),
        ("Fact_Inventory", "ProductID"),
    }

    tmdl, skipped = build_relationships(relationships, valid_columns, set())

    # Peer fact link is skipped to prevent duplicate key errors in Fabric
    assert len(skipped) == 1
    assert "Fact_Orders.ProductID -> Fact_Inventory.ProductID" in skipped[0]["relationship"]

    # Inverted relationship is flipped to Fact (Many) -> Dimension (One/PK)
    assert "fromColumn: Fact_Orders.CustomerID" in tmdl
    assert "toColumn: Dim_Customers.CustomerID" in tmdl
