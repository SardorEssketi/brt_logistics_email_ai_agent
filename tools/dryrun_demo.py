"""Dry run on 20 fake drivers, through the real web app + worker code. Nothing is sent.

    python tools/dryrun_demo.py [output_folder]

Scenarios:
  A. Excel file WITH a NOTES column (some cells empty, some with text, one already 'Sent')
  B. Excel file WITHOUT a NOTES column, header on row 3, First/Last name columns, and an unrelated column
  C. Google Sheet (in-memory simulation of the Sheets API; the real one is tested once the service account exists)
Writes the marked files + every generated .eml to the output folder and prints the NOTES column.
"""
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "dryrun-output").resolve()
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)

os.environ.update({
    "OUTREACH_ENV_FILE": "none", "DATA_DIR": str(OUT / "server-data"), "DRY_RUN": "1", "DRY_RUN_FAST": "1",
    "APP_SECRET": "demo-secret-" + "x" * 40, "FERNET_KEY": "8tC4Ej3mOEPj6hZm3GJYbDdT9pVZbK6jJ8m7u8E6Z5Q=",
    "ADMIN_USER": "admin", "COOKIE_SECURE": "0", "PUBLIC_BASE_URL": "https://example-name.duckdns.org",
})
from core import crypto  # noqa: E402

os.environ["ADMIN_PASSWORD_HASH"] = crypto.hash_password("demo")

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import Workbook, load_workbook  # noqa: E402
from openpyxl.styles import Font, PatternFill  # noqa: E402

from app.main import app  # noqa: E402
from core import db, listio  # noqa: E402
from worker.flusher import Flusher  # noqa: E402
from worker.sender import Sender  # noqa: E402

FIRST = ["Shannon", "Marcus", "Luis", "Dwayne", "Tasha", "Robert", "Jamal", "Kevin", "Maria", "Brian",
         "Carlos", "Derek", "Angela", "Travis", "Hector", "Wendy", "Curtis", "Omar", "Rick", "Pam"]
LAST = ["Clark", "Hill", "Garcia", "Brooks", "Reed", "Miller", "Young", "Price", "Lopez", "Ward",
        "Diaz", "Fox", "Bell", "Cole", "Ruiz", "Stone", "Lane", "Khan", "Dean", "Ross"]


def drivers():
    rows = []
    for i in range(20):
        email = f"{FIRST[i].lower()}.{LAST[i].lower()}{i}@{'gmail.com' if i % 3 else 'yahoo.com'}"
        notes = ""
        if i == 3:
            notes = "called 11/10, wants dry van"
        if i == 6:
            notes = "Sent 11/01/2026"          # skipped (already Sent)
        if i == 9:
            email = ""                           # Not sent - no email
        if i == 12:
            email = "angela.bell@gmial.com"      # typo (not fixed in this demo)
        if i == 15:
            email = rows[1][3]                   # duplicate of row 3
        if i == 18:
            email = "dispatch-at-nowhere"        # invalid
        pos = "Owner Operator" if i % 4 == 0 else "Company Driver"
        rows.append([f"{FIRST[i]} {LAST[i]}", f"(555) 01{i:02d}-{1000 + i}", pos, email, notes])
    return rows


def styled_header(ws):
    for c in ws[ws.max_row]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2F5597")


def make_a(path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Drivers"
    ws.append(["NAME", "NUMBER", "POSITION", "EMAIL", "NOTES"])
    styled_header(ws)
    for r in drivers():
        ws.append(r)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["D"].width = 34
    ws.column_dimensions["E"].width = 44
    wb.save(path)


def make_b(path):
    wb = Workbook()
    ws = wb.active
    ws.title = "List"
    ws.append(["November leads — do not share"])
    ws.append([])
    ws.append(["First Name", "Last Name", "Phone Number", "Driver Type", "E-mail Address", "", "Truck #"])
    styled_header(ws)
    for i, r in enumerate(drivers()):
        first, last = r[0].split(" ")
        ws.append([first, last, r[1], r[2], r[3], None, f"T-{100 + i}"])
    wb.save(path)


def login(c):
    r = c.post("/login", data={"username": "admin", "password": "demo"}, follow_redirects=False)
    assert r.status_code == 303, r.text


def setup(conn, c):
    for e, n in (("recruiter1.example@gmail.com", "[Recruiter 1] - [Company]"),
                 ("recruiter2.example@gmail.com", "[Recruiter 2] - [Company]"),
                 ("noname.example@gmail.com", "")):
        r = c.post("/settings/accounts/add", data={"email": e, "display_name": n, "app_password": "abcd abcd abcd abcd",
                                                    "skip_test": "1"}, follow_redirects=False)
        assert "added" in r.headers["location"], r.headers["location"]   # (unnamed one: added, but blocked)
    # Demo only: pretend the accounts finished warm-up (cap 80/day) so all three lists complete.
    # (Week-1 cap 20/day is covered by tests/test_sender.py::test_gap_and_cap.)
    conn.execute("UPDATE accounts SET added_at=?", (time.time() - 30 * 86400,))


def run_campaign(c, conn, create_kwargs, map_form, label):
    r = c.post("/campaigns/new", follow_redirects=False, **create_kwargs)
    assert r.status_code == 303, r.text
    cid = int(re.search(r"/campaigns/(\d+)/map", r.headers["location"]).group(1))
    page = c.get(f"/campaigns/{cid}/map" + (f"?tab={map_form['tab']}" if map_form.get("tab") else "")).text
    detected = re.findall(r'<option value="(\d+)" selected>Column (\w+): ([^<]*)</option>', page)
    print(f"\n=== {label} ===")
    print("Detected header row:", re.search(r'value="(\d+)" selected>Row \d+ \(detected\)', page).group(1))
    print("Detected columns:", ", ".join(f"{col}={hdr}" for _, col, hdr in detected))
    if "No NOTES column found" in page:
        print("No NOTES column ->", re.search(r"added in column <b>(\w+)</b>", page).group(1))
    r = c.post(f"/campaigns/{cid}/map", data=map_form, follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
    c.post(f"/campaigns/{cid}/compose", data={
        "subject": "CDL-A dry van, $0.65 CPM, home every weekend",
        "body": "Hi {first_name},\n\nWe're hiring {position}s for regional dry van: $0.65 CPM, home every weekend, "
                "2023 Freightliners, no touch freight.\n\nInterested? Just reply to this email or call me.\n\n"
                "[Recruiter] | [(555) 000-0000]\n[Company LLC] | MC [MC] / DOT [DOT]\n"
                "1200 [Main St], [City], TX 75201"})
    review = c.get(f"/campaigns/{cid}/review").text
    will = re.search(r"Start sending to (\d+) drivers", review)
    assert will, review[-2000:]
    t = c.post(f"/api/campaigns/{cid}/test-send", data={"to": "me@example.com"}).json()
    assert t["ok"], t
    r = c.post(f"/campaigns/{cid}/start", follow_redirects=False)
    assert "err=" not in r.headers["location"], r.headers["location"]
    s, f = Sender(conn=conn), Flusher(conn)
    for _ in range(60):
        if not s.tick():
            break
    s.tick()
    f.flush_all(time.time() + 3600)
    st = c.get(f"/api/campaigns/{cid}/stats").json()
    print(f"Will send: {will.group(1)} | sent: {st['sent']} | remaining: {st['remaining']} | status: {st['status']} "
          f"| notes written: {st['marks']['total'] - st['marks']['pending']}/{st['marks']['total']}")
    return cid, st


def show_notes(grid, header_row, notes_col, email_col):
    for r in range(header_row, len(grid) + 1):
        row = grid[r - 1]
        email = row[email_col - 1] if len(row) >= email_col else ""
        note = row[notes_col - 1] if len(row) >= notes_col else ""
        print(f"  row {r:>2}  {email[:34]:<34}  {note}")


def main():
    c = TestClient(app)
    conn = db.connect()
    login(c)
    setup(conn, c)

    # A
    a = OUT / "A - Drivers with NOTES.xlsx"
    make_a(a)
    cid, st = run_campaign(c, conn, {"files": {"file": (a.name, a.read_bytes())}},
                           {"tab": "Drivers", "header_row": "1", "col_name": "1", "col_number": "2",
                            "col_position": "3", "col_email": "4", "col_notes": "5", "skip_sent": "1"},
                           "A. Excel with NOTES column")
    marked = c.post(f"/api/campaigns/{cid}/mark-file", files={"file": (a.name, a.read_bytes())}).content
    a.write_bytes(marked)  # what "Save marks to Excel file" does to the original
    ws = load_workbook(a)["Drivers"]
    assert ws["A1"].font.bold and ws.column_dimensions["D"].width == 34
    show_notes(listio.XlsxSource(a).read("Drivers"), 1, 5, 4)

    # B
    b = OUT / "B - Drivers without NOTES.xlsx"
    make_b(b)
    cid, st = run_campaign(c, conn, {"files": {"file": (b.name, b.read_bytes())}},
                           {"tab": "List", "header_row": "3", "col_first_name": "1", "col_last_name": "2",
                            "col_number": "3", "col_position": "4", "col_email": "5", "skip_sent": "1"},
                           "B. Excel without NOTES column (header on row 3, First/Last name, unrelated column G)")
    dl = c.get(f"/campaigns/{cid}/download")
    marked_b = OUT / "B - Drivers without NOTES - marked.xlsx"
    marked_b.write_bytes(dl.content)
    grid = listio.XlsxSource(marked_b).read("List")
    assert grid[2][7] == "NOTES" and grid[3][6] == "T-100"
    show_notes(grid, 3, 8, 5)

    # C
    url = "mock://demo-sheet"
    listio.MockSheetSource(url, {"Sheet1": [["NAME", "NUMBER", "POSITION", "EMAIL", "NOTES"]] + drivers()})
    cid, st = run_campaign(c, conn, {"data": {"sheet_url": url, "name": "Sheet demo"}},
                           {"tab": "Sheet1", "header_row": "1", "col_name": "1", "col_number": "2",
                            "col_position": "3", "col_email": "4", "col_notes": "5", "skip_sent": "1"},
                           "C. Google Sheet (simulated API)")
    show_notes(listio.MockSheetSource(url).read("Sheet1"), 1, 5, 4)

    # Unsubscribe + one-click on a sent driver of campaign A
    r = conn.execute("SELECT id,email FROM recipients WHERE campaign_id=1 AND status='sent' LIMIT 1").fetchone()
    page = TestClient(app).get(f"/u/{crypto.make_unsub_token(r['id'], r['email'])}").text
    print("\nUnsubscribe link (campaign A, first sent driver):", "OK" if "You're unsubscribed" in page else "FAILED")
    print("Suppression now has:", db.suppression_reason(conn, r["email"]), r["email"])

    emls = sorted((OUT / "server-data" / "outbox").rglob("*.eml"))
    for e in emls:
        dest = OUT / "emails" / e.parent.name
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(e, dest / e.name)
    print(f"\n{len(emls)} .eml files written (incl. 3 'test to myself'). Copies in: {OUT / 'emails'}")
    sample = next(e for e in emls if "campaign-1" in str(e))
    print("\n--- Sample email:", sample.name, "---")
    print(sample.read_text(encoding="utf-8", errors="replace")[:2200])


if __name__ == "__main__":
    main()
