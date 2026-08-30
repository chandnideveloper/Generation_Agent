"""Push a PBIP package to GitHub as one commit.

Uses the git data API (blobs -> tree -> commit -> ref) so the whole package
lands atomically. The contents API would need one request per file and would
leave a half-written tree if it failed midway.
"""

from typing import Any, Dict, List, Optional

import requests

from app.config import config
from app.util.logging_utils import get_logger

logger = get_logger(__name__)


class GitHubError(RuntimeError):
    pass


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _check(response: requests.Response, action: str) -> Dict[str, Any]:
    if response.status_code >= 300:
        raise GitHubError(f"{action} failed: {response.status_code} {response.text[:300]}")
    return response.json() if response.text else {}


def _stale_deletions(
    base: str, headers: Dict[str, str], base_tree: str, prefix: str, new_paths: set
) -> List[Dict[str, Any]]:
    """Deletion entries (sha: None) for every blob under `prefix` in
    `base_tree` that isn't in `new_paths` - without these, files removed
    from the package (a dropped table, a renamed measure file) would
    accumulate forever instead of the folder truly mirroring this run."""
    response = requests.get(
        f"{base}/git/trees/{base_tree}", params={"recursive": "1"},
        headers=headers, timeout=60,
    )
    if response.status_code >= 300:
        logger.warning("Could not read existing tree for stale-file cleanup: %s", response.status_code)
        return []

    prefix_slash = f"{prefix}/"
    deletions = []
    for entry in response.json().get("tree", []):
        path = entry.get("path", "")
        if entry.get("type") == "blob" and path.startswith(prefix_slash) and path not in new_paths:
            deletions.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
    if deletions:
        logger.info("Removing %s stale file(s) under %s", len(deletions), prefix)
    return deletions


def deploy(
    package: Dict[str, str],
    prefix: str,
    branch: Optional[str] = None,
    message: Optional[str] = None,
    token: Optional[str] = None,
    org: Optional[str] = None,
    repo: Optional[str] = None,
) -> Dict[str, Any]:
    token = token or config.GITHUB_PAT
    org = org or config.GITHUB_ORG
    repo = repo or config.GITHUB_REPO
    branch = branch or config.BRANCH

    if not all([token, org, repo]):
        raise GitHubError(
            "GitHub deployment needs GITHUB_PAT, GITHUB_ORG and GITHUB_REPO"
        )

    base = f"{config.GITHUB_API}/repos/{org}/{repo}"
    headers = _headers(token)

    # 1. Resolve the branch head, falling back to the default branch or empty repo.
    ref = requests.get(f"{base}/git/ref/heads/{branch}", headers=headers, timeout=60)
    head_sha = None
    base_tree = None
    create_branch = False

    if ref.status_code == 404:
        repo_info = _check(requests.get(base, headers=headers, timeout=60), "read repo")
        default_branch = repo_info.get("default_branch", "main")
        ref_default = requests.get(
            f"{base}/git/ref/heads/{default_branch}", headers=headers, timeout=60
        )
        if ref_default.status_code == 200:
            head_sha = ref_default.json().get("object", {}).get("sha")
            create_branch = True
        else:
            # Completely empty repo (initial commit)
            create_branch = True
            head_sha = None
    else:
        head_sha = _check(ref, "read branch")["object"]["sha"]

    if head_sha:
        commit = _check(
            requests.get(f"{base}/git/commits/{head_sha}", headers=headers, timeout=60),
            "read commit",
        )
        base_tree = commit.get("tree", {}).get("sha")

    # 2. One blob per file.
    tree_entries: List[Dict[str, Any]] = []
    new_paths: set = set()
    for path, content in sorted(package.items()):
        blob = _check(
            requests.post(
                f"{base}/git/blobs", headers=headers,
                json={"content": content, "encoding": "utf-8"}, timeout=120,
            ),
            f"create blob {path}",
        )
        entry_path = f"{prefix}/{path}".strip("/") if prefix else path.strip("/")
        new_paths.add(entry_path)
        tree_entries.append({
            "path": entry_path,
            "mode": "100644",
            "type": "blob",
            "sha": blob["sha"],
        })

    # 2b. Remove files that existed under `prefix` in a previous run but
    # aren't in this one (e.g. a renamed/dropped table's old TMDL file).
    # `base_tree` only ever gets new/updated blobs merged on top of it, so
    # without an explicit deletion (sha: None) a shrinking package would
    # leave stale files behind forever — this is what makes "overwrite the
    # app folder on each run" actually true, not just additive.
    if base_tree and prefix:
        tree_entries.extend(
            _stale_deletions(base, headers, base_tree, prefix, new_paths)
        )

    # 3. Tree -> commit -> ref.
    tree_payload: Dict[str, Any] = {"tree": tree_entries}
    if base_tree:
        tree_payload["base_tree"] = base_tree

    tree = _check(
        requests.post(
            f"{base}/git/trees", headers=headers,
            json=tree_payload, timeout=120,
        ),
        "create tree",
    )
    commit_payload: Dict[str, Any] = {
        "message": message or f"Generate Power BI package for {prefix or 'root'}",
        "tree": tree["sha"],
        "parents": [head_sha] if head_sha else [],
    }
    new_commit = _check(
        requests.post(
            f"{base}/git/commits", headers=headers,
            json=commit_payload,
            timeout=120,
        ),
        "create commit",
    )

    if create_branch:
        _check(
            requests.post(
                f"{base}/git/refs", headers=headers,
                json={"ref": f"refs/heads/{branch}", "sha": new_commit["sha"]}, timeout=60,
            ),
            "create branch",
        )
    else:
        _check(
            requests.patch(
                f"{base}/git/refs/heads/{branch}", headers=headers,
                json={"sha": new_commit["sha"], "force": False}, timeout=60,
            ),
            "update branch",
        )

    logger.info("Pushed %s files to %s/%s@%s", len(package), org, repo, branch)
    return {
        "status": "success",
        "provider": "github",
        "repo": f"{org}/{repo}",
        "branch": branch,
        "commit": new_commit["sha"],
        "path": prefix,
        "file_count": len(package),
    }
