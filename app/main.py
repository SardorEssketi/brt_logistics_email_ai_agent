"""Web interface (FastAPI, server-rendered)."""
import csv
import io
import json
import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import (campaigns, caps, checks, columns, compose, config, crypto, db, emails, imap_util, listio,
                  outcomes, smtp_send, timeutil)
from core.marks import flush_campaign, mark_file_bytes
from core.notes import SKIP_LABELS
from core.render import PLACEHOLDER_HELP

from . import auth

log = logging.getLogger("web")
HERE = Path(__file__).resolve().parent
MAX_UPLOAD = 15 * 1024 * 1024
PUBLIC_PREFIXES = ("/login", "/u/", "/static/", "/healthz")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(fmt_ct=timeutil.fmt_central, col_letter=columns.col_letter, skip_labels=SKIP_LABELS)
# Changes whenever app.css / app.js change, so browsers don't keep an old copy after an update.
templates.env.globals["static_v"] = int(max((HERE / "static" / f).stat().st_mtime for f in ("app.css", "app.js")))


def conn():
    return db.connect()


def redirect(url: str, msg: str = None, err: str = None):
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
    elif err:
        url += f"{sep}err={quote(err)}"
    return RedirectResponse(url, status_code=303)


def page(request: Request, name: str, **ctx):
    ctx.setdefault("dry_run", config.dry_run())
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    return templates.TemplateResponse(request, name, ctx)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if request.method == "POST" and not path.startswith("/u/"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("Cross-site request blocked", status_code=403)
    if not path.startswith(PUBLIC_PREFIXES) and path != "/login":
        if not auth.valid_session(request.cookies.get(auth.COOKIE, "")):
            if path.startswith("/api/"):
                return JSONResponse({"error": "Please log in again."}, status_code=401)
            return RedirectResponse("/login", status_code=303)
    resp = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ------------------------------------------------------------------ login
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return page(request, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    c = conn()
    ip = request.client.host if request.client else "?"
    if auth.too_many_attempts(c, ip):
        return page(request, "login.html", err="Too many attempts. Wait 15 minutes and try again.")
    if not auth.check_credentials(username, password):
        auth.record_failure(c, ip)
        time.sleep(1)
        return page(request, "login.html", err="Wrong username or password.")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE, auth.make_session(config.get("ADMIN_USER", "admin")), max_age=auth.SESSION_TTL,
                    httponly=True, secure=config.cookie_secure(), samesite="lax")
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# ------------------------------------------------------------------ dashboard
def account_rows(c, now=None):
    now = now or time.time()
    schedule = db.get_setting(c, "warmup")
    out = []
    for a in c.execute("SELECT * FROM accounts ORDER BY id").fetchall():
        d = dict(a)
        d.pop("password_enc", None)
        d["cap"] = caps.account_cap(a, schedule, now)
        d["warmup_cap"] = caps.warmup_cap(a["added_at"], now, schedule)
        d["week"] = int((now - a["added_at"]) // caps.WEEK) + 1
        d["sent_24h"] = caps.sent_last_24h(c, a["id"], now)
        d["state"] = caps.account_state(a, now)
        d["paused_until_ct"] = timeutil.fmt_central(a["paused_until"]) if d["state"] == "paused" else ""
        out.append(d)
    return out


def alerts(c, campaign_id=None, limit=15):
    q = "SELECT * FROM events WHERE dismissed=0 AND level IN ('error','warn')"
    args = []
    if campaign_id:
        q += " AND (campaign_id=? OR campaign_id IS NULL)"
        args.append(campaign_id)
    return [dict(r) for r in c.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit))]


# Words people see for each campaign status (the database keeps the short names).
STATUS_LABELS = {"draft": "Draft", "running": "Sending", "paused": "Paused", "stopped": "Stopped", "done": "Finished"}
ACCOUNT_STATE_LABELS = {"ok": "Ready", "paused": "Resting", "no_name": "Needs sender name", "disabled": "Stopped"}
templates.env.globals.update(status_labels=STATUS_LABELS, account_state_labels=ACCOUNT_STATE_LABELS)


def attention_items(c, accounts=None) -> list:
    """What needs a person right now, built from the CURRENT state (fixed things disappear by themselves).
    Each item: level (red|amber), text, action label, action url."""
    items = []
    for a in accounts if accounts is not None else account_rows(c):
        if a["state"] == "disabled":
            items.append({"level": "red", "text": f"{a['email']} is stopped: {a['disabled_reason']}.",
                          "label": "Fix account", "url": f"/accounts#acc-{a['id']}"})
        elif a["state"] == "no_name":
            items.append({"level": "red", "text": f"{a['email']} can't send until it has a sender name.",
                          "label": "Add sender name", "url": f"/accounts#acc-{a['id']}"})
    for camp in c.execute("SELECT id, name, status_reason FROM campaigns WHERE status='paused' "
                          "AND status_reason LIKE 'Paused automatically%' ORDER BY id DESC"):
        items.append({"level": "red", "text": f"“{camp['name'] or 'Campaign ' + str(camp['id'])}” paused itself. "
                      + camp["status_reason"].replace("Paused automatically: ", "", 1),
                      "label": "Review and resume", "url": f"/campaigns/{camp['id']}"})
    for r in c.execute("SELECT m.campaign_id, COUNT(*) n, cp.name FROM marks m JOIN campaigns cp ON cp.id=m.campaign_id "
                       "WHERE m.unmatched=1 GROUP BY m.campaign_id"):
        items.append({"level": "amber", "text": f"{r['n']} note(s) for “{r['name']}” couldn't be written because the "
                      "row was not found in the list.", "label": "See which", "url": f"/campaigns/{r['campaign_id']}#notes"})
    return items


def setup_steps(c, accounts) -> list:
    """First-run checklist. Shown on the Campaigns page until an account can send."""
    named = [a for a in accounts if a["state"] in ("ok", "paused")]
    return [
        {"done": bool(named), "title": "Add a Gmail account",
         "text": "With its App Password and the sender name drivers will see. This is the only required step.",
         "url": "/accounts#add", "label": "Add Gmail account"},
        {"done": bool(db.get_setting(c, "test_inbox")), "title": "Set your test address (optional)",
         "text": "Where “Send test” emails go, so you can see the email before drivers do.",
         "url": "/settings#test-inbox", "label": "Set test address"},
        {"done": bool(listio.service_account_email()), "title": "Connect Google Sheets (optional)",
         "text": "Only needed if your driver lists are Google Sheets. Excel and CSV work without it.",
         "url": "/settings#sheets", "label": "How to connect"},
    ]


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    c = conn()
    rows = []
    for camp in c.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT 50").fetchall():
        d = dict(camp)
        d.update(outcomes.campaign_rates(c, camp["id"]))
        d["remaining"] = c.execute("SELECT COUNT(*) FROM recipients WHERE campaign_id=? AND status IN "
                                   "('queued','sending')", (camp["id"],)).fetchone()[0]
        total = d["sent"] + d["remaining"]
        d["pct"] = round(100 * d["sent"] / total) if total else 0
        d["link"], d["draft_step"] = _campaign_link(camp)
        d["needs_you"] = camp["status"] == "paused"
        rows.append(d)
    accs = account_rows(c)
    can_send = [a for a in accs if a["state"] in ("ok", "paused")]
    return page(request, "dashboard.html", campaigns=rows, alerts=alerts(c), accounts=accs,
                attention=attention_items(c, accs), setup=setup_steps(c, accs) if not can_send else None,
                capacity=sum(a["cap"] for a in can_send), sent_24h=sum(a["sent_24h"] for a in accs))


def _campaign_link(camp):
    """Where a click on the campaign should go, and (for drafts) which setup step it is waiting on."""
    cid = camp["id"]
    if camp["status"] != "draft":
        return f"/campaigns/{cid}", ""
    if camp["mapping"] and (camp["subject"] or "").strip() and (camp["body"] or "").strip():
        return f"/campaigns/{cid}/review", "Step 4: check & start"
    if camp["mapping"]:
        return f"/campaigns/{cid}/compose", "Step 3: message"
    return f"/campaigns/{cid}/map", "Step 2: columns"


def _safe_back(back: str) -> str:
    return back if back.startswith("/") and not back.startswith("//") else "/"


@app.post("/alerts/{eid}/dismiss")
def dismiss(eid: int, back: str = Form("/")):
    conn().execute("UPDATE events SET dismissed=1 WHERE id=?", (eid,))
    return redirect(_safe_back(back))


@app.post("/alerts/dismiss-all")
def dismiss_all(back: str = Form("/"), campaign_id: str = Form("")):
    c = conn()
    if campaign_id.isdigit():
        c.execute("UPDATE events SET dismissed=1 WHERE campaign_id=? OR campaign_id IS NULL", (int(campaign_id),))
    else:
        c.execute("UPDATE events SET dismissed=1")
    return redirect(_safe_back(back), msg="Activity cleared.")


# ------------------------------------------------------------------ accounts
@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    return page(request, "accounts.html", accounts=account_rows(conn()))


# ------------------------------------------------------------------ settings
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    c = conn()
    window = db.get_setting(c, "window")
    now = time.time()
    return page(request, "settings.html", accounts=account_rows(c, now), window=window, tashkent=timeutil.tashkent_range(window, now), warmup=db.get_setting(c, "warmup"),
                test_inbox=db.get_setting(c, "test_inbox"), day_names=timeutil.DAY_NAMES,
                recent_days=db.get_setting(c, "recent_days"),
                suppressed=c.execute("SELECT reason, COUNT(*) n FROM suppression GROUP BY reason").fetchall(),
                sa_email=listio.service_account_email(), hard_max=caps.HARD_MAX, risky=caps.RISKY_ABOVE)


def _test_account(email, password):
    problems = []
    try:
        smtp_send.test_login(email, password)
    except Exception as e:
        problems.append(f"Sending emails failed: {e}")
    try:
        imap_util.test_imap(email, password)
    except Exception as e:
        problems.append(f"Reading replies from the inbox failed: {e}")
    return problems


@app.post("/settings/accounts/add")
def account_add(email: str = Form(...), display_name: str = Form(""), app_password: str = Form(...),
                skip_test: str = Form("")):
    c = conn()
    email = emails.normalize(email)
    pw = re.sub(r"\s+", "", app_password)
    if not email or not emails.is_valid_syntax(email):
        return redirect("/accounts", err="That email address doesn't look right.")
    if c.execute("SELECT 1 FROM accounts WHERE email=?", (email,)).fetchone():
        return redirect("/accounts", err=f"{email} is already added.")
    if not (config.dry_run() and skip_test):
        problems = _test_account(email, pw)
        if problems:
            return redirect("/accounts", err=f"{email} was NOT added. " + " ".join(problems)
                            + " Check the App Password (16 letters) and that 2-Step Verification is on.")
    c.execute("INSERT INTO accounts(email,display_name,password_enc,added_at) VALUES(?,?,?,?)",
              (email, clean_sender_name(display_name), crypto.encrypt(pw), time.time()))
    if not clean_sender_name(display_name):
        return redirect("/accounts", err=f"{email} added and tested, but it won't send until you enter its "
                                         "sender name (shown in the inbox).")
    return redirect("/accounts", msg=f"{email} added and tested. It starts at the week-1 warm-up cap.")


def clean_sender_name(name: str) -> str:
    return re.sub(r"[\r\n\"<>]", " ", name or "").strip()[:80]


@app.post("/settings/accounts/{aid}/name")
def account_name(aid: int, display_name: str = Form("")):
    c = conn()
    name = clean_sender_name(display_name)
    c.execute("UPDATE accounts SET display_name=? WHERE id=?", (name, aid))
    if not name:
        return redirect("/accounts", err="Sender name removed: this account will not send until it has one.")
    return redirect("/accounts", msg=f"Sender name saved: {name}")


@app.post("/settings/accounts/{aid}/delete")
def account_delete(aid: int):
    c = conn()
    busy = c.execute("SELECT COUNT(*) FROM recipients WHERE account_id=? AND status='sending'", (aid,)).fetchone()[0]
    if busy:
        return redirect("/accounts", err="This account is sending right now. Pause campaigns first, then remove it.")
    c.execute("DELETE FROM accounts WHERE id=?", (aid,))
    return redirect("/accounts", msg="Account removed.")


@app.post("/settings/accounts/{aid}/password")
def account_password(aid: int, app_password: str = Form(...)):
    c = conn()
    a = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    pw = re.sub(r"\s+", "", app_password)
    problems = _test_account(a["email"], pw)
    if problems:
        return redirect("/accounts", err="New App Password not saved. " + " ".join(problems))
    c.execute("UPDATE accounts SET password_enc=?, disabled_reason=NULL, paused_until=NULL, last_error=NULL WHERE id=?",
              (crypto.encrypt(pw), aid))
    return redirect("/accounts", msg=f"{a['email']}: new App Password saved and account re-enabled.")


@app.post("/settings/accounts/{aid}/reenable")
def account_reenable(aid: int):
    c = conn()
    a = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    problems = _test_account(a["email"], crypto.decrypt(a["password_enc"])) if not config.dry_run() else []
    if problems:
        return redirect("/accounts", err=f"{a['email']} still fails: " + " ".join(problems))
    c.execute("UPDATE accounts SET disabled_reason=NULL, paused_until=NULL, last_error=NULL WHERE id=?", (aid,))
    return redirect("/accounts", msg=f"{a['email']} re-enabled.")


@app.post("/api/accounts/{aid}/test")
def account_test(aid: int):
    c = conn()
    a = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    if not a:
        return JSONResponse({"ok": False, "message": "Account not found."})
    problems = _test_account(a["email"], crypto.decrypt(a["password_enc"]))
    return {"ok": not problems, "message": " ".join(problems) or "Sending and inbox reading both work."}


def _parse_window(form):
    """(window dict, None) or (None, error). Same rules as before the redesign."""
    days = sorted(int(d) for d in form.getlist("days") if str(d).isdigit() and 0 <= int(d) <= 6)
    start, end = form.get("start", "09:00"), form.get("end", "16:00")
    if not re.match(r"^\d\d:\d\d$", start) or not re.match(r"^\d\d:\d\d$", end) or start >= end:
        return None, "The start time must be before the end time."
    return {"days": days, "start": start, "end": end}, None


def _parse_caps(c, form):
    """((warm-up list, {account id: override or None}), None) or (None, error). Nothing is saved here."""
    try:
        warm = [int(form.get(f"w{i}")) for i in range(4)]
    except (TypeError, ValueError):
        return None, "Warm-up caps must be whole numbers."
    if any(not 1 <= w <= caps.HARD_MAX for w in warm):
        return None, f"Caps must be between 1 and {caps.HARD_MAX}."
    overrides = {}
    for a in c.execute("SELECT id FROM accounts").fetchall():
        v = (form.get(f"cap_{a['id']}") or "").strip()
        if v and (not v.isdigit() or not 1 <= int(v) <= caps.HARD_MAX):
            return None, f"Caps must be between 1 and {caps.HARD_MAX}."
        overrides[a["id"]] = int(v) if v else None
    return (warm, overrides), None


def _save_caps(c, warm, overrides) -> bool:
    """Saves caps; True if any of them is above the risky level."""
    db.set_setting(c, "warmup", warm)
    for aid, v in overrides.items():
        c.execute("UPDATE accounts SET cap_override=? WHERE id=?", (v, aid))
    return max(warm + [v or 0 for v in overrides.values()]) > caps.RISKY_ABOVE


RISKY_MSG = f"Saved — but a cap above {caps.RISKY_ABOVE}/day is a high risk of the free Gmail account being locked."


def _parse_recent(v: str):
    v = (v or "").strip()
    if not v.isdigit() or not 0 <= int(v) <= 365:
        return None, "Enter a number of days from 0 to 365 (0 turns the check off)."
    return int(v), None


@app.post("/settings/rules")
async def rules_save(request: Request):
    """One form for the sending window, daily caps and the repeat-email rule. Everything is checked first,
    so a mistake in one field saves nothing (no half-saved settings)."""
    form = await request.form()
    c = conn()
    window, err = _parse_window(form)
    capv, err2 = _parse_caps(c, form)
    days, err3 = _parse_recent(form.get("recent_days", ""))
    errors = [e for e in (err, err2, err3) if e]
    if errors:
        return redirect("/settings#rules", err="Nothing was saved. " + " ".join(errors))
    db.set_setting(c, "window", window)
    risky = _save_caps(c, *capv)
    db.set_setting(c, "recent_days", days)
    if risky:
        return redirect("/settings#rules", err=RISKY_MSG)
    return redirect("/settings#rules", msg="Sending rules saved.")


# The three forms below are the pre-redesign endpoints, kept so old bookmarks/scripts keep working.
@app.post("/settings/window")
async def window_save(request: Request):
    window, err = _parse_window(await request.form())
    if err:
        return redirect("/settings", err=err)
    db.set_setting(conn(), "window", window)
    return redirect("/settings", msg="Sending window saved.")


@app.post("/settings/caps")
async def caps_save(request: Request):
    form = await request.form()
    c = conn()
    capv, err = _parse_caps(c, form)
    if err:
        return redirect("/settings", err=err)
    if _save_caps(c, *capv):
        return redirect("/settings", err=RISKY_MSG)
    return redirect("/settings", msg="Daily caps saved.")


@app.post("/settings/recent-days")
def recent_days_save(recent_days: str = Form("")):
    v, err = _parse_recent(recent_days)
    if err:
        return redirect("/settings#rules", err=err)
    db.set_setting(conn(), "recent_days", v)
    msg = (f"Saved: drivers emailed by any campaign in the last {v} days are skipped." if v
           else "Saved: the repeat-email check is off.")
    return redirect("/settings#rules", msg=msg)


@app.post("/settings/test-inbox")
def test_inbox_save(test_inbox: str = Form("")):
    e = emails.normalize(test_inbox)
    if test_inbox.strip() and not emails.is_valid_syntax(e):
        return redirect("/settings#test-inbox", err="That email address doesn't look right.")
    db.set_setting(conn(), "test_inbox", e)
    return redirect("/settings#test-inbox", msg="Test address saved." if e else "Test address cleared.")


@app.get("/settings/suppression.csv")
def suppression_export():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "reason", "added (Central)"])
    for r in conn().execute("SELECT * FROM suppression ORDER BY added_at"):
        w.writerow([r["email"], r["reason"], timeutil.fmt_central(r["added_at"])])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="do-not-email-list.csv"'})


@app.post("/settings/suppression/import")
async def suppression_import(file: UploadFile = File(...)):
    data = (await file.read())[:MAX_UPLOAD]
    text = data.decode("utf-8-sig", errors="replace")
    c = conn()
    n = 0
    with db.tx(c):
        for row in csv.reader(io.StringIO(text)):
            if not row:
                continue
            e = emails.normalize(row[0])
            if not e:
                continue
            reason = (row[1].strip().lower() if len(row) > 1 else "") or "manual"
            if reason not in ("unsubscribed", "bounced", "replied", "manual"):
                reason = "manual"
            db.suppress(c, e, reason, "imported")
            n += 1
    return redirect("/settings#suppression", msg=f"Imported {n} address(es) into the do-not-email list.")


@app.post("/settings/suppression/add")
def suppression_add(email: str = Form(...)):
    e = emails.normalize(email)
    if not e:
        return redirect("/settings#suppression", err="That email address doesn't look right.")
    db.suppress(conn(), e, "manual", "added by hand")
    return redirect("/settings#suppression", msg=f"{e} will never be emailed.")


# ------------------------------------------------------------------ new campaign
@app.get("/campaigns/new", response_class=HTMLResponse)
def new_campaign_page(request: Request):
    c = conn()
    can_send = any(caps.account_state(a) in ("ok", "paused") for a in c.execute("SELECT * FROM accounts"))
    return page(request, "new.html", sa_email=listio.service_account_email(), can_send=can_send)


@app.post("/campaigns/new")
async def new_campaign(request: Request, name: str = Form(""), sheet_url: str = Form(""),
                       file: UploadFile = File(None)):
    wants_json = "application/json" in request.headers.get("accept", "")

    def fail(message):
        if wants_json:
            return JSONResponse({"error": message}, status_code=400)
        return redirect("/campaigns/new", err=message)

    c = conn()
    now = time.time()
    if file is not None and file.filename:
        stype = listio.source_type_for_filename(file.filename)
        if not stype:
            return fail("Please choose an .xlsx or .csv file. (Old .xls files: open in Excel and 'Save As' .xlsx.)")
        data = await file.read()
        if len(data) > MAX_UPLOAD:
            return fail("That file is too big (max 15 MB).")
        cur = c.execute("INSERT INTO campaigns(name,created_at,source_type,source_ref,original_name) VALUES(?,?,?,?,?)",
                        (name.strip() or Path(file.filename).stem, now, stype, "", file.filename))
        cid = cur.lastrowid
        safe = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(file.filename).name)
        dest = config.data_dir() / "files" / str(cid) / safe
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        try:
            listio.open_source(stype, str(dest)).tabs()
        except Exception as e:
            c.execute("DELETE FROM campaigns WHERE id=?", (cid,))
            return fail(f"Couldn't read that file: {e}")
        c.execute("UPDATE campaigns SET source_ref=? WHERE id=?", (str(dest), cid))
    elif sheet_url.strip():
        url = sheet_url.strip()
        try:
            src = listio.open_source("gsheet", url)
            tabs = src.tabs()
            title = getattr(getattr(src, "_sh", None), "title", "") or "Google Sheet"
        except ValueError as e:
            return fail(str(e))
        except Exception as e:
            sa = listio.service_account_email() or "the service account"
            log.warning("Sheet open failed: %r", e)
            return fail(f"The tool can't open this sheet. In Google Sheets click Share and add {sa} as Editor, "
                        f"then try again. (Details: {type(e).__name__} {str(e)[:150]})")
        cur = c.execute("INSERT INTO campaigns(name,created_at,source_type,source_ref,original_name,tab) "
                        "VALUES(?,?,?,?,?,?)", (name.strip() or title, now, "gsheet", url, title, tabs[0] if tabs else None))
        cid = cur.lastrowid
    else:
        return fail("Choose a file or paste a Google Sheets link.")
    if wants_json:
        return {"id": cid, "redirect": f"/campaigns/{cid}/map"}
    return RedirectResponse(f"/campaigns/{cid}/map", status_code=303)


def _draft(c, cid):
    camp = campaigns.get(c, cid)
    if camp is None:
        raise LookupError
    return camp


@app.get("/campaigns/{cid}/map", response_class=HTMLResponse)
def map_page(request: Request, cid: int, tab: str = None, header_row: int = None):
    c = conn()
    camp = _draft(c, cid)
    if camp["status"] != "draft":
        return redirect(f"/campaigns/{cid}")
    src = listio.open_source(camp["source_type"], camp["source_ref"])
    try:
        tabs = src.tabs()
        tab = tab if tab in tabs else (camp["tab"] if camp["tab"] in tabs else tabs[0])
        grid = src.read(tab)
    except Exception as e:
        log.warning("Couldn't read list for campaign %s: %r", cid, e)
        msg = ("The driver list for this draft can't be opened. The file may have been removed from the server, "
               "or the Google Sheet is no longer shared with the tool.")
        return page(request, "error.html", message=msg, details=f"{type(e).__name__}: {str(e)[:300]}",
                    delete_draft=cid)
    detected_row = columns.find_header_row(grid)
    if header_row is None:
        header_row = camp["header_row"] if camp["header_row"] and camp["tab"] == tab else (detected_row or 1)
    header = grid[header_row - 1] if 0 < header_row <= len(grid) else []
    if camp["mapping"] and camp["tab"] == tab and camp["header_row"] == header_row:
        mapping = camp["mapping"]
    else:
        mapping = columns.detect_mapping(header)
    width = max([len(r) for r in grid[:header_row + 5]] + [len(header), 1])
    notes_new_col = columns.notes_insert_col(grid, header_row)
    data_rows = sum(1 for r in grid[header_row:] if any(str(x).strip() for x in r))
    return page(request, "map.html", camp=camp, tabs=tabs, tab=tab, header_row=header_row, detected_row=detected_row,
                header=header, width=width, mapping=mapping, preview=grid[header_row:header_row + 5],
                top_rows=grid[:10], notes_new_col=notes_new_col, data_rows=data_rows,
                fields=columns.FIELDS, labels=columns.FIELD_LABELS)


@app.post("/campaigns/{cid}/map")
async def map_save(request: Request, cid: int):
    form = await request.form()
    c = conn()
    camp = _draft(c, cid)
    tab = form.get("tab") or None
    header_row = int(form.get("header_row") or 1)
    mapping = {}
    for f in columns.FIELDS:
        v = form.get(f"col_{f}") or ""
        if v.isdigit() and int(v) > 0:
            mapping[f] = int(v)
    back = f"/campaigns/{cid}/map?tab={quote(tab or '')}&header_row={header_row}"
    if "email" not in mapping:
        return redirect(back, err="Choose which column has the EMAIL addresses.")
    if not ({"name", "first_name"} & mapping.keys()) and not form.get("no_name"):
        return redirect(back, err="Choose the name column — or tick \"This list has no names\" (then everyone gets \"Hi there\").")
    used = [v for k, v in mapping.items()]
    if len(used) != len(set(used)):
        return redirect(back, err="The same column is chosen twice. Each column can be used once.")
    src = listio.open_source(camp["source_type"], camp["source_ref"])
    grid = src.read(tab)
    notes_col = mapping.get("notes") or columns.notes_insert_col(grid, header_row)
    skip_sent = 1 if form.get("skip_sent") else 0
    c.execute("UPDATE campaigns SET tab=?, header_row=?, mapping=?, notes_col=?, notes_header_missing=?, skip_sent=? "
              "WHERE id=?", (tab, header_row, json.dumps(mapping), notes_col, 0 if "notes" in mapping else 1,
                             skip_sent, cid))
    campaigns.build_recipients(c, cid, grid, header_row, mapping, notes_col, bool(skip_sent))
    return RedirectResponse(f"/campaigns/{cid}/compose", status_code=303)


# ------------------------------------------------------------------ compose + review
@app.get("/campaigns/{cid}/compose", response_class=HTMLResponse)
def compose_page(request: Request, cid: int):
    c = conn()
    camp = _draft(c, cid)
    if camp["status"] != "draft":
        return redirect(f"/campaigns/{cid}")
    return page(request, "compose.html", camp=camp, placeholders=PLACEHOLDER_HELP,
                check=checks.check_content(camp["subject"], camp["body"]))


@app.post("/campaigns/{cid}/compose")
def compose_save(cid: int, subject: str = Form(""), body: str = Form("")):
    c = conn()
    camp = _draft(c, cid)
    if camp["status"] != "draft":
        return redirect(f"/campaigns/{cid}")
    c.execute("UPDATE campaigns SET subject=?, body=? WHERE id=?", (subject.strip(), body.strip(), cid))
    return RedirectResponse(f"/campaigns/{cid}/review", status_code=303)


@app.post("/api/check")
async def api_check(request: Request):
    data = await request.json()
    return checks.check_content(data.get("subject", ""), data.get("body", ""))


@app.post("/api/campaigns/{cid}/preview")
async def api_preview(request: Request, cid: int):
    """Render the typed (unsaved) subject/body for the first driver who will get it. Read-only: saves nothing."""
    c = conn()
    _draft(c, cid)
    data = await request.json()
    r = c.execute("SELECT * FROM recipients WHERE campaign_id=? AND status='queued' ORDER BY row_num LIMIT 1",
                  (cid,)).fetchone()
    vars_ = _vars(r) if r else {"first_name": "", "name": "", "last_name": "", "position": ""}
    unsub = compose.unsub_url(r["id"], r["email"]) if r else f"{config.public_base_url()}/u/…"
    subject, text, _ = compose.render_parts(data.get("subject", ""), data.get("body", ""), vars_, unsub)
    acc = next((a for a in c.execute("SELECT * FROM accounts ORDER BY id")
                if caps.account_state(a) in ("ok", "paused")), None)
    return {"subject": subject, "text": text, "to": (r["email"] if r else ""), "row": (r["row_num"] if r else None),
            "name": (r["name"] if r else ""),
            "from": f"{acc['display_name']} <{acc['email']}>" if acc else "(no Gmail account yet)"}


def start_blockers(c, camp, summary=None) -> list:
    blockers = []
    accs = c.execute("SELECT * FROM accounts").fetchall()
    usable = [a for a in accs if caps.account_state(a) in ("ok", "paused")]
    if not accs:
        blockers.append("No Gmail account yet. Add one in Settings.")
    elif not usable:
        unnamed = [a["email"] for a in accs if caps.account_state(a) == "no_name"]
        blockers.append("No Gmail account can send."
                        + (f" These need a sender name in Settings: {', '.join(unnamed)}." if unnamed else
                           " Fix the stopped accounts in Settings."))
    blockers += checks.check_content(camp["subject"], camp["body"])["blocks"]
    summary = summary or campaigns.summary(c, camp["id"])
    if summary["will_send"] == 0:
        blockers.append("Nobody to send to: every row was skipped (see the summary).")
    return blockers


def _vars(r):
    return {"first_name": r["first_name"], "last_name": r["last_name"], "name": r["name"], "position": r["position"]}


@app.get("/campaigns/{cid}/review", response_class=HTMLResponse)
def review_page(request: Request, cid: int):
    c = conn()
    camp = _draft(c, cid)
    if camp["status"] != "draft":
        return redirect(f"/campaigns/{cid}")
    summ = campaigns.summary(c, cid)
    acc = next((a for a in c.execute("SELECT * FROM accounts ORDER BY id")
                if caps.account_state(a) in ("ok", "paused")), None)
    queued = c.execute("SELECT * FROM recipients WHERE campaign_id=? AND status='queued'", (cid,)).fetchall()
    sample = random.sample(queued, min(3, len(queued)))
    previews = []
    for r in sample:
        subject, text, _ = compose.render_parts(camp["subject"], camp["body"], _vars(r),
                                                compose.unsub_url(r["id"], r["email"]))
        previews.append({"row": r["row_num"], "to": r["email"], "subject": subject, "text": text})
    blockers = start_blockers(c, camp, summ)
    usable = [a for a in c.execute("SELECT * FROM accounts") if caps.account_state(a) in ("ok", "paused")]
    window = db.get_setting(c, "window")
    return page(request, "review.html", camp=camp, s=summ, previews=previews, blockers=blockers,
                account_blocked=not usable, n_accounts=len(usable), window_text=window_text(window),
                sender=(f"{acc['display_name']} <{acc['email']}>" if acc else "(no Gmail account yet)"),
                test_inbox=db.get_setting(c, "test_inbox"), check=checks.check_content(camp["subject"], camp["body"]))


def window_text(window) -> str:
    """'Mon–Fri, 9:00 AM–4:00 PM' (Central) for sentences on the page."""
    days = window["days"]
    names = timeutil.DAY_NAMES
    if not days:
        d = "no days selected"
    elif days == list(range(days[0], days[-1] + 1)) and len(days) > 2:
        d = f"{names[days[0]]}–{names[days[-1]]}"
    else:
        d = ", ".join(names[i] for i in days)

    def t(hm):
        h, m = map(int, hm.split(":"))
        return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
    return f"{d}, {t(window['start'])}–{t(window['end'])}"


templates.env.globals["window_text"] = window_text


@app.post("/campaigns/{cid}/fix-typos")
async def fix_typos(request: Request, cid: int):
    form = await request.form()
    ids = [int(x) for x in form.getlist("fix") if str(x).isdigit()]
    n = campaigns.apply_typo_fixes(conn(), cid, ids)
    return redirect(f"/campaigns/{cid}/review", msg=f"Fixed {n} address(es).")


@app.post("/api/campaigns/{cid}/test-send")
def test_send(cid: int, to: str = Form("")):
    c = conn()
    camp = _draft(c, cid)
    to = emails.normalize(to)
    if not to or not emails.is_valid_syntax(to):
        return JSONResponse({"ok": False, "message": "Enter your email address."})
    blocks = checks.check_content(camp["subject"], camp["body"])["blocks"]
    if blocks:
        return JSONResponse({"ok": False, "message": " ".join(blocks)})
    now = time.time()
    schedule = db.get_setting(c, "warmup")
    # A test email is always really sent (also in dry run): it goes only to the one address typed in the box,
    # so you can see the real email in your own inbox. It counts toward the account's real daily cap.
    acc = next((a for a in c.execute("SELECT * FROM accounts ORDER BY id")
                if caps.account_state(a, now) == "ok"
                and caps.sent_last_24h(c, a["id"], now) < caps.account_cap(a, schedule, now)), None)
    if acc is None:
        return JSONResponse({"ok": False, "message": "No Gmail account is available: add one, give it a sender "
                                                     "name, or wait until one is under its daily cap."})
    r = c.execute("SELECT * FROM recipients WHERE campaign_id=? AND status='queued' ORDER BY row_num LIMIT 1",
                  (cid,)).fetchone()
    vars_ = _vars(r) if r else {"first_name": "", "name": "", "last_name": "", "position": ""}
    unsub = f"{config.public_base_url()}/u/{crypto.make_unsub_token(0, to)}"
    msg = compose.build_message(acc["email"], acc["display_name"], to, "", camp["subject"], camp["body"], vars_,
                                unsub, compose.new_message_id())
    try:
        smtp_send.send(acc["email"], crypto.decrypt(acc["password_enc"]), msg)
    except smtp_send.SendError as e:
        if e.kind in ("auth", "login_dropped"):
            return JSONResponse({"ok": False, "message": f"Gmail did not accept the App Password of {acc['email']}. "
                                 "Create a new one and save it under New App Password in Settings."})
        return JSONResponse({"ok": False, "message": f"Gmail refused: {smtp_send.short_reason(e.message)}"})
    info = f"Sent from {acc['email']} to {to}. Check your Inbox (and Spam, just in case)."
    if config.dry_run():
        info += " (Campaigns are still in dry run: drivers get nothing until you switch to live.)"
    c.execute("INSERT INTO send_log(account_id,recipient_id,ts,kind,dry) VALUES(?,?,?,'test',0)",
              (acc["id"], None, now))
    db.set_setting(c, "test_inbox", to)
    return {"ok": True, "message": info}


@app.post("/campaigns/{cid}/start")
def start(cid: int):
    c = conn()
    camp = _draft(c, cid)
    if camp["status"] != "draft":
        return redirect(f"/campaigns/{cid}")
    blockers = start_blockers(c, camp)
    if blockers:
        return redirect(f"/campaigns/{cid}/review", err=" ".join(blockers))
    with db.tx(c):
        campaigns.queue_skip_marks(c, cid)
        c.execute("UPDATE campaigns SET status='running', started_at=?, dry_run=? WHERE id=?",
                  (time.time(), 1 if config.dry_run() else 0, cid))
        db.add_event(c, "info", "Campaign started.", cid)
    return redirect(f"/campaigns/{cid}", msg="Started. You can close this page — sending continues on the server.")


@app.post("/campaigns/{cid}/delete")
def delete_draft(cid: int):
    c = conn()
    camp = _draft(c, cid)
    if camp["status"] != "draft":
        return redirect(f"/campaigns/{cid}", err="Only drafts can be deleted. Use Stop instead.")
    with db.tx(c):
        c.execute("DELETE FROM recipients WHERE campaign_id=?", (cid,))
        c.execute("DELETE FROM campaigns WHERE id=?", (cid,))
    return redirect("/", msg="Draft deleted.")


# ------------------------------------------------------------------ progress
@app.get("/campaigns/{cid}", response_class=HTMLResponse)
def progress_page(request: Request, cid: int):
    c = conn()
    camp = _draft(c, cid)
    if camp["status"] == "draft":
        return RedirectResponse(f"/campaigns/{cid}/review", status_code=303)
    return page(request, "progress.html", camp=camp, status_label=STATUS_LABELS.get(camp["status"], camp["status"]))


@app.get("/api/campaigns/{cid}/stats")
def stats(cid: int):
    c = conn()
    camp = _draft(c, cid)
    now = time.time()
    rates = outcomes.campaign_rates(c, cid)
    by = {r["status"]: r["n"] for r in c.execute(
        "SELECT status, COUNT(*) n FROM recipients WHERE campaign_id=? GROUP BY status", (cid,))}
    per_acc = {r["account_id"]: dict(r) for r in c.execute(
        """SELECT account_id, SUM(status='sent') sent, SUM(bounced_at IS NOT NULL) bounces,
           SUM(unsubscribed_at IS NOT NULL) unsubs, SUM(replied_at IS NOT NULL) replies, SUM(status='failed') errors
           FROM recipients WHERE campaign_id=? AND account_id IS NOT NULL GROUP BY account_id""", (cid,))}
    accs = []
    for a in account_rows(c, now):
        p = per_acc.get(a["id"], {})
        accs.append({"id": a["id"], "email": a["email"], "name": a["display_name"], "state": a["state"],
                     "state_label": ACCOUNT_STATE_LABELS[a["state"]], "sent_24h": a["sent_24h"], "cap": a["cap"],
                     "paused_until": a["paused_until_ct"], "disabled_reason": a["disabled_reason"],
                     "next_in": max(0, int((a["next_send_at"] or 0) - now)),
                     "sent": p.get("sent") or 0, "bounces": p.get("bounces") or 0, "unsubs": p.get("unsubs") or 0,
                     "replies": p.get("replies") or 0, "errors": p.get("errors") or 0})
    window = db.get_setting(c, "window")
    inside = timeutil.in_window(now, window)
    nxt = timeutil.next_window_start(now, window)
    window_info = "Inside the sending window — sending now." if inside else (
        f"Outside the sending window. Next start: {nxt:%a %m/%d %I:%M %p} CT "
        f"({nxt.astimezone(timeutil.TASHKENT):%H:%M} Tashkent)." if nxt else "No sending days are selected in Settings.")
    if config.dry_run_fast():
        window_info = "DRY RUN (fast demo): window and gaps ignored."
    pending = c.execute("SELECT COUNT(*) FROM marks WHERE campaign_id=? AND written_at IS NULL AND unmatched=0",
                        (cid,)).fetchone()[0]
    unmatched = [dict(r) for r in c.execute(
        "SELECT row_num, email, note FROM marks WHERE campaign_id=? AND unmatched=1 ORDER BY id", (cid,))]
    total_marks = c.execute("SELECT COUNT(*) FROM marks WHERE campaign_id=?", (cid,)).fetchone()[0]
    last_err = c.execute("SELECT last_error FROM marks WHERE campaign_id=? AND written_at IS NULL AND last_error IS NOT "
                         "NULL ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    # Rates: shown as a heads-up while running (the campaign pauses itself; thresholds live in core/outcomes.py).
    warnings = []
    if camp["status"] == "running":
        if rates["sent"] >= 10 and rates["bounce_rate"] > outcomes.BOUNCE_LIMIT:
            warnings.append(f"Bounce rate is {rates['bounce_rate']:.1%}. The campaign pauses itself above "
                            f"{outcomes.BOUNCE_LIMIT:.0%} once {outcomes.BOUNCE_MIN} emails are sent.")
        if rates["sent"] >= 10 and rates["unsub_rate"] > outcomes.UNSUB_LIMIT:
            warnings.append(f"Unsubscribe rate is {rates['unsub_rate']:.1%}. The campaign pauses itself above "
                            f"{outcomes.UNSUB_LIMIT:.0%} once {outcomes.UNSUB_MIN} emails are sent.")
    stopped = [a for a in accs if a["state"] == "disabled"]
    unnamed = [a for a in accs if a["state"] == "no_name"]
    resting = [a for a in accs if a["state"] == "paused"]
    usable = [a for a in accs if a["state"] in ("ok", "paused")]
    acct_problem = "; ".join(p for p in (
        f"{len(stopped)} account(s) stopped" if stopped else "",
        f"{len(unnamed)} account(s) need a sender name" if unnamed else "") if p)
    banner = _status_banner(camp, inside, nxt, accs, usable, resting, rates, by)
    return {
        "status": camp["status"], "status_label": STATUS_LABELS.get(camp["status"], camp["status"]),
        "reason": camp["status_reason"], "dry_run": bool(camp["dry_run"]),
        "sent": rates["sent"], "remaining": by.get("queued", 0) + by.get("sending", 0),
        "bounces": rates["bounces"], "unsubs": rates["unsubs"], "replies": rates["replies"],
        "errors": by.get("failed", 0), "skipped": by.get("skipped", 0),
        "accounts": accs, "window": window_info, "banner": banner, "warnings": warnings,
        "account_problem": acct_problem, "next_email": _next_email_text(accs, inside, camp["status"]),
        "alerts": alerts(c, cid, 8),
        "marks": {"pending": pending, "total": total_marks, "unmatched": unmatched,
                  "last_error": last_err[0] if last_err else None},
        "estimate": campaigns.estimate_finish(c, by.get("queued", 0), now) if camp["status"] == "running" else None,
    }


def _next_email_text(accs, inside, status) -> str:
    waits = [a["next_in"] for a in accs if a["state"] == "ok"]
    if status != "running" or not inside or not waits:
        return ""
    soon = min(waits)
    return "Next email any moment now." if soon <= 30 else f"Next email in about {max(1, round(soon / 60))} min."


def _status_banner(camp, inside, nxt, accs, usable, resting, rates, by) -> dict:
    """One sentence that says what is happening and whether the user needs to act. tone: green|amber|red|grey."""
    st = camp["status"]
    if st == "done":
        return {"tone": "green", "title": "Finished", "text": "Every driver on the list was handled."}
    if st == "stopped":
        return {"tone": "grey", "title": "Stopped", "text": "Drivers not yet emailed will not be emailed by this campaign."}
    if st == "paused":
        auto = (camp["status_reason"] or "").startswith("Paused automatically")
        text = (camp["status_reason"] or "").replace("Paused automatically: ", "", 1) if auto else \
            "Nothing is sent until you press Resume."
        return {"tone": "red" if auto else "amber", "title": "Paused automatically" if auto else "Paused by you",
                "text": text}
    # running
    if not by.get("queued", 0) and by.get("sending", 0):
        return {"tone": "green", "title": "Almost done", "text": "The last emails are being confirmed."}
    if not usable:
        return {"tone": "red", "title": "Not sending",
                "text": "No Gmail account can send right now. Fix the accounts below; sending continues by itself."}
    if config.dry_run_fast():
        return {"tone": "green", "title": "Sending (fast demo)", "text": "Dry run: the window and gaps are ignored."}
    if not inside:
        when = (f"{nxt:%a %m/%d at %I:%M %p} CT ({nxt.astimezone(timeutil.TASHKENT):%H:%M} Tashkent)" if nxt
                else None)
        return {"tone": "amber", "title": "Waiting for the sending window",
                "text": f"Sending starts again {when}." if when else
                "No sending days are selected. Choose days under Settings → Sending rules."}
    if len(resting) == len(usable):
        return {"tone": "amber", "title": "Accounts are resting",
                "text": "Every account is resting after a Gmail limit and starts again by itself."}
    return {"tone": "green", "title": "Sending now",
            "text": "One email at a time from each account, 1.5–5 minutes apart. You can close this page."}


@app.post("/campaigns/{cid}/pause")
def pause(cid: int):
    conn().execute("UPDATE campaigns SET status='paused', status_reason='Paused by you.' WHERE id=? AND status='running'",
                   (cid,))
    return redirect(f"/campaigns/{cid}")


@app.post("/campaigns/{cid}/resume")
def resume(cid: int):
    c = conn()
    r = outcomes.campaign_rates(c, cid)
    c.execute("UPDATE campaigns SET status='running', status_reason=NULL, ack_bounces=?, ack_unsubs=? "
              "WHERE id=? AND status='paused'", (r["bounces"], r["unsubs"], cid))
    return redirect(f"/campaigns/{cid}")


@app.post("/campaigns/{cid}/stop")
def stop(cid: int):
    c = conn()
    c.execute("UPDATE campaigns SET status='stopped', status_reason='Stopped by you. Drivers not yet emailed will not be.'"
              ", finished_at=? WHERE id=? AND status IN ('running','paused')", (time.time(), cid))
    return redirect(f"/campaigns/{cid}")


def _log_status(r) -> str:
    """One readable status per row; the latest outcome wins (unsubscribed > replied > bounced > sent)."""
    d = timeutil.us_date
    if r["unsubscribed_at"]:
        return f"Unsubscribed {d(r['unsubscribed_at'])}"
    if r["replied_at"]:
        return f"Replied {d(r['replied_at'])}"
    if r["bounced_at"]:
        return f"Bounced {d(r['bounced_at'])}"
    if r["status"] == "sent":
        return f"Sent {d(r['sent_at'])}" if r["sent_at"] else "Sent"
    if r["status"] == "skipped":
        return f"Not sent - {SKIP_LABELS.get(r['skip_reason'], r['skip_reason'] or 'skipped')}"
    if r["status"] == "failed":
        return f"Failed - {r['error']}" if r["error"] else "Failed"
    if r["status"] == "sending":
        return "Sending"
    return "Waiting"


@app.get("/campaigns/{cid}/log.csv")
def log_csv(cid: int):
    c = conn()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["row", "email", "name", "status"])
    for r in c.execute("SELECT * FROM recipients WHERE campaign_id=? ORDER BY row_num", (cid,)):
        w.writerow([r["row_num"], r["email"] or r["raw_email"], r["name"], _log_status(r)])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="campaign-{cid}-log.csv"'})


def _marked_name(camp) -> str:
    p = Path(camp["original_name"] or f"list-{camp['id']}.xlsx")
    return f"{p.stem} - marked{p.suffix}"


@app.get("/campaigns/{cid}/download")
def download_marked(cid: int):
    c = conn()
    camp_row = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if camp_row is None:
        raise LookupError
    if camp_row["source_type"] == "gsheet":
        return redirect(f"/campaigns/{cid}", err="This campaign uses a Google Sheet; notes are written there directly.")
    try:
        flush_campaign(c, camp_row)
    except Exception as e:
        log.warning("flush before download failed: %s", e)
    data = Path(camp_row["source_ref"]).read_bytes()
    name = _marked_name(camp_row)
    mt = "text/csv" if camp_row["source_type"] == "csv" else \
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(data, media_type=mt, headers={
        "Content-Disposition": f"attachment; filename=\"marked.{name.rsplit('.', 1)[-1]}\"; filename*=UTF-8''{quote(name)}"})


@app.post("/api/campaigns/{cid}/mark-file")
async def mark_file(cid: int, file: UploadFile = File(...)):
    c = conn()
    camp_row = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if camp_row is None:
        raise LookupError
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        return JSONResponse({"error": "File too big."}, status_code=400)
    try:
        out = mark_file_bytes(c, camp_row, data, file.filename or camp_row["original_name"])
    except Exception as e:
        return JSONResponse({"error": f"Couldn't update the file: {e}"}, status_code=400)
    return Response(out, media_type="application/octet-stream")


# ------------------------------------------------------------------ public unsubscribe
def _unsub_lookup(c, token: str):
    """('test'|'invalid'|'valid', recipient row or None). Changes nothing."""
    rid = crypto.parse_unsub_token(token)
    if rid is None:
        return "invalid", None
    if rid == 0:
        return "test", None
    r = c.execute("SELECT * FROM recipients WHERE id=?", (rid,)).fetchone()
    if r is None or not crypto.verify_unsub_token(token, r["email"] or ""):
        return "invalid", None
    return "valid", r


def _unsub(token: str):
    c = conn()
    state, r = _unsub_lookup(c, token)
    if state != "valid":
        return state
    outcomes.record(c, "unsubscribed", r if r["status"] == "sent" else None, r["email"])
    return "ok"


@app.get("/u/{token}", response_class=HTMLResponse)
def unsub_get(request: Request, token: str):
    """Opening the link only shows a Confirm button: mail scanners that open every link don't unsubscribe anyone."""
    c = conn()
    state, r = _unsub_lookup(c, token)
    if state == "valid":
        state = "already" if db.suppression_reason(c, r["email"]) in ("unsubscribed", "manual") else "confirm"
    return templates.TemplateResponse(request, "unsubscribed.html", {"state": state, "token": token})


@app.post("/u/{token}")
async def unsub_post(request: Request, token: str):
    """Our Confirm button (form field confirm=1) gets a page; Gmail's one-click POST
    (List-Unsubscribe=One-Click, RFC 8058) gets a plain 200, exactly as before."""
    form = await request.form()
    state = _unsub(token)
    if form.get("confirm"):
        return templates.TemplateResponse(request, "unsubscribed.html", {"state": state, "token": token},
                                          status_code=200 if state in ("ok", "test") else 400)
    return PlainTextResponse("Unsubscribed" if state in ("ok", "test") else "Invalid link",
                             status_code=200 if state in ("ok", "test") else 400)


@app.exception_handler(LookupError)
def not_found(request: Request, exc):
    resp = page(request, "error.html", message="That campaign or page doesn't exist. It may have been deleted.")
    resp.status_code = 404
    return resp
