"""Build the outgoing email: plain text + simple HTML copy, one unsubscribe line, List-Unsubscribe headers.

The user's text is sent as written; the ONLY thing the tool adds is the short unsubscribe line."""
import html
import re
import uuid
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from urllib.parse import urlparse

from . import config, crypto
from .render import render

FOOTER_MARKER = "Not interested? Reply"
URL_RE = re.compile(r"(https?://[^\s<>\"]+)")
def unsub_line(unsub_url: str) -> str:
    return f"{FOOTER_MARKER} 'unsubscribe' or click: {unsub_url}"


def new_message_id() -> str:
    host = urlparse(config.public_base_url()).hostname or "localhost"
    return f"<{uuid.uuid4().hex}@{host}>"


def unsub_url(recipient_id: int, email: str) -> str:
    return f"{config.public_base_url()}/u/{crypto.make_unsub_token(recipient_id, email)}"


def text_to_html(text: str) -> str:
    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        esc = html.escape(para)
        esc = URL_RE.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', esc)
        out.append("<p>" + esc.replace("\n", "<br>\n") + "</p>")
    return "\n".join(out)


def render_parts(subject_t: str, body_t: str, vars_: dict, unsub: str):
    subject = render(subject_t, vars_).replace("\n", " ").strip()
    body = render(body_t, vars_).strip()
    foot = unsub_line(unsub)
    text = f"{body}\n\n{foot}\n"
    html_body = (
        '<!DOCTYPE html><html><body style="font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#222">'
        f"{text_to_html(body)}"
        f'<p style="font-size:12px;color:#666;margin-top:24px">{text_to_html(foot)[3:-4]}</p>'
        "</body></html>")
    return subject, text, html_body


def build_message(from_email: str, display_name: str, to_email: str, to_name: str, subject_t: str, body_t: str,
                  vars_: dict, unsub: str, message_id: str) -> EmailMessage:
    if not (display_name or "").strip():
        raise ValueError(f"{from_email} has no sender name")
    subject, text, html_body = render_parts(subject_t, body_t, vars_, unsub)
    msg = EmailMessage()
    msg["From"] = formataddr((display_name, from_email))
    msg["To"] = formataddr((to_name, to_email)) if to_name else to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = message_id
    msg["List-Unsubscribe"] = f"<mailto:{from_email}?subject=unsubscribe>, <{unsub}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    return msg
