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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    c = conn()
    rows = []
    for camp in c.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT 50").fetchall():
        d = dict(camp)
        d.update(outcomes.campaign_rates(c, camp["id"]))
        d["remaining"] = c.execute("SELECT COUNT(*) FROM recipients WHERE campaign_id=? AND status IN "
                                   "('queued','sending')", (camp["id"],)).fetchone()[0]
        d["link"], d["draft_step"] = _campaign_link(camp)
        rows.append(d)
    return page(request, "dashboard.html", campaigns=rows, alerts=alerts(c), accounts=account_rows(c))


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


@app.post("/alerts/{eid}/dismiss")
def dismiss(eid: int, back: str = Form("/")):
    conn().execute("UPDATE events SET dismissed=1 WHERE id=?", (eid,))
    return redirect(back if back.startswith("/") else "/")


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
        problems.append(f"Sending (SMTP) failed: {e}")
    try:
        imap_util.test_imap(email, password)
    except Exception as e:
        problems.append(f"Reading the inbox (IMAP) failed: {e}")
    return problems


@app.post("/settings/accounts/add")
def account_add(email: str = Form(...), display_name: str = Form(""), app_password: str = Form(...),
                skip_test: str = Form("")):
    c = conn()
    email = emails.normalize(email)
    pw = re.sub(r"\s+", "", app_password)
    if not email or not emails.is_valid_syntax(email):
        return redirect("/settings", err="That email address doesn't look right.")
    if c.execute("SELECT 1 FROM accounts WHERE email=?", (email,)).fetchone():
        return redirect("/settings", err=f"{email} is already added.")
    if not (config.dry_run() and skip_test):
        problems = _test_account(email, pw)
        if problems:
            return redirect("/settings", err=f"{email} was NOT added. " + " ".join(problems)
                            + " Check the App Password (16 letters) and that 2-Step Verification is on.")
    c.execute("INSERT INTO accounts(email,display_name,password_enc,added_at) VALUES(?,?,?,?)",
              (email, clean_sender_name(display_name), crypto.encrypt(pw), time.time()))
    if not clean_sender_name(display_name):
        return redirect("/settings", err=f"{email} added and tested, but it won't send until you enter its "
                                         "sender name (shown in the inbox).")
    return redirect("/settings", msg=f"{email} added and tested. It starts at the week-1 warm-up cap.")


def clean_sender_name(name: str) -> str:
    return re.sub(r"[\r\n\"<>]", " ", name or "").strip()[:80]


@app.post("/settings/accounts/{aid}/name")
def account_name(aid: int, display_name: str = Form("")):
    c = conn()
    name = clean_sender_name(display_name)
    c.execute("UPDATE accounts SET display_name=? WHERE id=?", (name, aid))
    if not name:
        return redirect("/settings", err="Sender name removed: this account will not send until it has one.")
    return redirect("/settings", msg=f"Sender name saved: {name}")


@app.post("/settings/accounts/{aid}/delete")
def account_delete(aid: int):
    c = conn()
    busy = c.execute("SELECT COUNT(*) FROM recipients WHERE account_id=? AND status='sending'", (aid,)).fetchone()[0]
    if busy:
        return redirect("/settings", err="This account is sending right now. Pause campaigns first, then remove it.")
    c.execute("DELETE FROM accounts WHERE id=?", (aid,))
    return redirect("/settings", msg="Account removed.")


@app.post("/settings/accounts/{aid}/password")
def account_password(aid: int, app_password: str = Form(...)):
    c = conn()
    a = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    pw = re.sub(r"\s+", "", app_password)
    problems = _test_account(a["email"], pw)
    if problems:
        return redirect("/settings", err="New App Password not saved. " + " ".join(problems))
    c.execute("UPDATE accounts SET password_enc=?, disabled_reason=NULL, paused_until=NULL, last_error=NULL WHERE id=?",
              (crypto.encrypt(pw), aid))
    return redirect("/settings", msg=f"{a['email']}: new App Password saved and account re-enabled.")


@app.post("/settings/accounts/{aid}/reenable")
def account_reenable(aid: int):
    c = conn()
    a = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    problems = _test_account(a["email"], crypto.decrypt(a["password_enc"])) if not config.dry_run() else []
    if problems:
        return redirect("/settings", err=f"{a['email']} still fails: " + " ".join(problems))
    c.execute("UPDATE accounts SET disabled_reason=NULL, paused_until=NULL, last_error=NULL WHERE id=?", (aid,))
    return redirect("/settings", msg=f"{a['email']} re-enabled.")


@app.post("/api/accounts/{aid}/test")
def account_test(aid: int):
    c = conn()
    a = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    if not a:
        return JSONResponse({"ok": False, "message": "Account not found."})
    problems = _test_account(a["email"], crypto.decrypt(a["password_enc"]))
    return {"ok": not problems, "message": " ".join(problems) or "Sending and inbox reading both work."}


@app.post("/settings/window")
async def window_save(request: Request):
    form = await request.form()
    days = sorted(int(d) for d in form.getlist("days") if str(d).isdigit() and 0 <= int(d) <= 6)
    start, end = form.get("start", "09:00"), form.get("end", "16:00")
    if not re.match(r"^\d\d:\d\d$", start) or not re.match(r"^\d\d:\d\d$", end) or start >= end:
        return redirect("/settings", err="The start time must be before the end time.")
    db.set_setting(conn(), "window", {"days": days, "start": start, "end": end})
    return redirect("/settings", msg="Sending window saved.")


@app.post("/settings/caps")
async def caps_save(request: Request):
    form = await request.form()
    c = conn()
    try:
        warm = [int(form.get(f"w{i}")) for i in range(4)]
    except (TypeError, ValueError):
        return redirect("/settings", err="Warm-up caps must be whole numbers.")
    if any(not 1 <= w <= caps.HARD_MAX for w in warm):
        return redirect("/settings", err=f"Caps must be between 1 and {caps.HARD_MAX}.")
    db.set_setting(c, "warmup", warm)
    for a in c.execute("SELECT id FROM accounts").fetchall():
        v = (form.get(f"cap_{a['id']}") or "").strip()
        if v and (not v.isdigit() or not 1 <= int(v) <= caps.HARD_MAX):
            return redirect("/settings", err=f"Caps must be between 1 and {caps.HARD_MAX}.")
        c.execute("UPDATE accounts SET cap_override=? WHERE id=?", (int(v) if v else None, a["id"]))
    risky = max(warm + [int(form.get(f"cap_{a['id']}") or 0) for a in c.execute("SELECT id FROM accounts")])
    if risky > caps.RISKY_ABOVE:
        return redirect("/settings", err=f"Saved — but a cap above {caps.RISKY_ABOVE}/day is a high risk of the free "
                                         "Gmail account being locked.")
    return redirect("/settings", msg="Daily caps saved.")


@app.post("/settings/recent-days")
def recent_days_save(recent_days: str = Form("")):
    v = recent_days.strip()
    if not v.isdigit() or not 0 <= int(v) <= 365:
        return redirect("/settings#repeat", err="Enter a number of days from 0 to 365 (0 turns the check off).")
    db.set_setting(conn(), "recent_days", int(v))
    msg = (f"Saved: drivers emailed by any campaign in the last {int(v)} days are skipped." if int(v)
           else "Saved: the repeat-email check is off.")
    return redirect("/settings#repeat", msg=msg)


@app.post("/settings/test-inbox")
def test_inbox_save(test_inbox: str = Form("")):
    e = emails.normalize(test_inbox)
    if test_inbox.strip() and not emails.is_valid_syntax(e):
        return redirect("/settings", err="That email address doesn't look right.")
    db.set_setting(conn(), "test_inbox", e)
    return redirect("/settings", msg="Saved.")


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
    return redirect("/settings", msg=f"Imported {n} address(es) into the do-not-email list.")


@app.post("/settings/suppression/add")
def suppression_add(email: str = Form(...)):
    e = emails.normalize(email)
    if not e:
        return redirect("/settings", err="That email address doesn't look right.")
    db.suppress(conn(), e, "manual", "added by hand")
    return redirect("/settings", msg=f"{e} will never be emailed.")


# ------------------------------------------------------------------ new campaign
@app.get("/campaigns/new", response_class=HTMLResponse)
def new_campaign_page(request: Request):
    return page(request, "new.html", sa_email=listio.service_account_email())


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
        return page(request, "error.html", message=f"Couldn't read the list: {e}")
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
    return page(request, "review.html", camp=camp, s=summ, previews=previews, blockers=start_blockers(c, camp, summ),
                sender=(f"{acc['display_name']} <{acc['email']}>" if acc else "(no Gmail account yet)"),
                test_inbox=db.get_setting(c, "test_inbox"), check=checks.check_content(camp["subject"], camp["body"]))


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
    dry = config.dry_run()
    acc = next((a for a in c.execute("SELECT * FROM accounts ORDER BY id")
                if caps.account_state(a, now) == "ok"
                and caps.sent_last_24h(c, a["id"], now, dry=dry) < caps.account_cap(a, schedule, now)), None)
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
        if config.dry_run():
            p = config.data_dir() / "outbox" / "tests" / f"test-{int(now)}.eml"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(bytes(msg))
            info = f"DRY RUN: saved to {p} instead of sending."
        else:
            smtp_send.send(acc["email"], crypto.decrypt(acc["password_enc"]), msg)
            info = f"Sent from {acc['email']} to {to}. Check your Inbox (and Spam, just in case)."
    except smtp_send.SendError as e:
        return JSONResponse({"ok": False, "message": f"Gmail refused: {smtp_send.short_reason(e.message)}"})
    c.execute("INSERT INTO send_log(account_id,recipient_id,ts,kind,dry) VALUES(?,?,?,'test',?)",
              (acc["id"], None, now, 1 if dry else 0))
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
    return page(request, "progress.html", camp=camp)


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
        accs.append({"email": a["email"], "state": a["state"], "sent_24h": a["sent_24h"], "cap": a["cap"],
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
    red = []
    if rates["sent"] >= 10 and rates["bounce_rate"] > outcomes.BOUNCE_LIMIT:
        red.append(f"High bounce rate: {rates['bounce_rate']:.1%} (limit 3%).")
    if rates["sent"] >= 10 and rates["unsub_rate"] > outcomes.UNSUB_LIMIT:
        red.append(f"High unsubscribe rate: {rates['unsub_rate']:.1%} (limit 2%).")
    for a in accs:
        if a["state"] == "disabled":
            red.append(f"{a['email']} is stopped: {a['disabled_reason']}. Fix it in Settings.")
        elif a["state"] == "no_name":
            red.append(f"{a['email']} is not sending: it has no sender name. Add one in Settings.")
    return {
        "status": camp["status"], "reason": camp["status_reason"], "dry_run": bool(camp["dry_run"]),
        "sent": rates["sent"], "remaining": by.get("queued", 0) + by.get("sending", 0),
        "bounces": rates["bounces"], "unsubs": rates["unsubs"], "replies": rates["replies"],
        "errors": by.get("failed", 0), "skipped": by.get("skipped", 0),
        "accounts": accs, "window": window_info, "red": red,
        "alerts": alerts(c, cid, 8),
        "marks": {"pending": pending, "total": total_marks, "unmatched": unmatched,
                  "last_error": last_err[0] if last_err else None},
        "estimate": campaigns.estimate_finish(c, by.get("queued", 0), now) if camp["status"] == "running" else None,
    }


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


@app.get("/campaigns/{cid}/log.csv")
def log_csv(cid: int):
    c = conn()
    accs = {a["id"]: a["email"] for a in c.execute("SELECT id,email FROM accounts")}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["row", "email", "name", "status", "skip reason", "sent from", "sent at (CT)", "bounced", "replied",
                "unsubscribed", "error"])
    for r in c.execute("SELECT * FROM recipients WHERE campaign_id=? ORDER BY row_num", (cid,)):
        w.writerow([r["row_num"], r["email"] or r["raw_email"], r["name"], r["status"],
                    SKIP_LABELS.get(r["skip_reason"], r["skip_reason"] or ""), accs.get(r["account_id"], ""),
                    timeutil.fmt_central(r["sent_at"]), timeutil.fmt_central(r["bounced_at"]),
                    timeutil.fmt_central(r["replied_at"]), timeutil.fmt_central(r["unsubscribed_at"]), r["error"] or ""])
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
