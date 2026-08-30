"""Push a PBIP package to Azure DevOps Repos as one push.

The DevOps push API takes every file in a single request, so the whole package
commits atomically. Each change must declare `add` or `edit`, so existing
paths are looked up first — sending `add` for a file that already exists is
rejected outright.
"""

import base64
from typing import Any, Dict, List, Optional, Set

import requests

from app.config import config
from app.util.logging_utils import get_logger

logger = get_logger(__name__)


class DevOpsError(RuntimeError):
    pass


def _headers(pat: str) -> Dict[str, str]:
    encoded = base64.b64encode(f":{pat}".encode()).decode("ascii")
    return {"Authorization": f"Basic {encoded}", "Content-Type": "application/json"}


def _base(org: str, project: str, repo: str) -> str:
    return f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}"


def _existing_paths(base: str, headers: Dict[str, str], branch: str) -> Set[str]:
    """Paths already in the branch, so each change gets the right type."""
    response = requests.get(
        f"{base}/items",
        headers=headers,
        params={
            "recursionLevel": "Full",
            "versionDescriptor.version": branch,
            "api-version": config.DEVOPS_API_VERSION,
        },
        timeout=120,
    )
    if response.status_code != 200:
        return set()
    return {
        item["path"].lstrip("/")
        for item in response.json().get("value", [])
        if not item.get("isFolder")
    }


def deploy(
    package: Dict[str, str],
    prefix: str,
    branch: Optional[str] = None,
    message: Optional[str] = None,
    pat: Optional[str] = None,
    org: Optional[str] = None,
    project: Optional[str] = None,
    repo: Optional[str] = None,
) -> Dict[str, Any]:
    pat = pat or config.DEVOPS_PAT
    org = org or config.DEVOPS_ORG
    project = project or config.DEVOPS_PROJECT
    repo = repo or config.DEVOPS_REPO
    branch = branch or config.BRANCH

    if not all([pat, org, project, repo]):
        raise DevOpsError(
            "DevOps deployment needs AZURE_DEVOPS_PAT, AZURE_ORGANIZATION, "
            "AZURE_PROJECT and AZURE_REPO"
        )

    base = _base(org, project, repo)
    headers = _headers(pat)

    refs = requests.get(
        f"{base}/refs",
        headers=headers,
        params={"filter": f"heads/{branch}", "api-version": config.DEVOPS_API_VERSION},
        timeout=60,
    )
    if refs.status_code != 200:
        raise DevOpsError(f"Could not read refs: {refs.status_code} {refs.text[:200]}")

    values = refs.json().get("value", [])
    # An empty repo has no ref; DevOps expects a zero object id for the first push.
    old_object_id = values[0]["objectId"] if values else "0" * 40

    existing = _existing_paths(base, headers, branch) if values else set()

    changes: List[Dict[str, Any]] = []
    new_paths: Set[str] = set()
    for path, content in sorted(package.items()):
        full_path = f"/{prefix}/{path}".replace("//", "/")
        new_paths.add(full_path.lstrip("/"))
        changes.append({
            "changeType": "edit" if full_path.lstrip("/") in existing else "add",
            "item": {"path": full_path},
            "newContent": {"content": content, "contentType": "rawtext"},
        })

    # Remove files that existed under `prefix` in a previous push but aren't
    # in this one - `existing` only drives add-vs-edit above, so without this
    # a shrinking package leaves stale files behind instead of the folder
    # truly mirroring this run (the whole point of overwriting one folder
    # per app rather than a new folder per run).
    prefix_slash = f"{prefix}/".lstrip("/")
    stale = [p for p in existing if p.startswith(prefix_slash) and p not in new_paths]
    for path in stale:
        changes.append({"changeType": "delete", "item": {"path": f"/{path}"}})
    if stale:
        logger.info("Removing %s stale file(s) under %s", len(stale), prefix)

    payload = {
        "refUpdates": [{"name": f"refs/heads/{branch}", "oldObjectId": old_object_id}],
        "commits": [{
            "comment": message or f"Generate Power BI package for {prefix}",
            "changes": changes,
        }],
    }

    response = requests.post(
        f"{base}/pushes",
        headers=headers,
        params={"api-version": config.DEVOPS_API_VERSION},
        json=payload,
        timeout=300,
    )
    if response.status_code not in (200, 201):
        raise DevOpsError(
            f"Push failed: {response.status_code} {response.text[:400]}"
        )

    body = response.json()
    logger.info("Pushed %s files to %s/%s@%s", len(changes), project, repo, branch)
    return {
        "status": "success",
        "provider": "devops",
        "repo": f"{org}/{project}/{repo}",
        "branch": branch,
        "commit": (body.get("commits") or [{}])[0].get("commitId"),
        "path": prefix,
        "file_count": len(changes),
    }
