"""Push a PBIP package to GitHub as one commit.

Uses the git data API (blobs -> tree -> commit -> ref) so the whole package
lands atomically. The contents API would need one request per file and would
leave a half-written tree if it failed midway.
"""

from typing import Any, Dict, List, Optional, Tuple

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
    base: str, headers: Dict[str, str], base_tree: str, prefix: str, new_paths: set, session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """Deletion entries (sha: None) for every blob under `prefix` in
    `base_tree` that isn't in `new_paths`."""
    client = session or requests
    response = client.get(
        f"{base}/git/trees/{base_tree}", params={"recursive": "1"},
        timeout=60,
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


import concurrent.futures
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def _get_session(token: str) -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=25, pool_maxsize=25)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    return session


def _parse_github_repo(repo_input: Optional[str], org_input: Optional[str]) -> Tuple[str, str]:
    """Parse org and repo name from URL, owner/repo string, or bare name."""
    repo = (repo_input or "").strip()
    org = (org_input or "").strip()

    if repo.startswith("http://") or repo.startswith("https://") or repo.startswith("git@"):
        clean = repo.rstrip("/").removesuffix(".git")
        if "github.com/" in clean:
            parts = clean.split("github.com/", 1)[1].split("/")
            if len(parts) >= 2:
                return parts[0], parts[1]
        elif "github.com:" in clean:
            parts = clean.split("github.com:", 1)[1].split("/")
            if len(parts) >= 2:
                return parts[0], parts[1]
    elif "/" in repo:
        parts = repo.split("/", 1)
        return parts[0].strip(), parts[1].strip()

    return org or config.GITHUB_ORG, repo or config.GITHUB_REPO


def deploy(
    package: Dict[str, str],
    prefix: str,
    branch: Optional[str] = None,
    message: Optional[str] = None,
    token: Optional[str] = None,
    org: Optional[str] = None,
    repo: Optional[str] = None,
) -> Dict[str, Any]:
    org, repo = _parse_github_repo(repo, org)
    token = token or config.GITHUB_PAT
    branch = branch or config.BRANCH or "main"

    if not all([token, org, repo]):
        raise GitHubError(
            "GitHub deployment needs GITHUB_PAT, GITHUB_ORG and GITHUB_REPO"
        )

    base = f"{config.GITHUB_API}/repos/{org}/{repo}"
    session = _get_session(token)

    # 1. Resolve the branch head, falling back to the default branch or empty repo.
    ref = session.get(f"{base}/git/ref/heads/{branch}", timeout=60)
    head_sha = None
    base_tree = None
    create_branch = False

    if ref.status_code == 404:
        repo_info = _check(session.get(base, timeout=60), "read repo")
        default_branch = repo_info.get("default_branch", "main")
        ref_default = session.get(
            f"{base}/git/ref/heads/{default_branch}", timeout=60
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
            session.get(f"{base}/git/commits/{head_sha}", timeout=60),
            "read commit",
        )
        base_tree = commit.get("tree", {}).get("sha")

    # 2. Upload blobs concurrently using ThreadPoolExecutor
    tree_entries: List[Dict[str, Any]] = []
    new_paths: set = set()

    def _upload_blob(item: Tuple[str, str]) -> Tuple[str, str, str]:
        path, content = item
        blob_resp = session.post(
            f"{base}/git/blobs",
            json={"content": content, "encoding": "utf-8"},
            timeout=120,
        )
        blob_data = _check(blob_resp, f"create blob {path}")
        entry_path = f"{prefix}/{path}".strip("/") if prefix else path.strip("/")
        return entry_path, blob_data["sha"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_upload_blob, item): item[0] for item in sorted(package.items())}
        for future in concurrent.futures.as_completed(futures):
            entry_path, blob_sha = future.result()
            new_paths.add(entry_path)
            tree_entries.append({
                "path": entry_path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            })

    # 2b. Remove files that existed under `prefix` in a previous run but
    # aren't in this one (e.g. a renamed/dropped table's old TMDL file).
    if base_tree and prefix:
        tree_entries.extend(
            _stale_deletions(base, {}, base_tree, prefix, new_paths, session=session)
        )

    # 3. Tree -> commit -> ref.
    tree_payload: Dict[str, Any] = {"tree": tree_entries}
    if base_tree:
        tree_payload["base_tree"] = base_tree

    tree = _check(
        session.post(
            f"{base}/git/trees",
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
        session.post(
            f"{base}/git/commits",
            json=commit_payload,
            timeout=120,
        ),
        "create commit",
    )

    if create_branch:
        resp = session.post(
            f"{base}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": new_commit["sha"]}, timeout=60,
        )
        if resp.status_code >= 300:
            _check(
                session.patch(
                    f"{base}/git/refs/heads/{branch}",
                    json={"sha": new_commit["sha"], "force": True}, timeout=60,
                ),
                "update branch (fallback)",
            )
    else:
        _check(
            session.patch(
                f"{base}/git/refs/heads/{branch}",
                json={"sha": new_commit["sha"], "force": True}, timeout=60,
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
