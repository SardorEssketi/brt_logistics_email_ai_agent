"""Recording what happened after sending (bounce / reply / unsubscribe) + auto-pause rules."""
import time

from . import db
from .marks import queue_mark
from .timeutil import us_date

BOUNCE_LIMIT, BOUNCE_MIN = 0.03, 30
UNSUB_LIMIT, UNSUB_MIN = 0.02, 50


def find_recipient(conn, refs=(), email=None):
    """Our sent row this message refers to: by Message-ID first, else latest sent row to that address."""
    for mid in refs:
        r = conn.execute("SELECT * FROM recipients WHERE message_id=? AND status='sent'", (mid,)).fetchone()
        if r:
            return r
    if email:
        return conn.execute("SELECT * FROM recipients WHERE email=? AND status='sent' ORDER BY sent_at DESC LIMIT 1",
                            (email.lower(),)).fetchone()
    return None


def record(conn, kind: str, recipient, email: str = None, ts=None) -> bool:
    """kind: bounced | unsubscribed | replied. Suppresses the address and queues a NOTES mark. Idempotent."""
    ts = ts or time.time()
    email = (email or (recipient["email"] if recipient else "")).lower()
    if not email:
        return False
    col = {"bounced": "bounced_at", "unsubscribed": "unsubscribed_at", "replied": "replied_at"}[kind]
    label = {"bounced": "Bounced", "unsubscribed": "Unsubscribed", "replied": "Replied"}[kind]
    with db.tx(conn):
        db.suppress(conn, email, kind)
        if recipient is not None:
            fresh = conn.execute(f"SELECT {col} FROM recipients WHERE id=?", (recipient["id"],)).fetchone()
            if fresh[0] is None:
                conn.execute(f"UPDATE recipients SET {col}=? WHERE id=?", (ts, recipient["id"]))
                queue_mark(conn, recipient["campaign_id"], recipient["id"], recipient["row_num"], email,
                           f"{label} {us_date(ts)}")
    if recipient is not None:
        check_auto_pause(conn, recipient["campaign_id"])
    return True


def campaign_rates(conn, cid: int) -> dict:
    r = conn.execute("""SELECT SUM(status='sent') sent, SUM(bounced_at IS NOT NULL) b,
                        SUM(unsubscribed_at IS NOT NULL) u, SUM(replied_at IS NOT NULL) rp
                        FROM recipients WHERE campaign_id=?""", (cid,)).fetchone()
    sent = r["sent"] or 0
    return {"sent": sent, "bounces": r["b"] or 0, "unsubs": r["u"] or 0, "replies": r["rp"] or 0,
            "bounce_rate": (r["b"] or 0) / sent if sent else 0.0,
            "unsub_rate": (r["u"] or 0) / sent if sent else 0.0}


def check_auto_pause(conn, cid: int):
    c = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not c or c["status"] != "running":
        return None
    s = campaign_rates(conn, cid)
    reason = None
    if s["sent"] >= BOUNCE_MIN and s["bounce_rate"] > BOUNCE_LIMIT and s["bounces"] > c["ack_bounces"]:
        reason = (f"Paused automatically: {s['bounces']} of {s['sent']} emails bounced ({s['bounce_rate']:.1%}). "
                  "Above 3% means the list has many old or wrong addresses, and Gmail may lock the accounts. "
                  "Check the list, then press Resume.")
    elif s["sent"] >= UNSUB_MIN and s["unsub_rate"] > UNSUB_LIMIT and s["unsubs"] > c["ack_unsubs"]:
        reason = (f"Paused automatically: {s['unsubs']} of {s['sent']} drivers unsubscribed ({s['unsub_rate']:.1%}). "
                  "Above 2% usually means the message or the list doesn't fit. Review it, then press Resume.")
    if reason:
        with db.tx(conn):
            conn.execute("UPDATE campaigns SET status='paused', status_reason=? WHERE id=? AND status='running'",
                         (reason, cid))
            db.add_event(conn, "error", reason, cid)
    return reason
