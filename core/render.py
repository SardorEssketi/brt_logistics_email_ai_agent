"""Placeholder rendering with safe fallbacks."""
import re

PLACEHOLDER_RE = re.compile(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}")
KNOWN = ["first_name", "last_name", "name", "position"]
FALLBACK = {"first_name": "there", "name": "there", "last_name": "", "position": "driver"}
PLACEHOLDER_HELP = {
    "first_name": 'First word of the name. Empty → "there" (so "Hi {first_name}," becomes "Hi there,")',
    "name": 'Full name. Empty → "there"',
    "last_name": "Last name. Empty → nothing",
    "position": 'Position / driver type. Empty → "driver"',
}


def smart_case(s: str) -> str:
    s = (s or "").strip()
    if s and (s.isupper() or s.islower()):
        return s.title()
    return s


def split_name(full: str):
    """'Shannon Clark' -> ('Shannon','Clark'); 'Clark, Shannon' -> ('Shannon','Clark')."""
    full = re.sub(r"\s+", " ", (full or "").strip())
    if not full:
        return "", ""
    if "," in full:
        last, _, first = full.partition(",")
        first_word = first.strip().split(" ")[0] if first.strip() else ""
        return smart_case(first_word), smart_case(last.strip())
    parts = full.split(" ")
    return smart_case(parts[0]), smart_case(parts[-1]) if len(parts) > 1 else ""


def variables(name="", first_name="", last_name="", position="") -> dict:
    f, l = split_name(name)
    first = smart_case(first_name) or f
    last = smart_case(last_name) or l
    full = smart_case(name) if name else " ".join(x for x in (first, last) if x)
    return {"first_name": first, "last_name": last, "name": full, "position": (position or "").strip()}


def render(template: str, vars_: dict) -> str:
    def sub(m):
        key = m.group(1).lower()
        if key not in FALLBACK:
            return m.group(0)
        return (vars_.get(key) or "").strip() or FALLBACK[key]
    return PLACEHOLDER_RE.sub(sub, template or "")


def unknown_placeholders(text: str):
    return sorted({m.group(1) for m in PLACEHOLDER_RE.finditer(text or "") if m.group(1).lower() not in FALLBACK})
