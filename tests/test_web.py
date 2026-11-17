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


def test_full_flow_xlsx(client, conn, tmp_path):
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
    assert r.status_code == 200 and "detected" in r.text
    r = client.post(f"/campaigns/{cid}/map", data={"tab": "Drivers", "header_row": "1", "col_email": "4",
                                                   "col_name": "1", "col_number": "2", "col_position": "3",
                                                   "col_notes": "5", "skip_sent": "1"}, follow_redirects=False)
    assert r.status_code == 303

    chk = client.post("/api/check", json={"subject": "Re: job", "body": "Hi"}).json()
    assert chk["blocks"]
    client.post(f"/campaigns/{cid}/compose", data={"subject": "CDL-A dry van, $0.65 CPM",
                                                   "body": "Hi {first_name},\n\nHome weekly. Call me."})
    r = client.get(f"/campaigns/{cid}/review")
    assert "Will send" in r.text and "Start sending to 2 drivers" in r.text, r.text[-3000:]
    assert "joe@gmial.com" in r.text  # typo offered

    t = client.post(f"/api/campaigns/{cid}/test-send", data={"to": "me@example.com"}).json()
    assert t["ok"] and "DRY RUN" in t["message"]

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
    r = anon.get(f"/u/{token}")
    assert "You're unsubscribed" in r.text
    assert db.suppression_reason(conn, rid["email"]) == "unsubscribed"
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
    assert "not found — please choose" in page
    r = client.post(f"/campaigns/{cid}/map", data={"tab": "CSV", "header_row": "1"}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_settings_pages(client, conn):
    login(client)
    r = client.get("/settings")
    assert "Company footer" not in r.text and "Tashkent" in r.text and "Sender name (shown in the inbox)" in r.text
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
    assert "No sender name" in client.get("/settings").text
    client.post(f"/settings/accounts/{aid}/name", data={"display_name": "Mike - ABC Logistics"})
    assert conn.execute("SELECT display_name FROM accounts WHERE id=?", (aid,)).fetchone()[0] == "Mike - ABC Logistics"
