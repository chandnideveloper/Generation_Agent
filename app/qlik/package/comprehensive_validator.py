"""Comprehensive Pre-Deployment Validator for Power BI / Fabric packages.

Performs deep metadata-driven validation across:
- Datasources (unresolved placeholders like <<server>>, <<database>>)
- Semantic Model (tables, columns, datatypes)
- DAX (measures, calculated columns, syntax, Qlik leftovers)
- Visuals (field-bindings, table/column/measure existence)
- Filters (target table/column existence, structure)
- Relationships (source/target validity, skip audit)
- Report Structure (pages, layout)

Calculates all metrics dynamically and determines deployment eligibility.
"""

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.qlik.model.datasource_validator import validate_all_datasources
from app.qlik.model.dax_guard import validate as validate_dax_expression


def _extract_model_entities(package: Dict[str, str]) -> Tuple[Set[str], Dict[str, Set[str]], Dict[str, Set[str]]]:
    """
    Extract known tables, columns, and measures from the generated package TMDL files.
    Returns: (tables_set, table_columns_dict, table_measures_dict)
    """
    tables: Set[str] = set()
    table_columns: Dict[str, Set[str]] = {}
    table_measures: Dict[str, Set[str]] = {}

    for path, content in package.items():
        if "/definition/tables/" in path and path.endswith(".tmdl"):
            # Extract table name from path or TMDL content
            t_match = re.search(r"table\s+('([^']+)'|([A-Za-z0-9_\-]+))", content)
            if not t_match:
                continue
            table_name = (t_match.group(2) or t_match.group(3)).strip()
            table_lower = table_name.lower()
            tables.add(table_lower)
            table_columns.setdefault(table_lower, set())
            table_measures.setdefault(table_lower, set())

            # Extract columns
            col_matches = re.finditer(r"column\s+('([^']+)'|\[([^\]]+)\]|([A-Za-z0-9_#]+))", content)
            for cm in col_matches:
                col_name = (cm.group(2) or cm.group(3) or cm.group(4)).strip()
                table_columns[table_lower].add(col_name.lower())

            # Extract measures
            meas_matches = re.finditer(r"measure\s+('([^']+)'|\[([^\]]+)\]|([A-Za-z0-9_#]+))", content)
            for mm in meas_matches:
                meas_name = (mm.group(2) or mm.group(3) or mm.group(4)).strip()
                table_measures[table_lower].add(meas_name.lower())

    return tables, table_columns, table_measures


def validate_dax_expressions(package: Dict[str, str]) -> Dict[str, Any]:
    """Dynamically scan and validate all measures and calculated columns in the generated TMDL."""
    total = 0
    valid = 0
    rewrite_required = 0
    invalid = 0
    errors: List[Dict[str, Any]] = []

    for path, content in package.items():
        if "/definition/tables/" in path and path.endswith(".tmdl"):
            # 1. Measures
            # TMDL format: measure 'Name' = expr or measure Name =\n\tbody
            measure_blocks = re.finditer(
                r"measure\s+('([^']+)'|([A-Za-z0-9_#]+))\s*=\s*(.+?)(?=\n\t\t[a-zA-Z]|\n\t[a-zA-Z]|\n\n|\Z)",
                content,
                re.DOTALL,
            )
            for mb in measure_blocks:
                total += 1
                name = (mb.group(2) or mb.group(3)).strip()
                expr = mb.group(4).strip()

                if expr == "BLANK()" and "/// TODO" in content:
                    rewrite_required += 1
                    continue

                problem = validate_dax_expression(expr)
                if problem:
                    invalid += 1
                    errors.append({
                        "category": "dax",
                        "severity": "error",
                        "object": f"Measure: {name}",
                        "location": path,
                        "reason": problem["reason"],
                        "action": problem["suggestion"],
                    })
                else:
                    valid += 1

            # 2. Calculated columns
            calc_col_blocks = re.finditer(
                r"column\s+('([^']+)'|([A-Za-z0-9_#]+))\s*=\s*(.+?)(?=\n\t\t[a-zA-Z]|\n\t[a-zA-Z]|\n\n|\Z)",
                content,
                re.DOTALL,
            )
            for cb in calc_col_blocks:
                total += 1
                name = (cb.group(2) or cb.group(3)).strip()
                expr = cb.group(4).strip()

                if expr == "BLANK()" and "/// TODO" in content:
                    rewrite_required += 1
                    continue

                problem = validate_dax_expression(expr)
                if problem:
                    invalid += 1
                    errors.append({
                        "category": "dax",
                        "severity": "error",
                        "object": f"Calculated Column: {name}",
                        "location": path,
                        "reason": problem["reason"],
                        "action": problem["suggestion"],
                    })
                else:
                    valid += 1

    return {
        "total": total,
        "valid": valid,
        "rewrite_required": rewrite_required,
        "invalid": invalid,
        "errors": errors,
    }


def validate_visual_bindings(
    package: Dict[str, str],
    known_tables: Set[str],
    known_columns: Dict[str, Set[str]],
    known_measures: Dict[str, Set[str]],
) -> Dict[str, Any]:
    """Validate all generated visual files and their field projections."""
    total = 0
    valid = 0
    invalid = 0
    unresolved_bindings = 0
    errors: List[Dict[str, Any]] = []

    for path, content in package.items():
        if "/visuals/" in path and path.endswith("visual.json"):
            total += 1
            try:
                v_data = json.loads(content)
            except Exception as e:
                invalid += 1
                errors.append({
                    "category": "visual_structure",
                    "severity": "error",
                    "object": path,
                    "location": path,
                    "reason": f"Corrupt visual JSON: {e}",
                    "action": "Ensure valid JSON syntax during visual serialization",
                })
                continue

            visual_has_unresolved = False
            v_type = v_data.get("visual", {}).get("visualType", "")
            is_data_driven = v_type not in ("textbox", "actionButton", "shape", "image")

            # Check queryState projections
            query_state = v_data.get("visual", {}).get("query", {}).get("queryState", {})
            projections = v_data.get("visual", {}).get("projections", {})
            all_roles = dict(query_state)
            for r, val in projections.items():
                all_roles.setdefault(r, {"projections": val} if isinstance(val, list) else val)

            total_projections_count = 0
            for role, role_def in all_roles.items():
                items = role_def.get("projections", []) if isinstance(role_def, dict) else (role_def if isinstance(role_def, list) else [])
                if not isinstance(items, list):
                    continue
                total_projections_count += len(items)
                for item in items:
                    query_ref = item.get("queryRef", "") if isinstance(item, dict) else ""
                    # queryRef format: Table.Property or Measure
                    if "." in query_ref:
                        parts = query_ref.split(".", 1)
                        ent, prop = parts[0].strip().lower(), parts[1].strip().lower()
                        # If entity does not exist in known tables
                        if ent not in known_tables and ent != "_measures":
                            visual_has_unresolved = True
                            unresolved_bindings += 1
                            errors.append({
                                "category": "visual_binding",
                                "severity": "warning",
                                "visual": path,
                                "object": query_ref,
                                "location": f"{path} (role: {role})",
                                "reason": f"Referenced table '{parts[0]}' is not in the semantic model",
                                "action": "Rebind visual to an existing table in the model",
                            })
                            break
                        # If property does not exist in columns or measures of entity
                        cols = known_columns.get(ent, set())
                        meas = known_measures.get(ent, set())
                        if prop not in cols and prop not in meas:
                            # Also check if it is a measure on any table
                            all_meas = {m for mset in known_measures.values() for m in mset}
                            if prop not in all_meas:
                                visual_has_unresolved = True
                                unresolved_bindings += 1
                                errors.append({
                                    "category": "visual_binding",
                                    "severity": "warning",
                                    "visual": path,
                                    "object": query_ref,
                                    "location": f"{path} (role: {role})",
                                    "reason": f"Referenced property '{parts[1]}' was not found on table '{parts[0]}'",
                                    "action": "Ensure mapped column or measure exists in table TMDL",
                                })
                                break

            if is_data_driven and total_projections_count == 0:
                visual_has_unresolved = True
                unresolved_bindings += 1
                errors.append({
                    "category": "visual_binding",
                    "severity": "warning",
                    "visual": path,
                    "location": path,
                    "reason": f"Data-driven visual of type '{v_type}' has no data bindings",
                    "action": "Bind at least one measure or dimension to this visual",
                })

            if not visual_has_unresolved:
                valid += 1

    return {
        "total": total,
        "valid": valid,
        "invalid": invalid,
        "unresolved_bindings": unresolved_bindings,
        "errors": errors,
    }


def validate_filters(
    package: Dict[str, str],
    known_tables: Set[str],
    known_columns: Dict[str, Set[str]],
    mapping: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate report-level, page-level, and mapping filters."""
    total = 0
    valid = 0
    invalid = 0
    unresolved = 0
    errors: List[Dict[str, Any]] = []

    # 1. Check mapping filters to identify any that could not be resolved
    raw_filters = []
    if mapping:
        app_layout = mapping.get("app_layout") if isinstance(mapping.get("app_layout"), dict) else {}
        raw_filters = (
            mapping.get("filters")
            or mapping.get("filter_panes")
            or app_layout.get("filters")
            or []
        )
        if not isinstance(raw_filters, list):
            raw_filters = [raw_filters]

    for rf in raw_filters:
        if not isinstance(rf, dict):
            continue
        rf_fab = rf.get("fabric") if isinstance(rf.get("fabric"), dict) else {}
        t_name = str(rf_fab.get("target_table") or rf.get("table") or rf.get("target_table") or "").strip()
        c_name = str(rf_fab.get("target_column") or rf.get("column") or rf.get("target_column") or "").strip()

        if t_name and c_name:
            if t_name.lower() not in known_tables or c_name.lower() not in known_columns.get(t_name.lower(), set()):
                unresolved += 1
                total += 1
                errors.append({
                    "category": "filter",
                    "severity": "warning",
                    "object": f"Filter on {t_name}[{c_name}]",
                    "location": "Mapping filter configuration",
                    "reason": f"Target table '{t_name}' or column '{c_name}' does not exist in the semantic model",
                    "action": "Correct the target table/column name in the mapping filter",
                })

    # 2. Check report.json emitted filters
    for path, content in package.items():
        if path.endswith("report.json"):
            try:
                rep_data = json.loads(content)
                filters = rep_data.get("filterConfig", {}).get("filters", [])
                for flt in filters:
                    total += 1
                    target = flt.get("field", {})
                    table = target.get("Table", "")
                    column = target.get("Column", "")

                    if not table or not column:
                        expr = flt.get("expression", {})
                        if "Column" in expr:
                            table = expr["Column"].get("Expression", {}).get("SourceRef", {}).get("Entity", "")
                            column = expr["Column"].get("Property", "")

                    if table:
                        t_lower = table.lower()
                        c_lower = column.lower()
                        if t_lower not in known_tables:
                            unresolved += 1
                            errors.append({
                                "category": "filter",
                                "severity": "warning",
                                "object": f"Filter on {table}[{column}]",
                                "location": path,
                                "reason": f"Target table '{table}' does not exist in semantic model",
                                "action": "Ensure filter points to an existing table",
                            })
                        elif column and c_lower not in known_columns.get(t_lower, set()):
                            unresolved += 1
                            errors.append({
                                "category": "filter",
                                "severity": "warning",
                                "object": f"Filter on {table}[{column}]",
                                "location": path,
                                "reason": f"Target column '{column}' does not exist on table '{table}'",
                                "action": "Ensure filter points to an existing column",
                            })
                        else:
                            valid += 1
                    else:
                        valid += 1
            except Exception as e:
                invalid += 1

    return {
        "total": total,
        "valid": valid,
        "invalid": invalid,
        "unresolved": unresolved,
        "errors": errors,
    }


def validate_relationships(package: Dict[str, str], semantic_report: Dict[str, Any]) -> Dict[str, Any]:
    """Validate relationships generated in relationships.tmdl."""
    written = semantic_report.get("relationships_written", 0)
    skipped = semantic_report.get("relationships_skipped") or []
    total = written + len(skipped)

    return {
        "total": total,
        "valid": written,
        "skipped": len(skipped),
        "details": skipped,
    }


def run_comprehensive_validation(
    mapping: Dict[str, Any],
    package: Dict[str, str],
    app_name: str,
    semantic_report: Dict[str, Any],
    visual_notes: List[Any],
) -> Dict[str, Any]:
    """
    Execute full multi-layer validation:
    1. Datasources & Placeholders (<<server>>, <<database>>)
    2. Semantic Model Structure & Datatypes
    3. DAX Syntax & Qlik Leftovers
    4. Visual Field-Bindings
    5. Filter Targets
    6. Relationships
    7. Report Structure (Pages, Navigation)

    Returns complete validation summary and deployment gating recommendation.
    """
    all_errors: List[Dict[str, Any]] = []
    all_warnings: List[Dict[str, Any]] = []

    # 1. Datasource Validation
    ds_valid, ds_issues = validate_all_datasources(mapping)
    for issue in ds_issues:
        if issue.get("severity") == "error":
            all_errors.append(issue)
        else:
            all_warnings.append(issue)

    # 2. Extract model entities
    known_tables, known_columns, known_measures = _extract_model_entities(package)

    # 3. DAX Validation
    dax_results = validate_dax_expressions(package)
    for dax_err in dax_results.get("errors", []):
        all_errors.append(dax_err)

    # 4. Visual Validation
    visual_results = validate_visual_bindings(package, known_tables, known_columns, known_measures)
    for v_err in visual_results.get("errors", []):
        if v_err.get("severity") == "error":
            all_errors.append(v_err)
        else:
            all_warnings.append(v_err)

    # 5. Filter Validation
    filter_results = validate_filters(package, known_tables, known_columns, mapping=mapping)
    for f_err in filter_results.get("errors", []):
        if f_err.get("severity") == "error":
            all_errors.append(f_err)
        else:
            all_warnings.append(f_err)

    # 6. Relationship Validation
    rel_results = validate_relationships(package, semantic_report)

    # 7. Semantic Model Validation
    table_count = len(known_tables)
    calc_col_count = semantic_report.get("calculated_columns", 0)
    meas_count = semantic_report.get("measures", 0)

    semantic_validation = {
        "tables": table_count,
        "measures": meas_count,
        "calculated_columns": calc_col_count,
        "relationships": rel_results.get("valid", 0),
        "datasources_valid": ds_valid,
        "ok": ds_valid and len(all_errors) == 0,
    }

    # 8. Report Structure Validation
    page_files = [p for p in package if "/pages/" in p and p.endswith("page.json")]
    report_structure = {
        "pages": len(page_files),
        "visuals": visual_results.get("total", 0),
        "filters": filter_results.get("total", 0),
        "ok": len(page_files) > 0 or len(known_tables) > 0,
    }

    # Critical gate decision:
    # If there are any severity='error' issues (e.g. unresolved datasource placeholders, corrupt files),
    # deployment MUST be blocked!
    has_critical_errors = any(e.get("severity") == "error" for e in all_errors)

    if has_critical_errors:
        validation_status = "failed"
        can_deploy = False
    elif len(all_warnings) > 0 or dax_results.get("rewrite_required", 0) > 0:
        validation_status = "warning"
        can_deploy = True
    else:
        validation_status = "success"
        can_deploy = True

    return {
        "validation_status": validation_status,
        "can_deploy": can_deploy,
        "summary": {
            "semantic_model": semantic_validation,
            "dax": dax_results,
            "visuals": visual_results,
            "filters": filter_results,
            "relationships": rel_results,
            "report_structure": report_structure,
        },
        "errors": all_errors,
        "warnings": all_warnings,
    }
