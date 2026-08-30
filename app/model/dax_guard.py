"""Reject DAX that still contains Qlik syntax.

The mapping agent's converter handles the common aggregations but passes
through constructs with no direct DAX form — `Aggr(...)`, set analysis
`{<Field={'x'}>}`, `Num(...)`, `$(vVar)`. Emitting those verbatim produces a
model Power BI Desktop refuses to open, which fails the whole import for one
bad measure.

So a measure that does not survive validation is emitted as a safe
placeholder, with the original Qlik preserved in a comment above it and a
note explaining the rewrite. The model opens, every other measure works, and
nothing is silently lost.
"""

import re
from typing import Dict, List, Optional, Tuple

# Qlik constructs with no direct DAX equivalent.
QLIK_LEFTOVERS: List[Tuple[re.Pattern, str, str]] = [
    (
        re.compile(r"\bAGGR\s*\(", re.IGNORECASE),
        "Qlik AGGR() has no DAX equivalent",
        "Rebuild with SUMMARIZE/ADDCOLUMNS and an iterator, e.g. "
        "MAXX(SUMMARIZE(Table, Table[Key], \"v\", SUM(Table[Col])), [v]).",
    ),
    (
        re.compile(r"\{\s*[<$]|\{\s*\w+\s*<"),
        "Qlik set analysis {<...>} has no DAX equivalent",
        "Rewrite as CALCULATE(<aggregation>, <filter>), e.g. "
        "CALCULATE(SUM(T[Amount]), T[Band] = \"High Value\").",
    ),
    (
        re.compile(r"\$\(\s*[^)]+\)"),
        "Qlik $() dollar expansion has no DAX equivalent",
        "Replace with a DAX variable (VAR) or a parameter table column.",
    ),
    (
        re.compile(r"\bNUM\s*\(", re.IGNORECASE),
        "Qlik Num() is a formatting function, not a DAX one",
        "Drop it and set the measure's formatString instead, or use FORMAT().",
    ),
    (
        re.compile(r"\bAPPLYMAP\s*\(", re.IGNORECASE),
        "Qlik ApplyMap() has no DAX equivalent",
        "Model the mapping table as a real table and use RELATED() or LOOKUPVALUE().",
    ),
    (
        re.compile(r"\bONLY\s*\(", re.IGNORECASE),
        "Qlik Only() has no direct DAX equivalent",
        "Use SELECTEDVALUE(Table[Column]) instead.",
    ),
    (
        re.compile(r"\bRANGE(SUM|AVG|MIN|MAX)\s*\(", re.IGNORECASE),
        "Qlik Range*() functions have no DAX equivalent",
        "Use the matching DAX aggregation over a table expression.",
    ),
    (
        re.compile(r"\bSUM\s*\(\s*TOTAL\b", re.IGNORECASE),
        "Qlik SUM(TOTAL ...) has no DAX equivalent",
        "Use CALCULATE(SUM(...), ALL(...)) to disregard current filters.",
    ),
    (
        re.compile(r"\b\w+_Set\b", re.IGNORECASE),
        "Qlik set identifier has no direct DAX equivalent",
        "Convert set identifier into CALCULATE filter arguments.",
    ),
]

# 'Table'['Other'[col]] — a nested reference the converter can produce.
NESTED_REFERENCE = re.compile(r"'[^']+'\[\s*'[^']+'\[")

PLACEHOLDER = "BLANK()"


def validate(dax: str) -> Optional[Dict[str, str]]:
    """Return a problem dict when the expression is not loadable DAX."""
    expression = (dax or "").strip()
    if not expression:
        return {
            "reason": "no DAX expression was produced",
            "suggestion": "Convert the Qlik expression by hand.",
        }

    if expression.count("(") != expression.count(")"):
        return {
            "reason": "unbalanced parentheses in the generated DAX",
            "suggestion": "Repair the expression; the converter truncated it.",
        }

    if NESTED_REFERENCE.search(expression):
        return {
            "reason": "nested column reference such as 'A'['B'[col]]",
            "suggestion": "Qualify the column against a single table, e.g. 'B'[col].",
        }

    for pattern, reason, suggestion in QLIK_LEFTOVERS:
        if pattern.search(expression):
            return {"reason": reason, "suggestion": suggestion}

    return None


def guard(name: str, dax: str) -> Tuple[str, Optional[Dict[str, str]]]:
    """Return (safe_expression, problem-or-None)."""
    problem = validate(dax)
    if not problem:
        return dax, None
    return PLACEHOLDER, {
        "name": name,
        "original": (dax or "").strip(),
        "reason": problem["reason"],
        "suggestion": problem["suggestion"],
    }
