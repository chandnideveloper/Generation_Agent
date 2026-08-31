"""Qlik object type -> Power BI visual, with an honest substitution note.

Three outcomes per visual:

  native       a direct equivalent exists; nothing to report
  substituted  no exact match, but a close native visual carries the intent —
               emitted, with a note saying what changed
  manual       nothing native reproduces it; a placeholder is emitted and the
               note explains what to install or rebuild

Nothing here calls a model. The mapping is a lookup table plus a small amount
of shape logic, so the same input always produces the same output.
"""

from typing import Dict, Optional, Tuple

# Direct equivalents.
NATIVE = {
    "barchart": "barChart",
    "bar-chart": "barChart",
    "sn-bar-chart": "barChart",
    "combochart": "lineClusteredColumnComboChart",
    "combo-chart": "lineClusteredColumnComboChart",
    "sn-combo-chart": "lineClusteredColumnComboChart",
    "linechart": "lineChart",
    "line-chart": "lineChart",
    "sn-line-chart": "lineChart",
    "piechart": "pieChart",
    "pie-chart": "pieChart",
    "sn-pie-chart": "pieChart",
    "donutchart": "donutChart",
    "donut-chart": "donutChart",
    "sn-donut-chart": "donutChart",
    "table": "tableEx",
    "sn-table": "tableEx",
    "pivot-table": "pivotTable",
    "sn-pivot-table": "pivotTable",
    "kpi": "card",
    "sn-kpi": "card",
    "gauge": "gauge",
    "sn-gauge": "gauge",
    "scatterplot": "scatterChart",
    "scatter-plot": "scatterChart",
    "sn-scatterplot": "scatterChart",
    "sn-scatter-plot": "scatterChart",
    "treemap": "treemap",
    "sn-treemap": "treemap",
    "map": "map",
    "sn-map": "map",
    "filterpane": "slicer",
    "sn-filterpane": "slicer",
    "listbox": "slicer",
    "sn-listbox": "slicer",
    "text-image": "textbox",
    "waterfallchart": "waterfallChart",
    "waterfall-chart": "waterfallChart",
    "sn-waterfall": "waterfallChart",
    "sn-waterfall-chart": "waterfallChart",
    "histogram": "columnChart",
    "boxplot": "columnChart",
    "distributionplot": "scatterChart",
    "bulletchart": "gauge",
    "sn-funnel-chart": "funnel",
    "funnel": "funnel",
    "sankey": "sankeyDiagram",
    "auto-chart": "clusteredColumnChart",
    "barplus-chart": "clusteredColumnChart",
    "action-button": "actionButton",
    "actionbutton": "actionButton",
    "button": "actionButton",
    "image": "image",
    "variable-input": "slicer",
    "variableinput": "slicer",
    "variable": "slicer",
}

# No native equivalent: (fallback visual, why, what to do).
SUBSTITUTIONS: Dict[str, Tuple[str, str, str]] = {
    "boxplot": (
        "columnChart",
        "Power BI has no native box plot.",
        "Rendered as a column chart of the median. For true quartiles install "
        "the 'Box and Whisker chart' visual from AppSource, or add DAX measures "
        "for P25/P50/P75 and use an error-bar enabled visual.",
    ),
    "distributionplot": (
        "scatterChart",
        "Power BI has no native distribution plot.",
        "Rendered as a scatter chart. For density, use the AppSource "
        "'Violin Plot' visual.",
    ),
    "sankey": (
        "tableEx",
        "Sankey is not a built-in Power BI visual.",
        "Emitted as a table of source/target/value so no data is lost. Install "
        "'Sankey Chart by Microsoft' from AppSource and re-bind the same fields.",
    ),
    "mekkochart": (
        "clusteredColumnChart",
        "Marimekko/Mekko charts are not built in.",
        "Rendered as a clustered column chart. Install 'Mekko Chart' from "
        "AppSource for proportional widths.",
    ),
    "bulletchart": (
        "gauge",
        "Power BI's gauge does not support Qlik's bullet segments.",
        "Rendered as a gauge using the same measure and target. For qualitative "
        "bands install the AppSource 'Bullet Chart'.",
    ),
    "sn-org-chart": (
        "tableEx",
        "Org charts are not built in.",
        "Emitted as a parent/child table. Install 'Organization Chart' or "
        "'Hierarchy Chart' from AppSource.",
    ),
    "sn-nlg-insights": (
        "textbox",
        "Narrative insights have no deterministic Power BI equivalent.",
        "A textbox placeholder is emitted. Use Power BI's 'Smart Narrative' "
        "visual and re-select the fields manually.",
    ),
    "qlik-funnel-chart-ext": (
        "funnel",
        "Third-party Qlik funnel extension.",
        "Mapped to the native Power BI funnel, which covers the same intent. "
        "Verify the stage ordering after import.",
    ),
    "container": (
        "group",
        "PBIR groups a set of already-placed visuals by reference "
        "(parentGroupName); this pipeline does not yet resolve which visuals "
        "were children of this Qlik container, so an empty group placeholder "
        "is emitted instead of a populated one.",
        "In Desktop, select the visuals that were inside this container and "
        "use Format > Group to recreate the grouping, then delete the empty "
        "placeholder.",
    ),
    "tabbed-container": (
        "group",
        "PBIR has no tabbed-container equivalent; Power BI's closest concept "
        "is separate pages or a visual group with a bookmark-driven tab strip, "
        "neither of which this pipeline builds automatically.",
        "Recreate each tab as its own page (or bookmark) in Desktop, moving "
        "the tab's visuals onto it.",
    ),
}

# Types that carry no data and cannot be reproduced automatically.
MANUAL = {
    "extension": (
        "Third-party Qlik extension with no Power BI counterpart.",
        "A textbox placeholder is emitted naming the extension. Find an "
        "equivalent AppSource visual, or rebuild the object natively.",
    ),
    "sheet-navigation": (
        "Qlik sheet navigation objects have no direct equivalent.",
        "Use Power BI bookmarks with page-navigation buttons.",
    ),
}

PLACEHOLDER = "textbox"

# Fabric visual type names that only render if the target report explicitly
# registers the matching AppSource custom visual package. This pipeline has
# no such registration step (see app/report/report_files.py - it only ever
# emits a BaseTheme resourcePackage), so trusting one of these at face value
# produces Fabric/Power BI's "To see this custom visual, add it to this
# report first" placeholder instead of a chart. Anything upstream (mapping)
# that names one of these - whether via an explicit visual_type or its own
# requires_custom_visual flag - must be routed through resolve() below for
# its safe substitution rather than emitted as-is.
CUSTOM_VISUAL_ONLY_TYPES = {"boxPlot", "sankeyDiagram"}


def resolve(qlik_type: str, is_extension: bool = False) -> Tuple[str, str, Optional[str], Optional[str]]:
    """Return (visual_type, severity, reason, suggestion)."""
    key = (qlik_type or "").strip().lower()

    if is_extension and key not in SUBSTITUTIONS and key not in NATIVE:
        reason, suggestion = MANUAL["extension"]
        return PLACEHOLDER, "manual", reason, suggestion

    if key in SUBSTITUTIONS:
        visual, reason, suggestion = SUBSTITUTIONS[key]
        return visual, "substituted", reason, suggestion

    if key in NATIVE:
        return NATIVE[key], "native", None, None

    if key in MANUAL:
        reason, suggestion = MANUAL[key]
        return PLACEHOLDER, "manual", reason, suggestion

    return (
        "tableEx",
        "substituted",
        f"Unrecognised Qlik object type '{qlik_type}'.",
        "Emitted as a table so the underlying fields remain available. "
        "Replace it with the closest Power BI visual by hand.",
    )
