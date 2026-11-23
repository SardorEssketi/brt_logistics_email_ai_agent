"""Gmail SMTP sending and error classification.

Error kinds:
  quota     - daily limit / any "limit" message: stop this account for 24 h
  auth      - bad password, account disabled, suspicious activity: stop account, never auto-retry
  temporary - 4xx: retry this recipient later (max 2 retries)
  permanent - 5xx about this recipient/message: mark the row as failed
  network   - could not connect / dropped before the message was handed over: nothing was sent
  login_dropped - Gmail hung up during login. From cloud servers Gmail often does this instead of answering
              "wrong password", so it usually means the App Password is no longer accepted. Nothing was sent.
  uncertain - dropped while the message was being handed over: maybe sent -> checked via IMAP Sent Mail
"""
import smtplib
import ssl

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


class SendError(Exception):
    def __init__(self, kind: str, message: str, code=None):
        super().__init__(message)
        self.kind, self.message, self.code = kind, message, code


AUTH_WORDS = ("username and password not accepted", "application-specific password", "badcredentials",
              "account disabled", "account has been disabled", "suspended", "suspicious", "web login required",
              "please log in", "5.7.8", "5.7.9", "5.7.14", "authentication failed", "invalid credentials")
QUOTA_WORDS = ("5.4.5", "quota", "limit exceeded", "sending limit", "daily user sending", "too many",
               "rate limit", "exceeded", "limit")


def classify(code, text: str) -> str:
    t = (text or "").lower()
    if code in (534, 535) or any(w in t for w in AUTH_WORDS):
        return "auth"
    if any(w in t for w in QUOTA_WORDS):
        return "quota"
    if code is not None and 400 <= int(code) < 500:
        return "temporary"
    if code is not None and 500 <= int(code) < 600:
        return "permanent"
    return "temporary"


def short_reason(text: str) -> str:
    t = (text or "").replace("\n", " ")
    t = " ".join(w for w in t.split() if not (w[:1].isdigit() and "." in w))  # drop enhanced codes like 5.1.1
    t = t.split(" Learn more")[0].split(" https://")[0]
    return t.strip()[:80] or "unknown error"


def _decode(b):
    return b.decode(errors="replace") if isinstance(b, bytes) else str(b)


def send(account_email: str, app_password: str, msg, smtp_factory=None, timeout: int = 60) -> None:
    factory = smtp_factory or (lambda: smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=timeout,
                                                        context=ssl.create_default_context()))
    phase = "connect"
    server = None
    try:
        server = factory()
        phase = "login"
        server.login(account_email, app_password)
        phase = "send"
        refused = server.send_message(msg)
        if refused:
            (_, (code, resp)), = refused.items()
            raise SendError(classify(code, _decode(resp)), _decode(resp), code)
        phase = "done"
    except SendError:
        raise
    except smtplib.SMTPAuthenticationError as e:
        raise SendError("auth", _decode(e.smtp_error), e.smtp_code)
    except smtplib.SMTPRecipientsRefused as e:
        code, resp = next(iter(e.recipients.values()))
        raise SendError(classify(code, _decode(resp)), _decode(resp), code)
    except smtplib.SMTPResponseException as e:
        raise SendError(classify(e.smtp_code, _decode(e.smtp_error)), _decode(e.smtp_error), e.smtp_code)
    except (smtplib.SMTPException, OSError) as e:
        if phase == "send":
            raise SendError("uncertain", f"Connection dropped while sending: {e}")
        if phase == "login":
            raise SendError("login_dropped", f"Gmail closed the connection at login (App Password not accepted?): {e}")
        raise SendError("network", f"Could not reach Gmail ({phase}): {e}")
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def test_login(account_email: str, app_password: str) -> None:
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30, context=ssl.create_default_context()) as s:
        try:
            s.login(account_email, app_password)
        except smtplib.SMTPServerDisconnected:
            raise RuntimeError("Gmail did not accept the App Password (it closed the connection at login)") from None
