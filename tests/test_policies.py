"""Policies added after the first review:
1. skip drivers any campaign emailed in the last N days (setting), 2. dry runs never use up the real daily cap,
3. unsubscribe link needs a Confirm click (Gmail one-click POST still works), 4. login lockout is per IP only."""
import time

import pytest
from fastapi.testclient import TestClient

from app import auth
from core import caps, campaigns, crypto, db, emails
from tests.conftest import add_account
from tests.test_sender import drain, make_sender, setup_campaign
from worker.sender import Sender

DAY = 86400


def addrs(sent):
    return [m["To"].addresses[0].addr_spec for m in sent]


# ---------------------------------------------------------------- 1. repeat emails across campaigns
def test_driver_on_two_running_campaigns_is_emailed_once(conn):
    add_account(conn, "a@gmail.com")
    c1 = setup_campaign(conn, ["carlos@gmail.com"])
    c2 = setup_campaign(conn, ["carlos@gmail.com", "nina@gmail.com"])
    sent = []
    drain(make_sender(conn, sent), conn, time.time())
    assert sorted(addrs(sent)) == ["carlos@gmail.com", "nina@gmail.com"]
    r = conn.execute("SELECT status, skip_reason FROM recipients WHERE campaign_id=? AND email='carlos@gmail.com'",
                     (c2,)).fetchone()
    assert (r["status"], r["skip_reason"]) == ("skipped", "recent")
    note = conn.execute("SELECT note FROM marks WHERE campaign_id=? AND email='carlos@gmail.com'", (c2,)).fetchone()
    assert note["note"] == "Not sent - emailed in last 60 days"
    assert conn.execute("SELECT status FROM recipients WHERE campaign_id=?", (c1,)).fetchone()[0] == "sent"


def test_repeat_window_is_a_setting_and_0_turns_it_off(conn):
    add_account(conn, "a@gmail.com")
    db.set_setting(conn, "recent_days", 0)
    setup_campaign(conn, ["carlos@gmail.com"])
    setup_campaign(conn, ["carlos@gmail.com"])
    sent = []
    drain(make_sender(conn, sent), conn, time.time())
    assert addrs(sent) == ["carlos@gmail.com", "carlos@gmail.com"]


def test_note_uses_the_configured_number_of_days(conn):
    add_account(conn, "a@gmail.com")
    db.set_setting(conn, "recent_days", 30)
    setup_campaign(conn, ["carlos@gmail.com"])
    c2 = setup_campaign(conn, ["carlos@gmail.com"])
    drain(make_sender(conn, []), conn, time.time())
    assert conn.execute("SELECT note FROM marks WHERE campaign_id=?", (c2,)).fetchone()[0] == \
        "Not sent - emailed in last 30 days"


def test_older_than_the_window_is_emailed_again(conn):
    add_account(conn, "a@gmail.com")
    old = setup_campaign(conn, [])
    conn.execute("INSERT INTO recipients(campaign_id,row_num,email,raw_email,status,sent_at) "
                 "VALUES(?,2,'carlos@gmail.com','carlos@gmail.com','sent',?)", (old, time.time() - 61 * DAY))
    setup_campaign(conn, ["carlos@gmail.com"])
    sent = []
    drain(make_sender(conn, sent), conn, time.time())
    assert addrs(sent) == ["carlos@gmail.com"]


def test_dry_run_sends_never_block_live_sending(conn, tmp_path):
    add_account(conn, "a@gmail.com")
    setup_campaign(conn, ["carlos@gmail.com"], dry=1)
    live = setup_campaign(conn, ["carlos@gmail.com"], dry=0)
    sent = []
    s = Sender(conn=conn, smtp_factory=make_sender(conn, sent).smtp_factory, sent_checker=lambda a, r: False,
               outbox=tmp_path / "out")
    drain(s, conn, time.time())
    assert addrs(sent) == ["carlos@gmail.com"]                     # the live one really went out
    assert len(list((tmp_path / "out").rglob("*.eml"))) == 1        # the dry one was only saved
    assert conn.execute("SELECT status FROM recipients WHERE campaign_id=?", (live,)).fetchone()[0] == "sent"


def test_campaign_preparation_skips_recently_emailed(conn):
    other = setup_campaign(conn, [])
    conn.execute("INSERT INTO recipients(campaign_id,row_num,email,raw_email,status,sent_at) "
                 "VALUES(?,2,'carlos@gmail.com','carlos@gmail.com','sent',?)", (other, time.time() - 5 * DAY))
    cid = setup_campaign(conn, [])
    grid = [["Email", "Name"], ["carlos@gmail.com", "Carlos"], ["nina@gmail.com", "Nina"]]
    campaigns.build_recipients(conn, cid, grid, 1, {"email": 1, "name": 2}, 3, True, mx_checker=lambda d: True)
    got = {r["email"]: (r["status"], r["skip_reason"]) for r in
           conn.execute("SELECT * FROM recipients WHERE campaign_id=?", (cid,))}
    assert got == {"carlos@gmail.com": ("skipped", "recent"), "nina@gmail.com": ("queued", None)}
    campaigns.queue_skip_marks(conn, cid)
    assert conn.execute("SELECT note FROM marks WHERE campaign_id=?", (cid,)).fetchone()[0] == \
        "Not sent - emailed in last 60 days"


@pytest.fixture
def client(conn, monkeypatch):
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", crypto.hash_password("s3cret-pass"))
    monkeypatch.setattr(emails, "has_mail_server", lambda d: True)
    from app.main import app
    c = TestClient(app)
    return c


def login(c):
    assert c.post("/login", data={"username": "admin", "password": "s3cret-pass"},
                  follow_redirects=False).status_code == 303


def test_repeat_days_setting_page(client, conn):
    login(client)
    assert 'name="recent_days" min="0" max="365" value="60"' in client.get("/settings").text
    client.post("/settings/recent-days", data={"recent_days": "30"})
    assert db.get_setting(conn, "recent_days") == 30
    for bad in ("abc", "400", "-1", ""):
        r = client.post("/settings/recent-days", data={"recent_days": bad}, follow_redirects=False)
        assert "err=" in r.headers["location"], bad
    assert db.get_setting(conn, "recent_days") == 30


# ---------------------------------------------------------------- 2. dry runs and the real daily cap
def test_dry_run_sends_do_not_use_up_the_real_cap(conn, tmp_path):
    acc = add_account(conn, "a@gmail.com")
    conn.execute("UPDATE accounts SET cap_override=1")
    setup_campaign(conn, ["d1@gmail.com", "d2@gmail.com"], dry=1)
    sent = []
    s = Sender(conn=conn, smtp_factory=make_sender(conn, sent).smtp_factory, sent_checker=lambda a, r: False,
               outbox=tmp_path / "out")
    now = time.time()
    drain(s, conn, now)
    assert caps.sent_last_24h(conn, acc, now + 60, dry=True) == 1   # the dry run is still paced by the cap
    assert caps.sent_last_24h(conn, acc, now + 60) == 0             # ...but the real cap is untouched
    setup_campaign(conn, ["live@gmail.com"], dry=0)
    drain(s, conn, now + 100)
    assert addrs(sent) == ["live@gmail.com"]                       # live sending still has its full cap
    assert caps.sent_last_24h(conn, acc, now + 200) == 1


def _draft(conn):
    cid = conn.execute("INSERT INTO campaigns(name,created_at,status,source_type,source_ref,subject,body) "
                       "VALUES('t',?,'draft','csv','x','Hi','Hello {first_name}, $0.65 CPM')", (time.time(),)).lastrowid
    conn.execute("INSERT INTO recipients(campaign_id,row_num,email,raw_email,name,first_name,status) "
                 "VALUES(?,2,'driver@gmail.com','driver@gmail.com','Joe Driver','Joe','queued')", (cid,))
    return cid


def test_test_send_is_really_sent_even_in_dry_run(client, conn, monkeypatch):
    from core import smtp_send
    monkeypatch.setenv("DRY_RUN", "1")
    acc = add_account(conn, "a@gmail.com")
    login(client)
    cid = _draft(conn)
    mails = []
    monkeypatch.setattr(smtp_send, "send", lambda acc_email, pw, msg, **k: mails.append(msg))
    r = client.post(f"/api/campaigns/{cid}/test-send", data={"to": "me@example.com"}).json()
    assert r["ok"] and "Sent from a@gmail.com to me@example.com" in r["message"]
    assert len(mails) == 1 and mails[0]["To"] == "me@example.com"           # only to the typed address
    assert "Hello Joe" in mails[0].get_body(("plain",)).get_content()        # personalised with a real row
    assert caps.sent_last_24h(conn, acc) == 1                                 # a real email: real cap
    # the campaign itself is untouched: the driver was not emailed
    assert conn.execute("SELECT status FROM recipients WHERE campaign_id=?", (cid,)).fetchone()[0] == "queued"


def test_test_send_with_rejected_app_password_says_so(client, conn, monkeypatch):
    from core import smtp_send
    add_account(conn, "a@gmail.com")
    login(client)
    cid = _draft(conn)
    def refuse(*a, **k):
        raise smtp_send.SendError("login_dropped", "Gmail closed the connection at login")
    monkeypatch.setattr(smtp_send, "send", refuse)
    r = client.post(f"/api/campaigns/{cid}/test-send", data={"to": "me@example.com"}).json()
    assert not r["ok"] and "did not accept the App Password" in r["message"]


def test_old_database_gets_the_dry_column(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    old = sqlite3.connect(p)
    old.execute("CREATE TABLE send_log(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, recipient_id INTEGER, "
                "ts REAL NOT NULL, kind TEXT)")
    old.execute("INSERT INTO send_log(account_id,ts,kind) VALUES(1,?,'campaign')", (time.time(),))
    old.commit(); old.close()
    c = db.connect(p)
    assert "dry" in {r[1] for r in c.execute("PRAGMA table_info(send_log)")}
    assert caps.sent_last_24h(c, 1) == 1                            # existing sends still count as real


# ---------------------------------------------------------------- 3. unsubscribe confirmation
def _sent_recipient(conn):
    cid = setup_campaign(conn, ["joe@gmail.com"])
    conn.execute("UPDATE recipients SET status='sent', sent_at=? WHERE campaign_id=?", (time.time(), cid))
    r = conn.execute("SELECT * FROM recipients WHERE campaign_id=?", (cid,)).fetchone()
    return r, crypto.make_unsub_token(r["id"], r["email"])


def test_opening_unsubscribe_link_changes_nothing(client, conn):
    r, token = _sent_recipient(conn)
    for _ in range(3):  # e.g. a mail scanner opening the link
        page = client.get(f"/u/{token}")
        assert page.status_code == 200 and "Confirm unsubscribe" in page.text
    assert db.suppression_reason(conn, r["email"]) is None
    assert 'action="/u/' in page.text and 'name="confirm"' in page.text


def test_confirm_button_unsubscribes(client, conn):
    r, token = _sent_recipient(conn)
    page = client.post(f"/u/{token}", data={"confirm": "1"})
    assert page.status_code == 200 and "You're unsubscribed" in page.text
    assert db.suppression_reason(conn, r["email"]) == "unsubscribed"


def test_gmail_one_click_post_still_unsubscribes(client, conn):
    r, token = _sent_recipient(conn)
    resp = client.post(f"/u/{token}", content=b"List-Unsubscribe=One-Click",
                       headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://mail.google.com"})
    assert resp.status_code == 200 and resp.text == "Unsubscribed"
    assert db.suppression_reason(conn, r["email"]) == "unsubscribed"


def test_forged_unsubscribe_post_changes_nothing(client, conn):
    r, token = _sent_recipient(conn)
    resp = client.post(f"/u/{r['id']}.forgedsignature", data={"confirm": "1"})
    assert resp.status_code == 400 and db.suppression_reason(conn, r["email"]) is None


# ---------------------------------------------------------------- 4. login lockout per IP only
def test_other_ips_failing_never_lock_you_out(conn):
    for i in range(200):
        auth.record_failure(conn, f"10.0.{i // 250}.{i % 250}")
    assert not auth.too_many_attempts(conn, "203.0.113.50")


def test_one_ip_is_locked_after_5_failures(conn):
    for _ in range(4):
        auth.record_failure(conn, "1.2.3.4")
    assert not auth.too_many_attempts(conn, "1.2.3.4")
    auth.record_failure(conn, "1.2.3.4")
    assert auth.too_many_attempts(conn, "1.2.3.4")
    assert not auth.too_many_attempts(conn, "5.6.7.8")


# ---------------------------------------------------------------- Gmail hanging up at login
def test_hangup_at_login_is_its_own_error_kind():
    import smtplib
    from core import smtp_send
    from tests.conftest import FakeSMTP
    msg = __import__("email.message", fromlist=["EmailMessage"]).EmailMessage()
    with pytest.raises(smtp_send.SendError) as e:
        smtp_send.send("a@gmail.com", "pw", msg, smtp_factory=lambda: FakeSMTP(
            [], login_error=smtplib.SMTPServerDisconnected("Connection unexpectedly closed")))
    assert e.value.kind == "login_dropped"


def test_hangup_at_login_rests_1h_then_stops_the_account(conn):
    import smtplib
    acc = add_account(conn, "a@gmail.com")
    cid = setup_campaign(conn, ["d1@gmail.com"])
    sent = []
    s = make_sender(conn, sent, login_error=smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
    now = time.time()
    s.tick(now)                                                    # 1st hang-up
    a = conn.execute("SELECT * FROM accounts WHERE id=?", (acc,)).fetchone()
    assert caps.account_state(a, now + 60) == "paused" and a["paused_until"] == pytest.approx(now + 3600)
    assert a["disabled_reason"] is None
    r = conn.execute("SELECT status, message_id FROM recipients WHERE campaign_id=?", (cid,)).fetchone()
    assert r["status"] == "queued" and r["message_id"] is None    # nothing sent, nothing lost
    assert conn.execute("SELECT level FROM events WHERE account_id=?", (acc,)).fetchone()[0] == "warn"
    for t in (now + 60, now + 1800):                               # resting: no more login attempts
        conn.execute("UPDATE accounts SET next_send_at=0")
        assert s.tick(t) is False
    conn.execute("UPDATE accounts SET next_send_at=0")
    s.tick(now + 3700)                                             # 2nd hang-up in a row
    a = conn.execute("SELECT * FROM accounts WHERE id=?", (acc,)).fetchone()
    assert caps.account_state(a) == "disabled" and "App Password" in a["disabled_reason"]
    assert sent == []
    assert conn.execute("SELECT status FROM recipients WHERE campaign_id=?", (cid,)).fetchone()[0] == "queued"


def test_a_successful_send_resets_the_hangup_count(conn):
    import smtplib
    acc = add_account(conn, "a@gmail.com")
    setup_campaign(conn, ["d1@gmail.com", "d2@gmail.com", "d3@gmail.com"])
    now = time.time()
    bad = make_sender(conn, [], login_error=smtplib.SMTPServerDisconnected("closed"))
    bad.tick(now)                                                  # hang-up: rest 1 h
    good_sent = []
    good = make_sender(conn, good_sent)
    conn.execute("UPDATE accounts SET next_send_at=0")
    good.tick(now + 3700)                                          # works again: clears the count
    conn.execute("UPDATE accounts SET next_send_at=0, paused_until=NULL")
    bad.tick(now + 3800)                                           # a single new hang-up only rests again
    a = conn.execute("SELECT * FROM accounts WHERE id=?", (acc,)).fetchone()
    assert len(good_sent) == 1 and a["disabled_reason"] is None and a["paused_until"] > now + 3800
