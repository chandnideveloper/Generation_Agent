"""Readers for the mapping agent's Contract 2.0 payload.

The mapping result nests everything a generator needs, but sections are
optional and shapes vary between contract versions, so every access goes
through here rather than indexing directly.
"""

from typing import Any, Dict, List


def as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() or default


def unwrap_mapping(document: Any) -> Dict[str, Any]:
    """Accept a raw mapping result or a stored {mapping_result: ...} / {parsing_result: ...} row."""
    doc = as_dict(document)
    res = doc
    if isinstance(doc.get("mapping_result"), dict) and doc["mapping_result"]:
        res = dict(doc["mapping_result"])
    elif isinstance(doc.get("parsing_result"), dict) and doc["parsing_result"]:
        res = dict(doc["parsing_result"])

    for k in ("app_id", "app_name", "run_id", "space_id", "workspace_id", "source_type"):
        if k in doc and k not in res:
            res[k] = doc[k]

    # Normalize tables & attach flat columns if present
    flat_cols = res.get("columns")
    cols_by_table: Dict[str, List[Dict[str, Any]]] = {}
    if isinstance(flat_cols, list):
        for c in flat_cols:
            if isinstance(c, dict):
                tbl = str(c.get("fabric_table") or c.get("qlik_table") or "").strip().lower()
                if tbl:
                    col_dict = dict(c)
                    if "fabric_column" in col_dict and "fabric_column_name" not in col_dict:
                        col_dict["fabric_column_name"] = col_dict["fabric_column"]
                    if "qlik_column" in col_dict and "qlik_column_name" not in col_dict:
                        col_dict["qlik_column_name"] = col_dict["qlik_column"]
                    if "target_data_type" in col_dict and "fabric_datatype" not in col_dict:
                        col_dict["fabric_datatype"] = col_dict["target_data_type"]
                    cols_by_table.setdefault(tbl, []).append(col_dict)

    tbls = res.get("tables")
    if isinstance(tbls, list):
        for t in tbls:
            if isinstance(t, dict):
                t_name = str(t.get("fabric_table_name") or t.get("qlik_table_name") or t.get("name") or t.get("table_name") or "").strip()
                if t_name and not t.get("name"):
                    t["name"] = t_name
                if t_name and not t.get("table_name"):
                    t["table_name"] = t_name
                if not t.get("columns") and t_name and t_name.lower() in cols_by_table:
                    t["columns"] = cols_by_table[t_name.lower()]
                if t.get("source_query") and not t.get("qlik_query"):
                    t["qlik_query"] = t["source_query"]

    # Normalize measures
    meas = res.get("measures") or res.get("dax_measures")
    if isinstance(meas, list):
        for m in meas:
            if isinstance(m, dict):
                m_name = str(m.get("fabric_measure_name") or m.get("qlik_measure_name") or m.get("name") or "").strip()
                if m_name and not m.get("name"):
                    m["name"] = m_name
                m_dax = str(m.get("dax") or m.get("dax_expression") or m.get("target_expression") or "").strip()
                if m_dax and not m.get("dax_expression"):
                    m["dax_expression"] = m_dax

    # Normalize visuals
    vis = res.get("visuals")
    if isinstance(vis, list):
        for v in vis:
            if isinstance(v, dict):
                sheet = v.get("sheet") or v.get("sheet_name")
                if sheet and not v.get("sheet_name"):
                    v["sheet_name"] = sheet
                v_type = v.get("power_bi_visual_type") or v.get("qlik_visual_type") or v.get("type")
                if v_type and not v.get("type"):
                    v["type"] = v_type

    return res


def app_identity(mapping: Dict[str, Any]) -> Dict[str, Any]:
    meta = as_dict(mapping.get("workbook_metadata"))
    app_meta = as_dict(mapping.get("app_metadata"))
    return {
        "app_id": meta.get("app_id") or mapping.get("app_id"),
        "app_name": text(
            meta.get("app_name") or app_meta.get("app_name") or mapping.get("app_name"),
            "Qlik App",
        ),
        "run_id": meta.get("run_id") or mapping.get("run_id"),
        "space_id": app_meta.get("space_id") or mapping.get("space_id"),
        "tenant": meta.get("tenant") or app_meta.get("tenant"),
    }


def tables(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    found = mapping.get("tables")
    if isinstance(found, list) and found:
        return [t for t in found if isinstance(t, dict)]
    # Pre-2.0 payloads nested them one level down.
    legacy = as_dict(mapping.get("table_details")).get("tables")
    return [t for t in as_list(legacy) if isinstance(t, dict)]


def measures(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    found = mapping.get("measures") or mapping.get("dax_measures") or []
    return [m for m in as_list(found) if isinstance(m, dict)]


def dimensions(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [d for d in as_list(mapping.get("dimensions")) if isinstance(d, dict)]


def relationships(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [r for r in as_list(mapping.get("relationships")) if isinstance(r, dict)]


def visuals(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flat list of visuals, whichever shape the mapping agent used."""
    block = mapping.get("visuals")
    if isinstance(block, dict):
        found = block.get("sheet_visuals") or block.get("visuals") or []
    else:
        found = block or mapping.get("visualizations") or []
    return [v for v in as_list(found) if isinstance(v, dict)]


def sheets(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    block = mapping.get("visuals")
    if isinstance(block, dict) and block.get("sheets"):
        return [s for s in as_list(block["sheets"]) if isinstance(s, dict)]
    return [s for s in as_list(mapping.get("sheets")) if isinstance(s, dict)]


def connections(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    found = mapping.get("connections") or mapping.get("connection_details") or []
    if isinstance(found, dict):
        found = [found]
    return [c for c in as_list(found) if isinstance(c, dict)]


def data_files(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
    found = mapping.get("data_files") or mapping.get("dataFiles") or []
    return [f for f in as_list(found) if isinstance(f, dict)]
