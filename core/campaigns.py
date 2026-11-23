"""Campaign preparation: parse list, clean emails, decide who gets an email, summary + estimate."""
import json
import time
from datetime import timedelta

from . import caps, columns, config, db, emails, listio, notes, timeutil
from .marks import queue_mark
from .render import variables

AVG_GAP = (90 + 300) / 2


def get(conn, cid):
    row = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["mapping"] = json.loads(d["mapping"] or "{}")
    return d


def build_recipients(conn, cid: int, grid, header_row: int, mapping: dict, notes_col: int, skip_sent: bool,
                     mx_checker=None) -> None:
    """(Re)create the recipient list. Order = sheet order. Nothing is written to the list here."""
    rows = []
    seen = set()
    recent_days = db.get_setting(conn, "recent_days")
    dry = config.dry_run()
    for r in range(header_row + 1, len(grid) + 1):
        row = grid[r - 1]
        if not any(str(c).strip() for c in row):
            continue
        raw = columns.cell(grid, r, mapping["email"]) if mapping.get("email") else ""
        name = columns.cell(grid, r, mapping["name"]) if mapping.get("name") else ""
        first = columns.cell(grid, r, mapping["first_name"]) if mapping.get("first_name") else ""
        last = columns.cell(grid, r, mapping["last_name"]) if mapping.get("last_name") else ""
        if not name and (first or last):
            name = f"{first} {last}".strip()
        position = columns.cell(grid, r, mapping["position"]) if mapping.get("position") else ""
        existing_note = columns.cell(grid, r, notes_col)
        v = variables(name=name, first_name=first, last_name=last, position=position)
        rec = {"row_num": r, "raw_email": raw, "email": "", "name": v["name"], "first_name": v["first_name"],
               "last_name": v["last_name"], "position": v["position"], "status": "queued", "skip_reason": None,
               "suggestion": None}
        email = emails.normalize(raw)
        rec["email"] = email
        if not str(raw).strip():
            rec.update(status="skipped", skip_reason="no_email")
        elif not email or not emails.is_valid_syntax(email):
            rec.update(status="skipped", skip_reason="invalid")
        elif email in seen:
            rec.update(status="skipped", skip_reason="duplicate")
        else:
            seen.add(email)
            sugg = emails.typo_suggestion(email)
            reason = db.suppression_reason(conn, email)
            if sugg:
                rec.update(status="skipped", skip_reason="typo", suggestion=sugg)
            elif emails.is_role(email):
                rec.update(status="skipped", skip_reason="role")
            elif reason:
                rec.update(status="skipped", skip_reason=reason)
            elif skip_sent and notes.has_sent(existing_note):
                rec.update(status="skipped", skip_reason="already_sent")
            elif db.recently_emailed(conn, email, recent_days, cid, include_dry=dry):
                rec.update(status="skipped", skip_reason="recent")
        rows.append(rec)

    domains = {r["email"].split("@")[1] for r in rows if r["status"] == "queued"}
    ok = emails.check_domains(domains, mx_checker or emails.has_mail_server)
    for r in rows:
        if r["status"] == "queued" and not ok.get(r["email"].split("@")[1], True):
            r.update(status="skipped", skip_reason="invalid")

    with db.tx(conn):
        conn.execute("DELETE FROM recipients WHERE campaign_id=?", (cid,))
        conn.executemany(
            "INSERT INTO recipients(campaign_id,row_num,email,raw_email,name,first_name,last_name,position,status,"
            "skip_reason,suggestion) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(cid, r["row_num"], r["email"], r["raw_email"], r["name"], r["first_name"], r["last_name"],
              r["position"], r["status"], r["skip_reason"], r["suggestion"]) for r in rows])


def apply_typo_fixes(conn, cid: int, recipient_ids) -> int:
    fixed = 0
    with db.tx(conn):
        taken = {r[0] for r in conn.execute(
            "SELECT email FROM recipients WHERE campaign_id=? AND status='queued'", (cid,))}
        for rid in recipient_ids:
            r = conn.execute("SELECT * FROM recipients WHERE id=? AND campaign_id=? AND skip_reason='typo'",
                             (rid, cid)).fetchone()
            if not r or not r["suggestion"]:
                continue
            new = r["suggestion"]
            reason = db.suppression_reason(conn, new)
            if new in taken:
                conn.execute("UPDATE recipients SET email=?, skip_reason='duplicate' WHERE id=?", (new, rid))
            elif reason:
                conn.execute("UPDATE recipients SET email=?, skip_reason=? WHERE id=?", (new, reason, rid))
            elif db.recently_emailed(conn, new, db.get_setting(conn, "recent_days"), cid, include_dry=config.dry_run()):
                conn.execute("UPDATE recipients SET email=?, skip_reason='recent' WHERE id=?", (new, rid))
            else:
                conn.execute("UPDATE recipients SET email=?, status='queued', skip_reason=NULL WHERE id=?", (new, rid))
                taken.add(new)
                fixed += 1
    return fixed


def summary(conn, cid: int) -> dict:
    counts = {r["k"]: r["n"] for r in conn.execute(
        "SELECT COALESCE(skip_reason, status) k, COUNT(*) n FROM recipients WHERE campaign_id=? GROUP BY k", (cid,))}
    total = sum(counts.values())
    will_send = counts.get("queued", 0)
    typos = [dict(r) for r in conn.execute(
        "SELECT id,row_num,email,suggestion FROM recipients WHERE campaign_id=? AND skip_reason='typo'", (cid,))]
    return {
        "total": total,
        "will_send": will_send,
        "skipped": {k: v for k, v in counts.items() if k in notes.SKIP_LABELS},
        "typos": typos,
        "estimate": estimate_finish(conn, will_send),
    }


def estimate_finish(conn, n: int, now=None):
    """Rough finish date (Central) at current caps, warm-up and window. None if no account can send."""
    now = now or time.time()
    if n <= 0:
        return None
    window = db.get_setting(conn, "window")
    schedule = db.get_setting(conn, "warmup")
    accs = [a for a in conn.execute("SELECT * FROM accounts") if caps.account_state(a, now) in ("ok", "paused")]
    if not accs or not window["days"]:
        return None
    per_window_by_gap = max(1, int(timeutil.window_seconds(window) // AVG_GAP))
    remaining = n
    day = timeutil.central(now)
    for i in range(0, 400):
        d = day + timedelta(days=i)
        if d.weekday() not in window["days"]:
            continue
        eh, em = map(int, window["end"].split(":"))
        if i == 0 and (d.hour, d.minute) >= (eh, em):
            continue
        ts = d.timestamp()
        for a in accs:
            cap = caps.account_cap(a, schedule, ts)
            if i == 0:
                cap = max(0, cap - caps.sent_last_24h(conn, a["id"], now))
            remaining -= min(cap, per_window_by_gap)
        if remaining <= 0:
            return d.strftime("%a %m/%d/%Y")
    return None


def queue_skip_marks(conn, cid: int) -> None:
    """When a campaign starts: write 'Not sent - ...' for skipped rows (not for rows already marked Sent)."""
    recent_days = db.get_setting(conn, "recent_days")
    for r in conn.execute("SELECT * FROM recipients WHERE campaign_id=? AND status='skipped'", (cid,)).fetchall():
        note = notes.skip_note(r["skip_reason"], recent_days)
        if note:
            queue_mark(conn, cid, r["id"], r["row_num"], emails.normalize(r["raw_email"]), note)


def load_grid(campaign):
    src = listio.open_source(campaign["source_type"], campaign["source_ref"])
    return src.read(campaign["tab"])
