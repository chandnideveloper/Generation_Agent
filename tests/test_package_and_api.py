"""Packaging, deployment wiring and the HTTP surface."""

import base64
import json

import pytest
import requests

from app.package import fabric_packager
from app.schemas import Deploy, GenerateRequest, Target


# --- packaging ------------------------------------------------------------

def test_fabric_parts_are_base64_and_item_relative(package):
    items = fabric_packager.split_items(package, "FleetVision KSA")
    assert set(items) == {"semantic_model", "report"}

    for parts in items.values():
        for part in parts:
            assert part["payloadType"] == "InlineBase64"
            # Paths are relative to the item, not the PBIP folder.
            assert not part["path"].startswith("FleetVision KSA.")
            base64.b64decode(part["payload"])

    model_paths = {p["path"] for p in items["semantic_model"]}
    assert "definition/model.tmdl" in model_paths
    assert ".platform" in model_paths


def test_report_binding_is_rewritten_for_fabric(package):
    items = fabric_packager.split_items(package, "FleetVision KSA")
    bound = fabric_packager.report_definition_with_binding(items["report"], "dataset-123")

    pbir = next(p for p in bound if p["path"] == "definition.pbir")
    decoded = json.loads(base64.b64decode(pbir["payload"]).decode("utf-8"))
    # On disk the report points at a relative path; in a workspace it must
    # point at the deployed model's id.
    assert "byPath" not in decoded["datasetReference"]
    assert decoded["datasetReference"]["byConnection"]["pbiModelDatabaseName"] == "dataset-123"


def test_semantic_model_only_target_emits_no_report(mapping):
    from app.generator import generate

    result = generate(
        mapping,
        GenerateRequest(target=Target.SEMANTIC_MODEL_ONLY, write_to_disk=False),
    )
    assert result["summary"]["report"] == {}
    assert result["visual_notes"] == []


def test_package_writes_to_disk(mapping, tmp_path):
    from app.package import pbip_packager

    written = pbip_packager.write_to_disk({"a/b.tmdl": "x", ".gitignore": "y"}, str(tmp_path))
    assert written["file_count"] == 2
    assert (tmp_path / "a" / "b.tmdl").read_text() == "x"


# --- deployment -----------------------------------------------------------

def test_no_deployment_by_default(result):
    assert result["deployment"]["status"] == "skipped"


def test_deployment_failure_does_not_lose_the_package(mapping, monkeypatch):
    from app.deploy import fabric_deployer
    from app.generator import generate

    def boom(*args, **kwargs):
        raise fabric_deployer.FabricError("workspace unreachable")

    monkeypatch.setattr(fabric_deployer, "deploy", boom)
    result = generate(
        mapping,
        GenerateRequest(
            target=Target.FABRIC, deploy=Deploy.FABRIC,
            workspace_id="w", fabric_access_token="t", write_to_disk=False,
        ),
    )
    assert result["status"] == "success"
    assert result["deployment"]["status"] == "error"
    assert "unreachable" in result["deployment"]["error"]
    assert result["file_count"] > 0


def test_fabric_deploy_requires_workspace_and_token():
    from app.deploy import fabric_deployer

    with pytest.raises(fabric_deployer.FabricError):
        fabric_deployer.deploy({}, "App", "", "")


def test_github_deploy_requires_credentials(monkeypatch):
    from app.config import config
    from app.deploy import github_deployer

    monkeypatch.setattr(config, "GITHUB_PAT", "")
    with pytest.raises(github_deployer.GitHubError, match="GITHUB_PAT"):
        github_deployer.deploy({"a": "b"}, "prefix")


def test_devops_deploy_requires_credentials(monkeypatch):
    from app.config import config
    from app.deploy import devops_deployer

    monkeypatch.setattr(config, "DEVOPS_PAT", "")
    with pytest.raises(devops_deployer.DevOpsError, match="AZURE_DEVOPS_PAT"):
        devops_deployer.deploy({"a": "b"}, "prefix")


def test_fabric_deploy_creates_model_then_report(mapping, monkeypatch):
    """The report must be bound to the model's id, so ordering matters."""
    from app.deploy import fabric_deployer
    from app.package import pbip_packager
    from app.model.semantic_model import build_semantic_model
    from app.report.report_writer import build_report

    calls = []

    def fake_create(workspace_id, display_name, item_type, parts, token):
        calls.append(item_type)
        return f"{item_type.lower()}-id"

    monkeypatch.setattr(fabric_deployer, "create_or_update", fake_create)

    model_files, _ = build_semantic_model(mapping, "App")
    report_files, _, _ = build_report(mapping, "App", "../App.SemanticModel")
    package = pbip_packager.build_package("App", model_files, report_files)

    result = fabric_deployer.deploy(package, "App", "workspace-1", "token")
    assert calls == ["SemanticModel", "Report"]
    assert result["items"] == {
        "semantic_model": "semanticmodel-id", "report": "report-id"
    }


# --- API ------------------------------------------------------------------

def test_health_lists_targets(client):
    body = client.get("/health").json()
    assert body["status"] == "healthy"
    assert "powerbi_desktop" in body["targets"]


def test_visual_support_endpoint_documents_gaps(client):
    body = client.get("/visual-support").json()
    assert "barchart" in body["native"]
    assert "boxplot" in body["substituted"]
    assert body["substituted"]["boxplot"]["mapped_to"] == "columnChart"
    assert body["substituted"]["boxplot"]["suggestion"]


def test_generate_requires_an_identifier(client):
    assert client.post("/generate", json={}).status_code == 400


def test_generate_accepts_an_inline_mapping(client, mapping):
    response = client.post(
        "/generate",
        json={
            "mapping_result": mapping,
            "target": "fabric",
            "write_to_disk": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["file_count"] > 0
    assert body["summary"]["semantic_model"]["tables"] > 0


def test_missing_mapping_returns_404(client, monkeypatch):
    from app.sources import mapping_client

    def boom(app_id, run_id):
        raise mapping_client.MappingNotFound("nothing stored")

    monkeypatch.setattr("app.api.routes.fetch_mapping", boom)
    response = client.post("/generate", json={"app_id": "ghost"})
    assert response.status_code == 404
