"""Fetch the mapping result from the MongoDB microservice."""

from typing import Any, Dict, List, Optional

import requests

from app.config import config
from app.util.logging_utils import get_logger

logger = get_logger(__name__)


class MappingNotFound(RuntimeError):
    pass


def _timeout():
    return (config.HTTP_CONNECT_TIMEOUT, config.HTTP_READ_TIMEOUT)


def _rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return [payload] if isinstance(payload, dict) else []


def fetch_mapping(app_id: Optional[str], run_id: Optional[str]) -> Dict[str, Any]:
    """Newest mapping result for a run, falling back to the app across candidate base URLs."""
    bases = [
        "http://127.0.0.1:8008",
        "http://localhost:8008",
        "http://127.0.0.1:8005",
        "http://localhost:8005",
        config.MONGO_API_URL,
        "https://qlik-tableau-mapping.onrender.com",
        "https://mongo-db-k15s.onrender.com",
        "https://tableaue-mongo-db.onrender.com",
    ]
    # Remove trailing slashes & duplicates while preserving order
    clean_bases = []
    for b in bases:
        cb = b.rstrip("/") if b else ""
        if cb and cb not in clean_bases:
            clean_bases.append(cb)

    paths = []
    if run_id:
        paths.extend([
            f"/mapping/by-run/{run_id}",
            f"/api/mapping/by-run/{run_id}",
            f"/api/mapping/{run_id}",
        ])
    if app_id:
        paths.extend([
            f"/mapping/by-app/{app_id}",
            f"/api/mapping/by-app/{app_id}",
            f"/api/mapping/{app_id}",
        ])
    paths.append("/api/mapping")

    for base in clean_bases:
        for path in paths:
            url = f"{base}{path}"
            try:
                response = requests.get(url, timeout=_timeout())
            except requests.RequestException as exc:
                logger.warning("Mapping fetch failed for %s: %s", url, exc)
                continue

            if response.status_code != 200:
                logger.warning("Mapping fetch %s returned %s", url, response.status_code)
                continue

            try:
                rows = _rows(response.json())
            except ValueError:
                continue

            for row in reversed(rows):  # newest last
                mapping = row.get("mapping_result") if isinstance(row, dict) else None
                if isinstance(mapping, dict) and mapping:
                    logger.info("Loaded mapping from %s", url)
                    return row
                if isinstance(row, dict) and row.get("tables"):
                    logger.info("Loaded direct mapping from %s", url)
                    return row

    raise MappingNotFound(
        f"No mapping result found for run_id={run_id!r} / app_id={app_id!r}. "
        "Run the mapping agent first."
    )
