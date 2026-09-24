"""Bookmarks and page navigation.

Qlik bookmarks store a selection state; Power BI bookmarks store a report
state. They are not equivalent, so each Qlik bookmark becomes a *page*
bookmark — it navigates to the sheet it belongs to — and the selection it
carried is reported as a note rather than silently half-converted.

Navigation is separate: Qlik sheets are reached from a sheet list, which has
no Power BI counterpart, so a navigation button strip is generated when more
than one page exists.
"""

import json
from typing import Any, Dict, List, Tuple

from app.qlik.report import pbir_schemas as S
from app.qlik.util.ids import lineage_tag, safe_filename, slug
from app.qlik.util.payload import as_dict, as_list, text

BUTTON_HEIGHT = 40
BUTTON_WIDTH = 150
BUTTON_GAP = 8
BUTTON_TOP = 8


def build_bookmarks(
    mapping: Dict[str, Any], page_ids: Dict[str, str]
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """Return (files, notes) for `definition/bookmarks/`."""
    block = mapping.get("bookmarks")
    raw_items = (
        as_list(as_dict(block).get("converted_items"))
        if isinstance(block, dict) else as_list(block)
    )
    if not raw_items:
        return {}, []

    files: Dict[str, str] = {}
    notes: List[Dict[str, Any]] = []
    names: List[str] = []

    for index, raw in enumerate(raw_items):
        raw = as_dict(raw)
        title = text(raw.get("title") or raw.get("name"), f"Bookmark {index + 1}")
        sheet = text(raw.get("sheet_name") or raw.get("sheet"))
        page_id = page_ids.get(sheet) or next(iter(page_ids.values()), "")
        if not page_id:
            continue

        name = f"Bookmark{index + 1:03d}"
        names.append(name)
        files[f"definition/bookmarks/{name}.bookmark.json"] = json.dumps({
            "$schema": S.BOOKMARK,
            "name": name,
            "displayName": title,
            "explorationState": {
                "version": "1.0.0",
                "activeSection": page_id,
                "sections": {page_id: {"visualContainers": {}}},
            },
        }, indent=2)

        # A Qlik bookmark's selection state has no direct Power BI form.
        if raw.get("selections") or raw.get("selection_state"):
            notes.append({
                "object_id": raw.get("id"),
                "title": title,
                "qlik_type": "bookmark",
                "mapped_to": "page bookmark",
                "severity": "substituted",
                "reason": "Qlik bookmarks store a selection state; Power BI bookmarks store a report state.",
                "suggestion": (
                    f"'{title}' navigates to its page but does not restore the "
                    "original selection. Re-apply the slicer values and update "
                    "the bookmark in Desktop."
                ),
            })

    if files:
        files["definition/bookmarks/bookmarks.json"] = json.dumps({
            "$schema": S.BOOKMARKS_METADATA,
            "items": [{"name": name} for name in names],
        }, indent=2)

    return files, notes


def build_navigation_buttons(
    page_titles: List[str], page_ids: Dict[str, str], current_page: str
) -> List[Dict[str, Any]]:
    """Synthetic navigation buttons are disabled.
    Reports must contain only visuals and navigation elements explicitly defined in mapping.
    """
    return []
