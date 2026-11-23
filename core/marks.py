"""Queue of NOTES marks (SQLite) and applying them to a list. A mark is never lost: it stays queued until written."""
import logging
import time
from pathlib import Path

from . import columns, emails, listio
from .notes import append_note

log = logging.getLogger("marks")


def list_email(recipient) -> str:
    """The address as written in the list (used to find the row again). After a typo fix the address we send to
    differs from the cell, and the cell is not changed, so notes must be matched on the original."""
    return emails.normalize(recipient["raw_email"]) or (recipient["email"] or "")


def queue_mark(conn, campaign_id: int, recipient_id, row_num: int, email: str, note: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM marks WHERE campaign_id=? AND row_num=? AND note=? AND IFNULL(email,'')=IFNULL(?, '')",
        (campaign_id, row_num, note, email)).fetchone()
    if not exists:
        conn.execute("INSERT INTO marks(campaign_id,recipient_id,row_num,email,note,created_at) VALUES(?,?,?,?,?,?)",
                     (campaign_id, recipient_id, row_num, email, note, time.time()))


def locate_row(grid, row_num: int, email_col: int, email: str, header_row: int):
    """Row is found by row number + email; if the row moved, by email (nearest to the old position)."""
    current = emails.normalize(columns.cell(grid, row_num, email_col))
    if (email or "") == current:
        return row_num
    if not email:
        return None
    hits = [r for r in range(header_row + 1, len(grid) + 1)
            if emails.normalize(columns.cell(grid, r, email_col)) == email]
    return min(hits, key=lambda r: abs(r - row_num)) if hits else None


def compute_updates(grid, campaign, marks):
    """Apply marks in memory. Returns (updates {(r,c): value}, written_ids, unmatched_ids)."""
    mapping = campaign["mapping"]
    email_col, notes_col, header_row = mapping["email"], campaign["notes_col"], campaign["header_row"]
    work = {}
    updates, written, unmatched = {}, [], []
    if not str(columns.cell(grid, header_row, notes_col)).strip():
        updates[(header_row, notes_col)] = "NOTES"
    for m in marks:
        r = locate_row(grid, m["row_num"], email_col, m["email"] or "", header_row)
        if r is None:
            unmatched.append(m["id"])
            continue
        cur = work.get(r, columns.cell(grid, r, notes_col))
        new = append_note(cur, m["note"])
        work[r] = new
        if new != columns.cell(grid, r, notes_col):
            updates[(r, notes_col)] = new
        written.append(m["id"])
    return updates, written, unmatched


def _campaign_dict(row):
    import json
    d = dict(row)
    d["mapping"] = json.loads(d["mapping"] or "{}")
    return d


def flush_campaign(conn, campaign_row, source=None) -> dict:
    """Write pending marks of one campaign to its list (server copy for files). Raises on write failure."""
    campaign = _campaign_dict(campaign_row)
    pending = conn.execute(
        "SELECT * FROM marks WHERE campaign_id=? AND written_at IS NULL AND unmatched=0 ORDER BY id",
        (campaign["id"],)).fetchall()
    if not pending:
        return {"written": 0, "unmatched": 0}
    source = source or listio.open_source(campaign["source_type"], campaign["source_ref"])
    try:
        grid = source.read(campaign["tab"])
        updates, written, unmatched = compute_updates(grid, campaign, pending)
        style = None
        if (campaign["header_row"], campaign["notes_col"]) in updates and campaign["notes_col"] > 1:
            style = ((campaign["header_row"], campaign["notes_col"]), campaign["notes_col"] - 1)
        source.write_cells(campaign["tab"], updates, header_style_from=style)
    except Exception as e:
        ids = [m["id"] for m in pending]
        conn.execute(f"UPDATE marks SET attempts=attempts+1, last_error=? WHERE id IN ({','.join('?' * len(ids))})",
                     [str(e)[:300], *ids])
        raise
    now = time.time()
    if written:
        conn.execute(f"UPDATE marks SET written_at=? WHERE id IN ({','.join('?' * len(written))})", [now, *written])
    if unmatched:
        conn.execute(f"UPDATE marks SET unmatched=1 WHERE id IN ({','.join('?' * len(unmatched))})", unmatched)
        from .db import add_event
        add_event(conn, "warn", f"{len(unmatched)} note(s) couldn't be written because the row was not found "
                                f"(the email was changed or the row deleted). See the campaign page.", campaign["id"])
    return {"written": len(written), "unmatched": len(unmatched)}


def mark_file_bytes(conn, campaign_row, data: bytes, filename: str) -> bytes:
    """Apply ALL of a campaign's marks to a copy of the user's file (idempotent) and return the new bytes."""
    import tempfile, os
    campaign = _campaign_dict(campaign_row)
    marks = conn.execute("SELECT * FROM marks WHERE campaign_id=? ORDER BY id", (campaign["id"],)).fetchall()
    suffix = Path(filename).suffix or (".csv" if campaign["source_type"] == "csv" else ".xlsx")
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        Path(tmp).write_bytes(data)
        src = listio.open_source(campaign["source_type"], tmp)
        grid = src.read(campaign["tab"])
        updates, _, _ = compute_updates(grid, campaign, marks)
        style = None
        if (campaign["header_row"], campaign["notes_col"]) in updates and campaign["notes_col"] > 1:
            style = ((campaign["header_row"], campaign["notes_col"]), campaign["notes_col"] - 1)
        src.write_cells(campaign["tab"], updates, header_style_from=style)
        return Path(tmp).read_bytes()
    finally:
        os.unlink(tmp)
