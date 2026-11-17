"""Excel / CSV / (mock) Google Sheets parsing, recipient building and NOTES write-back."""
import json
import time

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

from core import campaigns, db, listio, marks

HEADERS = ["NAME", "NUMBER", "POSITION", "EMAIL", "NOTES"]


def make_xlsx(path, rows, headers=HEADERS):
    wb = Workbook()
    ws = wb.active
    ws.title = "Drivers"
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F5FBF")
    for r in rows:
        ws.append(r)
    ws.column_dimensions["A"].width = 31
    ws["B2"].number_format = "@"
    ws.freeze_panes = "A2"
    wb.create_sheet("Other").append(["keep", "me"])
    wb.save(path)


def new_campaign(conn, stype, ref, tab=None):
    cur = conn.execute("INSERT INTO campaigns(name,created_at,source_type,source_ref,original_name,tab) "
                       "VALUES(?,?,?,?,?,?)", ("t", time.time(), stype, str(ref), "list.xlsx", tab))
    return cur.lastrowid


def prepare(conn, cid, grid, mapping, notes_col, header_row=1, skip_sent=True, mx=lambda d: d != "nomx.com"):
    conn.execute("UPDATE campaigns SET header_row=?, mapping=?, notes_col=?, skip_sent=? WHERE id=?",
                 (header_row, json.dumps(mapping), notes_col, int(skip_sent), cid))
    campaigns.build_recipients(conn, cid, grid, header_row, mapping, notes_col, skip_sent, mx_checker=mx)
    return {r["row_num"]: dict(r) for r in conn.execute("SELECT * FROM recipients WHERE campaign_id=?", (cid,))}


ROWS = [
    ["Shannon Clark", "(530) 990-6119", "Company Driver", "shannon1965clark@gmail.com", ""],           # 2 send
    ["Bob Jones", "555", "Owner Operator", "BOB@yahoo.com", "called 11/10, wants dry van"],          # 3 send
    ["Dup Bob", "556", "Owner Operator", "bob@yahoo.com ", ""],                                       # 4 duplicate
    ["No Mail", "557", "Company Driver", "", ""],                                                     # 5 no email
    ["Bad Mail", "558", "Company Driver", "not-an-email", ""],                                        # 6 invalid
    ["Old Sent", "559", "Company Driver", "old@gmail.com", "Sent 11/01/2026"],                        # 7 already sent
    ["Unsub", "560", "Company Driver", "unsub@gmail.com", ""],                                        # 8 suppressed
    ["Typo", "561", "Company Driver", "joe@gmial.com", ""],                                           # 9 typo
    ["Role", "562", "Company Driver", "info@fleet.com", ""],                                          # 10 role
    ["NoMX", "563", "Company Driver", "x@nomx.com", ""],                                              # 11 no MX
    ["Bounced", "564", "Company Driver", "bounced@gmail.com", ""],                                    # 12 bounced
]
MAP = {"name": 1, "number": 2, "position": 3, "email": 4, "notes": 5}


def test_build_recipients_cleaning(conn, tmp_path):
    db.suppress(conn, "unsub@gmail.com", "unsubscribed")
    db.suppress(conn, "bounced@gmail.com", "bounced")
    p = tmp_path / "list.xlsx"
    make_xlsx(p, ROWS)
    cid = new_campaign(conn, "xlsx", p, "Drivers")
    grid = listio.XlsxSource(p).read("Drivers")
    rec = prepare(conn, cid, grid, MAP, 5)
    st = {r: (x["status"], x["skip_reason"]) for r, x in rec.items()}
    assert st[2] == ("queued", None) and st[3] == ("queued", None)
    assert st[4] == ("skipped", "duplicate")
    assert st[5] == ("skipped", "no_email")
    assert st[6] == ("skipped", "invalid")
    assert st[7] == ("skipped", "already_sent")
    assert st[8] == ("skipped", "unsubscribed")
    assert st[9] == ("skipped", "typo") and rec[9]["suggestion"] == "joe@gmail.com"
    assert st[10] == ("skipped", "role")
    assert st[11] == ("skipped", "invalid")
    assert st[12] == ("skipped", "bounced")
    assert rec[2]["first_name"] == "Shannon" and rec[3]["email"] == "bob@yahoo.com"
    s = campaigns.summary(conn, cid)
    assert s["total"] == 11 and s["will_send"] == 2

    # Unticked "skip Sent": row 7 is emailed again; unsubscribed/bounced still skipped
    rec = prepare(conn, cid, grid, MAP, 5, skip_sent=False)
    assert rec[7]["status"] == "queued" and rec[8]["status"] == "skipped" and rec[12]["status"] == "skipped"

    # Typo fix
    rec = prepare(conn, cid, grid, MAP, 5)
    assert campaigns.apply_typo_fixes(conn, cid, [rec[9]["id"]]) == 1
    assert conn.execute("SELECT status,email FROM recipients WHERE id=?", (rec[9]["id"],)).fetchone()[:] == \
        ("queued", "joe@gmail.com")


def _sent_marks(conn, cid, rec, rows_notes):
    for row, note in rows_notes:
        marks.queue_mark(conn, cid, rec[row]["id"], row, rec[row]["email"] or "", note)


def test_xlsx_writeback_preserves_formatting(conn, tmp_path):
    p = tmp_path / "list.xlsx"
    make_xlsx(p, ROWS)
    cid = new_campaign(conn, "xlsx", p, "Drivers")
    grid = listio.XlsxSource(p).read("Drivers")
    rec = prepare(conn, cid, grid, MAP, 5)
    campaigns.queue_skip_marks(conn, cid)
    _sent_marks(conn, cid, rec, [(2, "Sent 11/18/2026"), (3, "Sent 11/18/2026"), (2, "Replied 11/19/2026")])
    camp = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    marks.flush_campaign(conn, camp)
    marks.flush_campaign(conn, camp)  # second flush: nothing pending, nothing doubled

    wb = load_workbook(p)
    ws = wb["Drivers"]
    assert ws["E2"].value == "Sent 11/18/2026 | Replied 11/19/2026"
    assert ws["E3"].value == "called 11/10, wants dry van | Sent 11/18/2026"
    assert ws["E4"].value == "Not sent - duplicate"
    assert ws["E5"].value == "Not sent - no email"
    assert ws["E6"].value == "Not sent - invalid email"
    assert ws["E7"].value == "Sent 11/01/2026"            # already-sent row untouched
    assert ws["E9"].value == "Not sent - invalid email"   # unfixed typo
    assert ws["E10"].value == "Not sent - role address"
    # everything else untouched + formatting kept
    assert [c.value for c in ws[2]][:4] == ["Shannon Clark", "(530) 990-6119", "Company Driver", "shannon1965clark@gmail.com"]
    assert ws["A1"].font.bold and ws["A1"].fill.fgColor.rgb.endswith("1F5FBF")
    assert ws.column_dimensions["A"].width == 31 and ws.freeze_panes == "A2"
    assert wb["Other"]["A1"].value == "keep"


def test_xlsx_no_notes_column_and_unrelated_column(conn, tmp_path):
    p = tmp_path / "list.xlsx"
    headers = ["NAME", "EMAIL", "", "Truck #"]   # column C empty header but D has data
    make_xlsx(p, [["A One", "a1@gmail.com", "", "T1"], ["B Two", "b2@gmail.com", "", "T2"]], headers=headers)
    cid = new_campaign(conn, "xlsx", p, "Drivers")
    grid = listio.XlsxSource(p).read("Drivers")
    from core import columns
    notes_col = columns.notes_insert_col(grid, 1)
    assert notes_col == 5  # right of ALL existing data (C is a gap inside the data, D has data)
    rec = prepare(conn, cid, grid, {"name": 1, "email": 2}, notes_col)
    _sent_marks(conn, cid, rec, [(2, "Sent 11/18/2026")])
    marks.flush_campaign(conn, conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone())
    ws = load_workbook(p)["Drivers"]
    assert ws["E1"].value == "NOTES" and ws["E1"].font.bold    # header styled like its neighbour
    assert ws["E2"].value == "Sent 11/18/2026" and ws["E3"].value is None
    assert ws["D2"].value == "T1" and ws["C2"].value is None


def test_csv_roundtrip(conn, tmp_path):
    p = tmp_path / "list.csv"
    p.write_text("Full Name;Phone;E-mail\nShannon Clark;530;s@gmail.com\nBob;531;b@gmail.com\n", encoding="utf-8")
    src = listio.CsvSource(p)
    grid = src.read()
    assert grid[1] == ["Shannon Clark", "530", "s@gmail.com"] and src.delimiter == ";"
    cid = new_campaign(conn, "csv", p)
    rec = prepare(conn, cid, grid, {"name": 1, "number": 2, "email": 3}, 4)
    _sent_marks(conn, cid, rec, [(3, "Sent 11/18/2026")])
    marks.flush_campaign(conn, conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone())
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "Full Name;Phone;E-mail;NOTES"
    assert lines[2] == "Bob;531;b@gmail.com;Sent 11/18/2026"
    assert lines[1] == "Shannon Clark;530;s@gmail.com"   # untouched row stays byte-identical


def test_mock_sheet_retry_and_moved_rows(conn):
    url = "mock://sheet1"
    listio.MockSheetSource(url, {"Sheet1": [HEADERS] + [list(r) for r in ROWS[:3]]})
    cid = new_campaign(conn, "gsheet", url, "Sheet1")
    src = listio.open_source("gsheet", url)
    rec = prepare(conn, cid, src.read("Sheet1"), MAP, 5)
    _sent_marks(conn, cid, rec, [(2, "Sent 11/18/2026"), (3, "Sent 11/18/2026")])
    camp = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()

    # API failure: nothing lost, stays queued
    src.fail_writes = 1
    with pytest.raises(RuntimeError):
        marks.flush_campaign(conn, camp, source=src)
    assert conn.execute("SELECT COUNT(*) FROM marks WHERE written_at IS NULL").fetchone()[0] == 2

    # Someone sorts the sheet meanwhile: Bob moves to row 2, Shannon to row 3
    data = listio.MockSheetSource.registry[url]["Sheet1"]
    data[1], data[2] = data[2], data[1]
    marks.flush_campaign(conn, camp, source=src)
    data = listio.MockSheetSource.registry[url]["Sheet1"]
    assert data[1][3] == "BOB@yahoo.com" and data[1][4] == "called 11/10, wants dry van | Sent 11/18/2026"
    assert data[2][3] == "shannon1965clark@gmail.com" and data[2][4] == "Sent 11/18/2026"

    # Row deleted -> unmatched, reported, not written anywhere else
    marks.queue_mark(conn, cid, None, 9, "gone@gmail.com", "Sent 11/18/2026")
    marks.flush_campaign(conn, camp, source=src)
    assert conn.execute("SELECT unmatched FROM marks WHERE email='gone@gmail.com'").fetchone()[0] == 1


def test_mark_file_bytes_idempotent(conn, tmp_path):
    p = tmp_path / "list.xlsx"
    make_xlsx(p, ROWS[:2])
    original = p.read_bytes()
    cid = new_campaign(conn, "xlsx", tmp_path / "server-copy.xlsx", "Drivers")
    (tmp_path / "server-copy.xlsx").write_bytes(original)
    grid = listio.XlsxSource(p).read("Drivers")
    rec = prepare(conn, cid, grid, MAP, 5)
    _sent_marks(conn, cid, rec, [(2, "Sent 11/18/2026")])
    camp = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    once = marks.mark_file_bytes(conn, camp, original, "list.xlsx")
    twice = marks.mark_file_bytes(conn, camp, once, "list.xlsx")
    out = tmp_path / "out.xlsx"
    out.write_bytes(twice)
    ws = load_workbook(out)["Drivers"]
    assert ws["E2"].value == "Sent 11/18/2026"
    assert ws["E3"].value == "called 11/10, wants dry van"
