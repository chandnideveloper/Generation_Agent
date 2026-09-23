"""Deterministic identifiers and TMDL name quoting.

Lineage tags are derived from a stable seed rather than random, so
regenerating the same app twice produces byte-identical files. That keeps git
diffs meaningful and makes Fabric updates idempotent.
"""

import re
import uuid

NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
BARE_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
INVALID_PATH = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def lineage_tag(seed: str) -> str:
    """A stable GUID for a model object."""
    seed_str = str(seed) if seed else "default-seed"
    tag = str(uuid.uuid5(NAMESPACE, seed_str))
    return tag or str(uuid.uuid4())


def clean_tmdl_name(name: str) -> str:
    """Clean and sanitize a name so it is a valid, single-line TMDL identifier."""
    if not name:
        return "Measure"
    text = str(name).strip()

    # Remove block comments /* ... */
    text = re.sub(r"/\*[\s\S]*?\*/", " ", text)

    # Remove single line comments // ...
    if "//" in text:
        parts = [p.strip() for p in text.split("//") if p.strip()]
        if parts:
            first = parts[0]
            first = re.split(r"[,:\-\(\[]", first)[0].strip()
            if len(first) >= 3 and not any(kw in first.lower() for kw in ["uses", "note", "todo", "fixme", "window"]):
                text = first
            elif len(parts) > 1 and len(parts[1].split()[0]) >= 3:
                text = parts[1].split(",")[0].strip()
            else:
                text = " ".join(parts)
        else:
            text = "Custom_Measure"

    # Replace all newlines, carriage returns, tabs with a single space
    text = re.sub(r"[\r\n\t]+", " ", text).strip()

    # Remove non-ASCII replacement characters and control characters
    text = re.sub(r"[^\x20-\x7E]+", " ", text).strip()

    # If the name is a Qlik dynamic expression starting with '='
    # e.g. "='Total ' & Pick( $(vMeasure), 'Revenue', 'Trips', 'Customers', 'Drivers' )"
    if text.startswith("="):
        quoted_parts = re.findall(r"'([^']+)'|\"([^\"]+)\"", text)
        clean_words = []
        for q1, q2 in quoted_parts:
            w = (q1 or q2).strip()
            if w and w not in clean_words:
                clean_words.append(w)
        if clean_words:
            text = " ".join(clean_words[:3])
        else:
            text = re.sub(r"[=\$\(\)&,\+]+", " ", text).strip()
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                text = "Dynamic Measure"

    # If name looks like a raw formula or set analysis or code:
    # e.g. "Sum({ <Date = ...", "Avg( Pick(...) )", "Sum(A) / Sum(B)", or contains {<, >}, $(, or nested calls
    nested_calls = any(m in text for m in ["{<", ">}", "$(", "{", "}", " / ", " * ", " + ", " - ", "Pick(", "Match(", "Today()", "Date(", "If(", "Aggr(", "RangeMax(", "RangeMin("])
    if nested_calls or len(text) > 60:
        fn_match = re.match(r"^([A-Za-z0-9_]+)\s*\(", text)
        fn = fn_match.group(1) if fn_match else "Calc"
        idents = [w for w in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", text) if w.lower() not in ("sum", "avg", "count", "min", "max", "date", "floor", "ceil", "pick", "match", "upper", "trim", "total", "null", "today")]
        if idents:
            text = f"{fn}_{idents[0]}"
        else:
            words = re.findall(r"[A-Za-z0-9]+", text)
            text = "_".join(words[:3]) if words else f"{fn}_Measure"

    # Clean simple functional wrappers: Func(Column) -> Func_Column
    text = re.sub(r"^([A-Za-z0-9_]+)\s*\(\s*([A-Za-z0-9_]+)\s*\)$", r"\1_\2", text)

    # Clean any surrounding quotes that might have been part of the raw name
    if (text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"')):
        text = text[1:-1].strip()

    # Final pass: collapse whitespace and cap length strictly to 60 chars
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 60:
        text = text[:60].rstrip()

    return text or "Measure"


def quote_tmdl(name: str) -> str:
    """TMDL quotes an identifier unless it is a bare word."""
    text = clean_tmdl_name(name)
    if BARE_WORD.fullmatch(text):
        return text
    # In TMDL, any internal single quotes must be escaped by doubling: ' -> ''
    escaped = text.replace("'", "''")
    return f"'{escaped}'"


def quote_dax_table(name: str) -> str:
    """DAX table references are single-quoted when not a bare word."""
    return quote_tmdl(name)


def safe_filename(name: str, fallback: str = "item", max_len: int = 80) -> str:
    """Strip characters Windows and the Fabric API reject in a path."""
    cleaned = INVALID_PATH.sub("", str(name or "")).strip().strip(".")
    cleaned = cleaned.replace("\n", " ").strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned or fallback


def slug(name: str, fallback: str = "item") -> str:
    """Lowercase, hyphenated identifier for folder names."""
    text = re.sub(r"[^A-Za-z0-9]+", "-", str(name or "")).strip("-").lower()
    return text or fallback


def make_safe_m_var(name: str) -> str:
    """
    Ensure M step variable name starts with a letter or underscore.
    Tableau/Qlik tables starting with numbers (e.g., '14_Orders') are prefixed with 'raw_'
    to prevent Power Query syntax errors.
    """
    cleaned = re.sub(r'[^A-Za-z0-9_]', '_', str(name or "")).strip("_")
    if not cleaned or not re.match(r'^[A-Za-z_]', cleaned):
        return f"raw_{cleaned}" if cleaned else "raw_table"
    return cleaned
