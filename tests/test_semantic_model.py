"""The TMDL semantic model must be loadable by Power BI Desktop."""

import json
import re

from app.model.dax_guard import guard, validate
from app.model.datatypes import summarize_by, to_tmdl_type
from app.model.semantic_model import build_semantic_model

TMDL_TYPES = {"string", "int64", "double", "decimal", "dateTime", "boolean", "binary"}


def test_model_folder_has_every_required_file(package):
    root = "FleetVision KSA.SemanticModel"
    for required in (
        f"{root}/definition/model.tmdl",
        f"{root}/definition/database.tmdl",
        f"{root}/definition.pbism",
        f"{root}/.platform",
    ):
        assert required in package, f"missing {required}"


def test_every_table_is_referenced_by_the_model(package):
    model = package["FleetVision KSA.SemanticModel/definition/model.tmdl"]
    tables = [
        path.split("/")[-1][:-5]
        for path in package
        if "/definition/tables/" in path and path.endswith(".tmdl")
    ]
    assert tables
    for name in tables:
        assert f"ref table {name}" in model or f"ref table '{name}'" in model


def test_every_column_declares_a_legal_data_type(package):
    for path, content in package.items():
        if "/definition/tables/" not in path:
            continue
        for declared in re.findall(r"dataType:\s*(\w+)", content):
            assert declared in TMDL_TYPES, f"{path} declares dataType {declared}"


def test_tables_have_a_partition_with_a_source(package):
    for path, content in package.items():
        if "/definition/tables/" not in path:
            continue
        assert "partition " in content, f"{path} has no partition"
        assert "mode: import" in content
        assert "source =" in content
        # An empty source makes Desktop reject the whole model.
        source = content.split("source =", 1)[1]
        assert source.strip(), f"{path} has an empty partition source"


def test_relationships_never_reference_an_empty_table(package):
    path = "FleetVision KSA.SemanticModel/definition/relationships.tmdl"
    content = package.get(path, "")
    assert content
    assert "''." not in content, "relationship references an unresolved table"
    for line in content.splitlines():
        if "Column:" in line:
            assert re.search(r"Column:\s*\S+\.\S+", line), line


def test_relationship_direction_follows_cardinality(mapping):
    """one-to-many must be flipped: Power BI's from side is the many side."""
    from app.model.relationship_tmdl import build_relationships

    tmdl, skipped = build_relationships(
        [{
            "source_table": "Trucks", "source_column": "truck_id",
            "target_table": "Maintenance", "target_column": "truck_id",
            "qlik_relationship_type": "one-to-many",
        }],
        {("Trucks", "truck_id"), ("Maintenance", "truck_id")},
        key_columns=set(),
    )
    assert "fromColumn: Maintenance.truck_id" in tmdl
    assert "toColumn: Trucks.truck_id" in tmdl
    assert skipped == []


def test_relationships_with_unknown_columns_are_skipped_and_reported():
    from app.model.relationship_tmdl import build_relationships

    tmdl, skipped = build_relationships(
        [{"source_table": "A", "source_column": "x",
          "target_table": "Ghost", "target_column": "y"}],
        {("A", "x")},
    )
    assert tmdl.strip() == ""
    assert len(skipped) == 1
    assert "do not exist" in skipped[0]["reason"]


def test_measures_are_attached_to_their_table(package):
    loads = package["FleetVision KSA.SemanticModel/definition/tables/Loads.tmdl"]
    assert "measure 'Total Revenue'" in loads
    assert "DIVIDE(SUM('Loads'[revenue])" in loads


def test_no_measure_leaks_qlik_syntax_into_dax(package):
    """A single bad expression stops the whole model from loading."""
    banned = re.compile(r"=\s*[^\n]*(\bAggr\s*\(|\{<|\$\(|\bApplyMap\s*\()", re.IGNORECASE)
    for path, content in package.items():
        if "/definition/tables/" not in path:
            continue
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("///"):
                continue  # the preserved original, in a comment
            assert not banned.search(stripped), f"{path}: {stripped[:90]}"


def test_unconvertible_measures_keep_their_original_in_a_comment(package):
    loads = package["FleetVision KSA.SemanticModel/definition/tables/Loads.tmdl"]
    assert "/// TODO" in loads
    assert "/// Original Qlik:" in loads
    assert "= BLANK()" in loads


def test_dax_guard_detects_each_qlik_construct():
    assert validate("Max(Aggr(Sum(x), y))")["reason"].startswith("Qlik AGGR")
    assert "set analysis" in validate("SUM({<A={'x'}>} B)")["reason"]
    assert "dollar expansion" in validate("SUM($(vX))")["reason"]
    assert "ApplyMap" in validate("ApplyMap('M', k)")["reason"]
    assert "unbalanced" in validate("SUM('T'[a]")["reason"]
    assert "nested column reference" in validate("'A'['B'[c]]")["reason"]
    assert validate("DIVIDE(SUM('T'[a]), 100, 0)") is None


def test_guard_replaces_bad_dax_but_keeps_the_original():
    safe, problem = guard("M", "Max(Aggr(Sum(x), y))")
    assert safe == "BLANK()"
    assert problem["original"] == "Max(Aggr(Sum(x), y))"
    assert problem["suggestion"]

    safe, problem = guard("M", "SUM('T'[a])")
    assert safe == "SUM('T'[a])" and problem is None


def test_datatype_normalisation():
    assert to_tmdl_type("NUMBER") == "double"
    assert to_tmdl_type("STRING (CALCULATED)") == "string"
    assert to_tmdl_type("TIMESTAMP") == "dateTime"
    assert to_tmdl_type(None) == "string"
    # Keys must never aggregate.
    assert summarize_by("int64", is_key=True) == "none"
    assert summarize_by("int64") == "sum"
    assert summarize_by("string") == "none"


def test_derived_date_part_columns_are_not_forced_to_datetime():
    # Qlik reports Year(Date) as Date_year / QuarterName(Date) as
    # Date_quarterLabel with the parent Date field's own dataType ("dateTime"
    # / "DATE"), even though the real values are an int year or a text
    # label. Casting these to `type datetime` in Power Query breaks the
    # column on refresh and produces PBI Desktop's "Something's wrong with
    # one or more fields" on any visual bound to them.
    assert to_tmdl_type("dateTime", col_name="Date_year") == "int64"
    assert to_tmdl_type("dateTime", col_name="Date_quarterLabel") == "string"
    assert to_tmdl_type("dateTime", col_name="Date_monthLabel") == "string"
    # A genuine date column must still come through untouched.
    assert to_tmdl_type("dateTime", col_name="OrderDate") == "dateTime"
    assert summarize_by("int64", col_name="Date_year") == "none"


def test_generation_is_deterministic(mapping):
    """Same input, byte-identical output — so git diffs stay meaningful."""
    first, _ = build_semantic_model(mapping, "App")
    second, _ = build_semantic_model(mapping, "App")
    assert first == second


def test_platform_files_are_valid_json(package):
    for path, content in package.items():
        if path.endswith((".platform", ".pbism", ".pbir", ".pbip")):
            json.loads(content)
