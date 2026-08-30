"""Qlik sheet grid -> Power BI canvas coordinates.

Qlik sheets vary in grid size (24x12 classic, 84x42 custom-row sheets), so the
grid is read per sheet rather than assumed. The mapping payload usually
carries pre-computed pixels; those are trusted when present and recomputed
otherwise.
"""

from typing import Any, Dict, Optional, Tuple

from app.config import config
from app.util.payload import as_dict, as_list

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
