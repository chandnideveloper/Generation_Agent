"""Direct push into a Fabric workspace via the items API.

Order matters: the semantic model is created first so the report can be bound
to its id. Creating the report first would leave it pointing at a relative
path that does not exist in a workspace.

Long-running creates answer 202 with an operation URL; those are polled
rather than assumed successful.
"""

import time
from typing import Any, Dict, List, Optional

import requests

from app.config import config
from app.package import fabric_packager
from app.util.logging_utils import get_logger

logger = get_logger(__name__)

POLL_INTERVAL = 3
POLL_MAX_ATTEMPTS = 40


class FabricError(RuntimeError):
    pass


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _wait(operation_url: str, token: str) -> Optional[str]:
    """Poll an async operation until it settles; return the created item id."""
    for _ in range(POLL_MAX_ATTEMPTS):
        time.sleep(POLL_INTERVAL)
        response = requests.get(operation_url, headers=_headers(token), timeout=60)
        if response.status_code != 200:
            continue
        body = response.json()
        status = str(body.get("status", "")).lower()
        if status == "succeeded":
            result = requests.get(
                f"{operation_url}/result", headers=_headers(token), timeout=60
            )
            if result.status_code == 200:
                return result.json().get("id")
            return body.get("resourceId") or body.get("id")
        if status == "failed":
            raise FabricError(f"Fabric operation failed: {body.get('error')}")
    raise FabricError("Fabric operation did not complete in time")


def find_item(workspace_id: str, display_name: str, item_type: str, token: str) -> Optional[str]:
    """Existing item id, so a re-run updates instead of duplicating."""
    url = f"{config.FABRIC_API}/workspaces/{workspace_id}/items"
    response = requests.get(url, headers=_headers(token), timeout=60)
    if response.status_code != 200:
        return None
    for item in response.json().get("value", []):
        if item.get("displayName") == display_name and item.get("type") == item_type:
            return item.get("id")
    return None


def create_or_update(
    workspace_id: str, display_name: str, item_type: str,
    parts: List[Dict[str, str]], token: str,
) -> str:
    existing = find_item(workspace_id, display_name, item_type, token)
    definition = {"parts": parts}

    if existing:
        url = f"{config.FABRIC_API}/workspaces/{workspace_id}/items/{existing}/updateDefinition"
        response = requests.post(
            url, headers=_headers(token), json={"definition": definition}, timeout=300
        )
        if response.status_code in (200, 202):
            if response.status_code == 202 and response.headers.get("Location"):
                _wait(response.headers["Location"], token)
            logger.info("Updated Fabric %s '%s'", item_type, display_name)
            return existing
        raise FabricError(
            f"Update failed for {display_name}: {response.status_code} {response.text[:300]}"
        )

    url = f"{config.FABRIC_API}/workspaces/{workspace_id}/items"
    payload = {"displayName": display_name, "type": item_type, "definition": definition}
    response = requests.post(url, headers=_headers(token), json=payload, timeout=300)

    if response.status_code in (200, 201):
        return response.json().get("id")
    if response.status_code == 202:
        location = response.headers.get("Location")
        if not location:
            raise FabricError(f"Accepted without an operation URL for {display_name}")
        return _wait(location, token)
    raise FabricError(
        f"Create failed for {display_name}: {response.status_code} {response.text[:300]}"
    )


def deploy(
    package: Dict[str, str], app_name: str, workspace_id: str, token: str
) -> Dict[str, Any]:
    """Publish the semantic model, then the report bound to it."""
    if not workspace_id or not token:
        raise FabricError("workspace_id and fabric_access_token are required")

    items = fabric_packager.split_items(package, app_name)
    result: Dict[str, Any] = {"workspace_id": workspace_id, "items": {}}

    if "semantic_model" in items:
        dataset_id = create_or_update(
            workspace_id, app_name, "SemanticModel", items["semantic_model"], token
        )
        result["items"]["semantic_model"] = dataset_id
        logger.info("Fabric semantic model '%s' -> %s", app_name, dataset_id)

        if "report" in items:
            bound = fabric_packager.report_definition_with_binding(
                items["report"], dataset_id
            )
            report_id = create_or_update(
                workspace_id, app_name, "Report", bound, token
            )
            result["items"]["report"] = report_id
            logger.info("Fabric report '%s' -> %s", app_name, report_id)

    result["status"] = "success"
    return result
