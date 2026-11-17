"""Gmail IMAP helpers: Sent Mail lookup (crash recovery) and inbox fetching."""
import imaplib
import re
from datetime import datetime, timedelta, timezone

IMAP_HOST = "imap.gmail.com"


def connect(email: str, app_password: str):
    m = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=60)
    m.login(email, app_password)
    return m


def find_sent_folder(m) -> str:
    typ, data = m.list()
    for line in data or []:
        s = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
        if "\\Sent" in s:
            mt = re.search(r'"([^"]+)"\s*$', s) or re.search(r"\s(\S+)\s*$", s)
            if mt:
                return mt.group(1)
    return "[Gmail]/Sent Mail"


def _since(ts: float) -> str:
    return (datetime.fromtimestamp(ts, timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")


def was_sent(m, message_id: str, to_addr: str, since_ts: float) -> bool:
    """True if the Sent folder has our Message-ID — or, as a safety net, any message to that address since then."""
    folder = find_sent_folder(m)
    typ, _ = m.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        raise RuntimeError(f"Cannot open Sent folder {folder}")
    typ, data = m.search(None, "HEADER", "Message-ID", f'"{message_id}"')
    if typ == "OK" and data and data[0].split():
        return True
    typ, data = m.search(None, "SINCE", _since(since_ts), "TO", f'"{to_addr}"')
    return typ == "OK" and bool(data and data[0].split())


def test_imap(email: str, app_password: str) -> None:
    m = connect(email, app_password)
    try:
        m.select("INBOX", readonly=True)
    finally:
        try:
            m.logout()
        except Exception:
            pass


def fetch_new(m, last_uidvalidity, last_uid: int, since_ts: float):
    """Yield (uidvalidity, uid, raw_bytes) for INBOX messages newer than last_uid (no messages marked read)."""
    typ, _ = m.select("INBOX", readonly=True)
    if typ != "OK":
        raise RuntimeError("Cannot open INBOX")
    uv = None
    typ, data = m.response("UIDVALIDITY")
    if data and data[0]:
        uv = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
    if uv != last_uidvalidity:
        last_uid = 0
    if last_uid:
        typ, data = m.uid("SEARCH", None, f"UID {last_uid + 1}:*")
    else:
        typ, data = m.uid("SEARCH", None, "SINCE", _since(since_ts))
    uids = [int(u) for u in (data[0].split() if data and data[0] else []) if int(u) > last_uid]
    for uid in sorted(uids):
        typ, msg_data = m.uid("FETCH", str(uid), "(BODY.PEEK[])")
        raw = None
        for part in msg_data or []:
            if isinstance(part, tuple) and len(part) >= 2:
                raw = part[1]
                break
        yield uv, uid, raw
