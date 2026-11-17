"""Time helpers. Notes and the sending window use US Central time."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
TASHKENT = ZoneInfo("Asia/Tashkent")
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def central(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, CENTRAL)


def us_date(ts: float) -> str:
    return central(ts).strftime("%m/%d/%Y")


def fmt_central(ts) -> str:
    if not ts:
        return ""
    return central(ts).strftime("%m/%d/%Y %I:%M %p CT")


def _hm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def in_window(ts: float, window: dict) -> bool:
    dt = central(ts)
    if dt.weekday() not in window["days"]:
        return False
    sh, sm = _hm(window["start"])
    eh, em = _hm(window["end"])
    start = dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= dt < end


def next_window_start(ts: float, window: dict):
    """Next datetime (Central) when the window opens, or None if no days are selected."""
    if not window["days"]:
        return None
    dt = central(ts)
    sh, sm = _hm(window["start"])
    for i in range(0, 8):
        day = (dt + timedelta(days=i)).replace(hour=sh, minute=sm, second=0, microsecond=0)
        if day.weekday() in window["days"] and day > dt:
            return day
    return None


def window_seconds(window: dict) -> int:
    sh, sm = _hm(window["start"])
    eh, em = _hm(window["end"])
    return max(0, (eh * 60 + em - sh * 60 - sm) * 60)


def tashkent_range(window: dict, ref_ts: float) -> str:
    """'20:00–03:00' — the window converted to Tashkent time for the date of ref_ts (handles US DST)."""
    dt = central(ref_ts)
    sh, sm = _hm(window["start"])
    eh, em = _hm(window["end"])
    s = dt.replace(hour=sh, minute=sm, second=0, microsecond=0).astimezone(TASHKENT)
    e = dt.replace(hour=eh, minute=em, second=0, microsecond=0).astimezone(TASHKENT)
    return f"{s:%H:%M}–{e:%H:%M}"
