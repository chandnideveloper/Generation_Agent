"""Assemble the PBIR report folder from the mapping payload.

Produces `<name>.Report/`:

    definition/report.json                     report root (schema 3.2.0)
    definition/version.json                    required version marker
    definition/pages/pages.json
    definition/pages/<page>/page.json          schema 2.0.0
    definition/pages/<page>/visuals/<v>/visual.json   schema 2.9.0
    definition/bookmarks/…                     when the mapping has any
    StaticResources/SharedResources/BaseThemes/CY24SU10.json
    definition.pbir  .platform  .pbi/localSettings.json

One page per Qlik sheet; visuals belonging to no sheet land on an
"Unassigned" page rather than being dropped.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from app.config import config
from app.report import bookmarks as bookmark_builder
from app.report import layout as layout_util
from app.report import pbir_schemas as S
from app.report.report_files import _pbir, _platform, _report_json
from app.report.theme import base_theme_json, build_theme_json
from app.report.visual_builder import build_visual
from app.util.ids import lineage_tag, safe_filename, slug
from app.util.payload import as_dict, as_list, text
from app.util import payload as P


def _sheet_key(visual: Dict[str, Any]) -> str:
    source = as_dict(visual.get("qlik_source")) or visual
    return text(
        source.get("sheet_name") or source.get("source") or visual.get("sheet_name"),
        "Unassigned",
    )


from app.model.table_tmdl import _clean_table_name


def _entity_maps(mapping: Dict[str, Any]):
    """Field name -> owning table, for measures and for columns.

    Dynamically extracts table/column ownership from tables, dimensions,
    and measures in the mapping payload without hardcoded lookups.
    """
    measure_home: Dict[str, str] = {}
    for measure in P.measures(mapping):
        name = text(measure.get("name"))
        tables = measure.get("tables") or []
        home = _clean_table_name(text(tables[0])) if tables else _clean_table_name(text(as_dict(measure.get("fabric")).get("table")))
        if name:
            measure_home[name] = home or "_Measures"

    column_home: Dict[str, str] = {}
    field_resolver: Dict[str, Tuple[str, str]] = {}

    # 1. Map all physical columns from tables
    for table in P.tables(mapping):
        table_name = _clean_table_name(text(table.get("name") or table.get("table_name")))
        for column in (table.get("columns") or table.get("fields") or []):
            column_name = text(
                as_dict(column).get("fabric_column_name")
                or as_dict(column).get("qlik_column_name")
                or as_dict(column).get("name")
            )
            if column_name:
                if column_name not in column_home:
                    column_home[column_name] = table_name
                cl = column_name.lower()
                field_resolver[cl] = (table_name, column_name)
                field_resolver[cl.replace("_", " ")] = (table_name, column_name)
                field_resolver[cl.replace(" ", "_")] = (table_name, column_name)

    # 2. Map all master dimensions dynamically from payload
    from app.report.visual_builder import _extract_base_field
    for dimension in P.dimensions(mapping):
        name = text(dimension.get("name")).strip()
        dim_fabric = as_dict(dimension.get("fabric"))
        dim_table = _clean_table_name(text(dim_fabric.get("table") or (dimension.get("tables") or [""])[0]))
        dax_expr = text(dim_fabric.get("dax_expression") or dim_fabric.get("tmdl"))

        # Check if the dimension references a specific column like 'Customers'[customer_name]
        col_match = re.search(r"'([^']+)'\[([^\]]+)\]", dax_expr)
        if col_match:
            target_t, target_c = col_match.group(1), col_match.group(2)
            target_t_clean = _clean_table_name(target_t)
            nl = name.lower()
            field_resolver[nl] = (target_t_clean, target_c)
            field_resolver[nl + " name"] = (target_t_clean, target_c)
            field_resolver[nl + "s"] = (target_t_clean, target_c)
            if name not in column_home:
                column_home[name] = target_t_clean
        elif dim_table and name:
            table_cols = [c for c, t in column_home.items() if t == dim_table]
            base_name = _extract_base_field(name)
            matched_col = None
            for c in table_cols:
                if c.lower() == name.lower() or c.lower() == base_name.lower():
                    matched_col = c
                    break
            if not matched_col:
                for c in table_cols:
                    if c.lower() in name.lower() or name.lower() in c.lower():
                        matched_col = c
                        break
            final_col = matched_col or (base_name if base_name in table_cols else (table_cols[0] if table_cols else name))
            nl = name.lower()
            field_resolver[nl] = (dim_table, final_col)
            field_resolver[nl.replace("_", " ")] = (dim_table, final_col)
            field_resolver[nl.replace(" ", "_")] = (dim_table, final_col)
            if base_name:
                bl = base_name.lower()
                field_resolver[bl] = (dim_table, final_col)

    return measure_home, column_home, field_resolver


def _auto_register_expression_labels(
    mapping: Dict[str, Any],
    measure_home: Dict[str, str],
    column_home: Dict[str, str],
) -> None:
    """Register Qlik visual expression labels (e.g. 'Average Grade Point') into
    measure_home when they're not already known as a column or measure.

    The mapping agent auto-generates stub DAX measures for these; here we ensure
    the generation layer can find them when building visual projections.
    """
    # First, pick up any stub measures the coordinator added
    for m in P.measures(mapping):
        name = text(m.get("name"))
        if name and name not in measure_home:
            tables = m.get("tables") or []
            from app.model.table_tmdl import _clean_table_name
            home = _clean_table_name(text(tables[0])) if tables else "_Measures"
            measure_home[name] = home or "_Measures"

    # Then scan all visual y_axis / measures fields for unresolved labels
    for visual in P.visuals(mapping):
        source = as_dict(visual.get("qlik_source")) or visual
        fabric = as_dict(visual.get("fabric"))
        y_fields = (
            as_list(source.get("y_axis"))
            or as_list(fabric.get("y_axis_fields"))
            or as_list(source.get("measures"))
        )
        for label in y_fields:
            label_clean = text(label)
            if label_clean and label_clean not in measure_home and label_clean not in column_home:
                measure_home[label_clean] = "_Measures"


def build_report(mapping: Dict[str, Any], app_name: str, model_path: str):
    """Return (files, notes, report)."""
    visuals = P.visuals(mapping)
    sheets = {text(s.get("sheet_name") or s.get("title")): s for s in P.sheets(mapping)}
    measure_home, column_home, field_resolver = _entity_maps(mapping)
    _auto_register_expression_labels(mapping, measure_home, column_home)

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for visual in visuals:
        grouped.setdefault(_sheet_key(visual), []).append(visual)
    if not grouped:
        grouped["Page 1"] = []

    page_titles = list(grouped)
    page_ids = {
        title: slug(title, f"page{index + 1}")
        for index, title in enumerate(page_titles)
    }

    files: Dict[str, str] = {
        "definition/report.json": _report_json(),
        # Desktop refuses a PBIR report with no version marker.
        "definition/version.json": json.dumps(
            {"$schema": S.VERSION_METADATA, "version": S.REPORT_VERSION}, indent=2
        ),
        f"StaticResources/SharedResources/{S.BASE_THEME_PATH}": build_theme_json(mapping),
        "definition.pbir": _pbir(model_path),
        ".platform": _platform(app_name),
        ".pbi/localSettings.json": json.dumps(
            {"$schema": S.REPORT_LOCAL_SETTINGS},
            indent=2,
        ),
    }

    notes: List[Dict[str, Any]] = []
    visual_count = 0
    nav_button_count = 0
    filter_totals = {"total": 0, "top_n": 0, "categorical": 0}

    for sheet_name, sheet_visuals in grouped.items():
        page_id = page_ids[sheet_name]
        grid = layout_util.sheet_grid(sheets.get(sheet_name, {}), visuals=sheet_visuals)
        columns, rows = grid

        # Calculate exact sheet page height based on Qlik row count and visual bounds
        base_height = max(config.CANVAS_HEIGHT, int(round(rows * 60.0))) if rows > 12 else config.CANVAS_HEIGHT
        max_bottom = 0
        for position, visual in enumerate(sheet_visuals):
            c_test = layout_util.to_canvas(visual, grid, position, canvas_height=base_height)
            max_bottom = max(max_bottom, c_test["y"] + c_test["height"])

        page_height = max(base_height, max_bottom + 40)
        display_option = "FitToWidth" if page_height > config.CANVAS_HEIGHT else "FitToPage"

        for position, visual in enumerate(sheet_visuals):
            canvas = layout_util.to_canvas(visual, grid, position, canvas_height=page_height)
            document, note, stats = build_visual(
                visual, canvas, position, measure_home, column_home, field_resolver=field_resolver
            )
            visual_id = safe_filename(document["name"], f"visual{position}")
            files[f"definition/pages/{page_id}/visuals/{visual_id}/visual.json"] = (
                json.dumps(document, indent=2)
            )
            visual_count += 1
            for key in filter_totals:
                filter_totals[key] += stats.get(key, 0)
            if note:
                note["sheet"] = sheet_name
                notes.append(note)

        # Qlik sheets that already carry a native action button do not get a
        # duplicate synthetic top nav strip. Real dashboards almost always
        # have *some* visual near the top of the page (a KPI row, a header
        # chart), so gating on "any visual at y < 45" as this used to do
        # suppressed the synthetic strip for virtually every populated
        # sheet - a multi-page report could end up with zero navigation
        # buttons of any kind, exactly the "buttons don't work" symptom.
        # `power_bi_visual_type` carries a `visualType` key (not `type`),
        # and mapping's own `fabric.visual_type` is the primary, always-
        # populated signal - check that directly instead of a key that
        # was never actually present in mapping's output shape.
        has_native_nav = any(
            (as_dict(v.get("fabric")).get("visual_type") == "actionButton"
             or as_dict(v.get("qlik_source")).get("chart_type") in ("action-button", "actionButton", "button")
             or as_dict(as_dict(v.get("fabric")).get("power_bi_visual_type")).get("visualType") == "actionButton"
             or v.get("name") == "ActionButton")
            for v in sheet_visuals
        )
        if not has_native_nav and len(page_titles) > 1:
            for button in bookmark_builder.build_navigation_buttons(page_titles, page_ids, sheet_name):
                button_id = safe_filename(button["name"], f"nav{nav_button_count}")
                files[f"definition/pages/{page_id}/visuals/{button_id}/visual.json"] = (
                    json.dumps(button, indent=2)
                )
                nav_button_count += 1

        files[f"definition/pages/{page_id}/page.json"] = json.dumps({
            "$schema": S.PAGE,
            "name": page_id,
            "displayName": sheet_name,
            "displayOption": display_option,
            "height": page_height,
            "width": config.CANVAS_WIDTH,
        }, indent=2)

    files["definition/pages/pages.json"] = json.dumps({
        "$schema": S.PAGES_METADATA,
        "pageOrder": [page_ids[title] for title in page_titles],
        "activePageName": page_ids[page_titles[0]] if page_titles else "",
    }, indent=2)

    bookmark_files, bookmark_notes = bookmark_builder.build_bookmarks(mapping, page_ids)
    files.update(bookmark_files)
    notes.extend(bookmark_notes)

    report = {
        "pages": len(page_titles),
        "visuals": visual_count,
        "native": visual_count - len([n for n in notes if n["qlik_type"] != "bookmark"]),
        "substituted": sum(1 for n in notes if n["severity"] == "substituted"),
        "manual": sum(1 for n in notes if n["severity"] == "manual"),
        "filters_applied": filter_totals["total"],
        "top_n_filters": filter_totals["top_n"],
        "categorical_filters": filter_totals["categorical"],
        "bookmarks": len([f for f in bookmark_files if f.endswith(".bookmark.json")]),
        "navigation_buttons": nav_button_count,
    }
    return files, notes, report
