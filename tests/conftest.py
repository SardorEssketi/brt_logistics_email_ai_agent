import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OUTREACH_ENV_FILE"] = str(ROOT / "tests" / "no-such.env")  # never read a real .env in tests
os.environ.setdefault("APP_SECRET", "t" * 48)
os.environ.setdefault("FERNET_KEY", "8tC4Ej3mOEPj6hZm3GJYbDdT9pVZbK6jJ8m7u8E6Z5Q=")
os.environ["COOKIE_SECURE"] = "0"
os.environ["PUBLIC_BASE_URL"] = "https://test.duckdns.org"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("DRY_RUN_FAST", raising=False)
    return tmp_path / "data"


@pytest.fixture
def conn(data_dir):
    from core import db
    c = db.connect()
    # Always inside the window for tests unless a test changes it
    db.set_setting(c, "window", {"days": [0, 1, 2, 3, 4, 5, 6], "start": "00:00", "end": "23:59"})
    yield c
    c.close()


def add_account(conn, email="rec1@gmail.com", name="Mike, ABC Recruiting", added_days_ago=30):
    import time
    from core import crypto
    cur = conn.execute("INSERT INTO accounts(email,display_name,password_enc,added_at) VALUES(?,?,?,?)",
                       (email, name, crypto.encrypt("abcdabcdabcdabcd"), time.time() - added_days_ago * 86400))
    return cur.lastrowid


class FakeSMTP:
    """Stand-in for smtplib.SMTP_SSL. `errors` = list of exceptions to raise on the next send_message calls."""
    def __init__(self, log, errors=None, login_error=None):
        self.log, self.errors, self.login_error = log, errors if errors is not None else [], login_error

    def login(self, u, p):
        if self.login_error:
            raise self.login_error

    def send_message(self, msg):
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        self.log.append(msg)
        return {}

    def quit(self):
        pass
