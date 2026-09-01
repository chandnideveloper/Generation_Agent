"""Field-binding projections.

A visual binds to the semantic model through `projections` that name an
entity and a property. Columns and measures use different envelopes, so
each has its own builder.
"""

from typing import Any, Dict, List

from app.util.payload import as_list, text


def _names(items: Any) -> List[str]:
    resolved = []
    for item in as_list(items):
        if isinstance(item, dict):
            expr = text(item.get("expression") or item.get("qDef"))
            name = text(item.get("name") or item.get("field_name") or item.get("title") or item.get("label"))
            cand = expr if (expr and ("{" in expr or "(" in expr)) else (name or expr)
            if cand:
                resolved.append(cand)
        else:
            name = text(item)
            if name:
                resolved.append(name)
    return resolved


def _projection(entity: str, prop: str, index: int) -> Dict[str, Any]:
    return {
        "field": {
            "Column": {
                "Expression": {"SourceRef": {"Entity": entity}},
                "Property": prop,
            }
        },
        "queryRef": f"{entity}.{prop}",
        "nativeQueryRef": prop,
        "active": index == 0,
    }


def _measure_projection(entity: str, prop: str) -> Dict[str, Any]:
    return {
        "field": {
            "Measure": {
                "Expression": {"SourceRef": {"Entity": entity}},
                "Property": prop,
            }
        },
        "queryRef": f"{entity}.{prop}",
        "nativeQueryRef": prop,
    }


def _aggregation_projection(entity: str, prop: str, index: int, function_id: int = 0) -> Dict[str, Any]:
    func_name = "Sum" if function_id == 0 else ("Avg" if function_id == 1 else "Count")
    return {
        "field": {
            "Aggregation": {
                "Expression": {
                    "Column": {
                        "Expression": {"SourceRef": {"Entity": entity}},
                        "Property": prop,
                    }
                },
                "Function": function_id,
            }
        },
        "queryRef": f"{func_name}({entity}.{prop})",
        "nativeQueryRef": prop,
        "active": index == 0,
    }

