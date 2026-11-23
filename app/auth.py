"""Single shared admin login: signed session cookie + login rate limiting."""
import hmac
import time

from core import config, crypto

COOKIE = "outreach_session"
SESSION_TTL = 12 * 3600
WINDOW = 15 * 60
MAX_PER_IP = 5   # failed logins per IP per 15 minutes (no global limit: nobody else can lock you out)


def make_session(user: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    return f"{exp}.{crypto.sign(f'session:{user}:{exp}', length=43)}"


def valid_session(value: str) -> bool:
    try:
        exp_s, sig = value.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < time.time():
        return False
    user = config.get("ADMIN_USER", "admin")
    return hmac.compare_digest(sig, crypto.sign(f"session:{user}:{exp}", length=43))


def too_many_attempts(conn, ip: str) -> bool:
    since = time.time() - WINDOW
    conn.execute("DELETE FROM login_attempts WHERE ts<?", (since,))
    per_ip = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE ip=? AND ts>=?", (ip, since)).fetchone()[0]
    return per_ip >= MAX_PER_IP


def record_failure(conn, ip: str) -> None:
    conn.execute("INSERT INTO login_attempts(ip,ts) VALUES(?,?)", (ip, time.time()))


def check_credentials(user: str, password: str) -> bool:
    want_user = config.get("ADMIN_USER", "admin")
    stored = config.get("ADMIN_PASSWORD_HASH")
    ok_user = hmac.compare_digest(user.strip().lower(), want_user.lower())
    ok_pass = crypto.verify_password(password, stored) if stored else False
    return ok_user and ok_pass
