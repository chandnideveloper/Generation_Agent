"""One PBIR visual.json.

Field bindings are emitted as `projections` referencing model
entities/properties, which is how Power BI binds a visual to the semantic
model. A visual whose fields cannot be resolved still renders — it just comes
up empty — so binding failures are reported rather than raised.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from app.report import pbir_schemas, visual_catalog
from app.report.filters import build_filter_config, count_applied
from app.report.projections import _aggregation_projection, _measure_projection, _names, _projection
from app.util.ids import lineage_tag
from app.util.payload import as_dict, as_list, text

# Which projection bucket each visual type uses for categories vs values.
ROLES = {
    "card": ("", "Values"),
    "multiRowCard": ("", "Values"),
    "gauge": ("", "Y"),
    "slicer": ("Values", ""),
    "textbox": ("", ""),
    "actionButton": ("", ""),
    "tableEx": ("Values", ""),          # dimensions only; measures added separately
    "pivotTable": ("Rows", "Values"),
    "pieChart": ("Category", "Y"),
    "donutChart": ("Category", "Y"),
    "treemap": ("Group", "Values"),
    "funnel": ("Category", "Y"),
    "map": ("Category", "Size"),
    "filledMap": ("Category", "Y"),
    "scatterChart": ("Details", ""),    # handled specially below
    "waterfallChart": ("Category", "Y"),
    "lineChart": ("Category", "Y"),
    "barChart": ("Category", "Y"),
    "columnChart": ("Category", "Y"),
    "clusteredBarChart": ("Category", "Y"),
    "clusteredColumnChart": ("Category", "Y"),
    "lineClusteredColumnComboChart": ("Category", "Y"),  # Y2 handled specially
}
DEFAULT_ROLES = ("Category", "Y")


def _source(visual: Dict[str, Any]) -> Dict[str, Any]:
    return as_dict(visual.get("qlik_source")) or visual


def _resolve_title(source: Dict[str, Any], visual: Dict[str, Any], qlik_type: str) -> str:
    """Extract human-readable visual title from mapping payload."""
    formatting = as_dict(source.get("formatting"))
    fabric = as_dict(visual.get("fabric"))
    candidates = [
        formatting.get("title"),
        fabric.get("title"),
        source.get("title"),
        visual.get("title"),
        visual.get("name"),
    ]
    for c in candidates:
        if c and isinstance(c, str) and c.strip() and c.strip().lower() != "visualizations item":
            return c.strip()
    return qlik_type.replace("-", " ").replace("sn-", "").title()

import difflib
import re


def _extract_base_field(field_expr: str) -> str:
    if not field_expr:
        return ""
    clean = field_expr.strip()
    if clean.startswith("[") and clean.endswith("]"):
        clean = clean[1:-1].strip()
    m = re.search(r"\b(?:Sum|Count|Avg|Min|Max|CountRows|DistinctCount)\s*\(\s*(?:\{.*?\}\s*)?(?:\[)?([a-zA-Z0-9_\s]+)(?:\])?\s*\)", clean, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m_col = re.search(r"\[([a-zA-Z0-9_\s]+)\]", clean)
    if m_col:
        return m_col.group(1).strip()
    # Strip drilldown arrow if single extraction
    if " -> " in clean:
        return clean.split(" -> ")[0].strip()
    if " > " in clean:
        return clean.split(" > ")[0].strip()
    # Strip common Qlik dimension labels like "Region Drill-down" -> "Region"
    if re.search(r"[\s_-]*drill[\s_-]*down$", clean, re.IGNORECASE):
        return re.sub(r"[\s_-]*drill[\s_-]*down$", "", clean, flags=re.IGNORECASE).strip()
    # Strip Qlik autoCalendar / derived date suffixes (e.g. transaction_date.YearMonth -> transaction_date)
    if "." in clean and not clean.startswith("."):
        parts = clean.split(".")
        if len(parts) >= 2 and parts[0].strip():
            return parts[0].strip()
    return clean


def _match_field(
    name: str,
    measure_home: Dict[str, str],
    column_home: Dict[str, str],
    field_resolver: Optional[Dict[str, Tuple[str, str]]] = None,
) -> Optional[Tuple[str, str, bool]]:
    """Returns (entity, property, is_measure) if found, else None."""
    if not name:
        return None
    clean = name.strip()
    c_lower = clean.lower()
    base = _extract_base_field(clean)
    b_lower = base.lower()

    # 1. Exact or base in measure_home
    if clean in measure_home:
        return (measure_home[clean], clean, True)
    for m in measure_home:
        if m.lower() in (c_lower, b_lower):
            return (measure_home[m], m, True)

    # 2. Exact or base in field_resolver
    if field_resolver:
        for k in (c_lower, b_lower):
            if k in field_resolver:
                ent, prop = field_resolver[k]
                return (ent, prop, False)

    # 3. Exact or base in column_home
    if clean in column_home:
        return (column_home[clean], clean, False)
    if base in column_home:
        return (column_home[base], base, False)
    for col in column_home:
        if col.lower() in (c_lower, b_lower):
            return (column_home[col], col, False)

    # 4. Fuzzy match against measures (handles typos like "Revneue" -> "Revenue")
    if measure_home:
        meas_names = list(measure_home.keys())
        close_meas = difflib.get_close_matches(clean, meas_names, n=1, cutoff=0.75)
        if not close_meas:
            close_meas = difflib.get_close_matches(base, meas_names, n=1, cutoff=0.75)
        if close_meas:
            m = close_meas[0]
            return (measure_home[m], m, True)

    # 5. Fuzzy match against columns
    all_cols = list(column_home.keys())
    if field_resolver:
        all_cols.extend([prop for ent, prop in field_resolver.values()])
    if all_cols:
        close_cols = difflib.get_close_matches(clean, all_cols, n=1, cutoff=0.75)
        if not close_cols:
            close_cols = difflib.get_close_matches(base, all_cols, n=1, cutoff=0.75)
        if close_cols:
            col_match = close_cols[0]
            if field_resolver and col_match.lower() in field_resolver:
                ent, prop = field_resolver[col_match.lower()]
                return (ent, prop, False)
            if col_match in column_home:
                return (column_home[col_match], col_match, False)

def _parse_font_size(size_obj: Any) -> Optional[float]:
    """Parse font size from float, int, str ('14pt', '14px', '14', 'M'), or dict ({'fixed': '14'})."""
    if not size_obj:
        return None
    if isinstance(size_obj, dict):
        raw = size_obj.get("fixed") or size_obj.get("size") or size_obj.get("fontSize") or size_obj.get("value")
    else:
        raw = size_obj
    if raw is None or str(raw).lower() in ("auto", "none", "default", ""):
        return None
    raw_str = str(raw).strip().lower()
    tshirt_map = {"xs": 9.0, "s": 11.0, "small": 11.0, "m": 14.0, "medium": 14.0, "l": 18.0, "large": 18.0, "xl": 24.0, "xxl": 36.0}
    if raw_str in tshirt_map:
        return tshirt_map[raw_str]
    try:
        return float(str(raw).replace("pt", "").replace("px", "").strip())
    except (ValueError, TypeError):
        return None


def build_visual(
    visual: Dict[str, Any],
    position: Dict[str, int],
    z_index: int,
    measure_home: Dict[str, str],
    column_home: Dict[str, str],
    field_resolver: Optional[Dict[str, Tuple[str, str]]] = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], Dict[str, int]]:
    """Return (visual.json, note-or-None, stats)."""
    source = _source(visual)
    fabric = as_dict(visual.get("fabric"))

    qlik_type = text(
        source.get("chart_type")
        or source.get("qlik_type")
        or source.get("type")
        or visual.get("chart_type")
        or visual.get("qlik_type")
        or visual.get("visual_type"),
        "unknown",
    )
    is_extension = bool(source.get("is_extension") or "ext" in qlik_type.lower())

    # Prioritize explicit visual_type specified in fabric mapping, otherwise
    # resolve from catalog. But an explicit type is only trustworthy when it
    # doesn't require an AppSource custom visual this pipeline never
    # registers (see CUSTOM_VISUAL_ONLY_TYPES) - mapping stages emit
    # "boxPlot"/"sankeyDiagram" as if they were ordinary native types, which
    # produces Fabric's "add this custom visual first" placeholder instead of
    # a chart. In that case, defer to the catalog's own safe substitution
    # (e.g. boxplot -> columnChart) instead of trusting the literal value.
    explicit_visual_type = text(fabric.get("visual_type"))
    requires_custom_visual = bool(fabric.get("requires_custom_visual")) or (
        explicit_visual_type in visual_catalog.CUSTOM_VISUAL_ONLY_TYPES
    )
    if explicit_visual_type and not requires_custom_visual:
        visual_type = explicit_visual_type
        severity, reason, suggestion = "native", None, None
    else:
        visual_type, severity, reason, suggestion = visual_catalog.resolve(qlik_type, is_extension)

    title = _resolve_title(source, visual, qlik_type)
    object_id = text(source.get("qlik_name") or source.get("id") or visual.get("object_id"))

    category_role, value_role = ROLES.get(visual_type, DEFAULT_ROLES)
    projections: Dict[str, List[Dict[str, Any]]] = {}
    unbound: List[str] = []

    raw_dims = _names(
        source.get("dimensions")
        or source.get("x_axis")
        or fabric.get("x_axis_fields")
        or visual.get("x_axis")
    )
    dimensions = []
    for d in raw_dims:
        if " -> " in d:
            dimensions.extend([p.strip() for p in d.split(" -> ") if p.strip()])
        elif " > " in d:
            dimensions.extend([p.strip() for p in d.split(" > ") if p.strip()])
        else:
            dimensions.append(d)

    measures = _names(
        source.get("measures")
        or source.get("y_axis")
        or fabric.get("y_axis_fields")
        or visual.get("y_axis")
    )

    if category_role:
        bucket = []
        for index, field in enumerate(dimensions):
            field_clean = field.strip()
            match = _match_field(field_clean, measure_home, column_home, field_resolver)
            if match:
                ent, prop, is_meas = match
                proj = _measure_projection(ent, prop) if is_meas else _projection(ent, prop, index)
                bucket.append(proj)
            else:
                unbound.append(field_clean)
        if not bucket and visual_type == "slicer" and qlik_type in ("qlik-variable-input", "variable-input", "variableinput", "variable"):
            # Variable input slicer bound to dynamic Parameters table
            bucket.append(_projection("Parameters", "Label", 0))
        if bucket:
            projections[category_role] = bucket

    # ── scatterChart: X / Y / Size / Category ────────────────────────────────
    if visual_type == "scatterChart":
        def _resolve_scatter_field(name: str, idx: int, use_agg: bool):
            match = _match_field(name, measure_home, column_home, field_resolver)
            if match:
                ent, prop, is_meas = match
                if is_meas:
                    return _measure_projection(ent, prop)
                return _aggregation_projection(ent, prop, idx, 0) if use_agg else _projection(ent, prop, idx)
            unbound.append(name.strip())
            return None

        # dimensions → Category / Details (the category grouping)
        cat_bucket = []
        for i, d in enumerate(dimensions):
            p = _resolve_scatter_field(d, i, False)
            if p:
                cat_bucket.append(p)
        if cat_bucket:
            projections["Category"] = cat_bucket

        # measures[0] → X Axis, measures[1] → Y Axis, measures[2] → Size
        x_fields = measures[:1]
        y_fields = measures[1:2]
        z_fields = measures[2:3]
        if not y_fields and len(measures) == 1:
            y_fields = x_fields
            x_fields = []
        x_bucket = []
        for f in x_fields:
            p = _resolve_scatter_field(f, len(x_bucket), True)
            if p:
                x_bucket.append(p)
        y_bucket = []
        for f in y_fields:
            p = _resolve_scatter_field(f, len(y_bucket), True)
            if p:
                y_bucket.append(p)
        size_bucket = []
        for f in z_fields:
            p = _resolve_scatter_field(f, len(size_bucket), True)
            if p:
                size_bucket.append(p)
        if x_bucket:
            projections["X"] = x_bucket
        if y_bucket:
            projections["Y"] = y_bucket
        if size_bucket:
            projections["Size"] = size_bucket

    elif value_role:
        bucket = []
        is_chart_value = visual_type not in ("tableEx", "pivotTable", "slicer") and value_role in ("Y", "Values", "Size")
        for field in measures:
            field_clean = field.strip()
            match = _match_field(field_clean, measure_home, column_home, field_resolver)
            if match:
                ent, prop, is_meas = match
                if is_meas:
                    bucket.append(_measure_projection(ent, prop))
                else:
                    proj = _aggregation_projection(ent, prop, len(bucket), 0) if is_chart_value else _projection(ent, prop, len(bucket))
                    bucket.append(proj)
            else:
                unbound.append(field_clean)

        if visual_type == "tableEx":
            # tableEx: dimensions already in Values from category_role; add measures separately
            meas_bucket = [p for p in bucket]
            if meas_bucket:
                projections.setdefault("Values", []).extend(meas_bucket)
        elif visual_type == "lineClusteredColumnComboChart" and len(bucket) > 1:
            # Combo chart: first measure → Y (bars), rest → Y2 (line)
            projections["Y"] = [bucket[0]]
            projections["Y2"] = bucket[1:]
        elif bucket:
            projections.setdefault(value_role, []).extend(bucket)


    name = object_id or lineage_tag(f"visual:{title}:{z_index}")[:8]

    
    # 1. Title styling
    style = as_dict(source.get("style")) or as_dict(fabric.get("style"))
    formatting_dict = as_dict(source.get("formatting")) or as_dict(fabric.get("formatting"))
    title_props: Dict[str, Any] = {
        "text": {"expr": {"Literal": {"Value": f"'{title}'"}}},
        "show": {"expr": {"Literal": {"Value": "true"}}},
    }
    title_color = text(formatting_dict.get("title_color") or style.get("title_color") or style.get("titleColor"))
    if title_color and title_color != "default":
        title_props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{title_color}'"}}}}}
    
    title_font_size = _parse_font_size(formatting_dict.get("title_font_size") or style.get("font_size") or style.get("fontSize"))
    if title_font_size is not None:
        title_props["fontSize"] = {"expr": {"Literal": {"Value": f"{title_font_size}D"}}}

    title_font = text(
        formatting_dict.get("title_font_family")
        or style.get("title_font_family")
        or style.get("font_family")
        or style.get("fontFamily")
    )
    if title_font and title_font.lower() not in ("default", "none", "auto"):
        clean_font = title_font.split(",")[0].strip().strip("'").strip('"')
        title_props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{clean_font}'"}}}

    objects: Dict[str, Any] = {
        "title": [{"properties": title_props}]
    }

    # ── legend styling ───────────────────────────────────────────────────────
    legend_cfg = as_dict(formatting_dict.get("legend"))
    if visual_type in ("pieChart", "donutChart", "lineChart", "barChart", "columnChart", "clusteredBarChart", "clusteredColumnChart", "lineClusteredColumnComboChart", "comboChart"):
        show_legend = legend_cfg.get("show", True)
        legend_props: Dict[str, Any] = {
            "show": {"expr": {"Literal": {"Value": "true" if show_legend else "false"}}}
        }
        dock = str(legend_cfg.get("dock", "auto")).lower()
        pos_map = {"top": "'Top'", "bottom": "'Bottom'", "left": "'Left'", "right": "'Right'"}
        if dock in pos_map:
            legend_props["position"] = {"expr": {"Literal": {"Value": pos_map[dock]}}}
        objects["legend"] = [{"properties": legend_props}]

    # ── slicer: add slicerSettings ────────────────────────────────────────────
    if visual_type == "slicer":
        objects["slicerSettings"] = [{
            "properties": {
                "orientation": {"expr": {"Literal": {"Value": "'vertical'"}}},
                "mode": {"expr": {"Literal": {"Value": "'basic'"}}},
            }
        }]

    # ── Custom coloring & single color fill ──────────────────────────────────
    custom_coloring = (
        as_dict(source.get("custom_coloring"))
        or as_dict(fabric.get("custom_coloring"))
        or as_dict(source.get("coloring"))
        or as_dict(fabric.get("coloring"))
    )

    # Extract measure color from parsing conditional_coloring (e.g. #002833)
    meas_color = None
    for m in as_list(source.get("measures")):
        if isinstance(m, dict):
            cc = as_dict(m.get("conditional_coloring"))
            psc = as_dict(cc.get("paletteSingleColor"))
            if psc.get("color"):
                meas_color = psc.get("color")
                break
            if cc.get("singleColor") and isinstance(cc.get("singleColor"), str):
                meas_color = cc.get("singleColor")
                break
            coloring = as_dict(m.get("coloring"))
            if coloring.get("color"):
                meas_color = coloring.get("color")
                break

    single_color = (
        text(custom_coloring.get("single_color"))
        or text(custom_coloring.get("baseColor"))
        or text(custom_coloring.get("color"))
        or text(meas_color)
    )

    # ── actionButton: add text, fill, outline, and action objects ────────────
    if visual_type == "actionButton":
        btn_action_data = (
            as_dict(source.get("button_action"))
            or as_dict(source.get("actions"))
            or as_dict(fabric.get("button_action"))
        )
        bookmark_ref = text(btn_action_data.get("bookmark") or btn_action_data.get("bookmark_id"))
        page_ref = text(btn_action_data.get("page") or btn_action_data.get("target_sheet"))
        url_ref = text(btn_action_data.get("url") or btn_action_data.get("navigation_url"))
        if bookmark_ref:
            nav_props = {
                "type": {"expr": {"Literal": {"Value": "'Bookmark'"}}},
                "bookmarkName": {"expr": {"Literal": {"Value": f"'{bookmark_ref}'"}}},
            }
        elif page_ref:
            nav_props = {
                "type": {"expr": {"Literal": {"Value": "'PageNavigation'"}}},
                "navigationSection": {"expr": {"Literal": {"Value": f"'{page_ref}'"}}},
            }
        elif url_ref:
            nav_props = {
                "type": {"expr": {"Literal": {"Value": "'WebURL'"}}},
                "url": {"expr": {"Literal": {"Value": f"'{url_ref}'"}}},
            }
        else:
            nav_props = {
                "type": {"expr": {"Literal": {"Value": "'PageNavigation'"}}},
            }
        objects["action"] = [{"properties": nav_props}]

        btn_color = text(
            style.get("button_color")
            or style.get("primary_color")
            or (custom_coloring.get("single_color") if isinstance(custom_coloring, dict) else None)
        )
        btn_text = text(
            source.get("button_text")
            or source.get("label")
            or formatting_dict.get("title")
            or source.get("title")
            or title
        )
        btn_text_color = text(style.get("font_color") or style.get("color") or formatting_dict.get("font_color"))
        text_props: Dict[str, Any] = {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "text": {"expr": {"Literal": {"Value": f"'{btn_text}'"}}},
            "alignment": {"expr": {"Literal": {"Value": "'center'"}}},
        }
        btn_font_size = _parse_font_size(style.get("font_size") or formatting_dict.get("font_size"))
        if btn_font_size is not None:
            text_props["fontSize"] = {"expr": {"Literal": {"Value": f"{btn_font_size}D"}}}
        if btn_text_color:
            text_props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{btn_text_color}'"}}}}}
        objects["text"] = [{"properties": text_props}]

        if btn_color:
            objects["fill"] = [{
                "properties": {
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "fillColor": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{btn_color}'"}}}}},
                    "transparency": {"expr": {"Literal": {"Value": "0D"}}},
                }
            }]
            objects["outline"] = [{
                "properties": {
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "lineColor": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{btn_color}'"}}}}},
                    "weight": {"expr": {"Literal": {"Value": "1D"}}},
                }
            }]

    # ── image visual: add image url and scaling ──────────────────────────────
    if visual_type == "image":
        img_url = text(
            source.get("image_url")
            or source.get("url")
            or source.get("media_url")
            or source.get("src")
            or as_dict(source.get("image")).get("url")
            or as_dict(as_dict(source.get("style")).get("image")).get("url")
            or as_dict(fabric.get("image")).get("url")
        )
        if img_url:
            objects["image"] = [{
                "properties": {
                    "url": {"expr": {"Literal": {"Value": f"'{img_url}'"}}},
                    "scaling": {"expr": {"Literal": {"Value": "'Fit'"}}},
                }
            }]

    # ── Canvas / Container background & border ───────────────────────────────
    formatting_dict = as_dict(source.get("formatting"))
    components_list = as_list(as_dict(source.get("style_and_formatting")).get("components"))
    comp_bg = None
    for c in components_list:
        if isinstance(c, dict) and c.get("key") == "general":
            bg_c = as_dict(c.get("bgColor")).get("color")
            if isinstance(bg_c, dict) and bg_c.get("color"):
                comp_bg = bg_c.get("color")
            elif isinstance(bg_c, str):
                comp_bg = bg_c

    bg_color = text(
        style.get("background_color")
        or formatting_dict.get("background_color")
        or comp_bg
        or style.get("backgroundColor")
        or style.get("bgColor")
    )
    if bg_color and bg_color.lower() not in ("default", "none", "auto"):
        objects["background"] = [{
            "properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}},
                "color": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{bg_color}'"}}}}},
                "transparency": {"expr": {"Literal": {"Value": "0D"}}}
            }
        }]

    border_color = text(style.get("border_color") or formatting_dict.get("border_color"))
    if border_color and border_color.lower() not in ("default", "none", "auto"):
        objects["border"] = [{
            "properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}},
                "color": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{border_color}'"}}}}},
                "radius": {"expr": {"Literal": {"Value": "4D"}}}
            }
        }]

    kpi_styling = as_dict(source.get("kpi_styling")) or as_dict(fabric.get("kpi_styling"))
    if visual_type == "card":
        val_color = text(kpi_styling.get("value_color") or single_color)
        val_size = _parse_font_size(kpi_styling.get("value_font_size"))
        val_font = text(kpi_styling.get("value_font_family"))
        val_props: Dict[str, Any] = {}
        if val_color and val_color.lower() not in ("default", "none", "auto"):
            val_props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{val_color}'"}}}}}
        if val_font and val_font.lower() not in ("default", "none", "auto"):
            clean_vf = val_font.split(",")[0].strip().strip("'").strip('"')
            val_props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{clean_vf}'"}}}
        if val_size is not None:
            if val_size < 5:
                val_size = val_size * 45.0
            val_props["fontSize"] = {"expr": {"Literal": {"Value": f"{val_size}D"}}}
        if val_props:
            objects["valueLabel"] = [{"properties": val_props}]

        lbl_color = text(kpi_styling.get("label_color"))
        lbl_font = text(kpi_styling.get("label_font_family"))
        lbl_size = _parse_font_size(kpi_styling.get("label_font_size"))
        lbl_props: Dict[str, Any] = {}
        if lbl_color and lbl_color.lower() not in ("default", "none", "auto"):
            lbl_props["fontColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{lbl_color}'"}}}}}
        if lbl_font and lbl_font.lower() not in ("default", "none", "auto"):
            clean_lf = lbl_font.split(",")[0].strip().strip("'").strip('"')
            lbl_props["fontFamily"] = {"expr": {"Literal": {"Value": f"'{clean_lf}'"}}}
        if lbl_size is not None:
            lbl_props["fontSize"] = {"expr": {"Literal": {"Value": f"{lbl_size}D"}}}
        if lbl_props:
            objects["categoryLabel"] = [{"properties": lbl_props}]
    elif single_color and single_color.lower() not in ("default", "none", "auto"):
        objects["dataPoint"] = [{
            "properties": {
                "fill": {"solid": {"color": {"expr": {"Literal": {"Value": f"'{single_color}'"}}}}}
            }
        }]

    # 4. Reference lines
    ref_lines = (
        as_list(source.get("reference_lines"))
        or as_list(fabric.get("reference_lines"))
        or as_list(source.get("ref_lines"))
        or as_list(fabric.get("ref_lines"))
    )
    if ref_lines:
        ref_objects = []
        for ref in ref_lines:
            ref = as_dict(ref)
            ref_label = text(ref.get("label") or "Reference Line")
            ref_expr = text(ref.get("expression") or ref.get("value") or "0")
            ref_color = text(ref.get("color"))
            ref_style = text(ref.get("line_type") or ref.get("lineStyle") or "dashed").lower()
            if ref_style not in ("solid", "dashed", "dotted"):
                ref_style = "dashed"

            ref_prop: Dict[str, Any] = {
                "show": {"expr": {"Literal": {"Value": "true"}}},
                "displayName": {"expr": {"Literal": {"Value": f"'{ref_label}'"}}},
                "lineStyle": {"expr": {"Literal": {"Value": f"'{ref_style}'"}}},
                "dataLabelShow": {"expr": {"Literal": {"Value": "true" if ref.get("show_label", True) else "false"}}},
            }
            if ref_color:
                ref_prop["lineColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{ref_color}'"}}}}}
            if ref_expr:
                try:
                    num_val = float(ref_expr)
                    ref_prop["value"] = {"expr": {"Literal": {"Value": f"{num_val}D"}}}
                except ValueError:
                    ref_prop["value"] = {"expr": {"Literal": {"Value": f"'{ref_expr}'"}}}
            ref_objects.append({"properties": ref_prop})
        if ref_objects:
            objects["valueAxisReferenceLine"] = ref_objects

    # 5. Trend lines
    trend_lines = as_list(source.get("trend_lines")) or as_list(fabric.get("trend_lines"))
    if not trend_lines:
        for m in as_list(source.get("measures") or fabric.get("measures")):
            m_dict = as_dict(m)
            if m_dict.get("trend_lines"):
                trend_lines.extend(as_list(m_dict.get("trend_lines")))
    if trend_lines:
        t_item = as_dict(trend_lines[0])
        t_style = text(t_item.get("line_type") or "solid").lower()
        tl_color = text(t_item.get("color") or (custom_coloring.get("single_color") if isinstance(custom_coloring, dict) else None))
        tl_props: Dict[str, Any] = {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "style": {"expr": {"Literal": {"Value": f"'{t_style}'"}}}
        }
        if tl_color:
            tl_props["lineColor"] = {"solid": {"color": {"expr": {"Literal": {"Value": f"'{tl_color}'"}}}}}
        objects["trendLine"] = [{"properties": tl_props}]

    if visual_type == "textbox":
        # A placeholder must say why it is empty, on the canvas itself.
        objects["general"] = [{
            "properties": {
                "paragraphs": [{
                    "textRuns": [{
                        "value": f"{title}\n\n[{qlik_type}] {reason or ''} {suggestion or ''}".strip()
                    }]
                }]
            }
        }]

    document = {
        "$schema": pbir_schemas.VISUAL_CONTAINER,
        "name": name,
        "position": {**position, "z": z_index, "tabOrder": z_index},
        "visual": {
            "visualType": visual_type,
            "query": {"queryState": _query_state(projections)},
            "objects": objects,
            "drillFilterOtherVisuals": True,
        },
        "filterConfig": build_filter_config(visual, column_home, measure_home, field_resolver=field_resolver),
    }

    applied = count_applied(document["filterConfig"])

    note = None
    if severity != "native" or unbound:
        note = {
            "object_id": object_id or None,
            "title": title,
            "qlik_type": qlik_type,
            "mapped_to": visual_type,
            "severity": severity if severity != "native" else "info",
            "reason": reason or f"{len(unbound)} field(s) could not be bound to the model.",
            "suggestion": suggestion or (
                f"Bind these fields by hand after import: {', '.join(sorted(set(unbound)))}."
                if unbound else ""
            ),
        }

    return document, note, applied


def _query_state(projections: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    return {
        role: {"projections": items} for role, items in projections.items() if items
    }
