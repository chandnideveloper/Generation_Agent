import pytest
from app.qlik.config import config
from app.qlik.report.layout import sheet_grid, to_canvas, compute_sheet_layout

def test_visual_layout_honors_mapping_target_position():
    # Visual with explicit target coordinates from mapping
    visual = {
        "title": "Monthly Revenue Trend",
        "layout": {
            "col": 0, "row": 5, "colspan": 12, "rowspan": 7,
            "pixel_bounds_720p": {"left": 0, "top": 300, "width": 640, "height": 420},
        },
        "position": {
            "source": {"coordinate_system": "qlik_grid", "col": 0, "row": 5, "colspan": 12, "rowspan": 7},
            "target": {"coordinate_system": "powerbi_canvas", "x": 0, "y": 300, "width": 640, "height": 420},
        }
    }
    grid = (24.0, 12.0)
    canvas = to_canvas(visual, grid, 0, canvas_height=720)
    assert canvas["x"] == 0
    assert canvas["y"] == 300
    assert canvas["width"] == 640
    assert canvas["height"] == 420

def test_single_visual_sparse_sheet_grid_does_not_shrink():
    # Visual occupying only col 0..12 and row 0..6
    visual = {
        "title": "Top 10 Trucks by Revenue",
        "layout": {"col": 0, "row": 0, "colspan": 12, "rowspan": 6},
        "position": {
            "source": {"coordinate_system": "qlik_grid", "col": 0, "row": 0, "colspan": 12, "rowspan": 6},
            "target": {"coordinate_system": "powerbi_canvas", "x": 0, "y": 0, "width": 640, "height": 360},
        }
    }
    grid = sheet_grid({}, visuals=[visual])
    assert grid[0] == 24.0  # Must not shrink to 12.0
    assert grid[1] == 12.0  # Must not shrink to 6.0

    canvas = to_canvas(visual, grid, 0, canvas_height=720)
    assert canvas["x"] == 0
    assert canvas["y"] == 0
    assert canvas["width"] == 640
    assert canvas["height"] == 360

def test_standard_sheet_does_not_expand_height_unnecessarily():
    sheet_visuals = [
        {
            "title": "Active Trucks",
            "layout": {"col": 0, "row": 0, "colspan": 24, "rowspan": 12},
            "position": {
                "target": {"x": 0, "y": 0, "width": 1280, "height": 720}
            }
        }
    ]
    grid = (24.0, 12.0)
    rects, page_height = compute_sheet_layout(sheet_visuals, grid)
    assert page_height == 720
    assert rects[0]["height"] == 720
