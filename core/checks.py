"""Content checker: warnings (allowed) and blocks (must fix)."""
import re

from .render import unknown_placeholders

URL_RE = re.compile(r"(https?://[^\s<>]+|www\.[^\s<>]+)", re.I)
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly",
              "cutt.ly", "shorturl.at", "tiny.cc", "rb.gy", "t.ly", "bl.ink", "short.io", "s.id"}
SPAM_PHRASES = ["guaranteed", "act now", "100% free", "risk-free", "risk free", "click here", "limited time",
                "urgent", "no obligation", "winner", "cash bonus", "earn $", "make money", "apply now!!",
                "once in a lifetime", "100% guaranteed", "free money"]
ACRONYMS = {"CDL", "OTR", "CPM", "MC", "DOT", "USA", "LLC", "INC", "FMCSA", "ELD", "PTO", "TWIC", "HAZMAT",
            "LTL", "FTL", "IFTA", "IRP", "MVR", "PSP", "SAP", "CST", "CT", "EST", "PST", "OO", "HR", "USDOT",
            "TX", "CA", "IL", "FL", "GA", "OH", "PA", "NY", "NJ", "AZ", "TN", "IN", "NC", "SC", "MI", "MO",
            "TEAM", "DM", "SMS", "ASAP", "FAQ", "AM", "PM", "LTL", "OK"}
MAX_WORDS = 150

US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
             "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR").split()
_STATE = "|".join(US_STATES)
STREET_RE = re.compile(r"\b\d{1,6}[A-Za-z]?\s+(?:[NSEW]\.?\s+)?[A-Za-z][A-Za-z0-9.'-]*|\bP\.?\s?O\.?\s+Box\s+\d+", re.I)
# "..., TX 75201" / "..., TX" at end of line or before , . / "Columbus OH 43215". Not "48 states, OK?"
STATE_ZIP_RE = re.compile(rf"(?:,\s*(?:{_STATE})(?=[ \t]*(?:\d{{5}}|$|[,.;]|\s+USA))|"
                          rf"\b(?:{_STATE})\s+\d{{5}}(?:-\d{{4}})?\b)", re.M)
ADDRESS_WARNING = ("US law (CAN-SPAM) may require a physical mailing address in this email. "
                   "Add one to your text if this is a commercial offer.")


def has_postal_address(text: str) -> bool:
    """Rough check: a street number (or PO Box) AND a state abbreviation after a comma / before a ZIP, nearby."""
    for m in STREET_RE.finditer(text or ""):
        if STATE_ZIP_RE.search(text[m.start(): m.start() + 160]):
            return True
    return False


def check_content(subject: str, body: str) -> dict:
    subject = subject or ""
    body = body or ""
    blocks, warnings = [], []
    text = f"{subject}\n{body}"

    if not subject.strip():
        blocks.append("The subject is empty.")
    if not body.strip():
        blocks.append("The message is empty.")
    if re.match(r"^\s*(re|fwd?|fw)\s*:", subject, re.I):
        blocks.append('The subject can\'t start with "Re:" or "Fwd:" — that pretends to be a reply and is illegal under CAN-SPAM.')
    unk = unknown_placeholders(text)
    if unk:
        blocks.append("Unknown placeholder(s): " + ", ".join("{" + u + "}" for u in unk)
                      + ". Use only {first_name}, {name}, {last_name}, {position}.")

    urls = URL_RE.findall(text)
    if len(urls) > 1:
        warnings.append(f"{len(urls)} links. Use at most 1 link — more links look like spam.")
    for u in urls:
        host = re.sub(r"^(https?://)?(www\.)?", "", u, flags=re.I).split("/")[0].lower()
        if host in SHORTENERS:
            warnings.append(f"Link shortener ({host}). Use the full link instead — shorteners are a strong spam signal.")
    caps = sorted({w for w in re.findall(r"\b[A-Z]{4,}\b", text) if w not in ACRONYMS})
    if caps:
        warnings.append("ALL CAPS words: " + ", ".join(caps[:8]) + ". Write them normally.")
    if "!!" in text:
        warnings.append('Avoid "!!!" — use at most one exclamation mark.')
    if "$$" in text:
        warnings.append('Avoid "$$$".')
    low = text.lower()
    found = [p for p in SPAM_PHRASES if p in low]
    if found:
        warnings.append("Spammy phrases: " + ", ".join(f'"{p}"' for p in found) + ".")
    if body.strip() and not has_postal_address(body):
        warnings.append(ADDRESS_WARNING)
    words = len(re.findall(r"\S+", body))
    if words > MAX_WORDS:
        warnings.append(f"The message has {words} words. Keep it under ~{MAX_WORDS} — short emails get more replies.")
    return {"blocks": blocks, "warnings": warnings, "words": words}
