"""GitHub/DevOps push previously only ever added or updated files - a table
removed or renamed between runs left its old file behind forever, even
though the whole point of "one folder per app, overwritten each run" is
that the folder should exactly mirror the latest run.
"""

from unittest.mock import MagicMock, patch

from app.deploy import devops_deployer, github_deployer


def _resp(status=200, json_body=None, text="ok"):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body or {}
    r.text = text
    return r


def test_github_deploy_removes_stale_files_under_prefix():
    def get_side_effect(url, headers=None, timeout=None, params=None):
        if url.endswith("/git/ref/heads/main"):
            return _resp(200, {"object": {"sha": "head-sha"}})
        if url.endswith("/git/commits/head-sha"):
            return _resp(200, {"tree": {"sha": "base-tree-sha"}})
        if url.endswith("/git/trees/base-tree-sha"):
            return _resp(200, {"tree": [
                {"path": "myapp/Old.SemanticModel/model.tmdl", "type": "blob"},
                {"path": "myapp/keep.txt", "type": "blob"},
                {"path": "other-app/unrelated.txt", "type": "blob"},
            ]})
        raise AssertionError(f"unexpected GET {url}")

    created_trees = []

    def post_side_effect(url, headers=None, json=None, timeout=None):
        if url.endswith("/git/blobs"):
            return _resp(201, {"sha": f"blob-{json['content'][:8]}"})
        if url.endswith("/git/trees"):
            created_trees.append(json)
            return _resp(201, {"sha": "new-tree-sha"})
        if url.endswith("/git/commits"):
            return _resp(201, {"sha": "new-commit-sha"})
        if url.endswith("/refs"):
            return _resp(201, {})
        raise AssertionError(f"unexpected POST {url}")

    def patch_side_effect(url, headers=None, json=None, timeout=None):
        if "/git/refs/heads/main" in url:
            return _resp(200, {})
        raise AssertionError(f"unexpected PATCH {url}")

    with patch("requests.get", side_effect=get_side_effect), \
         patch("requests.post", side_effect=post_side_effect), \
         patch("requests.patch", side_effect=patch_side_effect):
        result = github_deployer.deploy(
            {"keep.txt": "still here"}, "myapp",
            token="tok", org="org", repo="repo",
        )

    assert result["status"] == "success"
    tree = created_trees[0]["tree"]
    paths = {e["path"]: e for e in tree}

    assert paths["myapp/keep.txt"]["sha"] != None  # noqa: E711 - real blob sha, present
    assert paths["myapp/Old.SemanticModel/model.tmdl"]["sha"] is None  # deletion
    assert "other-app/unrelated.txt" not in paths  # untouched, outside prefix


def test_devops_deploy_removes_stale_files_under_prefix():
    def get_side_effect(url, headers=None, params=None, timeout=None):
        if url.endswith("/refs"):
            return _resp(200, {"value": [{"objectId": "old-obj-id"}]})
        if url.endswith("/items"):
            return _resp(200, {"value": [
                {"path": "/myapp/Old.SemanticModel/model.tmdl", "isFolder": False},
                {"path": "/myapp/keep.txt", "isFolder": False},
                {"path": "/other-app/unrelated.txt", "isFolder": False},
            ]})
        raise AssertionError(f"unexpected GET {url}")

    def post_side_effect(url, headers=None, params=None, json=None, timeout=None):
        assert url.endswith("/pushes")
        return _resp(201, {"commits": [{"commitId": "abc123"}]})

    captured = {}

    def post_capture(url, headers=None, params=None, json=None, timeout=None):
        captured["payload"] = json
        return post_side_effect(url, headers, params, json, timeout)

    with patch("requests.get", side_effect=get_side_effect), \
         patch("requests.post", side_effect=post_capture):
        result = devops_deployer.deploy(
            {"keep.txt": "still here"}, "myapp",
            pat="pat", org="org", project="proj", repo="repo",
        )

    assert result["status"] == "success"
    changes = captured["payload"]["commits"][0]["changes"]
    by_path = {c["item"]["path"]: c for c in changes}

    assert by_path["/myapp/keep.txt"]["changeType"] == "edit"
    assert by_path["/myapp/Old.SemanticModel/model.tmdl"]["changeType"] == "delete"
    assert "/other-app/unrelated.txt" not in by_path
