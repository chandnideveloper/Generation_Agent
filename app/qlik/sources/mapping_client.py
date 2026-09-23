"""Fetch the mapping result from the MongoDB microservice."""

import os
from typing import Any, Dict, List, Optional

import requests

from app.qlik.config import config
from app.qlik.util.logging_utils import get_logger

logger = get_logger(__name__)


class MappingNotFound(RuntimeError):
    pass


def _timeout():
    return (config.HTTP_CONNECT_TIMEOUT, config.HTTP_READ_TIMEOUT)


def _discovery_timeout():
    """Shorter budget for probing candidate bases.

    `fetch_mapping` tries several base URLs; using the full read timeout for
    each turns one unreachable host into minutes of dead time before the
    caller sees any answer.
    """
    return (min(config.HTTP_CONNECT_TIMEOUT, 5), min(config.HTTP_READ_TIMEOUT, 30))


def _rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return [payload] if isinstance(payload, dict) else []


def _identity_matches(row: Dict[str, Any], app_id: Optional[str], run_id: Optional[str]) -> bool:
    """True when `row` really is the mapping for the requested app/run.

    Some store endpoints ignore the id in the path and answer with the newest
    document they have. Building a report from another app's mapping is worse
    than failing: it produces a confident, successful-looking deployment of
    the wrong report. Anything that carries a conflicting id is rejected; a
    document that states no id at all is accepted, since older records were
    written without one.
    """
    mapping = row.get("mapping_result") if isinstance(row.get("mapping_result"), dict) else row
    metadata = mapping.get("workbook_metadata") if isinstance(mapping.get("workbook_metadata"), dict) else {}

    def _candidates(key: str) -> set:
        found = set()
        for source in (row, mapping, metadata):
            value = source.get(key)
            if value is not None and str(value).strip():
                found.add(str(value).strip())
        return found

    for requested, key in ((run_id, "run_id"), (app_id, "app_id")):
        if not requested:
            continue
        stated = _candidates(key)
        if stated and str(requested).strip() not in stated:
            return False
    return True


def _bases() -> List[str]:
    """Candidate mapping-store base URLs, most specific first.

    Only the configured store and an optional local sidecar are probed.
    Extra hosts belong in MAPPING_API_FALLBACKS (comma-separated), not
    hardcoded here - a hardcoded shared host silently serves whatever
    unrelated run happens to be newest on it.
    """
    extra = [b.strip() for b in os.getenv("MAPPING_API_FALLBACKS", "").split(",") if b.strip()]
    candidates = [config.MONGO_API_URL, os.getenv("MAPPING_LOCAL_API", "http://127.0.0.1:8008"), *extra]

    clean: List[str] = []
    for base in candidates:
        stripped = base.rstrip("/") if base else ""
        if stripped and stripped not in clean:
            clean.append(stripped)
    return clean


def fetch_mapping(app_id: Optional[str], run_id: Optional[str]) -> Dict[str, Any]:
    """Newest mapping result for a run, falling back to the app.

    Every candidate path is scoped to the requested run_id or app_id, and
    every returned document is checked against that id before it is used.
    """
    if not (run_id or app_id):
        raise MappingNotFound("A run_id or app_id is required to fetch a mapping result.")

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

    rejected = 0
    for base in _bases():
        for path in paths:
            url = f"{base}{path}"
            try:
                response = requests.get(url, timeout=_discovery_timeout())
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

            best_row = None
            best_score = -1
            for row in rows:
                if not isinstance(row, dict):
                    continue
                mapping = row.get("mapping_result")
                has_mapping = (isinstance(mapping, dict) and bool(mapping)) or bool(row.get("tables"))
                if not has_mapping:
                    continue
                if not _identity_matches(row, app_id, run_id):
                    rejected += 1
                    logger.warning(
                        "Discarding mapping from %s: it belongs to a different app/run than "
                        "app_id=%r run_id=%r.", url, app_id, run_id,
                    )
                    continue
                m_dict = mapping if isinstance(mapping, dict) else row
                score = (
                    len(m_dict.get("tables") or []) * 10
                    + len(m_dict.get("measures") or []) * 5
                    + len(m_dict.get("visuals") or []) * 20
                )
                if score >= best_score:
                    best_score = score
                    best_row = row

            if best_row is not None:
                logger.info("Loaded mapping from %s (score=%d)", url, best_score)
                return best_row

    detail = (
        f" {rejected} document(s) were returned but belonged to a different app/run."
        if rejected else ""
    )
    raise MappingNotFound(
        f"No mapping result found for run_id={run_id!r} / app_id={app_id!r}.{detail} "
        "Run the mapping agent first."
    )
