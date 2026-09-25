"""Qlik sheet grid -> Power BI canvas coordinates.

Qlik sheets vary in grid size (24x12 classic, 84x42 custom-row sheets), so the
grid is read per sheet rather than assumed. The mapping payload usually
carries pre-computed pixels; those are trusted when present and recomputed
otherwise.
"""

from typing import Any, Dict, List, Optional, Tuple

from app.qlik.config import config
from app.qlik.util.payload import as_dict, as_list

FALLBACK_COLUMNS = 24.0
FALLBACK_ROWS = 12.0
MIN_SIZE = 40

# Per-visual-type minimum heights in pixels.
VISUAL_TYPE_MIN_HEIGHT: Dict[str, int] = {
    "barChart": 300, "columnChart": 300, "clusteredBarChart": 300,
    "clusteredColumnChart": 300, "lineChart": 300, "lineClusteredColumnComboChart": 300,
    "pieChart": 280, "donutChart": 280, "treemap": 280, "scatterChart": 300,
    "waterfallChart": 300, "funnel": 260, "gauge": 200, "map": 300,
    "card": 120, "multiRowCard": 120, "slicer": 180,
    "tableEx": 200, "pivotTable": 200,
    "textbox": 80, "actionButton": 60,
}


def _number(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result


def sheet_grid(
    sheet: Dict[str, Any],
    visuals: Optional[Any] = None,
) -> Tuple[float, float]:
    grid = as_dict(as_dict(sheet).get("grid")) or as_dict(as_dict(sheet).get("properties"))
    columns = _number(grid.get("columns"), 0)
    rows = _number(grid.get("rows") or grid.get("customRowBase"), 0)

    # Detect true column/row bounds from visuals
    if visuals:
        max_col = 0
        max_row = 0
        for v in as_list(visuals):
            pos = (
                as_dict(v.get("layout"))
                or as_dict(as_dict(v.get("fabric")).get("layout"))
                or as_dict(v.get("qlik_source"))
                or as_dict(v.get("position"))
            )
            c = int(_number(pos.get("col", pos.get("x")), 0) + _number(pos.get("colspan", pos.get("width")), 0))
            r = int(_number(pos.get("row", pos.get("y")), 0) + _number(pos.get("rowspan", pos.get("height")), 0))
            max_col = max(max_col, c)
            max_row = max(max_row, r)

        if not columns or max_col > columns:
            columns = float(max_col) if max_col > 0 else FALLBACK_COLUMNS
        if not rows or max_row > rows:
            rows = float(max_row) if max_row > 0 else FALLBACK_ROWS

    return columns or FALLBACK_COLUMNS, rows or FALLBACK_ROWS


def to_canvas(
    visual: Dict[str, Any],
    grid: Tuple[float, float],
    index: int,
    canvas_height: Optional[int] = None,
) -> Dict[str, int]:
    """Resolve a visual's position/size in canvas pixels."""
    fabric = as_dict(visual.get("fabric"))
    layout = as_dict(visual.get("layout")) or as_dict(fabric.get("layout"))
    position = as_dict(visual.get("position")) or as_dict(visual.get("qlik_source"))
    power_bi_gen = as_dict(fabric.get("power_bi_visual_type")).get("general")

    effective_height = canvas_height or config.CANVAS_HEIGHT
    columns, rows = grid

    # 1. First priority: True grid coordinates (col, row, colspan, rowspan)
    col_val = layout.get("col", position.get("col", None))
    row_val = layout.get("row", position.get("row", None))
    colspan_val = layout.get("colspan", position.get("colspan", None))
    rowspan_val = layout.get("rowspan", position.get("rowspan", None))

    if col_val is not None and row_val is not None and colspan_val is not None and rowspan_val is not None:
        col = _number(col_val, 0)
        row = _number(row_val, 0)
        colspan = _number(colspan_val, columns / 4)
        rowspan = _number(rowspan_val, rows / 3)

        scale_x = config.CANVAS_WIDTH / float(columns)
        # In Qlik Sense, row unit height is 60px (720 / 12 = 60). For scrolling sheets (>12 rows),
        # each row is 60px; for standard 12-row sheets, row_height is effective_height / 12 = 60px.
        row_height = float(effective_height) / float(rows) if (rows <= 12 and rows > 0) else 60.0

        computed_x = int(round(col * scale_x))
        computed_y = int(round(row * row_height))
        computed_w = max(int(round(colspan * scale_x)), 40)
        computed_h = max(int(round(rowspan * row_height)), 30)

        return _clamp(
            computed_x,
            computed_y,
            computed_w,
            computed_h,
            canvas_height=effective_height,
        )

    # 2. Second priority: Pre-computed pixels if within reasonable canvas bounds
    for source in (layout, position, power_bi_gen):
        if not isinstance(source, dict):
            continue
        if source.get("width") and source.get("height") and (
            source.get("x") is not None or source.get("y") is not None
        ):
            src_x = int(_number(source.get("x"), 0))
            src_y = int(_number(source.get("y"), 0))
            src_w = int(_number(source.get("width"), 320))
            src_h = int(_number(source.get("height"), 240))

            if src_x + src_w <= config.CANVAS_WIDTH and src_y + src_h <= effective_height:
                return _clamp(
                    src_x,
                    src_y,
                    src_w,
                    src_h,
                    canvas_height=effective_height,
                )

    # 3. Fallback tiling for unpositioned visuals
    col = (index % 2) * (columns / 2)
    row = (index // 2) * (rows / 3)
    scale_x = config.CANVAS_WIDTH / columns
    row_height = (effective_height / rows) if rows > 0 else (effective_height / 12.0)

    return _clamp(
        int(col * scale_x),
        int(row * row_height),
        int((columns / 2) * scale_x),
        int((rows / 3) * row_height),
        canvas_height=effective_height,
    )



def _clamp(
    x: int,
    y: int,
    width: int,
    height: int,
    canvas_height: Optional[int] = None,
) -> Dict[str, int]:
    """Keep every visual inside the canvas and above a usable minimum size."""
    effective_height = canvas_height or config.CANVAS_HEIGHT
    width = max(MIN_SIZE, min(width, config.CANVAS_WIDTH))
    height = max(MIN_SIZE, min(height, effective_height))
    x = max(0, min(x, config.CANVAS_WIDTH - width))
    y = max(0, min(y, max(0, effective_height - height)))
    return {"x": x, "y": y, "width": width, "height": height}


def _get_pb_type(v: Dict[str, Any]) -> str:
    pb = v.get("power_bi_visual_type")
    if isinstance(pb, dict):
        return str(pb.get("power_bi_visual_type") or "").strip().lower()
    return str(pb or "").strip().lower()


def _is_kpi_visual(v: Dict[str, Any]) -> bool:
    qt = str(v.get("qlik_type") or "").strip().lower()
    pbt = _get_pb_type(v)
    if pbt in ("card", "kpi", "multirowcard"):
        return True
    if qt in ("kpi", "sn-kpi"):
        return True
    # Qlik auto-chart with only measures and no dimensions is a KPI card
    if qt == "auto-chart":
        cols = v.get("columns") or []
        rows = v.get("rows") or []
        if isinstance(cols, list) and len(cols) == 0 and isinstance(rows, list) and len(rows) > 0:
            return True
        if v.get("kpi_styling"):
            return True
    return False


def _is_slicer_visual(v: Dict[str, Any]) -> bool:
    qt = str(v.get("qlik_type") or "").strip().lower()
    pbt = _get_pb_type(v)
    return pbt in ("slicer",) or qt in ("filterpane", "sn-filterpane", "listbox", "variable-input", "variableinput")


def compute_sheet_layout(
    sheet_visuals: list,
    grid: Tuple[float, float],
    sheet_obj: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, int]], int]:
    """Compute non-overlapping canvas bounds for all visuals on a sheet.

    If visuals have explicit Qlik grid or pixel coordinates, preserves them.
    If visuals are unpositioned, arranges them in an enterprise dashboard layout:
    - KPIs / Metric Cards on top (horizontal row)
    - Slicers / Filters below KPIs
    - Charts / Tables below in a balanced 2-column or full-width grid
    - Dynamically expands canvas height to fit all content cleanly without clipping.
    """
    import math

    if not sheet_visuals:
        return [], config.CANVAS_HEIGHT

    # Check if ANY visual has explicit coordinates
    def has_coords(v: Dict[str, Any]) -> bool:
        lay = as_dict(v.get("layout")) or as_dict(as_dict(v.get("fabric")).get("layout"))
        pos = as_dict(v.get("position"))
        if lay.get("col") is not None and lay.get("row") is not None:
            return True
        if pos.get("col") is not None and pos.get("row") is not None:
            return True
        if lay.get("x") is not None and lay.get("y") is not None:
            return True
        if pos.get("x") is not None and pos.get("y") is not None:
            return True
        return False

    explicit_count = sum(1 for v in sheet_visuals if has_coords(v))

    if explicit_count > len(sheet_visuals) // 2:
        # Most visuals have explicit coordinates: calculate bounds directly
        columns, rows = grid
        base_h = max(config.CANVAS_HEIGHT, int(round(rows * 60.0))) if rows > 12 else config.CANVAS_HEIGHT
        prelim = [to_canvas(v, grid, i, canvas_height=base_h) for i, v in enumerate(sheet_visuals)]
        max_bottom = max((p["y"] + p["height"] for p in prelim), default=base_h)
        actual_h = max(base_h, max_bottom + 40)
        final_rects = [to_canvas(v, grid, i, canvas_height=actual_h) for i, v in enumerate(sheet_visuals)]
        return final_rects, actual_h

    # Otherwise, arrange intelligently without overlap
    CANVAS_WIDTH = config.CANVAS_WIDTH
    MIN_HEIGHT = config.CANVAS_HEIGHT
    margin = 16
    gap = 16
    content_w = CANVAS_WIDTH - 2 * margin

    kpis = [v for v in sheet_visuals if _is_kpi_visual(v)]
    slicers = [v for v in sheet_visuals if _is_slicer_visual(v)]
    charts = [v for v in sheet_visuals if not _is_kpi_visual(v) and not _is_slicer_visual(v)]

    positions: Dict[int, Dict[str, int]] = {}
    cur_y = margin

    # 1. KPIs on top
    if kpis:
        num_kpis = len(kpis)
        cols = num_kpis if num_kpis <= 5 else (4 if num_kpis <= 8 else 5)
        kpi_w = (content_w - (cols - 1) * gap) // cols
        kpi_h = 105
        for i, k in enumerate(kpis):
            r = i // cols
            c = i % cols
            x = margin + c * (kpi_w + gap)
            y = cur_y + r * (kpi_h + gap)
            positions[id(k)] = {"x": int(x), "y": int(y), "width": int(kpi_w), "height": int(kpi_h)}
        num_rows = math.ceil(num_kpis / cols)
        cur_y += num_rows * (kpi_h + gap)

    # 2. Slicers / Filters below KPIs
    if slicers:
        num_slicers = len(slicers)
        cols = num_slicers if num_slicers <= 4 else 3
        sl_w = (content_w - (cols - 1) * gap) // cols
        sl_h = 80
        for i, s in enumerate(slicers):
            r = i // cols
            c = i % cols
            x = margin + c * (sl_w + gap)
            y = cur_y + r * (sl_h + gap)
            positions[id(s)] = {"x": int(x), "y": int(y), "width": int(sl_w), "height": int(sl_h)}
        num_rows = math.ceil(num_slicers / cols)
        cur_y += num_rows * (sl_h + gap)

    # 3. Charts & Visualizations below
    if charts:
        num_charts = len(charts)
        ch_w = (content_w - gap) // 2
        ch_h = 320
        for i, ch in enumerate(charts):
            r = i // 2
            c = i % 2
            is_lone_last = (i == num_charts - 1 and c == 0)
            actual_w = content_w if is_lone_last else ch_w
            x = margin if c == 0 else (margin + ch_w + gap)
            y = cur_y + r * (ch_h + gap)
            positions[id(ch)] = {"x": int(x), "y": int(y), "width": int(actual_w), "height": int(ch_h)}
        num_rows = math.ceil(num_charts / 2)
        cur_y += num_rows * (ch_h + gap)

    page_height = max(MIN_HEIGHT, cur_y + margin)
    result_rects = [positions.get(id(v), {"x": margin, "y": margin, "width": 400, "height": 300}) for v in sheet_visuals]
    return result_rects, page_height
