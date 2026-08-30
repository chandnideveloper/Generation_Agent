"""The response must carry the generated files, not just counts."""

import json

from app.generator import generate
from app.package import artifacts
from app.schemas import GenerateRequest, Target


def _result(mapping, **overrides):
    return generate(
        mapping,
        GenerateRequest(target=Target.FABRIC, write_to_disk=False, **overrides),
    )


def test_artifacts_are_returned_by_default(mapping):
    result = _result(mapping)
    tree = result["artifacts"]
    assert tree is not None
    assert set(tree) == {"pbip", "semantic_model", "report", "other"}
    assert tree["semantic_model"] and tree["report"]


def test_every_generated_file_appears_in_the_tree(mapping):
    result = _result(mapping)
    tree = result["artifacts"]
    in_tree = (
        len(tree["semantic_model"]) + len(tree["report"]) + len(tree["other"])
        + (1 if tree["pbip"] is not None else 0)
    )
    assert in_tree == result["file_count"]


def test_tmdl_is_returned_as_readable_text(mapping):
    model = _result(mapping)["artifacts"]["semantic_model"]
    database = model["definition/database.tmdl"]
    assert isinstance(database, str)
    assert database.startswith("database")
    assert "compatibilityLevel" in database


def test_json_files_are_returned_as_objects_not_strings(mapping):
    tree = _result(mapping)["artifacts"]
    # Parsed, so a caller can index straight into them.
    assert isinstance(tree["pbip"], dict)
    assert isinstance(tree["semantic_model"][".platform"], dict)
    assert tree["report"]["definition/pages/pages.json"]["pageOrder"]

    visual_path = next(p for p in tree["report"] if p.endswith("visual.json"))
    assert tree["report"][visual_path]["visual"]["visualType"]


def test_paths_are_item_relative(mapping):
    tree = _result(mapping)["artifacts"]
    for section in ("semantic_model", "report"):
        for path in tree[section]:
            assert not path.startswith("FleetVision"), path
            assert not path.startswith("/"), path


def test_total_bytes_matches_the_package(mapping):
    result = _result(mapping)
    tree = result["artifacts"]
    rendered = sum(
        len(value if isinstance(value, str) else json.dumps(value))
        for section in ("semantic_model", "report", "other")
        for value in tree[section].values()
    )
    assert result["total_bytes"] > 0
    # JSON is re-serialized when parsed, so allow drift but not an order of
    # magnitude — this catches a section going missing.
    assert rendered > result["total_bytes"] * 0.5


def test_artifacts_can_be_suppressed(mapping):
    result = _result(mapping, include_artifacts=False)
    assert result.get("artifacts") is None
    assert result["file_count"] > 0  # counts still reported


def test_index_only_returns_paths_and_sizes(mapping):
    result = _result(mapping, index_only=True)
    assert result.get("artifacts") is None
    index = result["artifact_index"]
    assert index["semantic_model"] and index["report"]
    entry = index["semantic_model"][0]
    assert set(entry) == {"path", "bytes"}
    assert entry["bytes"] > 0

    listed = sum(len(v) for v in index.values())
    assert listed == result["file_count"]


def test_unparseable_json_is_kept_as_text():
    tree = artifacts.build_tree({"App.Report/broken.json": "{not json"}, "App")
    assert tree["report"]["broken.json"] == "{not json"


def test_semantic_model_only_has_no_report_section(mapping):
    result = generate(
        mapping,
        GenerateRequest(
            target=Target.SEMANTIC_MODEL_ONLY, write_to_disk=False
        ),
    )
    tree = result["artifacts"]
    assert tree["semantic_model"]
    assert tree["report"] == {}
    assert tree["pbip"] is None


def test_response_model_accepts_the_tree(client, mapping):
    """The FastAPI response_model must not strip the artifacts."""
    response = client.post(
        "/generate",
        json={"mapping_result": mapping, "target": "fabric", "write_to_disk": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["artifacts"]["semantic_model"]
    assert body["artifacts"]["report"]
    assert body["total_bytes"] > 0
