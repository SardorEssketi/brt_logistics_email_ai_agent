"""NOTES column rules: append with ' | ', never delete, never duplicate."""
import re

SEP = " | "
SENT_RE = re.compile(r"(?<![A-Za-z])Sent \d{1,2}/\d{1,2}/\d{4}")

SKIP_NOTES = {
    "no_email": "Not sent - no email",
    "invalid": "Not sent - invalid email",
    "typo": "Not sent - invalid email",
    "role": "Not sent - role address",
    "duplicate": "Not sent - duplicate",
    "unsubscribed": "Not sent - unsubscribed",
    "bounced": "Not sent - bounced before",
    "replied": "Not sent - replied before",
    "manual": "Not sent - unsubscribed",
    "recent": "Not sent - emailed in last {days} days",
    # "already_sent" deliberately absent: those rows stay unchanged.
}

SKIP_LABELS = {
    "no_email": "Empty email cell",
    "invalid": "Invalid email (bad format or no mail server)",
    "typo": "Possible typo in email",
    "role": "Role address (info@, admin@...)",
    "duplicate": "Duplicate (same email earlier in the list)",
    "unsubscribed": "Unsubscribed before",
    "bounced": "Bounced before",
    "replied": "Replied before (not emailed again automatically)",
    "manual": "On the do-not-email list",
    "already_sent": "NOTES already say Sent",
    "recent": "Emailed recently by another campaign (see Settings)",
}


def skip_note(reason: str, recent_days=60):
    """The NOTES text for a skipped row, or None when the row is left unchanged."""
    note = SKIP_NOTES.get(reason)
    return note.format(days=recent_days) if note else None


def append_note(existing, note: str) -> str:
    existing = "" if existing is None else str(existing)
    note = note.strip()
    parts = [p.strip() for p in existing.split("|")]
    if note in parts:
        return existing
    if not existing.strip():
        return note
    return existing.rstrip() + SEP + note


def has_sent(existing) -> bool:
    return bool(SENT_RE.search(str(existing or "")))


def error_note(reason: str) -> str:
    reason = re.sub(r"\s+", " ", (reason or "unknown")).strip()
    return f"Not sent - error: {reason[:80]}"
