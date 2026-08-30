"""Cache a generated package (the flat {relative_path: content} dict) keyed
by run_id, so `/download` can hand back the exact files `/generate` already
built without re-running generation.

Two layers:
  - an in-process dict, always available, always fast;
  - a best-effort remote copy on the same MongoDB microservice `/generate`
    already talks to (app.sources.mapping_client), so the cache survives a
    process restart or a second instance behind a load balancer.

The remote layer degrades silently: if the microservice has no
`generation-package` collection yet (a 404, or the endpoint doesn't exist),
`save`/`load` simply fall back to memory-only rather than raising - a cache
miss just means `/download` re-runs generation, which is always correct,
only slower.
"""

import threading
from typing import Any, Dict, Optional

import requests

from app.config import config
from app.util.logging_utils import get_logger

logger = get_logger(__name__)

_store: Dict[str, Dict[str, str]] = {}

# Short and deliberately separate from config.HTTP_*_TIMEOUT (which is tuned
# for legitimate large mapping fetches, up to 180s) - this is a best-effort
# cache read/write, not something worth waiting on.
_REMOTE_TIMEOUT = (3, 5)


def _remote_save(run_id: str, package: Dict[str, str], app_id: str) -> None:
    try:
        requests.post(
            f"{config.MONGO_API_URL}/generation-package",
            json={"run_id": run_id, "app_id": app_id, "package": package},
            timeout=_REMOTE_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.debug("Remote package cache write skipped for run_id=%s: %s", run_id, exc)


def save(run_id: str, package: Dict[str, str], app_id: Optional[str] = None) -> None:
    """Cache `package` under `run_id`. Never blocks the caller: the in-memory
    write is synchronous (and instant), the remote copy runs on a background
    thread so a slow/unreachable microservice never adds latency to
    /generate."""
    if not run_id:
        return
    _store[run_id] = package
    threading.Thread(
        target=_remote_save, args=(run_id, package, app_id or ""), daemon=True
    ).start()


def load(run_id: str) -> Optional[Dict[str, str]]:
    if not run_id:
        return None
    cached = _store.get(run_id)
    if cached is not None:
        return cached

    try:
        response = requests.get(
            f"{config.MONGO_API_URL}/generation-package/{run_id}", timeout=_REMOTE_TIMEOUT
        )
        if response.status_code == 200:
            body: Any = response.json()
            package = body.get("package") if isinstance(body, dict) else None
            if isinstance(package, dict) and package:
                _store[run_id] = package
                return package
    except (requests.RequestException, ValueError) as exc:
        logger.debug("Remote package cache read skipped for run_id=%s: %s", run_id, exc)

    return None
