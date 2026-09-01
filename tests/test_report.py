"""The PBIR report, visual substitution and layout."""

import json

from app.report import visual_catalog
from app.report.layout import sheet_grid, to_canvas
from app.report.report_writer import build_report
from app.report.visual_builder import build_visual


def _visuals(package):
    return {
        path: json.loads(content)
        for path, content in package.items()
        if path.endswith("visual.json")
    }


def test_report_folder_has_every_required_file(package):
    root = "FleetVision KSA.Report"
    assert f"{root}/definition/report.json" in package
    assert f"{root}/definition/pages/pages.json" in package
    assert f"{root}/definition.pbir" in package
    assert f"{root}/.platform" in package
    assert "FleetVision KSA.pbip" in package


def test_report_points_at_its_semantic_model(package):
    pbir = json.loads(package["FleetVision KSA.Report/definition.pbir"])
    assert pbir["datasetReference"]["byPath"]["path"] == "../FleetVision KSA.SemanticModel"


def test_pages_json_lists_every_page(package):
    pages = json.loads(package["FleetVision KSA.Report/definition/pages/pages.json"])
    on_disk = {
        path.split("/pages/")[1].split("/")[0]
        for path in package if "/pages/" in path and path.endswith("page.json")
    }
    assert set(pages["pageOrder"]) == on_disk
    assert pages["activePageName"] in on_disk


def test_every_visual_json_is_well_formed(package):
    documents = _visuals(package)
    assert documents
    for path, document in documents.items():
        assert document["name"], path
        assert document["visual"]["visualType"], path
        for axis in ("x", "y", "width", "height"):
            assert isinstance(document["position"][axis], int), path


def test_visuals_stay_inside_the_canvas(package):
    """Each page's own declared height (page.json) is the real bound - not
    the fixed config.CANVAS_HEIGHT. build_report deliberately grows a page
    taller than the nominal canvas for content-heavy Qlik sheets (setting
    displayOption to FitToWidth in that case), so a visual is only actually
    overflowing if it exceeds the height its own page declares.
    """
    from app.config import config

    page_heights = {
        path.split("/pages/")[1].split("/")[0]: json.loads(content)["height"]
        for path, content in package.items()
        if path.endswith("/page.json")
    }

    for path, document in _visuals(package).items():
        position = document["position"]
        page_id = path.split("/pages/")[1].split("/")[0]
        assert position["x"] + position["width"] <= config.CANVAS_WIDTH, path
        assert position["y"] + position["height"] <= page_heights[page_id], path
        assert position["width"] >= 40 and position["height"] >= 40, path


def test_field_bindings_reference_real_entities(package):
    for path, document in _visuals(package).items():
        # Navigation buttons bind no data, so they carry no query block.
        query = document["visual"].get("query")
        if not query:
            assert document["visual"]["visualType"] == "actionButton", path
            continue
        for role in query["queryState"].values():
            for projection in role["projections"]:
                field = projection["field"]
                binding = field.get("Column") or field.get("Measure") or (field.get("Aggregation", {}).get("Expression", {}).get("Column") if isinstance(field.get("Aggregation"), dict) else None)
                assert binding and binding["Expression"]["SourceRef"]["Entity"], path
                assert binding["Property"], path


# --- visual substitution -------------------------------------------------

def test_native_visuals_report_no_note():
    visual_type, severity, reason, suggestion = visual_catalog.resolve("barchart")
    assert visual_type == "barChart"
    assert severity == "native" and reason is None and suggestion is None


def test_substituted_visuals_explain_the_swap():
    visual_type, severity, reason, suggestion = visual_catalog.resolve("boxplot")
    assert visual_type == "columnChart"
    assert severity == "substituted"
    assert "no native box plot" in reason
    assert "AppSource" in suggestion


def test_unknown_extensions_fall_back_with_guidance():
    visual_type, severity, reason, suggestion = visual_catalog.resolve(
        "acme-custom-widget", is_extension=True
    )
    assert visual_type == "textbox"
    assert severity == "manual"
    assert suggestion


def test_unknown_type_degrades_to_a_table_not_a_crash():
    visual_type, severity, reason, suggestion = visual_catalog.resolve("mystery-object")
    assert visual_type == "tableEx"
    assert severity == "substituted"
    assert "Unrecognised" in reason


def test_placeholder_visual_carries_the_reason_on_the_canvas():
    document, note, _stats = build_visual(
        {"qlik_source": {"qlik_type": "sn-nlg-insights", "title": "Insights"}},
        {"x": 0, "y": 0, "width": 320, "height": 240}, 0, {}, {},
    )
    assert document["visual"]["visualType"] == "textbox"
    text = json.dumps(document["visual"]["objects"])
    assert "sn-nlg-insights" in text
    assert note["severity"] == "substituted" or note["severity"] == "manual"


def test_notes_are_raised_for_unbound_fields():
    document, note, _stats = build_visual(
        {"qlik_source": {"qlik_type": "barchart", "title": "Sales",
                         "dimensions": [{"name": "Ghost"}], "measures": []}},
        {"x": 0, "y": 0, "width": 320, "height": 240}, 0, {}, {},
    )
    assert note is not None
    assert "Ghost" in note["suggestion"]


def test_report_counts_match_the_notes(mapping):
    _, notes, stats = build_report(mapping, "App", "../App.SemanticModel")
    assert stats["visuals"] == stats["native"] + len(notes)
    assert stats["substituted"] + stats["manual"] <= len(notes)


# --- layout ---------------------------------------------------------------

def test_sheet_grid_is_read_per_sheet():
    assert sheet_grid({"grid": {"columns": 84, "rows": 42}}) == (84.0, 42.0)
    assert sheet_grid({}) == (24.0, 12.0)


def test_grid_scales_to_the_canvas():
    from app.config import config

    full = to_canvas(
        {"layout": {"col": 0, "row": 0, "colspan": 24, "rowspan": 12}}, (24.0, 12.0), 0
    )
    assert full["width"] == config.CANVAS_WIDTH
    assert full["height"] == config.CANVAS_HEIGHT


def test_oversized_visual_is_clamped_not_dropped():
    from app.config import config

    result = to_canvas(
        {"layout": {"col": 20, "row": 10, "colspan": 40, "rowspan": 40}}, (24.0, 12.0), 0
    )
    assert result["x"] + result["width"] <= config.CANVAS_WIDTH
    assert result["y"] + result["height"] <= config.CANVAS_HEIGHT
