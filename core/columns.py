"""Header-row and column auto-detection. The user always confirms the result on screen."""
import re

FIELDS = ["email", "name", "first_name", "last_name", "number", "position", "notes"]
FIELD_LABELS = {
    "email": "Email", "name": "Full name", "first_name": "First name", "last_name": "Last name",
    "number": "Phone", "position": "Position / driver type", "notes": "NOTES",
}

VARIANTS = {
    "email": {"email", "emails", "email address", "email addresses", "mail", "e mail"},
    "name": {"name", "full name", "fullname", "driver name", "driver", "drivers name", "driver full name"},
    "first_name": {"first name", "firstname", "first", "fname", "given name"},
    "last_name": {"last name", "lastname", "last", "lname", "surname", "family name"},
    "number": {"number", "phone", "phone number", "phone no", "cell", "cell phone", "mobile", "tel", "telephone"},
    "position": {"position", "type", "driver type", "role", "job"},
    "notes": {"notes", "note", "comments", "comment"},
}


def norm(h) -> str:
    s = str(h or "").lower().replace("_", " ")
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _is_email_header(h) -> bool:
    n = norm(h)
    return n in VARIANTS["email"] or bool(re.search(r"\bemail\b", n))


def find_header_row(grid, max_scan: int = 10):
    """1-based header row: first of the first 10 rows that contains an email-like header. None if not found."""
    for i, row in enumerate(grid[:max_scan]):
        if any(_is_email_header(c) for c in row):
            return i + 1
    return None


def detect_mapping(header: list) -> dict:
    """{field: 1-based column}. Exact variant matches first; 'email' also accepts e.g. 'Driver Email'."""
    mapping = {}
    normed = [norm(h) for h in header]
    for field in FIELDS:
        for idx, n in enumerate(normed):
            if n and n in VARIANTS[field] and (idx + 1) not in mapping.values():
                mapping[field] = idx + 1
                break
    if "email" not in mapping:
        for idx, h in enumerate(header):
            if _is_email_header(h) and (idx + 1) not in mapping.values():
                mapping["email"] = idx + 1
                break
    return mapping


def cell(grid, r: int, c: int) -> str:
    """1-based safe accessor for a ragged grid of strings."""
    if r < 1 or c < 1 or r > len(grid):
        return ""
    row = grid[r - 1]
    return row[c - 1] if c <= len(row) else ""


def notes_insert_col(grid, header_row: int) -> int:
    """First completely empty column to the right of the header data. Never a column with data."""
    header = grid[header_row - 1] if header_row <= len(grid) else []
    last = 0
    for i, v in enumerate(header):
        if str(v).strip():
            last = i + 1
    c = last + 1
    while any(str(cell(grid, r, c)).strip() for r in range(1, len(grid) + 1)):
        c += 1
    return c


def col_letter(c: int) -> str:
    s = ""
    while c:
        c, rem = divmod(c - 1, 26)
        s = chr(65 + rem) + s
    return s
