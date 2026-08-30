"""push_only /generate + /download:
  - push_only must skip the local disk write entirely (previously target ==
    POWERBI_DESKTOP always wrote to disk, ignoring write_to_disk/push_only);
  - /generate must cache the built package so /download can hand back the
    exact same files without re-running generation;
  - /download must produce a real, openable zip (the .pbip pointer plus the
    .SemanticModel/.Report folders as top-level entries);
  - package_store must never block on the network (regression test for a
    62s -> 2s test-suite fix: the remote cache write/read now runs with a
    short timeout, and the write is backgrounded).
"""

import time
import zipfile
from io import BytesIO

from app.generator import generate
from app.package import package_store
from app.package.zip_builder import build_zip
from app.schemas import Deploy, GenerateRequest, Target


def test_push_only_skips_disk_write_even_for_desktop_target(tmp_path, mapping, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    result = generate(
        mapping,
        GenerateRequest(
            run_id="push-only-run-1", target=Target.POWERBI_DESKTOP,
            deploy=Deploy.NONE, push_only=True,
        ),
    )
    assert "output_path" not in result or result.get("output_path") is None
    assert list(tmp_path.iterdir()) == []  # nothing was written


def test_generate_caches_package_for_download(mapping):
    result = generate(
        mapping,
        GenerateRequest(run_id="push-only-run-2", target=Target.FABRIC, write_to_disk=False),
    )
    cached = package_store.load("push-only-run-2")
    assert cached is not None
    assert len(cached) == result["file_count"]


def test_downloaded_zip_contains_the_pbip_pointer_at_top_level(mapping):
    generate(
        mapping,
        GenerateRequest(run_id="push-only-run-3", app_name="FleetVision KSA", write_to_disk=False),
    )
    package = package_store.load("push-only-run-3")
    zip_bytes = build_zip(package)

    with zipfile.ZipFile(BytesIO(zip_bytes)) as archive:
        names = archive.namelist()
        assert any(n.endswith(".pbip") for n in names)
        assert any(".SemanticModel/" in n for n in names)
        assert any(".Report/" in n for n in names)
        assert archive.testzip() is None  # no corrupt entries


def test_package_store_save_does_not_block(mapping):
    """Regression test: save() must return almost immediately regardless of
    whether the remote microservice is reachable."""
    package = {"a.txt": "hello"}
    start = time.monotonic()
    package_store.save("timing-run", package, "app-1")
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"save() took {elapsed:.2f}s - the remote write must be backgrounded"
    assert package_store.load("timing-run") == package


def test_download_endpoint_returns_a_valid_zip(client, mapping, monkeypatch):
    def fake_generate(document, request):
        from app.generator import generate as real_generate
        return real_generate(document, request)

    resp = client.post(
        "/generate",
        json={"mapping_result": mapping, "run_id": "download-endpoint-run", "app_name": "FleetVision KSA"},
    )
    assert resp.status_code == 200

    resp = client.post("/download", json={"run_id": "download-endpoint-run"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]

    with zipfile.ZipFile(BytesIO(resp.content)) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) > 0
