"""Notes, columns, emails, placeholders, content checks, tokens, caps."""
import time

from core import caps, checks, columns, crypto, emails, notes, render, timeutil


# ---------------- notes
def test_append_note_rules():
    assert notes.append_note("", "Sent 11/18/2026") == "Sent 11/18/2026"
    assert notes.append_note(None, "Sent 11/18/2026") == "Sent 11/18/2026"
    assert notes.append_note("called 11/10, wants dry van", "Sent 11/18/2026") == \
        "called 11/10, wants dry van | Sent 11/18/2026"
    once = notes.append_note("Sent 11/18/2026", "Replied 11/19/2026")
    assert once == "Sent 11/18/2026 | Replied 11/19/2026"
    assert notes.append_note(once, "Replied 11/19/2026") == once  # never twice
    assert notes.append_note(once, "Sent 11/18/2026") == once


def test_has_sent():
    assert notes.has_sent("Sent 11/18/2026")
    assert notes.has_sent("called | Sent 1/2/2026 | Replied 1/3/2026")
    assert not notes.has_sent("Not sent - duplicate")
    assert not notes.has_sent("Sent")
    assert not notes.has_sent("")


# ---------------- columns
def test_detect_standard_headers():
    hdr = ["NAME", "NUMBER", "POSITION", "EMAIL", "NOTES"]
    assert columns.detect_mapping(hdr) == {"email": 4, "name": 1, "number": 2, "position": 3, "notes": 5}


def test_detect_variants_and_header_row():
    grid = [["Driver list Nov"], [], ["First Name", "Last Name", "Phone Number", "Driver Type", "E-mail Address", "Comments"],
            ["Shannon", "Clark", "5309906119", "Company Driver", "s@x.com", ""]]
    hr = columns.find_header_row(grid)
    assert hr == 3
    m = columns.detect_mapping(grid[hr - 1])
    assert m == {"email": 5, "first_name": 1, "last_name": 2, "number": 3, "position": 4, "notes": 6}


def test_header_row_not_found():
    assert columns.find_header_row([["a", "b"], ["c", "d"]]) is None


def test_notes_insert_col_skips_columns_with_data():
    grid = [["NAME", "EMAIL", "", "Other"], ["a", "a@x.com", "", "z"], ["b", "b@x.com", "stray", ""]]
    # column C has data in row 3, column D has a header => first fully empty column is E
    assert columns.notes_insert_col(grid, 1) == 5
    grid2 = [["NAME", "EMAIL"], ["a", "a@x.com"]]
    assert columns.notes_insert_col(grid2, 1) == 3


# ---------------- emails
def test_normalize_and_validate():
    assert emails.normalize("  Shannon1965Clark@Gmail.com ") == "shannon1965clark@gmail.com"
    assert emails.normalize("mailto:a@b.com") == "a@b.com"
    assert emails.normalize('=HYPERLINK("mailto:x@y.com","x@y.com")') == "x@y.com"
    assert emails.normalize("a@b.com; c@d.com") == "a@b.com"
    assert emails.normalize("n/a") == ""
    assert emails.is_valid_syntax("a.b+c@sub.example.com")
    for bad in ["a@b", "a..b@c.com", ".a@b.com", "a@-b.com", "a@b.c"]:
        assert not emails.is_valid_syntax(bad), bad


def test_role_and_typos():
    assert emails.is_role("info@company.com") and emails.is_role("noreply@x.com")
    assert not emails.is_role("mike@company.com")
    assert emails.typo_suggestion("joe@gmial.com") == "joe@gmail.com"
    assert emails.typo_suggestion("joe@yaho.com") == "joe@yahoo.com"
    assert emails.typo_suggestion("joe@gmail.com") is None


# ---------------- placeholders
def test_render_placeholders_and_fallbacks():
    v = render.variables(name="SHANNON CLARK", position="Company Driver")
    assert render.render("Hi {first_name}, {name} - {position}", v) == "Hi Shannon, Shannon Clark - Company Driver"
    empty = render.variables()
    assert render.render("Hi {first_name},", empty) == "Hi there,"
    assert render.render("{position} jobs", empty) == "driver jobs"
    assert render.render("Hi { first_name }", render.variables(name="Clark, Shannon")) == "Hi Shannon"
    assert render.render("{unknown}", v) == "{unknown}"
    assert render.unknown_placeholders("Hi {firstname} {name}") == ["firstname"]
    fl = render.variables(first_name="joe", last_name="doe")
    assert fl["name"] == "Joe Doe" and fl["first_name"] == "Joe"


# ---------------- content checker
def test_checker_blocks_and_warnings():
    r = checks.check_content("Re: CDL job", "Hi")
    assert any("Re:" in b for b in r["blocks"])
    assert checks.check_content("FWD: x", "Hi")["blocks"]
    r = checks.check_content("CDL-A job", "GUARANTEED pay!!! $$$ act now https://bit.ly/x https://a.com")
    w = " ".join(r["warnings"])
    for frag in ("links", "shortener", "ALL CAPS", "!!!", "$$$", "guaranteed", "act now"):
        assert frag.lower() in w.lower(), frag
    clean = "Hi {first_name}, home weekly.\n\nMike Smith\nABC Logistics LLC, 1200 Main St, Dallas, TX 75201"
    assert not checks.check_content("CDL-A dry van, OTR, $0.65 CPM", clean)["warnings"]


def test_address_warning():
    w = checks.ADDRESS_WARNING
    no_addr = "Hi {first_name}, we pay $0.65 CPM on 2023 Freightliners. Call (555) 010-2000."
    assert w in checks.check_content("CDL-A job", no_addr)["warnings"]
    assert not checks.check_content("CDL-A job", no_addr)["blocks"]          # warning only, never blocks
    for addr in ["ABC Logistics, 1200 N. Main St, Dallas, TX 75201", "4500 Industrial Pkwy\nJoliet, IL 60431",
                 "PO Box 77, Laredo, TX", "18 Elm Rd Suite 4, Columbus OH 43215-1234"]:
        assert checks.has_postal_address(f"Hi there.\n\n{addr}"), addr
    for not_addr in ["Home every 14 days in TX", "We run 48 states, OK?", "2023 Freightliners, no touch freight"]:
        assert not checks.has_postal_address(not_addr), not_addr
    long_body = "word " * 160
    assert any("words" in x for x in checks.check_content("x", long_body)["warnings"])
    assert checks.check_content("x", "Hi {frist_name}")["blocks"]


# ---------------- tokens
def test_unsub_token_signing():
    secret = b"s" * 40
    t = crypto.make_unsub_token(42, "Joe@Gmail.com", secret)
    assert "joe" not in t.lower() and "@" not in t
    assert crypto.verify_unsub_token(t, "joe@gmail.com", secret)
    assert not crypto.verify_unsub_token(t, "other@gmail.com", secret)
    assert not crypto.verify_unsub_token(t.replace("42.", "43."), "joe@gmail.com", secret)
    assert not crypto.verify_unsub_token(t[:-2] + "xx", "joe@gmail.com", secret)
    assert crypto.parse_unsub_token("garbage") is None


def test_password_hash():
    h = crypto.hash_password("correct horse")
    assert "$" not in h
    assert crypto.verify_password("correct horse", h)
    assert not crypto.verify_password("wrong", h)


# ---------------- caps
def test_warmup_schedule():
    sched = [20, 40, 60, 80]
    now = time.time()
    assert caps.warmup_cap(now, now, sched) == 20
    assert caps.warmup_cap(now - 8 * 86400, now, sched) == 40
    assert caps.warmup_cap(now - 15 * 86400, now, sched) == 60
    assert caps.warmup_cap(now - 100 * 86400, now, sched) == 80
    acc = {"cap_override": 500, "added_at": now}
    assert caps.account_cap(acc, sched, now) == caps.HARD_MAX


def test_rolling_24h(conn):
    from tests.conftest import add_account
    aid = add_account(conn)
    now = time.time()
    for ago in (100, 3600, 86000, 86500, 90000):   # last two are older than 24h
        conn.execute("INSERT INTO send_log(account_id,ts,kind) VALUES(?,?,'campaign')", (aid, now - ago))
    assert caps.sent_last_24h(conn, aid, now) == 3
    assert caps.sent_last_24h(conn, aid, now + 600) == 2  # rolling, not calendar day


# ---------------- time
def test_window_and_dates():
    from datetime import datetime
    w = {"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "16:00"}
    wed_10 = datetime(2026, 11, 18, 10, 0, tzinfo=timeutil.CENTRAL).timestamp()
    wed_17 = datetime(2026, 11, 18, 17, 0, tzinfo=timeutil.CENTRAL).timestamp()
    sat_10 = datetime(2026, 11, 21, 10, 0, tzinfo=timeutil.CENTRAL).timestamp()
    assert timeutil.in_window(wed_10, w) and not timeutil.in_window(wed_17, w) and not timeutil.in_window(sat_10, w)
    assert timeutil.next_window_start(sat_10, w).weekday() == 0
    assert timeutil.us_date(wed_10) == "11/18/2026"
    assert timeutil.tashkent_range(w, wed_10) == "20:00–03:00"       # CST: +11 h
    july = datetime(2026, 7, 15, 10, 0, tzinfo=timeutil.CENTRAL).timestamp()
    assert timeutil.tashkent_range(w, july) == "19:00–02:00"         # CDT: +10 h
