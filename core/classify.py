"""Classify incoming mail: bounce, unsubscribe, reply, auto-reply or ignore."""
import email
import re
from email import policy
from email.utils import getaddresses, parseaddr

from .compose import FOOTER_MARKER

ADDR_RE = re.compile(r"[A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MSGID_RE = re.compile(r"<[^<>\s]+>")
UNSUB_RE = re.compile(r"\b(unsubscribe|unsub|opt[\s-]?out|remove me|remove my (e-?mail|address|name|info)|"
                      r"take me off|not interested|no longer interested|stop (e-?mailing|sending|contacting|messaging)|"
                      r"please stop|do not (e-?mail|contact)|don'?t (e-?mail|contact)|leave me alone)\b", re.I)
SHORT_NO = {"stop", "remove", "no", "no thanks", "no thank you", "nope", "pass"}
BOUNCE_SUBJECT_RE = re.compile(r"(delivery status notification \(failure\)|undeliver|mail delivery (failed|subsystem)|"
                               r"returned mail|delivery failure|failure notice|could not be delivered)", re.I)
AUTO_SUBJECT_RE = re.compile(r"^(automatic reply|auto(matic)?[- ]?reply|out of (the )?office|ooo\b|away from)", re.I)
QUOTE_CUTS = [re.compile(r"^\s*On .{0,200}wrote:\s*$", re.M | re.S),
              re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.M | re.I),
              re.compile(r"^\s*From:\s.+$", re.M),
              re.compile(r"^_{5,}", re.M),
              re.compile(r"^\s*Sent from my ", re.M)]


def parse(raw: bytes):
    return email.message_from_bytes(raw, policy=policy.default)


def _text_of(msg) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        txt = part.get_content()
        if part.get_content_type() == "text/html":
            txt = re.sub(r"<br\s*/?>|</p>|</div>", "\n", txt, flags=re.I)
            txt = re.sub(r"<blockquote.*?</blockquote>", "", txt, flags=re.I | re.S)
            txt = re.sub(r"<[^>]+>", "", txt)
        return txt
    except Exception:
        return ""


def new_text(body: str) -> str:
    """Only what the person wrote: drop quoted lines and anything after a quote header or our footer."""
    cut = len(body)
    for rx in QUOTE_CUTS:
        m = rx.search(body)
        if m:
            cut = min(cut, m.start())
    i = body.find(FOOTER_MARKER)
    if i != -1:
        cut = min(cut, i)
    lines = [ln for ln in body[:cut].splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def _delivery_status_failures(msg):
    failed, has_report, only_delayed = [], False, True
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            has_report = True
            payload = part.get_payload()
            blocks = payload if isinstance(payload, list) else [part]
            text = "\n".join(str(b) for b in blocks)
            for block in re.split(r"\n\s*\n", text):
                action = re.search(r"^Action:\s*(\S+)", block, re.M | re.I)
                rcpt = re.search(r"^(?:Final|Original)-Recipient:\s*rfc822;\s*(\S+)", block, re.M | re.I)
                if action and action.group(1).lower() == "failed":
                    only_delayed = False
                    if rcpt:
                        failed.append(rcpt.group(1).strip("<>").lower())
                elif action and action.group(1).lower() != "delayed":
                    only_delayed = False
    return failed, has_report, (has_report and only_delayed)


def _referenced_ids(msg) -> list:
    ids = []
    for h in ("In-Reply-To", "References"):
        ids += MSGID_RE.findall(str(msg.get(h, "")))
    return ids


def classify(msg, own_addresses=()) -> dict:
    own = {a.lower() for a in own_addresses}
    from_addr = parseaddr(str(msg.get("From", "")))[1].lower()
    subject = str(msg.get("Subject", ""))
    result = {"kind": "ignore", "from": from_addr, "refs": _referenced_ids(msg), "failed": [], "subject": subject}

    failed, has_report, only_delayed = _delivery_status_failures(msg)
    is_daemon = bool(re.match(r"^(mailer-daemon|postmaster|mail-daemon)@", from_addr))
    if has_report or is_daemon or BOUNCE_SUBJECT_RE.search(subject):
        if only_delayed:
            return result  # a "still trying" notice is not a bounce
        if not failed:
            hdr = str(msg.get("X-Failed-Recipients", ""))
            failed = [a.lower() for a in ADDR_RE.findall(hdr)]
        if not failed:
            body = _text_of(msg)
            failed = [a.lower() for a in ADDR_RE.findall(body)
                      if a.lower() not in own and not re.match(r"^(mailer-daemon|postmaster)@", a.lower())][:1]
        result.update(kind="bounce", failed=sorted(set(failed)), refs=result["refs"] + _safe_orig_ids(msg))
        return result

    if from_addr in own or not from_addr:
        return result
    auto = str(msg.get("Auto-Submitted", "")).lower()
    if (auto and auto != "no") or msg.get("X-Autoreply") or msg.get("X-Autorespond") or AUTO_SUBJECT_RE.search(subject) \
            or str(msg.get("Precedence", "")).lower() in ("auto_reply", "bulk", "junk"):
        result["kind"] = "auto"
        return result

    text = new_text(_text_of(msg))
    subj = re.sub(r"^\s*((re|fwd?|fw)\s*:\s*)+", "", subject, flags=re.I)
    short = re.sub(r"[^a-z ]", "", text.lower()).strip()
    subj_unsub = subj.strip().lower() in ("unsubscribe", "stop", "remove") \
        or (UNSUB_RE.search(subj) and not result["refs"])
    if UNSUB_RE.search(text) or short in SHORT_NO or subj_unsub:
        result["kind"] = "unsubscribe"
    else:
        result["kind"] = "reply"
    return result


def _safe_orig_ids(msg) -> list:
    ids = []
    for part in msg.walk():
        ct = part.get_content_type()
        if ct not in ("message/rfc822", "text/rfc822-headers"):
            continue
        try:
            inner = part.get_payload(0) if part.is_multipart() else part.get_payload()
            m = re.search(r"^Message-ID:\s*(<[^>]+>)", str(inner), re.M | re.I)
            if m:
                ids.append(m.group(1))
        except Exception:
            continue
    return ids
