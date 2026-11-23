"""Background sender. One email at a time per account, random 90-300 s gaps, only inside the window.

Never double-send:
  1. row -> 'sending' (+ new Message-ID) is committed BEFORE the SMTP call;
  2. row -> 'sent' after Gmail accepts it;
  3. any row found in 'sending' later (crash, dropped connection) is looked up in that account's
     IMAP Sent Mail. Found -> 'sent'. Not found -> back in the queue. IMAP unreachable -> left alone, retried.
"""
import logging
import random
import time
from pathlib import Path

from core import caps, compose, config, crypto, db, imap_util, outcomes, smtp_send, timeutil
from core.marks import list_email, queue_mark
from core.notes import error_note, skip_note

log = logging.getLogger("sender")

GAP_MIN, GAP_MAX = 90, 300
MAX_ATTEMPTS = 3          # first try + 2 retries
RECONCILE_AFTER = 120     # seconds a row must sit in 'sending' before we check Sent Mail


class Sender:
    def __init__(self, conn=None, smtp_factory=None, sent_checker=None, rng=None, outbox=None):
        self.conn = conn or db.connect()
        self.smtp_factory = smtp_factory
        self.sent_checker = sent_checker or self._imap_was_sent
        self.rng = rng or random.Random()
        self.outbox = Path(outbox) if outbox else config.data_dir() / "outbox"

    # ------------------------------------------------------------ helpers
    def _gap(self) -> float:
        if config.dry_run_fast():
            return 0
        return self.rng.uniform(GAP_MIN, GAP_MAX)

    def _password(self, acc) -> str:
        return crypto.decrypt(acc["password_enc"])

    def _imap_was_sent(self, acc, row) -> bool:
        if row["campaign_id"] and self._is_dry(row["campaign_id"]):
            return self._eml_path(row).exists()
        m = imap_util.connect(acc["email"], self._password(acc))
        try:
            return imap_util.was_sent(m, row["message_id"], row["email"], row["sending_at"] or time.time())
        finally:
            try:
                m.logout()
            except Exception:
                pass

    def _is_dry(self, cid) -> bool:
        r = self.conn.execute("SELECT dry_run FROM campaigns WHERE id=?", (cid,)).fetchone()
        return bool(r and r["dry_run"])

    def _eml_path(self, row) -> Path:
        safe = row["message_id"].strip("<>").replace("@", "_at_")
        return self.outbox / f"campaign-{row['campaign_id']}" / f"row{row['row_num']:05d}-{safe}.eml"

    # ------------------------------------------------------------ recovery
    def reconcile(self, now=None, force=False) -> int:
        """Resolve rows stuck in 'sending'. Returns how many were resolved."""
        now = now or time.time()
        q = "SELECT * FROM recipients WHERE status='sending' AND next_attempt_at<=?"
        args = [now]
        if not force:
            q += " AND sending_at<?"
            args.append(now - RECONCILE_AFTER)
        resolved = 0
        for row in self.conn.execute(q, args).fetchall():
            acc = self.conn.execute("SELECT * FROM accounts WHERE id=?", (row["account_id"],)).fetchone()
            try:
                if acc is None:
                    raise RuntimeError("account was removed")
                found = self.sent_checker(acc, row)
            except Exception as e:
                log.warning("Sent-Mail check failed for row %s: %s (will retry)", row["id"], e)
                self.conn.execute("UPDATE recipients SET next_attempt_at=? WHERE id=?", (now + 300, row["id"]))
                continue
            with db.tx(self.conn):
                if found:
                    self._mark_sent(row, acc["id"], row["sending_at"] or now)
                    log.info("Row %s was already sent before the restart — marked sent, NOT resent", row["id"])
                else:
                    self.conn.execute("UPDATE recipients SET status='queued', message_id=NULL, account_id=NULL, "
                                      "attempts=MAX(attempts-1,0) WHERE id=?", (row["id"],))
            resolved += 1
        return resolved

    def _mark_sent(self, row, account_id, ts):
        self.conn.execute("UPDATE recipients SET status='sent', sent_at=?, error=NULL WHERE id=?", (ts, row["id"]))
        self.conn.execute("INSERT INTO send_log(account_id,recipient_id,ts,kind,dry) VALUES(?,?,?,'campaign',?)",
                          (account_id, row["id"], ts, 1 if self._is_dry(row["campaign_id"]) else 0))
        queue_mark(self.conn, row["campaign_id"], row["id"], row["row_num"], list_email(row), f"Sent {timeutil.us_date(ts)}")

    # ------------------------------------------------------------ main step
    def usable_accounts(self, now, dry=False):
        schedule = db.get_setting(self.conn, "warmup")
        out = []
        for acc in self.conn.execute("SELECT * FROM accounts ORDER BY id").fetchall():
            if caps.account_state(acc, now) != "ok":
                continue
            if (acc["next_send_at"] or 0) > now:
                continue
            if caps.sent_last_24h(self.conn, acc["id"], now, dry=dry) >= caps.account_cap(acc, schedule, now):
                continue
            out.append(acc)
        # Rotate: the account that sent least recently goes first.
        last = {r[0]: r[1] for r in self.conn.execute("SELECT account_id, MAX(ts) FROM send_log GROUP BY account_id")}
        out.sort(key=lambda a: (last.get(a["id"], 0), a["id"]))
        return out

    def modes_waiting(self, now):
        """Modes with rows waiting, oldest campaign first: [False] live, [True] dry run, or both.
        Caps are counted per mode, so a dry run that hit its cap never holds up live sending (and vice versa)."""
        rows = self.conn.execute(
            """SELECT c.dry_run, MIN(c.id) first FROM recipients r JOIN campaigns c ON c.id=r.campaign_id
               WHERE c.status='running' AND r.status='queued' AND r.next_attempt_at<=?
               GROUP BY c.dry_run ORDER BY first""", (now,)).fetchall()
        return [bool(r[0]) for r in rows]

    def claim_next(self, acc, now, dry=None):
        """Atomically pick the next queued row (sheet order) and mark it 'sending'.
        dry=True/False limits the pick to dry-run / live campaigns (caps are counted per mode)."""
        mode = "" if dry is None else f" AND c.dry_run={1 if dry else 0}"
        recent_days = db.get_setting(self.conn, "recent_days")
        while True:
            with db.tx(self.conn):
                row = self.conn.execute(
                    f"""SELECT r.*, c.dry_run AS camp_dry FROM recipients r JOIN campaigns c ON c.id=r.campaign_id
                       WHERE c.status='running' AND r.status='queued' AND r.next_attempt_at<=?{mode}
                       ORDER BY c.id, r.row_num LIMIT 1""", (now,)).fetchone()
                if row is None:
                    return None
                reason = db.suppression_reason(self.conn, row["email"])
                if not reason and db.recently_emailed(self.conn, row["email"], recent_days, row["campaign_id"],
                                                      include_dry=bool(row["camp_dry"]), now=now):
                    reason = "recent"  # another campaign emailed this driver within the last N days
                if reason:  # unsubscribed / bounced / replied / recently emailed since the campaign was prepared
                    self.conn.execute("UPDATE recipients SET status='skipped', skip_reason=? WHERE id=?",
                                      (reason, row["id"]))
                    queue_mark(self.conn, row["campaign_id"], row["id"], row["row_num"], list_email(row),
                               skip_note(reason, recent_days) or "Not sent - unsubscribed")
                    continue
                mid = compose.new_message_id()
                self.conn.execute("UPDATE recipients SET status='sending', account_id=?, message_id=?, sending_at=?, "
                                  "attempts=attempts+1 WHERE id=?", (acc["id"], mid, now, row["id"]))
                return self.conn.execute("SELECT * FROM recipients WHERE id=?", (row["id"],)).fetchone()

    def tick(self, now=None) -> bool:
        """Send at most one email. Returns True if something was sent or attempted."""
        now = now or time.time()
        self.reconcile(now)
        self.finish_done_campaigns()
        window = db.get_setting(self.conn, "window")
        if not config.dry_run_fast() and not timeutil.in_window(now, window):
            return False
        for dry in self.modes_waiting(now):
            for acc in self.usable_accounts(now, dry=dry):   # accounts without a sender name are excluded here
                row = self.claim_next(acc, now, dry=dry)
                if row is None:
                    break                                    # everything left in this mode was skipped
                self.send_one(acc, row, now)
                return True
        return False

    def send_one(self, acc, row, now):
        c = self.conn.execute("SELECT * FROM campaigns WHERE id=?", (row["campaign_id"],)).fetchone()
        vars_ = {"first_name": row["first_name"], "last_name": row["last_name"], "name": row["name"],
                 "position": row["position"]}
        unsub = compose.unsub_url(row["id"], row["email"])
        try:
            msg = compose.build_message(acc["email"], acc["display_name"], row["email"], row["name"], c["subject"],
                                        c["body"], vars_, unsub, row["message_id"])
        except Exception as e:  # nothing was sent: don't leave the row stuck in 'sending'
            log.exception("Could not build the email for row %s", row["id"])
            self.handle_error(acc, row, smtp_send.SendError("permanent", f"could not build the email ({e})"), now)
            return
        try:
            if c["dry_run"]:
                p = self._eml_path(row)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(bytes(msg))
            else:
                smtp_send.send(acc["email"], self._password(acc), msg, smtp_factory=self.smtp_factory)
        except smtp_send.SendError as e:
            self.handle_error(acc, row, e, now)
            return
        except Exception as e:  # unexpected: treat as uncertain so the IMAP check decides
            log.exception("Unexpected error sending row %s", row["id"])
            e2 = smtp_send.SendError("uncertain", str(e))
            self.handle_error(acc, row, e2, now)
            return
        with db.tx(self.conn):
            self._mark_sent(row, acc["id"], now)
            self.conn.execute("UPDATE accounts SET next_send_at=?, last_error=NULL WHERE id=?",
                              (now + self._gap(), acc["id"]))
        log.info("Sent row %s (campaign %s) via %s", row["row_num"], row["campaign_id"], acc["email"])

    def handle_error(self, acc, row, e: smtp_send.SendError, now):
        kind, reason = e.kind, smtp_send.short_reason(e.message)
        log.warning("Send error [%s] row %s via %s: %s", kind, row["id"], acc["email"], e.message)
        requeue = ("UPDATE recipients SET status='queued', message_id=NULL, account_id=NULL, "
                   "attempts=MAX(attempts-1,0), next_attempt_at=? WHERE id=?")
        with db.tx(self.conn):
            self.conn.execute("UPDATE accounts SET last_error=? WHERE id=?", (f"{kind}: {e.message[:300]}", acc["id"]))
            if kind == "quota":
                self.conn.execute("UPDATE accounts SET paused_until=? WHERE id=?", (now + caps.DAY, acc["id"]))
                self.conn.execute(requeue, (now, row["id"]))
                db.add_event(self.conn, "error", f"{acc['email']} hit Gmail's sending limit. It is stopped for 24 hours; "
                             f"the other accounts continue. Gmail said: {reason}", account_id=acc["id"])
            elif kind == "auth":
                self.conn.execute("UPDATE accounts SET disabled_reason=? WHERE id=?", (reason, acc["id"]))
                self.conn.execute(requeue, (now, row["id"]))
                db.add_event(self.conn, "error", f"{acc['email']} was STOPPED: Gmail refused the login or flagged the "
                             f"account ({reason}). Sign in to this Gmail in a browser, fix any warning, create a new App "
                             f"Password, then re-enable it in Settings.", account_id=acc["id"])
            elif kind == "network":
                self.conn.execute(requeue, (now + 300, row["id"]))
                self.conn.execute("UPDATE accounts SET next_send_at=? WHERE id=?", (now + 300, acc["id"]))
            elif kind == "uncertain":
                # Leave in 'sending': reconcile() will check Sent Mail after RECONCILE_AFTER seconds.
                self.conn.execute("UPDATE accounts SET next_send_at=? WHERE id=?", (now + self._gap(), acc["id"]))
            elif kind == "temporary" and row["attempts"] < MAX_ATTEMPTS:
                backoff = 600 * (2 ** (row["attempts"] - 1))
                self.conn.execute("UPDATE recipients SET status='queued', message_id=NULL, account_id=NULL, "
                                  "next_attempt_at=?, error=? WHERE id=?", (now + backoff, reason, row["id"]))
                self.conn.execute("UPDATE accounts SET next_send_at=? WHERE id=?", (now + self._gap(), acc["id"]))
            else:
                self.conn.execute("UPDATE recipients SET status='failed', error=? WHERE id=?", (reason, row["id"]))
                queue_mark(self.conn, row["campaign_id"], row["id"], row["row_num"], list_email(row), error_note(reason))
                self.conn.execute("UPDATE accounts SET next_send_at=? WHERE id=?", (now + self._gap(), acc["id"]))

    def finish_done_campaigns(self):
        for c in self.conn.execute("SELECT id FROM campaigns WHERE status='running'").fetchall():
            left = self.conn.execute("SELECT COUNT(*) FROM recipients WHERE campaign_id=? AND status IN "
                                     "('queued','sending')", (c["id"],)).fetchone()[0]
            if left == 0:
                with db.tx(self.conn):
                    self.conn.execute("UPDATE campaigns SET status='done', finished_at=? WHERE id=?",
                                      (time.time(), c["id"]))
                    db.add_event(self.conn, "info", "Campaign finished: every driver on the list was handled.", c["id"])


def run(stop_event):
    s = Sender()
    log.info("Sender started (dry_run=%s)", config.dry_run())
    s.reconcile(force=True)
    while not stop_event.is_set():
        try:
            did = s.tick()
        except Exception:
            log.exception("Sender tick failed")
            did = False
        stop_event.wait(1 if did else 10)
