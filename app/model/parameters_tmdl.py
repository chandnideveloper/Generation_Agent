"""Dynamic single consolidated Parameters table generator for TMDL semantic models.

Extracts all interactive variables and alternatives from the JSON payload with
zero hardcoding.
"""

from typing import Any, Dict, List, Optional
from app.util.ids import lineage_tag
from app.util.payload import as_dict, as_list, text


def build_parameters_tmdl(mapping: Dict[str, Any]) -> Optional[str]:
    """Generate a single unified Parameters.tmdl containing all variables dynamically."""
    variables_raw = mapping.get("variables", {})
    var_list = []
    if isinstance(variables_raw, dict):
        var_list = variables_raw.get("converted_items", []) or variables_raw.get("items", []) or variables_raw.get("variables", [])
    elif isinstance(variables_raw, list):
        var_list = variables_raw

    # Collect dynamic variable alternatives from visuals or custom_properties
    var_options = {}
    visuals = mapping.get("visuals", {})
    sheet_vis = visuals.get("sheet_visuals", []) if isinstance(visuals, dict) else as_list(visuals)
    for sv in sheet_vis:
        if not isinstance(sv, dict):
            continue
        qs = as_dict(sv.get("qlik_source")) or sv
        chart_type = text(qs.get("chart_type") or qs.get("type") or qs.get("qlik_type")).lower()
        if chart_type in ("qlik-variable-input", "variable-input", "variableinput", "variable"):
            vname = text(qs.get("variable_name") or qs.get("title") or qs.get("name"))
            props = as_dict(qs.get("custom_properties")) or as_dict(qs.get("properties"))
            alts = props.get("alternatives") or props.get("options") or props.get("values")
            if alts and vname:
                var_options[vname] = alts

    rows = []
    seen = set()
    order = 1

    # 1. Add interactive variables with explicit options first
    for vname, opts in var_options.items():
        for opt in as_list(opts):
            if isinstance(opt, dict):
                val = text(opt.get("value") or opt.get("label"))
                lbl = text(opt.get("label") or opt.get("value"))
            else:
                val = text(opt)
                lbl = text(opt)
            key = (vname, val)
            if key not in seen and val:
                seen.add(key)
                rows.append((vname, val, lbl, order))
                order += 1

    # 2. Add remaining variables from variable inventory
    for v in var_list:
        if not isinstance(v, dict):
            continue
        vname = text(v.get("name") or v.get("variable_name"))
        if not vname:
            continue
        val = text(v.get("value") or v.get("definition"))
        lbl = text(v.get("description") or v.get("comment") or vname)
        key = (vname, val)
        if key not in seen and val:
            seen.add(key)
            rows.append((vname, val, lbl, order))
            order += 1

    if not rows:
        return None

    m_rows = []
    for p, v, l, o in rows:
        p_esc = p.replace('"', '""')
        v_esc = v.replace('"', '""')
        l_esc = l.replace('"', '""')
        m_rows.append(f'{{"{p_esc}", "{v_esc}", "{l_esc}", {o}}}')

    rows_str = ",\n\t\t\t            ".join(m_rows)

    tmdl = f"""table Parameters
\tlineageTag: {lineage_tag("table:Parameters")}

\tcolumn Parameter
\t\tdataType: string
\t\tlineageTag: {lineage_tag("col:Parameters.Parameter")}
\t\tsummarizeBy: none
\t\tsourceColumn: Parameter

\t\tannotation SummarizationSetBy = Automatic

\tcolumn Value
\t\tdataType: string
\t\tlineageTag: {lineage_tag("col:Parameters.Value")}
\t\tsummarizeBy: none
\t\tsourceColumn: Value

\t\tannotation SummarizationSetBy = Automatic

\tcolumn Label
\t\tdataType: string
\t\tlineageTag: {lineage_tag("col:Parameters.Label")}
\t\tsummarizeBy: none
\t\tsourceColumn: Label

\t\tannotation SummarizationSetBy = Automatic

\tcolumn Order
\t\tdataType: int64
\t\tlineageTag: {lineage_tag("col:Parameters.Order")}
\t\tsummarizeBy: none
\t\tsourceColumn: Order

\t\tannotation SummarizationSetBy = Automatic

\tpartition Parameters = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t    Source = #table(
\t\t\t\t        type table [Parameter = text, Value = text, Label = text, #"Order" = Int64.Type],
\t\t\t\t        {{
\t\t\t            {rows_str}
\t\t\t\t        }}
\t\t\t\t    )
\t\t\t\tin
\t\t\t\t    Source

\tannotation PBI_ResultType = Table
"""
    return tmdl
