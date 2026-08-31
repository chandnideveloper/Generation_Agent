"""Upload a Qlik CSV/file-based table's real content into a Fabric Lakehouse.

Every file-based table's M-query previously pointed at a fixed external
Supabase Storage bucket (see app/model/table_tmdl.py's SUPABASE_STORAGE_URL) -
a hardcoded stand-in with no upload step of its own anywhere in this
pipeline. If the exact filename hadn't already been staged there by some
other, unrelated process, the report's visuals would fail to resolve their
fields at refresh time ("something's wrong with one or more fields").

This module does the two things that stand-in was never actually backed by:
ensure a Lakehouse exists in the target Fabric workspace, and upload each
data file's real bytes into it via OneLake's ADLS Gen2-compatible REST API
(create -> append -> flush), so a real Fabric-hosted copy exists that the
M-query can be rewritten to point at instead.

Note: this talks to two live Microsoft Fabric REST surfaces (the Fabric
items API and the OneLake DFS API) that cannot be exercised against a real
tenant in this environment - the request/response shapes here follow
Microsoft's published OneLake/Fabric REST API documentation and are covered
by mocked unit tests, but a live smoke test against a real workspace is
recommended before depending on this in production.
"""

import base64
from typing import Any, Dict, List, Optional

import requests

from app.config import config
from app.deploy.fabric_deployer import FabricError, _headers, _wait, find_item
from app.model.table_tmdl import clean_data_file_name
from app.util.logging_utils import get_logger

logger = get_logger(__name__)

ONELAKE_DFS_HOST = "https://onelake.dfs.fabric.microsoft.com"


def ensure_lakehouse(workspace_id: str, display_name: str, token: str) -> str:
    """Return the id of a Lakehouse named `display_name` in the workspace,
    creating it if it doesn't already exist."""
    existing = find_item(workspace_id, display_name, "Lakehouse", token)
    if existing:
        return existing

    url = f"{config.FABRIC_API}/workspaces/{workspace_id}/items"
    response = requests.post(
        url, headers=_headers(token),
        json={"displayName": display_name, "type": "Lakehouse"}, timeout=300,
    )
    if response.status_code in (200, 201):
        return response.json()["id"]
    if response.status_code == 202:
        location = response.headers.get("Location")
        if not location:
            raise FabricError(f"Lakehouse creation accepted without an operation URL for {display_name}")
        lakehouse_id = _wait(location, token)
        if not lakehouse_id:
            raise FabricError(f"Lakehouse creation did not return an id for {display_name}")
        return lakehouse_id
    raise FabricError(
        f"Lakehouse creation failed for {display_name}: {response.status_code} {response.text[:300]}"
    )


def onelake_file_url(workspace_id: str, lakehouse_id: str, file_path: str) -> str:
    """The OneLake DFS path a Lakehouse file lives at, in the exact shape
    Power Query's AzureStorage.DataLake M function expects."""
    return f"{ONELAKE_DFS_HOST}/{workspace_id}/{lakehouse_id}/Files/{file_path.lstrip('/')}"


def upload_file_to_lakehouse(
    workspace_id: str, lakehouse_id: str, file_path: str, content: bytes, token: str,
) -> None:
    """Upload `content` to Files/{file_path} in the given Lakehouse.

    OneLake is ADLS Gen2-compatible: a file is created empty, its bytes are
    appended at offset 0, then the write is committed with a flush at the
    final byte offset - three calls are required even for a single-shot
    upload, per the documented Data Lake Storage Gen2 REST contract.
    """
    base_url = onelake_file_url(workspace_id, lakehouse_id, file_path)
    dfs_headers = {"Authorization": f"Bearer {token}"}

    create_resp = requests.put(f"{base_url}?resource=file", headers=dfs_headers, timeout=60)
    if create_resp.status_code not in (200, 201):
        raise FabricError(
            f"OneLake file create failed for {file_path}: {create_resp.status_code} {create_resp.text[:300]}"
        )

    append_resp = requests.patch(
        f"{base_url}?action=append&position=0",
        headers={**dfs_headers, "Content-Type": "application/octet-stream"},
        data=content, timeout=300,
    )
    if append_resp.status_code not in (200, 202):
        raise FabricError(
            f"OneLake file append failed for {file_path}: {append_resp.status_code} {append_resp.text[:300]}"
        )

    flush_resp = requests.patch(
        f"{base_url}?action=flush&position={len(content)}",
        headers=dfs_headers, timeout=60,
    )
    if flush_resp.status_code not in (200, 201):
        raise FabricError(
            f"OneLake file flush failed for {file_path}: {flush_resp.status_code} {flush_resp.text[:300]}"
        )


def _file_bytes(data_file: Dict[str, Any]) -> Optional[bytes]:
    if data_file.get("content_text") is not None:
        return str(data_file["content_text"]).encode("utf-8")
    if data_file.get("content_base64"):
        try:
            return base64.b64decode(data_file["content_base64"])
        except Exception:  # noqa: BLE001
            return None
    return None


def provision_data_files(
    data_files: List[Dict[str, Any]], workspace_id: str, token: str, lakehouse_name: str,
) -> Dict[str, str]:
    """Upload every data file that carries real content into a Fabric
    Lakehouse, returning {clean_file_name: onelake_url} for each success.

    A file with no downloaded content (extraction failed, was over the
    inline size cap, etc.) is skipped rather than uploaded as empty/garbage -
    its table keeps pointing at the Supabase placeholder, which is a visible,
    debuggable gap rather than a silently wrong upload.
    """
    if not data_files or not workspace_id or not token:
        return {}

    try:
        lakehouse_id = ensure_lakehouse(workspace_id, lakehouse_name, token)
    except FabricError as exc:
        logger.warning("Could not ensure Lakehouse '%s' for data file upload: %s", lakehouse_name, exc)
        return {}

    resolved: Dict[str, str] = {}
    for f in data_files:
        if not isinstance(f, dict):
            continue
        raw_name = f.get("name") or f.get("baseName")
        if not raw_name:
            continue
        content = _file_bytes(f)
        if content is None:
            logger.info("Skipping data file '%s' - no content was downloaded for it", raw_name)
            continue
        clean_name = clean_data_file_name(str(raw_name))
        try:
            upload_file_to_lakehouse(workspace_id, lakehouse_id, clean_name, content, token)
        except FabricError as exc:
            logger.warning("Failed to upload data file '%s' to Fabric Lakehouse: %s", clean_name, exc)
            continue
        resolved[clean_name] = onelake_file_url(workspace_id, lakehouse_id, clean_name)
    return resolved


def rewrite_package_data_file_urls(
    package: Dict[str, str], uploaded_urls: Dict[str, str]
) -> Dict[str, str]:
    """Rewrite any M-query pointing at the placeholder Supabase Storage URL
    to the newly-provisioned Fabric Lakehouse OneLake URL instead."""
    if not uploaded_urls:
        return package

    from app.model.table_tmdl import SUPABASE_STORAGE_URL

    rewritten: Dict[str, str] = {}
    for path, content in package.items():
        if not path.endswith((".tmdl", ".json", ".m")):
            rewritten[path] = content
            continue

        updated = content
        for clean_name, onelake_url in uploaded_urls.items():
            old_url = f"{SUPABASE_STORAGE_URL}/{clean_name}"
            if old_url in updated:
                updated = updated.replace(old_url, onelake_url)
        rewritten[path] = updated

    return rewritten
