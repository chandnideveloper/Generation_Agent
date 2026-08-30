import re
from typing import Any, Dict, Optional

from app.util.payload import text

# TMDL accepts exactly these on a column.
TMDL_TYPES = {"string", "int64", "double", "decimal", "dateTime", "boolean", "binary"}

QLIK_TO_TMDL = {
    "STRING": "string", "TEXT": "string", "ASCII": "string", "VARCHAR": "string", "NVARCHAR": "string", "CHAR": "string",
    "INTEGER": "int64", "INT": "int64", "INT64": "int64", "BIGINT": "int64", "SMALLINT": "int64", "TINYINT": "int64",
    "NUMBER": "double", "NUMERIC": "double", "DOUBLE": "double", "REAL": "double", "FLOAT": "double", "NUM": "double",
    "DECIMAL": "decimal", "MONEY": "decimal", "CURRENCY": "decimal",
    "DATE": "dateTime", "TIME": "dateTime", "TIMESTAMP": "dateTime",
    "DATETIME": "dateTime", "INTERVAL": "dateTime",
    "BOOLEAN": "boolean", "BOOL": "boolean", "BIT": "boolean",
    "BINARY": "binary", "BLOB": "binary",
}

# Power BI needs a summarizeBy; only numeric columns default to sum.
NUMERIC_TYPES = {"int64", "double", "decimal"}

NUMERIC_KEYWORDS = {
    "pnl", "price", "quantity", "qty", "volume", "quotevolume", "amount", "total",
    "percent", "percentage", "change", "rate", "cost", "profit", "loss", "revenue", "sales",
    "discount", "balance", "score", "weight", "avg", "min", "max", "value", "entryprice",
    "exitprice", "lastprice", "bidprice", "askprice", "openprice", "highprice", "lowprice",
    "pricechange", "pricechangepercent", "weightedavgprice", "prevcloseprice", "lastqty",
    "bidqty", "askqty", "units", "gpa", "credits", "grade_point", "salary", "bonus", "fee", "tax"
}

DATETIME_KEYWORDS = {
    "date", "tradedate", "orderdate", "shipdate", "hiredate", "dob", "created_at", "updated_at"
}

INTEGER_KEYWORDS = {
    "opentime", "closetime", "timestamp", "firstid", "lastid", "year", "month", "day", "rank", "count"
}


def infer_type_from_name(col_name: str) -> Optional[str]:
    if not col_name:
        return None
    clean = re.sub(r"[^a-zA-Z0-9_]", "", col_name.lower())
    # Derived date-part labels (Date_quarterLabel, Date_monthLabel) carry
    # "date" in their name but hold text, not a real date - check this
    # before the generic "date" suffix/keyword match below.
    if "label" in clean:
        return "string"
    if any(clean.endswith(sw) or clean == sw or sw in clean for sw in ["band", "tier", "bucket", "bracket", "range", "group"]):
        return "string"
    if any(clean.endswith(sw) or clean == sw for sw in ["result", "status", "type", "name", "category", "desc", "description", "side", "symbol", "trader", "reduction", "comment", "note", "title", "flag", "code"]):
        return "string"
    if clean in INTEGER_KEYWORDS or clean.endswith("year") or clean.endswith("count"):
        return "int64"
    if clean in NUMERIC_KEYWORDS or any(clean.endswith(kw) or clean.startswith(kw) for kw in ("price", "qty", "quantity", "volume", "amount", "rate", "percent", "cost", "profit", "loss", "revenue", "sales", "discount", "balance", "fee", "tax")):
        if not clean.endswith("id"):
            return "double"
    if clean in DATETIME_KEYWORDS or clean.endswith("date"):
        return "dateTime"
    return None



def to_tmdl_type(raw: Any, col_name: str = "") -> str:
    """Normalize any inbound type string to a legal TMDL data type."""
    value = text(raw, "")
    if col_name:
        inferred = infer_type_from_name(col_name)
        if inferred:
            if value in ("string", "text", "", "STRING", "TEXT"):
                return inferred
            if inferred == "string" and any(k in col_name.lower() for k in ("band", "tier", "bucket", "bracket", "status", "type", "name", "category", "desc", "label")):
                return "string"
            if inferred == "double" and value in TMDL_TYPES and value not in ("dateTime", "boolean"):
                return inferred
            if inferred == "int64" and value == "double" and col_name.lower() in ("count", "year", "rank", "id", "month", "day"):
                return inferred
            if value == "dateTime" and inferred in ("string", "int64"):
                return inferred

    if value in TMDL_TYPES:
        return value

    upper = value.upper()
    if upper in QLIK_TO_TMDL:
        return QLIK_TO_TMDL[upper]

    # Fall back on substring matching for decorated types like
    # "STRING (CALCULATED)" or "NUMERIC(18,2)".
    for token, mapped in QLIK_TO_TMDL.items():
        if token in upper:
            return mapped

    if col_name:
        inferred = infer_type_from_name(col_name)
        if inferred:
            return inferred

    return "string"


def summarize_by(data_type: str, is_key: bool = False, col_name: str = "") -> str:
    """Keys must not aggregate, or Power BI sums identifiers by default."""
    if is_key:
        return "none"
    if col_name:
        c_lower = col_name.lower()
        if c_lower.endswith("id") or c_lower.endswith("code") or c_lower.endswith("year") or c_lower.endswith("rank"):
            return "none"
    return "sum" if data_type in NUMERIC_TYPES else "none"



def format_string(column: Dict[str, Any], data_type: str) -> str:
    """Pick a display format, preferring whatever the mapping agent resolved."""
    num_fmt = column.get("num_format") or column.get("qlik_number_format")
    if isinstance(num_fmt, dict):
        pat = num_fmt.get("format_pattern") or num_fmt.get("qFmt") or num_fmt.get("format")
        if pat:
            return str(pat).strip()
        qtype = str(num_fmt.get("type") or num_fmt.get("qType") or "").upper()
        if qtype == "M":
            return "$#,##0.00"
        if qtype == "P":
            return "0.00%"
        if qtype == "I":
            return "#,##0"
        if qtype in ("F", "R"):
            return "#,##0.00"
        if qtype == "D":
            return "yyyy-mm-dd"
        if qtype == "TS":
            return "yyyy-mm-dd hh:nn:ss"
        if qtype == "T":
            return "hh:nn:ss"

    explicit = text(column.get("format_string") or column.get("inferred_format"))
    # "General Text" is Qlik's placeholder, not a Power BI format string.
    # A string/boolean column never gets a display format applied by PBI,
    # so an inherited date/number pattern here is always stale metadata
    # left over from a type the pipeline corrected - drop it rather than
    # writing a nonsensical formatString into the TMDL.
    if explicit and explicit.lower() not in ("general text", "general", "none") and data_type not in ("string", "boolean"):
        return explicit
    if data_type == "dateTime":
        return "General Date"
    if data_type == "int64":
        return "#,##0"
    if data_type in ("double", "decimal"):
        return "#,##0.00"
    return ""
