import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="session")
def mapping():
    """A real mapping-agent Contract 2.0 result (FleetVision KSA)."""
    with open(FIXTURES / "mapping_result.json", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="session")
def package(mapping):
    """The generated PBIP file map, built once."""
    from app.generator import generate
    from app.schemas import GenerateRequest, Target

    from app.model.semantic_model import build_semantic_model
    from app.package import pbip_packager
    from app.report.report_writer import build_report

    model_files, _ = build_semantic_model(mapping, "FleetVision KSA")
    report_files, _, _ = build_report(
        mapping, "FleetVision KSA", "../FleetVision KSA.SemanticModel"
    )
    return pbip_packager.build_package("FleetVision KSA", model_files, report_files)


@pytest.fixture(scope="session")
def result(mapping):
    """Full generation result without touching disk or the network."""
    from app.generator import generate
    from app.schemas import GenerateRequest, Target

    return generate(
        mapping,
        GenerateRequest(target=Target.FABRIC, write_to_disk=False),
    )


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)
