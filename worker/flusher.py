"""Writes queued NOTES marks to Google Sheets / the server copy of Excel-CSV files, in batches, with backoff."""
import logging
import time

from core import db
from core.marks import flush_campaign

log = logging.getLogger("flusher")
INTERVAL = 30


class Flusher:
    def __init__(self, conn=None):
        self.conn = conn or db.connect()
        self.retry_at = {}   # campaign_id -> ts
        self.failures = {}

    def flush_all(self, now=None):
        now = now or time.time()
        cids = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT campaign_id FROM marks WHERE written_at IS NULL AND unmatched=0")]
        for cid in cids:
            if self.retry_at.get(cid, 0) > now:
                continue
            c = self.conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
            try:
                res = flush_campaign(self.conn, c)
                self.failures.pop(cid, None)
                self.retry_at.pop(cid, None)
                if res["written"]:
                    log.info("Campaign %s: wrote %s note(s)", cid, res["written"])
            except Exception as e:
                n = self.failures.get(cid, 0) + 1
                self.failures[cid] = n
                self.retry_at[cid] = now + min(1800, 30 * 2 ** n)
                log.warning("Campaign %s: writing notes failed (try %s): %s", cid, n, e)
                if n in (3, 10, 30):
                    db.add_event(self.conn, "warn", "Couldn't write notes to the list yet — they are saved and will be "
                                 f"retried automatically. Reason: {str(e)[:200]}", cid)


def run(stop_event):
    f = Flusher()
    log.info("Notes flusher started")
    while not stop_event.is_set():
        try:
            f.flush_all()
        except Exception:
            log.exception("Flusher pass failed")
        stop_event.wait(INTERVAL)
