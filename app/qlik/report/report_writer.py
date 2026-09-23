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
import difflib
from typing import Any, Dict, List, Optional, Tuple

from app.qlik.config import config
from app.qlik.report import bookmarks as bookmark_builder
from app.qlik.report import layout as layout_util
from app.qlik.report import pbir_schemas as S
from app.qlik.report.filters import _categorical_filter, _field_filter
from app.qlik.report.report_files import _pbir, _platform, _report_json
from app.qlik.report.theme import base_theme_json, build_theme_json
from app.qlik.report.visual_builder import build_visual
from app.qlik.util.ids import lineage_tag, safe_filename, slug
from app.qlik.util.payload import as_dict, as_list, text
from app.qlik.util import payload as P


def _sheet_key(visual: Dict[str, Any]) -> str:
    source = as_dict(visual.get("qlik_source")) or visual
    return text(
        source.get("sheet_name") or source.get("source") or visual.get("sheet_name"),
        "Unassigned",
    )


from app.qlik.model.table_tmdl import _clean_table_name


def _entity_maps(mapping: Dict[str, Any]):
    """Field name -> owning table, for measures and for columns.

    Dynamically extracts table/column ownership from tables, dimensions,
    and measures in the mapping payload without hardcoded lookups.
    """
    known_tables = {
        _clean_table_name(text(t.get("name") or t.get("table_name")))
        for t in P.tables(mapping)
    }
    measure_home: Dict[str, str] = {}
    for measure in P.measures(mapping):
        name = text(measure.get("name"))
        tables = measure.get("tables") or []
        raw_tbl = text(tables[0]) if tables else text(as_dict(measure.get("fabric")).get("table"))
        home_candidate = _clean_table_name(raw_tbl) if raw_tbl else ""
        home = home_candidate if home_candidate in known_tables else "_Measures"
        if name:
            measure_home[name] = home

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
                field_resolver[f"{table_name}.{column_name}".lower()] = (table_name, column_name)
                field_resolver[f"{table_name}.{column_name.replace('_', ' ')}".lower()] = (table_name, column_name)
                field_resolver[f"[{table_name}].[{column_name}]".lower()] = (table_name, column_name)

                # Check for raw source column names (e.g. DEPARTMENT when column_name is appointments_department)
                for alt_key in ("qlik_column_name", "source_column", "sourceColumn", "name"):
                    raw_alt = text(as_dict(column).get(alt_key))
                    if raw_alt and raw_alt != column_name:
                        ral = raw_alt.lower()
                        if ral not in field_resolver:
                            field_resolver[ral] = (table_name, column_name)
                        field_resolver[f"{table_name}.{raw_alt}".lower()] = (table_name, column_name)
                        field_resolver[f"[{table_name}].[{raw_alt}]".lower()] = (table_name, column_name)

                # Strip table prefix if column_name was prefixed with table_name_ (e.g. appointments_department -> department)
                tbl_prefix = table_name.lower() + "_"
                if cl.startswith(tbl_prefix):
                    unprefixed = cl[len(tbl_prefix):]
                    if unprefixed not in field_resolver:
                        field_resolver[unprefixed] = (table_name, column_name)
                    field_resolver[f"{table_name}.{unprefixed}".lower()] = (table_name, column_name)
                    field_resolver[f"[{table_name}].[{unprefixed}]".lower()] = (table_name, column_name)

    # 2. Map all master dimensions dynamically from payload
    from app.qlik.report.visual_builder import _extract_base_field
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
            matched = difflib.get_close_matches(base_name, table_cols, n=1, cutoff=0.6) if table_cols else []
            if matched:
                field_resolver[name.lower()] = (dim_table, matched[0])
                if name not in column_home:
                    column_home[name] = dim_table
            else:
                # Even if not matching an existing physical column, register this dimension under its host table
                field_resolver[name.lower()] = (dim_table, name)
                if name not in column_home:
                    column_home[name] = dim_table

    # 3. Map all master measures explicitly
    for measure in P.measures(mapping):
        name = text(measure.get("name")).strip()
        if name:
            home = measure_home.get(name, "_Measures")
            field_resolver[name.lower()] = (home, name)

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
            from app.qlik.model.table_tmdl import _clean_table_name
            raw_tbl = text(tables[0]) if tables else ""
            home_cand = _clean_table_name(raw_tbl) if raw_tbl else ""
            measure_home[name] = home_cand if (home_cand and home_cand != "Table") else "_Measures"

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

    # Process report-level and page-level filters from mapping with strict de-duplication
    raw_filters = as_list(mapping.get("filters")) or as_list(mapping.get("filter_panes")) or as_list(as_dict(mapping.get("app_layout")).get("filters"))
    report_filters: List[Dict[str, Any]] = []
    page_filters_by_sheet: Dict[str, List[Dict[str, Any]]] = {}
    seen_report_filters: Set[Tuple[str, str]] = set()
    seen_page_filters: Dict[str, Set[Tuple[str, str]]] = {}

    for f in raw_filters:
        f_dict = as_dict(f)
        fabric_f = as_dict(f_dict.get("fabric"))
        target_t = fabric_f.get("target_table") or f_dict.get("table") or f_dict.get("target_table")
        target_c = fabric_f.get("target_column") or f_dict.get("column") or f_dict.get("target_column")
        f_sheet = fabric_f.get("sheet_name") or f_dict.get("sheet_name")

        if not (target_t and target_c):
            fld = text(f_dict.get("field") or f_dict.get("name"))
            if fld and fld.lower() in field_resolver:
                target_t, target_c = field_resolver[fld.lower()]

        if target_t and target_c:
            filter_key = (target_t.strip().lower(), target_c.strip().lower())
            fname = lineage_tag(f"filt:{target_t}:{target_c}")[:20]
            built_f = _field_filter(fname, target_t, target_c)
            if f_sheet and fabric_f.get("filter_scope") == "page":
                if filter_key not in seen_page_filters.setdefault(f_sheet, set()):
                    seen_page_filters[f_sheet].add(filter_key)
                    page_filters_by_sheet.setdefault(f_sheet, []).append(built_f)
            else:
                if filter_key not in seen_report_filters:
                    seen_report_filters.add(filter_key)
                    report_filters.append(built_f)

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for s in P.sheets(mapping):
        stitle = text(s.get("title") or s.get("name") or s.get("sheet_name"))
        if stitle:
            grouped.setdefault(stitle, [])
    for visual in visuals:
        grouped.setdefault(_sheet_key(visual), []).append(visual)
    if not grouped:
        grouped["Page 1"] = []

    page_titles = list(grouped)
    page_ids = {
        title: slug(title, f"page{index + 1}")
        for index, title in enumerate(page_titles)
    }

    report_doc = json.loads(_report_json())
    if report_filters:
        report_doc["filterConfig"] = {"filters": report_filters}

    from app.qlik.templates.loader import get_base_theme_json, get_static_template, get_page_template

    files: Dict[str, str] = {
        "definition/report.json": json.dumps(report_doc, indent=2),
        # Desktop refuses a PBIR report with no version marker.
        "definition/version.json": json.dumps(
            {"$schema": S.VERSION_METADATA, "version": S.REPORT_VERSION}, indent=2
        ),
        # Bundle both standard Power BI base theme (CY24SU10) and custom converted QlikAppTheme
        f"StaticResources/SharedResources/{S.BASE_THEME_PATH}": get_base_theme_json(),
        f"StaticResources/SharedResources/{S.QLIK_THEME_PATH}": build_theme_json(mapping),
        "definition.pbir": _pbir(model_path),
        ".platform": _platform(app_name),
        ".pbi/localSettings.json": get_static_template("report_localSettings.json"),
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

        sheet_obj = sheets.get(sheet_name, {})
        sheet_props = as_dict(sheet_obj.get("properties"))
        sheet_style = as_dict(sheet_obj.get("style"))
        page_bg = (
            text(sheet_props.get("backgroundColor") or sheet_props.get("background_color"))
            or text(sheet_style.get("backgroundColor") or sheet_style.get("background_color"))
            or text(as_dict(mapping.get("app_layout", {}).get("theme", {})).get("background_color"))
        )
        page_doc = get_page_template()
        page_doc.update({
            "name": page_id,
            "displayName": sheet_name,
            "displayOption": display_option,
            "height": page_height,
            "width": config.CANVAS_WIDTH,
        })
        if page_bg and page_bg.startswith("#"):
            page_doc["objects"] = {
                "background": [{
                    "properties": {
                        "color": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{page_bg}'"}}}}},
                        "transparency": {"expr": {"Literal": {"Value": "0D"}}}
                    }
                }]
            }

        if sheet_name in page_filters_by_sheet and page_filters_by_sheet[sheet_name]:
            page_doc["filterConfig"] = {"filters": page_filters_by_sheet[sheet_name]}

        files[f"definition/pages/{page_id}/page.json"] = json.dumps(page_doc, indent=2)

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
