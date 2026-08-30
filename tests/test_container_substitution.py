"""Qlik containers/tabbed-containers used to map into the NATIVE catalog
(no note emitted) even though a plain 'group' visualType with no child
linkage silently drops the grouping/tab structure - the substitution note
is what tells a reviewer this needs manual rework in Desktop.
"""

from app.report.visual_catalog import resolve


def test_container_is_substituted_not_silently_native():
    visual_type, severity, reason, suggestion = resolve("container")
    assert visual_type == "group"
    assert severity == "substituted"
    assert reason and suggestion


def test_tabbed_container_is_substituted_not_silently_native():
    visual_type, severity, reason, suggestion = resolve("tabbed-container")
    assert visual_type == "group"
    assert severity == "substituted"
    assert reason and suggestion
