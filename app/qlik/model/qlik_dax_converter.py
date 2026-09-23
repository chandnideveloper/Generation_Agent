"""Generic Qlik to DAX expression analyzer and converter.

Classifies expressions:
1. directly_convertible: valid DAX expressions requiring no change.
2. safely_transformable: expressions with well-defined mappings (e.g.
   SWITCH(Match(...)), Pick(Match(...)), AVERAGE(SWITCH(...))).
3. rewrite_required: complex constructs (AGGR, Set Analysis, nested non-convertible Match)
   that must be guarded with BLANK() and flagged for manual rewrite.
4. unsupported: malformed or empty expressions.
"""

import re
from typing import List, Optional, Tuple


def _split_args(s: str) -> List[str]:
    """Split comma-separated arguments respecting nested parentheses, brackets, and quotes."""
    args = []
    curr = []
    depth = 0
    in_single_quote = False
    in_double_quote = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            curr.append(ch)
        elif ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            curr.append(ch)
        elif in_single_quote or in_double_quote:
            curr.append(ch)
        elif ch in "([":
            depth += 1
            curr.append(ch)
        elif ch in ")]":
            depth -= 1
            curr.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(curr).strip())
            curr = []
        else:
            curr.append(ch)
        i += 1
    if curr:
        args.append("".join(curr).strip())
    return [a for a in args if a]


def _normalize_dax_val(val: str) -> str:
    """Ensure string literals in DAX use double quotes."""
    v = val.strip()
    if v.startswith("'") and v.endswith("'") and len(v) >= 2:
        inner = v[1:-1].replace('"', '""')
        return f'"{inner}"'
    return v


def _extract_table_from_expr(expr: str, fallback_table: Optional[str] = None) -> str:
    """Find the first referenced table name 'Table'[Col] or fallback."""
    m = re.search(r"'([^']+)'\[", expr)
    if m:
        return m.group(1)
    if fallback_table and fallback_table != "_Measures":
        return fallback_table
    return "Table"


def transform_switch_match(dax: str) -> str:
    """
    Rewrite SWITCH(Match(expr, v1, v2, ...), 1, r1, 2, r2, ..., [default])
    into valid DAX: SWITCH(expr, v1, r1, v2, r2, ..., [default])
    """
    if not dax or "match" not in dax.lower():
        return dax

    pattern = re.compile(r"\bSWITCH\s*\(\s*Match\s*\(", re.IGNORECASE)
    pos = 0
    out = []

    while pos < len(dax):
        m = pattern.search(dax, pos)
        if not m:
            out.append(dax[pos:])
            break

        out.append(dax[pos:m.start()])
        # Find closing paren for the entire SWITCH call
        switch_start = m.start()
        paren_depth = 0
        idx = dax.find("(", switch_start)
        switch_args_start = idx + 1
        paren_depth = 1
        idx += 1
        while idx < len(dax) and paren_depth > 0:
            if dax[idx] == "(":
                paren_depth += 1
            elif dax[idx] == ")":
                paren_depth -= 1
            idx += 1

        if paren_depth != 0:
            # Unbalanced, skip
            out.append(dax[switch_start:idx])
            pos = idx
            continue

        switch_body = dax[switch_args_start : idx - 1]
        switch_args = _split_args(switch_body)
        if not switch_args:
            out.append(dax[switch_start:idx])
            pos = idx
            continue

        first_arg = switch_args[0]
        # Verify first_arg is Match(...)
        m_match = re.match(r"^Match\s*\((.*)\)$", first_arg, re.IGNORECASE | re.DOTALL)
        if not m_match:
            out.append(dax[switch_start:idx])
            pos = idx
            continue

        match_inner = m_match.group(1).strip()
        match_args = _split_args(match_inner)
        if len(match_args) < 2:
            out.append(dax[switch_start:idx])
            pos = idx
            continue

        target_col = match_args[0]
        match_values = match_args[1:]  # 1-indexed positions 1..N

        # Parse the remaining switch args: pairs of (index, result) plus optional default
        results_map = {}
        default_val = None
        rem = switch_args[1:]

        i = 0
        while i < len(rem):
            curr_item = rem[i]
            # Check if curr_item is an integer index (1, 2, 3...)
            if curr_item.isdigit() and i + 1 < len(rem):
                idx_num = int(curr_item)
                res_val = rem[i + 1]
                results_map[idx_num] = res_val
                i += 2
            else:
                # Could be default value at the end
                if i == len(rem) - 1:
                    default_val = curr_item
                i += 1

        # Build translated SWITCH:
        # SWITCH(target_col, v1, r1, v2, r2, ..., [default])
        new_args = [target_col]
        for val_idx, val_raw in enumerate(match_values, start=1):
            if val_idx in results_map:
                new_args.append(_normalize_dax_val(val_raw))
                new_args.append(results_map[val_idx])

        if default_val:
            new_args.append(default_val)

        new_switch = f"SWITCH({', '.join(new_args)})"
        out.append(new_switch)
        pos = idx

    return "".join(out)


def transform_pick_match(dax: str) -> str:
    """
    Rewrite Pick(Match(expr, v1, v2, ...), r1, r2, ...)
    into valid DAX: SWITCH(expr, v1, r1, v2, r2, ...)
    """
    if not dax or "pick" not in dax.lower() or "match" not in dax.lower():
        return dax

    pattern = re.compile(r"\bPick\s*\(\s*Match\s*\(", re.IGNORECASE)
    pos = 0
    out = []

    while pos < len(dax):
        m = pattern.search(dax, pos)
        if not m:
            out.append(dax[pos:])
            break

        out.append(dax[pos:m.start()])
        pick_start = m.start()
        idx = dax.find("(", pick_start)
        paren_depth = 1
        pick_args_start = idx + 1
        idx += 1
        while idx < len(dax) and paren_depth > 0:
            if dax[idx] == "(":
                paren_depth += 1
            elif dax[idx] == ")":
                paren_depth -= 1
            idx += 1

        if paren_depth != 0:
            out.append(dax[pick_start:idx])
            pos = idx
            continue

        pick_body = dax[pick_args_start : idx - 1]
        pick_args = _split_args(pick_body)
        if not pick_args:
            out.append(dax[pick_start:idx])
            pos = idx
            continue

        first_arg = pick_args[0]
        m_match = re.match(r"^Match\s*\((.*)\)$", first_arg, re.IGNORECASE | re.DOTALL)
        if not m_match:
            out.append(dax[pick_start:idx])
            pos = idx
            continue

        match_args = _split_args(m_match.group(1).strip())
        if len(match_args) < 2:
            out.append(dax[pick_start:idx])
            pos = idx
            continue

        target_col = match_args[0]
        match_values = match_args[1:]
        pick_results = pick_args[1:]

        new_args = [target_col]
        for v, r in zip(match_values, pick_results):
            new_args.append(_normalize_dax_val(v))
            new_args.append(r)

        new_switch = f"SWITCH({', '.join(new_args)})"
        out.append(new_switch)
        pos = idx

    return "".join(out)


def transform_agg_iterators(dax: str, home_table: Optional[str] = None) -> str:
    """
    Rewrite AVERAGE(SWITCH(...)), SUM(SWITCH(...)), MIN(SWITCH(...)), MAX(SWITCH(...))
    into AVERAGEX('Table', SWITCH(...)), SUMX('Table', SWITCH(...)), etc.
    """
    if not dax or "switch" not in dax.lower():
        return dax

    pattern = re.compile(r"\b(AVERAGE|SUM|MIN|MAX|COUNT)\s*\(\s*(?=SWITCH\s*\()", re.IGNORECASE)
    pos = 0
    out = []

    while pos < len(dax):
        m = pattern.search(dax, pos)
        if not m:
            out.append(dax[pos:])
            break

        out.append(dax[pos:m.start()])
        func_name = m.group(1).upper()
        iter_func = f"{func_name}X"

        idx = dax.find("(", m.start())
        paren_depth = 1
        args_start = idx + 1
        idx += 1
        while idx < len(dax) and paren_depth > 0:
            if dax[idx] == "(":
                paren_depth += 1
            elif dax[idx] == ")":
                paren_depth -= 1
            idx += 1

        if paren_depth != 0:
            out.append(dax[m.start():idx])
            pos = idx
            continue

        inner_arg = dax[args_start : idx - 1].strip()
        tbl = _extract_table_from_expr(inner_arg, home_table)
        out.append(f"{iter_func}('{tbl}', {inner_arg})")
        pos = idx

    return "".join(out)


def transform_qlik_expression(dax: str, home_table: Optional[str] = None) -> Tuple[str, str, Optional[str]]:
    """
    Full pipeline to convert safe Qlik constructs to valid DAX.
    Returns: (final_expression, status, problem_reason)
    status is one of:
      - 'directly_convertible'
      - 'safely_transformed'
      - 'rewrite_required'
      - 'unsupported'
    """
    expr = (dax or "").strip()
    if not expr:
        return "BLANK()", "unsupported", "Empty DAX expression"

    orig = expr

    # 1. Transform Pick(Match(...)) -> SWITCH(...)
    expr = transform_pick_match(expr)

    # 2. Transform SWITCH(Match(...)) -> SWITCH(...)
    expr = transform_switch_match(expr)

    # 3. Transform AVERAGE(SWITCH(...)) -> AVERAGEX('Table', SWITCH(...))
    expr = transform_agg_iterators(expr, home_table)

    transformed = expr != orig

    # Check for remaining unhandled Qlik constructs
    qlik_unhandled = [
        (re.compile(r"\bAGGR\s*\(", re.IGNORECASE), "Qlik AGGR() has no direct DAX equivalent"),
        (re.compile(r"\{\s*[<$]|\{\s*\w+\s*<"), "Qlik set analysis {<...>} has no direct DAX equivalent"),
        (re.compile(r"\$\(\s*[^)]+\)"), "Qlik $() dollar expansion has no direct DAX equivalent"),
        (re.compile(r"\bMATCH\s*\(", re.IGNORECASE), "Qlik Match() expression could not be safely converted to DAX"),
        (re.compile(r"\bPICK\s*\(", re.IGNORECASE), "Qlik Pick() expression could not be safely converted to DAX"),
        (re.compile(r"\bAPPLYMAP\s*\(", re.IGNORECASE), "Qlik ApplyMap() has no direct DAX equivalent"),
        (re.compile(r"\bRANGE(SUM|AVG|MIN|MAX)\s*\(", re.IGNORECASE), "Qlik Range*() functions have no DAX equivalent"),
    ]

    for pat, reason in qlik_unhandled:
        if pat.search(expr):
            return orig, "rewrite_required", reason

    status = "safely_transformed" if transformed else "directly_convertible"
    return expr, status, None
