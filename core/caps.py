"""Per-account daily caps: warm-up by account age, counted over a rolling 24 hours."""
import time

DAY = 86400
WEEK = 7 * DAY
HARD_MAX = 300
RISKY_ABOVE = 100


def warmup_cap(added_at: float, now: float, schedule) -> int:
    week = max(0, int((now - added_at) // WEEK))
    return int(schedule[min(week, len(schedule) - 1)])


def account_cap(acc, schedule, now=None) -> int:
    now = now or time.time()
    cap = acc["cap_override"] if acc["cap_override"] else warmup_cap(acc["added_at"], now, schedule)
    return max(0, min(int(cap), HARD_MAX))


def sent_last_24h(conn, account_id: int, now=None) -> int:
    now = now or time.time()
    return conn.execute("SELECT COUNT(*) FROM send_log WHERE account_id=? AND ts>?",
                        (account_id, now - DAY)).fetchone()[0]


def account_state(acc, now=None) -> str:
    """'ok', 'paused' (24h quota stop), 'disabled' (needs a human) or 'no_name' (sender name missing)."""
    now = now or time.time()
    if acc["disabled_reason"]:
        return "disabled"
    if not (acc["display_name"] or "").strip():
        return "no_name"
    if acc["paused_until"] and acc["paused_until"] > now:
        return "paused"
    return "ok"
