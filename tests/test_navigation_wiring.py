"""build_navigation_buttons existed but was never called from build_report -
report["navigation_buttons"] was hardcoded to 0 and no button visual.json
files were ever written, so a multi-page report had no way to move between
pages once opened in Power BI (Qlik's sheet navigator has no PBIR
equivalent otherwise).
"""

import json

from app.report.report_writer import build_report


def test_multi_page_report_gets_navigation_buttons(mapping):
    files, notes, report = build_report(mapping, "FleetVision KSA", "../FleetVision KSA.SemanticModel")

    assert report["pages"] > 1, "fixture must be multi-page for this test to mean anything"
    assert report["navigation_buttons"] > 0

    nav_visual_files = [
        path for path, content in files.items()
        if path.endswith("visual.json") and json.loads(content).get("visual", {}).get("visualType") == "actionButton"
    ]
    assert len(nav_visual_files) == report["navigation_buttons"]

    # Every page should carry one button per page (self-navigation included).
    pages_json = json.loads(files["definition/pages/pages.json"])
    page_count = len(pages_json["pageOrder"])
    assert report["navigation_buttons"] == page_count * page_count


def test_single_page_report_gets_no_navigation_buttons():
    single_page_mapping = {
        "tables": [], "measures": [], "dimensions": [], "relationships": [],
        "visuals": {"sheet_visuals": [
            {"qlik_source": {"sheet_name": "Only Page"}, "fabric": {"visual_type": "table"}, "name": "T1"},
        ]},
    }
    files, notes, report = build_report(single_page_mapping, "SinglePageApp", "../SinglePageApp.SemanticModel")
    assert report["pages"] == 1
    assert report["navigation_buttons"] == 0
