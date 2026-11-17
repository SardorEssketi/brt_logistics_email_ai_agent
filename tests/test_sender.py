"""Sender: sending, rotation, caps, window, SMTP error handling, crash-resume with no double sends."""
import json
import smtplib
import time

import pytest

from core import db, smtp_send
from tests.conftest import FakeSMTP, add_account
from worker.sender import Sender


def setup_campaign(conn, emails_, dry=0):
    cur = conn.execute("INSERT INTO campaigns(name,created_at,status,source_type,source_ref,tab,header_row,mapping,"
                       "notes_col,subject,body,dry_run) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("t", time.time(), "running", "gsheet", "mock://s", "S", 1, json.dumps({"email": 1, "name": 2}),
                        3, "CDL-A job for {first_name}", "Hi {first_name},\n\nWe pay $0.65 CPM.", dry))
    cid = cur.lastrowid
    for i, e in enumerate(emails_):
        conn.execute("INSERT INTO recipients(campaign_id,row_num,email,raw_email,name,first_name,status) "
                     "VALUES(?,?,?,?,?,?, 'queued')", (cid, i + 2, e, e, f"Driver {i}", f"Driver{i}"))
    return cid


def make_sender(conn, sent, errors=None, checker=None, login_error=None):
    return Sender(conn=conn, smtp_factory=lambda: FakeSMTP(sent, errors, login_error),
                  sent_checker=checker or (lambda acc, row: False))


def drain(s, conn, now, n=50):
    """Run ticks, ignoring the per-account gap (we advance the clock)."""
    t = now
    for _ in range(n):
        conn.execute("UPDATE accounts SET next_send_at=0")
        s.tick(t)
        t += 1
    return t


def test_sends_once_each_and_marks(conn):
    add_account(conn, "a@gmail.com")
    add_account(conn, "b@gmail.com")
    cid = setup_campaign(conn, [f"d{i}@gmail.com" for i in range(5)])
    sent = []
    s = make_sender(conn, sent)
    drain(s, conn, time.time())
    assert sorted(m["To"].addresses[0].addr_spec for m in sent) == [f"d{i}@gmail.com" for i in range(5)]
    msg = sent[0]
    assert msg["Subject"] == "CDL-A job for Driver0"
    assert "List-Unsubscribe" in msg and "List-Unsubscribe-Post" in msg
    assert msg["List-Unsubscribe"].startswith("<mailto:") and "/u/" in msg["List-Unsubscribe"]
    assert msg.get_content_type() == "multipart/alternative"
    text = msg.get_body(("plain",)).get_content()
    # the user's text, then ONLY the unsubscribe line
    assert text.startswith("Hi Driver0,\n\nWe pay $0.65 CPM.\n\nNot interested? Reply 'unsubscribe' or click: https://")
    assert text.strip().count("\n") == 4 and "/u/" in text.strip().splitlines()[-1]
    html = msg.get_body(("html",)).get_content()
    assert "unsubscribe" in html and "<img" not in html
    assert len(msg["To"].addresses) == 1 and msg["Cc"] is None and msg["Bcc"] is None
    # rotation used both accounts
    assert {r[0] for r in conn.execute("SELECT DISTINCT account_id FROM recipients")} == {1, 2}
    notes = [r[0] for r in conn.execute("SELECT note FROM marks WHERE campaign_id=?", (cid,))]
    assert len(notes) == 5 and all(n.startswith("Sent ") for n in notes)
    assert conn.execute("SELECT status FROM campaigns").fetchone()[0] == "done"


def test_gap_and_cap(conn):
    aid = add_account(conn, "a@gmail.com", added_days_ago=0)   # week 1 => cap 20
    setup_campaign(conn, [f"d{i}@gmail.com" for i in range(25)])
    sent = []
    s = make_sender(conn, sent)
    now = time.time()
    s.tick(now)
    s.tick(now + 1)       # gap not elapsed -> nothing
    assert len(sent) == 1
    nxt = conn.execute("SELECT next_send_at FROM accounts WHERE id=?", (aid,)).fetchone()[0]
    assert 90 <= nxt - now <= 300
    drain(s, conn, now + 400, 40)
    assert len(sent) == 20  # rolling-24h cap reached
    drain(s, conn, now + 86400 + 500, 10)
    assert len(sent) == 25


def test_outside_window_waits(conn):
    add_account(conn)
    db.set_setting(conn, "window", {"days": [], "start": "09:00", "end": "16:00"})
    setup_campaign(conn, ["d@gmail.com"])
    sent = []
    make_sender(conn, sent).tick(time.time())
    assert sent == []


def test_account_without_sender_name_is_blocked_alone(conn):
    a = add_account(conn, "noname@gmail.com", name="")
    b = add_account(conn, "named@gmail.com", name="Mike - ABC Logistics")
    setup_campaign(conn, ["d1@gmail.com", "d2@gmail.com"])
    sent = []
    s = make_sender(conn, sent)
    drain(s, conn, time.time(), 5)
    assert len(sent) == 2 and all(m["From"].addresses[0].addr_spec == "named@gmail.com" for m in sent)
    assert str(sent[0]["From"]) == "Mike - ABC Logistics <named@gmail.com>"


def test_no_named_account_sends_nothing(conn):
    add_account(conn, "noname@gmail.com", name="  ")
    setup_campaign(conn, ["d1@gmail.com"])
    sent = []
    make_sender(conn, sent).tick(time.time())
    assert sent == [] and conn.execute("SELECT status FROM recipients").fetchone()[0] == "queued"


def test_suppressed_after_prepare_is_skipped(conn):
    add_account(conn)
    cid = setup_campaign(conn, ["gone@gmail.com", "ok@gmail.com"])
    db.suppress(conn, "gone@gmail.com", "unsubscribed")
    sent = []
    drain(make_sender(conn, sent), conn, time.time(), 5)
    assert [m["To"].addresses[0].addr_spec for m in sent] == ["ok@gmail.com"]
    assert conn.execute("SELECT note FROM marks WHERE email='gone@gmail.com'").fetchone()[0] == "Not sent - unsubscribed"


def test_quota_error_pauses_account_24h(conn):
    a = add_account(conn, "a@gmail.com")
    b = add_account(conn, "b@gmail.com")
    setup_campaign(conn, ["d1@gmail.com", "d2@gmail.com"])
    err = smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded.")
    sent = []
    s = make_sender(conn, sent, errors=[err])
    now = time.time()
    s.tick(now)
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (a,)).fetchone()
    assert acc["paused_until"] >= now + 86000
    assert conn.execute("SELECT status FROM recipients WHERE row_num=2").fetchone()[0] == "queued"
    drain(s, conn, now + 10, 5)
    assert len(sent) == 2 and all(r[0] == b for r in conn.execute("SELECT account_id FROM recipients"))
    assert conn.execute("SELECT COUNT(*) FROM events WHERE level='error'").fetchone()[0] == 1


def test_auth_error_stops_account(conn):
    a = add_account(conn)
    setup_campaign(conn, ["d1@gmail.com"])
    sent = []
    s = make_sender(conn, sent, login_error=smtplib.SMTPAuthenticationError(
        535, b"5.7.8 Username and Password not accepted"))
    s.tick(time.time())
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (a,)).fetchone()
    assert acc["disabled_reason"]
    assert conn.execute("SELECT status, attempts FROM recipients").fetchone()[:] == ("queued", 0)
    drain(s, conn, time.time() + 100000, 3)
    assert sent == []  # never retried automatically


def test_temporary_error_retries_then_fails(conn):
    add_account(conn)
    cid = setup_campaign(conn, ["d1@gmail.com"])
    tmp = lambda: smtplib.SMTPDataError(451, b"4.3.0 Temporary server error. Please try again later")
    sent = []
    s = make_sender(conn, sent, errors=[tmp(), tmp(), tmp()])
    now = time.time()
    for i in range(3):
        conn.execute("UPDATE accounts SET next_send_at=0")
        conn.execute("UPDATE recipients SET next_attempt_at=0 WHERE status='queued'")
        s.tick(now + i * 5000)
    r = conn.execute("SELECT * FROM recipients").fetchone()
    assert r["status"] == "failed" and r["attempts"] == 3
    note = conn.execute("SELECT note FROM marks").fetchone()[0]
    assert note.startswith("Not sent - error: ") and "Temporary server error" in note


def test_temporary_error_then_success(conn):
    add_account(conn)
    setup_campaign(conn, ["d1@gmail.com"])
    sent = []
    s = make_sender(conn, sent, errors=[smtplib.SMTPDataError(421, b"4.7.0 Try again later")])
    now = time.time()
    s.tick(now)
    r = conn.execute("SELECT * FROM recipients").fetchone()
    assert r["status"] == "queued" and r["next_attempt_at"] > now + 500
    conn.execute("UPDATE accounts SET next_send_at=0")
    s.tick(r["next_attempt_at"] + 1)
    assert len(sent) == 1


def test_crash_resume_no_double_send(conn):
    """A row left in 'sending' after a crash is looked up in Sent Mail. Found -> marked sent, never resent."""
    add_account(conn)
    setup_campaign(conn, ["was-sent@gmail.com", "not-sent@gmail.com", "later@gmail.com"])
    t0 = time.time() - 600
    conn.execute("UPDATE recipients SET status='sending', account_id=1, message_id='<m1@x>', sending_at=?, attempts=1 "
                 "WHERE row_num=2", (t0,))
    conn.execute("UPDATE recipients SET status='sending', account_id=1, message_id='<m2@x>', sending_at=?, attempts=1 "
                 "WHERE row_num=3", (t0,))
    in_sent_folder = {"<m1@x>"}
    checked = []

    def checker(acc, row):
        checked.append(row["message_id"])
        return row["message_id"] in in_sent_folder
    sent = []
    s = make_sender(conn, sent, checker=checker)     # "restart"
    s.reconcile(force=True)
    assert sorted(checked) == ["<m1@x>", "<m2@x>"]
    drain(s, conn, time.time(), 10)
    to = [m["To"].addresses[0].addr_spec for m in sent]
    assert "was-sent@gmail.com" not in to
    assert sorted(to) == ["later@gmail.com", "not-sent@gmail.com"]
    assert conn.execute("SELECT COUNT(*) FROM send_log").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM marks WHERE note LIKE 'Sent %'").fetchone()[0] == 3


def test_crash_resume_imap_down_never_resends(conn):
    add_account(conn)
    setup_campaign(conn, ["x@gmail.com"])
    conn.execute("UPDATE recipients SET status='sending', account_id=1, message_id='<m@x>', sending_at=?, attempts=1",
                 (time.time() - 600,))

    def broken(acc, row):
        raise OSError("imap down")
    sent = []
    s = make_sender(conn, sent, checker=broken)
    s.reconcile(force=True)
    drain(s, conn, time.time(), 5)
    assert sent == [] and conn.execute("SELECT status FROM recipients").fetchone()[0] == "sending"


def test_dropped_connection_during_send_is_checked_not_resent(conn):
    add_account(conn)
    setup_campaign(conn, ["x@gmail.com"])
    sent = []
    s = make_sender(conn, sent, errors=[smtplib.SMTPServerDisconnected("Connection unexpectedly closed")],
                    checker=lambda acc, row: True)   # Gmail actually got it
    now = time.time()
    s.tick(now)
    assert conn.execute("SELECT status FROM recipients").fetchone()[0] == "sending"
    conn.execute("UPDATE accounts SET next_send_at=0")
    s.tick(now + 200)  # reconcile runs: found in Sent Mail
    assert conn.execute("SELECT status FROM recipients").fetchone()[0] == "sent"
    assert sent == []


def test_dry_run_writes_eml(conn, tmp_path):
    add_account(conn)
    setup_campaign(conn, ["x@gmail.com"], dry=1)
    s = Sender(conn=conn, smtp_factory=lambda: pytest.fail("SMTP used in dry run"), outbox=tmp_path / "out")
    s.tick(time.time())
    files = list((tmp_path / "out").rglob("*.eml"))
    assert len(files) == 1 and b"x@gmail.com" in files[0].read_bytes()
    assert conn.execute("SELECT status FROM recipients").fetchone()[0] == "sent"


# ---------------- classification of SMTP errors
@pytest.mark.parametrize("code,text,kind", [
    (550, "5.4.5 Daily user sending limit exceeded", "quota"),
    (421, "4.7.0 Temporary System Problem. Try again later", "temporary"),
    (452, "4.5.3 Your message has too many recipients", "quota"),
    (535, "5.7.8 Username and Password not accepted", "auth"),
    (534, "5.7.9 Application-specific password required", "auth"),
    (554, "5.7.0 Your account has been disabled", "auth"),
    (550, "5.1.1 The email account that you tried to reach does not exist", "permanent"),
])
def test_smtp_error_classification(code, text, kind):
    assert smtp_send.classify(code, text) == kind


def test_short_reason():
    assert smtp_send.short_reason("5.1.1 The email account does not exist. Learn more at https://x") == \
        "The email account does not exist."
