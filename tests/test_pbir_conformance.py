"""PBIR / PBIP conformance.

Every assertion here corresponds to something that made Power BI Desktop or
Fabric reject a generated project. The schema versions were taken from a
working Power BI project, not inferred.
"""

import json

from app.package import validator
from app.report import pbir_schemas as S


def _json(package, path):
    return json.loads(package[path])


def _visuals(package):
    return {
        path: json.loads(content)
        for path, content in package.items()
        if path.endswith("visual.json")
    }


# --- the whole package ----------------------------------------------------

def test_package_passes_structural_validation(package):
    result = validator.validate(package, "FleetVision KSA")
    assert result["ok"], result["errors"]


def test_validator_catches_a_wrong_schema_version(package):
    broken = dict(package)
    path = "FleetVision KSA.Report/definition/report.json"
    document = json.loads(broken[path])
    document["$schema"] = S.REPORT.replace("3.2.0", "1.0.0")
    broken[path] = json.dumps(document)

    result = validator.validate(broken, "FleetVision KSA")
    assert not result["ok"]
    assert any("$schema" in error for error in result["errors"])


def test_validator_catches_a_missing_version_file(package):
    broken = {k: v for k, v in package.items() if not k.endswith("definition/version.json")}
    result = validator.validate(broken, "FleetVision KSA")
    assert not result["ok"]
    assert any("version.json" in error for error in result["errors"])


def test_validator_catches_an_empty_logical_id(package):
    broken = dict(package)
    path = "FleetVision KSA.Report/.platform"
    document = json.loads(broken[path])
    document["config"]["logicalId"] = ""
    broken[path] = json.dumps(document)

    result = validator.validate(broken, "FleetVision KSA")
    assert not result["ok"]
    assert any("logicalId" in error for error in result["errors"])


# --- required files -------------------------------------------------------

def test_report_carries_a_version_marker(package):
    version = _json(package, "FleetVision KSA.Report/definition/version.json")
    assert version["$schema"] == S.VERSION_METADATA
    assert version["version"] == S.REPORT_VERSION


def test_theme_resource_is_shipped_not_just_referenced(package):
    report = _json(package, "FleetVision KSA.Report/definition/report.json")
    resource = report["resourcePackages"][0]
    item = resource["items"][0]
    expected = (
        f"FleetVision KSA.Report/StaticResources/{resource['name']}/{item['path']}"
    )
    assert expected in package
    assert json.loads(package[expected])["name"] == S.BASE_THEME_NAME


def test_semantic_model_ships_its_pbi_settings(package):
    root = "FleetVision KSA.SemanticModel"
    for name in ("localSettings.json", "editorSettings.json", "diagramLayout.json"):
        assert f"{root}/.pbi/{name}" in package


# --- schema versions ------------------------------------------------------

def test_report_uses_the_live_schema_versions(package):
    root = "FleetVision KSA.Report"
    assert _json(package, f"{root}/definition/report.json")["$schema"] == S.REPORT
    assert _json(package, f"{root}/definition/pages/pages.json")["$schema"] == S.PAGES_METADATA
    assert _json(package, f"{root}/definition.pbir")["$schema"] == S.REPORT_DEFINITION_PROPERTIES

    pages = [p for p in package if p.endswith("page.json")]
    assert pages
    for path in pages:
        assert json.loads(package[path])["$schema"] == S.PAGE

    for path, document in _visuals(package).items():
        assert document["$schema"] == S.VISUAL_CONTAINER, path


def test_semantic_model_definition_declares_its_schema(package):
    pbism = _json(package, "FleetVision KSA.SemanticModel/definition.pbism")
    assert pbism["$schema"] == S.MODEL_DEFINITION_PROPERTIES


def test_no_visual_carries_the_legacy_config_string(package):
    for path, document in _visuals(package).items():
        assert "config" not in document, path


# --- filters --------------------------------------------------------------

def test_visuals_declare_a_filter_config(package):
    for path, document in _visuals(package).items():
        if document["visual"]["visualType"] == "actionButton":
            continue
        assert "filterConfig" in document, path
        assert isinstance(document["filterConfig"]["filters"], list)


def test_visual_filters_are_emitted_not_just_counted(package):
    applied = [
        document for document in _visuals(package).values()
        if document.get("filterConfig", {}).get("filters")
    ]
    assert applied, "the mapping payload carries filters but none were emitted"
    for filter_entry in applied[0]["filterConfig"]["filters"]:
        assert filter_entry["type"] in ("Categorical", "TopN")
        assert filter_entry["field"]
        assert filter_entry["filter"]["Where"]


def test_top_n_filter_shape():
    from app.report.filters import build_filter_config

    visual = {
        "qlik_source": {
            "measures": [{"name": "Revenue"}],
            "filters": [{
                "field_defs": ["customer_name"],
                "limitation": {
                    "limitation_type": "Fixed number",
                    "direction": "Top", "count_limit": 10,
                },
            }],
        }
    }
    config = build_filter_config(visual, {"customer_name": "Customers"}, {"Revenue": "Loads"})
    assert len(config["filters"]) == 1
    top_n = config["filters"][0]
    assert top_n["type"] == "TopN"
    condition = top_n["filter"]["Where"][0]["Condition"]["Not"]["Expression"]["TopN"]
    assert condition["Top"] == 10
    assert condition["OrderBy"][0]["Direction"] == 2  # descending for Top


def test_calculated_dimension_is_not_used_as_a_filter_column():
    from app.report.filters import build_filter_config

    visual = {"qlik_source": {"filters": [{"field_defs": ["=a & b"], "selected_values": ["x"]}]}}
    assert build_filter_config(visual, {}, {})["filters"] == []


# --- navigation -----------------------------------------------------------

def test_navigation_buttons_target_real_pages():
    from app.report.bookmarks import build_navigation_buttons
    page_titles = ["Page 1", "Page 2"]
    page_ids = {"Page 1": "page1", "Page 2": "page2"}
    buttons = build_navigation_buttons(page_titles, page_ids, "Page 1")
    assert buttons, "navigation builder should generate buttons for multi-page"
    for button in buttons:
        link = button["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]
        target = link["navigationSection"]["expr"]["Literal"]["Value"].strip("'")
        assert target in page_ids.values()
