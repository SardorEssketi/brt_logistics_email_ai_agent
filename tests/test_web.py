"""Web flow through the real FastAPI app: login, settings, upload → map → compose → review → start, unsubscribe."""
import io
import re
import time

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from core import crypto, db, emails
from tests.conftest import add_account
from tests.test_lists import ROWS, make_xlsx


@pytest.fixture
def client(conn, monkeypatch):
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", crypto.hash_password("s3cret-pass"))
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setattr(emails, "has_mail_server", lambda d: d != "nomx.com")
    from app.main import app
    return TestClient(app)


def login(client):
    r = client.post("/login", data={"username": "admin", "password": "s3cret-pass"}, follow_redirects=False)
    assert r.status_code == 303


def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/campaigns/1/stats").status_code == 401
    assert client.get("/healthz").status_code == 200


def test_login_rate_limit(client):
    for _ in range(5):
        client.post("/login", data={"username": "admin", "password": "nope"})
    r = client.post("/login", data={"username": "admin", "password": "s3cret-pass"}, follow_redirects=False)
    assert r.status_code == 200 and "Too many attempts" in r.text


def test_cross_site_post_blocked(client):
    login(client)
    r = client.post("/settings/test-inbox", data={"test_inbox": "x@y.com"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_full_flow_xlsx(client, conn, tmp_path, monkeypatch):
    login(client)
    add_account(conn, "rec1@gmail.com")
    db.suppress(conn, "unsub@gmail.com", "unsubscribed")
    db.suppress(conn, "bounced@gmail.com", "bounced")
    p = tmp_path / "Drivers Nov.xlsx"
    make_xlsx(p, ROWS)
    r = client.post("/campaigns/new", data={"name": "Nov"}, files={"file": ("Drivers Nov.xlsx", p.read_bytes())},
                    follow_redirects=False)
    assert r.status_code == 303
    cid = int(re.search(r"/campaigns/(\d+)/map", r.headers["location"]).group(1))

    r = client.get(f"/campaigns/{cid}/map?tab=Drivers")
    assert r.status_code == 200 and "Found automatically" in r.text
    r = client.post(f"/campaigns/{cid}/map", data={"tab": "Drivers", "header_row": "1", "col_email": "4",
                                                   "col_name": "1", "col_number": "2", "col_position": "3",
                                                   "col_notes": "5", "skip_sent": "1"}, follow_redirects=False)
    assert r.status_code == 303

    chk = client.post("/api/check", json={"subject": "Re: job", "body": "Hi"}).json()
    assert chk["blocks"]
    client.post(f"/campaigns/{cid}/compose", data={"subject": "CDL-A dry van, $0.65 CPM",
                                                   "body": "Hi {first_name},\n\nHome weekly. Call me."})
    r = client.get(f"/campaigns/{cid}/review")
    assert "drivers will get it" in r.text and "Start sending to 2 drivers" in r.text, r.text[-3000:]
    assert "joe@gmial.com" in r.text  # typo offered

    from core import smtp_send
    test_mails = []
    monkeypatch.setattr(smtp_send, "send", lambda acc, pw, msg, **k: test_mails.append(msg))
    t = client.post(f"/api/campaigns/{cid}/test-send", data={"to": "me@example.com"}).json()
    assert t["ok"] and "Sent from" in t["message"] and "still in dry run" in t["message"]
    assert [m["To"] for m in test_mails] == ["me@example.com"]   # really sent, even in dry run, only to me

    r = client.post(f"/campaigns/{cid}/start", follow_redirects=False)
    assert r.status_code == 303 and conn.execute("SELECT status FROM campaigns WHERE id=?", (cid,)).fetchone()[0] == "running"

    from worker.sender import Sender
    from worker.flusher import Flusher
    s = Sender(conn=conn)
    for i in range(5):
        conn.execute("UPDATE accounts SET next_send_at=0")
        s.tick(time.time() + i)
    Flusher(conn).flush_all()
    st = client.get(f"/api/campaigns/{cid}/stats").json()
    assert st["sent"] == 2 and st["remaining"] == 0 and st["status"] in ("running", "done")

    # Download marked copy (server copy)
    r = client.get(f"/campaigns/{cid}/download")
    ws = load_workbook(io.BytesIO(r.content))["Drivers"]
    assert ws["E2"].value.startswith("Sent ") and ws["E8"].value == "Not sent - unsubscribed"
    assert "Drivers%20Nov%20-%20marked.xlsx" in r.headers["content-disposition"]

    # "Save marks to Excel file": browser sends the original file, gets it back marked
    r = client.post(f"/api/campaigns/{cid}/mark-file", files={"file": ("Drivers Nov.xlsx", p.read_bytes())})
    ws = load_workbook(io.BytesIO(r.content))["Drivers"]
    assert ws["E3"].value.startswith("called 11/10, wants dry van | Sent ")

    # Public unsubscribe link (no login)
    rid = conn.execute("SELECT id, email FROM recipients WHERE campaign_id=? AND row_num=2", (cid,)).fetchone()
    token = crypto.make_unsub_token(rid["id"], rid["email"])
    anon = TestClient(client.app)
    r = anon.get(f"/u/{token}")                                # opening the link changes nothing
    assert "Confirm unsubscribe" in r.text and db.suppression_reason(conn, rid["email"]) is None
    r = anon.post(f"/u/{token}", data={"confirm": "1"})         # the button
    assert r.status_code == 200 and "You're unsubscribed" in r.text
    assert db.suppression_reason(conn, rid["email"]) == "unsubscribed"
    assert "You're unsubscribed" in anon.get(f"/u/{token}").text  # opening it again
    assert anon.post(f"/u/{token}").status_code == 200       # one-click POST, idempotent
    assert anon.get("/u/1.forged").status_code == 200 and "not valid" in anon.get("/u/1.forged").text
    assert "test email" in anon.get(f"/u/{crypto.make_unsub_token(0, 'me@example.com')}").text

    # pause / resume / stop
    client.post(f"/campaigns/{cid}/stop")
    log_csv = client.get(f"/campaigns/{cid}/log.csv").text
    assert "shannon1965clark@gmail.com" in log_csv


def test_map_requires_email_choice(client, conn, tmp_path):
    login(client)
    p = tmp_path / "x.csv"
    p.write_text("Who,Contact\nJoe,joe@gmail.com\n")
    r = client.post("/campaigns/new", files={"file": ("x.csv", p.read_bytes())}, follow_redirects=False)
    cid = int(re.search(r"/campaigns/(\d+)/map", r.headers["location"]).group(1))
    page = client.get(f"/campaigns/{cid}/map").text
    assert "Not found. Choose the column." in page
    r = client.post(f"/campaigns/{cid}/map", data={"tab": "CSV", "header_row": "1"}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_settings_pages(client, conn):
    login(client)
    r = client.get("/settings")
    assert "Company footer" not in r.text and "Tashkent" in r.text and "Sending rules" in r.text
    assert "Sender name" in client.get("/accounts").text
    r = client.post("/settings/caps", data={"w0": "20", "w1": "40", "w2": "60", "w3": "150"}, follow_redirects=False)
    assert "high%20risk" in r.headers["location"]
    r = client.post("/settings/caps", data={"w0": "20", "w1": "40", "w2": "60", "w3": "400"}, follow_redirects=False)
    assert "between" in r.headers["location"]
    r = client.post("/settings/accounts/add", data={"email": "new@gmail.com", "display_name": "Ann, ABC Recruiting",
                                                     "app_password": "abcd efgh ijkl mnop", "skip_test": "1"},
                    follow_redirects=False)
    acc = conn.execute("SELECT * FROM accounts WHERE email='new@gmail.com'").fetchone()
    assert acc and crypto.decrypt(acc["password_enc"]) == "abcdefghijklmnop"
    assert "abcdefghijklmnop" not in acc["password_enc"]
    # added without a sender name: saved, but blocked until it has one
    r = client.post("/settings/accounts/add", data={"email": "nameless@gmail.com", "app_password": "x" * 16,
                                                     "skip_test": "1"}, follow_redirects=False)
    assert "sender%20name" in r.headers["location"]
    aid = conn.execute("SELECT id FROM accounts WHERE email='nameless@gmail.com'").fetchone()[0]
    assert "Needs sender name" in client.get("/accounts").text
    client.post(f"/settings/accounts/{aid}/name", data={"display_name": "Mike - ABC Logistics"})
    assert conn.execute("SELECT display_name FROM accounts WHERE id=?", (aid,)).fetchone()[0] == "Mike - ABC Logistics"


def test_missing_campaign_download_is_404(client):
    login(client)
    assert client.get("/campaigns/999/download").status_code == 404
    assert client.post("/api/campaigns/999/mark-file", files={"file": ("x.xlsx", b"x")}).status_code == 404


# ---------------------------------------------------------------- redesign: new pages and routes
def test_new_pages_require_login(client):
    for url in ("/accounts", "/settings"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.post("/api/campaigns/1/preview", json={}).status_code == 401
    assert client.post("/settings/rules", data={}, follow_redirects=False).headers["location"] == "/login"


def test_first_run_checklist_then_attention(client, conn):
    login(client)
    home = client.get("/").text
    assert "Get ready to send" in home and "Add Gmail account" in home
    aid = add_account(conn, "stopped@gmail.com")
    conn.execute("UPDATE accounts SET disabled_reason='Username and Password not accepted' WHERE id=?", (aid,))
    add_account(conn, "ok@gmail.com")
    home = client.get("/").text
    assert "Get ready to send" not in home                      # an account can send now
    assert "Needs your attention" in home and f"/accounts#acc-{aid}" in home
    conn.execute("UPDATE accounts SET disabled_reason=NULL WHERE id=?", (aid,))
    assert "Needs your attention" not in client.get("/").text  # fixed -> gone, no dismissing needed


def test_sending_rules_one_form_saves_all_or_nothing(client, conn):
    login(client)
    aid = add_account(conn, "rec1@gmail.com")
    before = (db.get_setting(conn, "window"), db.get_setting(conn, "warmup"), db.get_setting(conn, "recent_days"))
    bad = {"days": ["0", "2"], "start": "10:00", "end": "15:00", "w0": "20", "w1": "40", "w2": "60", "w3": "80",
           "recent_days": "999"}
    r = client.post("/settings/rules", data=bad, follow_redirects=False)
    assert "Nothing%20was%20saved" in r.headers["location"]
    assert (db.get_setting(conn, "window"), db.get_setting(conn, "warmup"), db.get_setting(conn, "recent_days")) == before
    good = dict(bad, recent_days="30", **{f"cap_{aid}": "50"})
    r = client.post("/settings/rules", data=good, follow_redirects=False)
    assert "Sending%20rules%20saved" in r.headers["location"]
    assert db.get_setting(conn, "window") == {"days": [0, 2], "start": "10:00", "end": "15:00"}
    assert db.get_setting(conn, "recent_days") == 30
    assert conn.execute("SELECT cap_override FROM accounts WHERE id=?", (aid,)).fetchone()[0] == 50
    r = client.post("/settings/rules", data=dict(good, w3="150"), follow_redirects=False)
    assert "high%20risk" in r.headers["location"] and db.get_setting(conn, "warmup")[3] == 150


def test_live_preview_and_dismiss(client, conn, tmp_path):
    login(client)
    add_account(conn, "rec1@gmail.com")
    p = tmp_path / "x.csv"
    p.write_text("Name,Email\nShannon Clark,shannon@gmail.com\n")
    r = client.post("/campaigns/new", files={"file": ("x.csv", p.read_bytes())}, follow_redirects=False)
    cid = int(re.search(r"/campaigns/(\d+)/map", r.headers["location"]).group(1))
    client.post(f"/campaigns/{cid}/map", data={"tab": "CSV", "header_row": "1", "col_name": "1", "col_email": "2"})
    pv = client.post(f"/api/campaigns/{cid}/preview", json={"subject": "Hi {first_name}", "body": "Hello {first_name}"}).json()
    assert pv["subject"] == "Hi Shannon" and pv["text"].startswith("Hello Shannon") and "Not interested?" in pv["text"]
    camp = conn.execute("SELECT subject, body FROM campaigns WHERE id=?", (cid,)).fetchone()
    assert camp["subject"] == "" and camp["body"] == ""        # preview saves nothing
    db.add_event(conn, "error", "Something happened", cid)
    assert client.post("/alerts/dismiss-all", data={"back": "//evil.example"}, follow_redirects=False).headers["location"].startswith("/?")
    assert conn.execute("SELECT COUNT(*) FROM events WHERE dismissed=0").fetchone()[0] == 0


def test_unreadable_draft_offers_delete(client, conn):
    login(client)
    conn.execute("INSERT INTO campaigns(id,name,created_at,source_type,source_ref) VALUES(77,'Gone',0,'csv','/nope/x.csv')")
    r = client.get("/campaigns/77/map")
    assert "be opened" in r.text and "/campaigns/77/delete" in r.text and "/nope/x.csv" not in r.text.split("Technical details")[0]
    client.post("/campaigns/77/delete")
    assert conn.execute("SELECT COUNT(*) FROM campaigns WHERE id=77").fetchone()[0] == 0


def test_progress_banner(client, conn):
    login(client)
    add_account(conn, "rec1@gmail.com")
    conn.execute("INSERT INTO campaigns(id,name,created_at,source_type,source_ref,status,status_reason) "
                 "VALUES(5,'C',0,'csv','x','paused','Paused automatically: 2 of 40 emails bounced (5.0%).')")
    st = client.get("/api/campaigns/5/stats").json()
    assert st["banner"]["tone"] == "red" and st["banner"]["title"] == "Paused automatically"
    assert "Review and resume" in client.get("/").text
    assert "Resume sending" in client.get("/campaigns/5").text
