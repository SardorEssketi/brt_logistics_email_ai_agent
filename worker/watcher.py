"""IMAP watcher: every 10 minutes, read each account's new INBOX mail for bounces, unsubscribes and replies."""
import logging
import time

from core import classify, crypto, db, imap_util, outcomes

log = logging.getLogger("watcher")
INTERVAL = 600


def process_message(conn, raw: bytes, own_addresses) -> str:
    msg = classify.parse(raw)
    res = classify.classify(msg, own_addresses)
    kind = res["kind"]
    if kind == "bounce":
        hit = False
        for addr in res["failed"]:
            r = outcomes.find_recipient(conn, res["refs"], addr)
            if r is not None:
                outcomes.record(conn, "bounced", r, addr)
                hit = True
        if not hit and res["refs"]:
            r = outcomes.find_recipient(conn, res["refs"])
            if r is not None:
                outcomes.record(conn, "bounced", r)
                hit = True
        return "bounce" if hit else "ignore"
    if kind in ("unsubscribe", "reply"):
        r = outcomes.find_recipient(conn, res["refs"], res["from"])
        if r is None:
            if kind == "unsubscribe" and not res["refs"]:
                # e.g. Gmail's "Unsubscribe" button sends a bare "unsubscribe" email: honour it if we ever emailed them
                known = conn.execute("SELECT 1 FROM recipients WHERE email=? AND status='sent'", (res["from"],)).fetchone()
                if known:
                    outcomes.record(conn, "unsubscribed", None, res["from"])
                    return "unsubscribe"
            return "ignore"
        outcomes.record(conn, "unsubscribed" if kind == "unsubscribe" else "replied", r, res["from"])
        return kind
    return kind


def check_account(conn, acc, own_addresses) -> dict:
    state = conn.execute("SELECT * FROM imap_state WHERE account_id=?", (acc["id"],)).fetchone()
    uv, last = (state["uidvalidity"], state["last_uid"]) if state else (None, 0)
    counts = {}
    m = imap_util.connect(acc["email"], crypto.decrypt(acc["password_enc"]))
    try:
        for new_uv, uid, raw in imap_util.fetch_new(m, uv, last or 0, acc["added_at"]):
            if raw:
                try:
                    k = process_message(conn, raw, own_addresses)
                    counts[k] = counts.get(k, 0) + 1
                except Exception:
                    log.exception("Could not process message uid %s on %s", uid, acc["email"])
            conn.execute("INSERT INTO imap_state(account_id,uidvalidity,last_uid,last_check) VALUES(?,?,?,?) "
                         "ON CONFLICT(account_id) DO UPDATE SET uidvalidity=excluded.uidvalidity, "
                         "last_uid=excluded.last_uid, last_check=excluded.last_check",
                         (acc["id"], new_uv, uid, time.time()))
        conn.execute("INSERT INTO imap_state(account_id,last_check) VALUES(?,?) ON CONFLICT(account_id) "
                     "DO UPDATE SET last_check=excluded.last_check", (acc["id"], time.time()))
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return counts


def run(stop_event):
    conn = db.connect()
    log.info("IMAP watcher started")
    while not stop_event.is_set():
        accounts = conn.execute("SELECT * FROM accounts WHERE disabled_reason IS NULL").fetchall()
        own = [a["email"] for a in conn.execute("SELECT email FROM accounts")]
        for acc in accounts:
            if stop_event.is_set():
                break
            try:
                counts = check_account(conn, acc, own)
                if counts:
                    log.info("%s: %s", acc["email"], counts)
            except Exception as e:
                log.warning("IMAP check failed for %s: %s", acc["email"], e)
        stop_event.wait(INTERVAL)
