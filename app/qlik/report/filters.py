"""Visual-level filters, including Qlik's Top-N limitations.

The mapping payload carries a `filters` array on every visual that has one —
20 of 57 visuals on the test app. The generator previously read it only to
produce a count, so every Top-N and field filter was silently dropped and the
rendered visual showed unrestricted data.

Each filter becomes an entry in the visual's `filterConfig.filters`, which is
where PBIR stores them.
"""

from typing import Any, Dict, List, Optional, Tuple

from app.qlik.util.ids import lineage_tag
from app.qlik.util.payload import as_dict, as_list, text

# Qlik limitation types that map onto a Power BI TopN filter.
TOP_N_TYPES = {"fixed number", "fixednumber", "first", "largest", "smallest"}


def _field_ref(entity: str, prop: str, is_measure: bool = False) -> Dict[str, Any]:
    kind = "Measure" if is_measure else "Column"
    return {
        kind: {
            "Expression": {"SourceRef": {"Entity": entity}},
            "Property": prop,
        }
    }


def _categorical_filter(
    name: str, entity: str, prop: str, values: List[Any]
) -> Dict[str, Any]:
    """An `in` filter restricting a column to a value list."""
    literals = [
        {"Literal": {"Value": f"'{text(value)}'"}} for value in values if text(value)
    ]
    if not literals:
        return {}
    return {
        "name": name,
        "field": _field_ref(entity, prop),
        "type": "Categorical",
        "filter": {
            "Version": 2,
            "From": [{"Name": entity[:1].lower(), "Entity": entity, "Type": 0}],
            "Where": [{
                "Condition": {
                    "In": {
                        "Expressions": [{
                            "Column": {
                                "Expression": {"SourceRef": {"Source": entity[:1].lower()}},
                                "Property": prop,
                            }
                        }],
                        "Values": [[literal] for literal in literals],
                    }
                }
            }],
        },
        "howCreated": "Auto",
    }


def _field_filter(name: str, entity: str, prop: str) -> Dict[str, Any]:
    """A categorical filter placing a column on the filter pane."""
    return {
        "name": name,
        "field": _field_ref(entity, prop),
        "type": "Categorical",
        "howCreated": "Auto",
    }


def _top_n_filter(
    name: str, entity: str, prop: str, count: int, direction: str,
    order_entity: Optional[str], order_prop: Optional[str],
) -> Dict[str, Any]:
    """A TopN filter — the Power BI equivalent of Qlik's fixed-number limit."""
    order = "Descending" if str(direction or "top").lower().startswith("top") else "Ascending"
    order_by = (
        _field_ref(order_entity, order_prop, is_measure=True)
        if order_entity and order_prop
        else _field_ref(entity, prop)
    )
    return {
        "name": name,
        "field": _field_ref(entity, prop),
        "type": "TopN",
        "filter": {
            "Version": 2,
            "From": [{"Name": entity[:1].lower(), "Entity": entity, "Type": 0}],
            "Where": [{
                "Condition": {
                    "Not": {
                        "Expression": {
                            "TopN": {
                                "Expression": {
                                    "Column": {
                                        "Expression": {
                                            "SourceRef": {"Source": entity[:1].lower()}
                                        },
                                        "Property": prop,
                                    }
                                },
                                "OrderBy": [{"Direction": 2 if order == "Descending" else 1,
                                             "Expression": order_by}],
                                "Top": int(count),
                            }
                        }
                    }
                }
            }],
        },
        "howCreated": "Auto",
    }


def _not_blank_filter(name: str, entity: str, prop: str) -> Dict[str, Any]:
    """A non-blank filter — the Power BI equivalent of Qlik's null suppression."""
    return {
        "name": name,
        "field": _field_ref(entity, prop),
        "type": "Categorical",
        "filter": {
            "Version": 2,
            "From": [{"Name": entity[:1].lower(), "Entity": entity, "Type": 0}],
            "Where": [{
                "Condition": {
                    "Not": {
                        "Expression": {
                            "Comparison": {
                                "ComparisonKind": 0,
                                "Left": {
                                    "Column": {
                                        "Expression": {"SourceRef": {"Source": entity[:1].lower()}},
                                        "Property": prop,
                                    }
                                },
                                "Right": {"Literal": {"Value": "null"}},
                            }
                        }
                    }
                }
            }],
        },
        "howCreated": "Auto",
    }


def build_filter_config(
    visual: Dict[str, Any],
    column_home: Dict[str, str],
    measure_home: Dict[str, str],
    field_resolver: Optional[Dict[str, Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    """Return the `filterConfig` block for one visual."""
    source = as_dict(visual.get("qlik_source")) or visual
    fabric = as_dict(visual.get("fabric"))
    built: List[Dict[str, Any]] = []
    seen_filter_keys = set()

    # 1. Process explicit filters from source or fabric
    filter_list = as_list(source.get("filters")) or as_list(fabric.get("filters")) or as_list(visual.get("filters"))
    for index, raw in enumerate(filter_list):
        raw = as_dict(raw)
        field_defs = as_list(raw.get("field_defs"))
        field = text(field_defs[0] if field_defs else raw.get("field"))

        # A calculated dimension ("=expr") has no column to filter on.
        if field and field.startswith("="):
            continue

        entity = None
        prop = field
        if field:
            field_lower = field.lower()
            if field_resolver and field_lower in field_resolver:
                entity, prop = field_resolver[field_lower]
            elif field in column_home:
                entity = column_home[field]

        limitation = as_dict(raw.get("limitation"))
        kind = text(limitation.get("limitation_type") or limitation.get("mode") or raw.get("type")).lower()
        limit_val = (
            limitation.get("limit_value")
            or limitation.get("value")
            or limitation.get("count_limit")
            or limitation.get("threshold_value")
        )

        if entity and (kind in TOP_N_TYPES or "number" in kind or "fixed" in kind) and limit_val:
            measure_names = [
                text(as_dict(m).get("name")) for m in as_list(source.get("measures") or fabric.get("measures"))
            ]
            order_prop = measure_names[0] if measure_names else None
            name = lineage_tag(f"filter:topn:{prop}:{index}")[:20]
            built.append(_top_n_filter(
                name, entity, prop,
                int(limit_val),
                text(limitation.get("direction"), "Top"),
                measure_home.get(order_prop) if order_prop else None,
                order_prop,
            ))
            seen_filter_keys.add((entity, prop, "topn"))
            continue

        values = as_list(raw.get("selected_values") or raw.get("values"))
        if entity and values:
            name = lineage_tag(f"filter:cat:{prop}:{index}")[:20]
            categorical = _categorical_filter(name, entity, prop, values)
            if categorical:
                built.append(categorical)
                seen_filter_keys.add((entity, prop, "cat"))

    # 2. Process dimension-level limitations and null suppression
    dims = as_list(source.get("dimensions")) or as_list(fabric.get("dimensions"))
    for d_idx, dim_item in enumerate(dims):
        dim_item = as_dict(dim_item)
        field_defs = as_list(dim_item.get("field_defs"))
        field = text(field_defs[0] if field_defs else dim_item.get("name") or dim_item.get("field"))
        if not field or field.startswith("="):
            continue

        entity = None
        prop = field
        field_lower = field.lower()
        if field_resolver and field_lower in field_resolver:
            entity, prop = field_resolver[field_lower]
        elif field in column_home:
            entity = column_home[field]

        if not entity:
            continue

        limitations = as_dict(dim_item.get("limitations"))
        limit_kind = text(limitations.get("limitation_type") or limitations.get("mode")).lower()
        limit_val = (
            limitations.get("limit_value")
            or limitations.get("value")
            or limitations.get("count_limit")
        )

        # Top / Bottom N limitation on dimension
        if (limit_kind in TOP_N_TYPES or "number" in limit_kind or "fixed" in limit_kind) and limit_val:
            if (entity, prop, "topn") not in seen_filter_keys:
                measure_names = [
                    text(as_dict(m).get("name")) for m in as_list(source.get("measures") or fabric.get("measures"))
                ]
                order_prop = measure_names[0] if measure_names else None
                name = lineage_tag(f"filter:dim_topn:{prop}:{d_idx}")[:20]
                built.append(_top_n_filter(
                    name, entity, prop,
                    int(limit_val),
                    text(limitations.get("direction"), "Top"),
                    measure_home.get(order_prop) if order_prop else None,
                    order_prop,
                ))
                seen_filter_keys.add((entity, prop, "topn"))

        # Null value suppression
        suppress_null = (
            limitations.get("suppress_null") is True
            or dim_item.get("suppress_null") is True
            or dim_item.get("null_suppression") is True
            or (limitations.get("include_null_values") is False)
        )
        if suppress_null and (entity, prop, "not_blank") not in seen_filter_keys:
            name = lineage_tag(f"filter:notblank:{prop}:{d_idx}")[:20]
            built.append(_not_blank_filter(name, entity, prop))
            seen_filter_keys.add((entity, prop, "not_blank"))

    return {"filters": built}


def count_applied(filter_config: Dict[str, Any]) -> Dict[str, int]:
    filters = as_list(as_dict(filter_config).get("filters"))
    return {
        "total": len(filters),
        "top_n": sum(1 for f in filters if as_dict(f).get("type") == "TopN"),
        "categorical": sum(1 for f in filters if as_dict(f).get("type") == "Categorical"),
    }
