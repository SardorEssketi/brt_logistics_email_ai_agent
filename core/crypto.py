"""Encryption of App Passwords at rest, password hashing, HMAC tokens."""
import base64
import hashlib
import hmac
import os

from cryptography.fernet import Fernet

from . import config


def encrypt(text: str) -> str:
    return Fernet(config.fernet_key().encode()).encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return Fernet(config.fernet_key().encode()).decrypt(token.encode()).decode()


# Format: scrypt:n:r:p:salt_hex:hash_hex  (no '$' so it is safe in env files)
def hash_password(password: str, n: int = 2 ** 14, r: int = 8, p: int = 1) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt:{n}:{r}:{p}:{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, want = stored.split(":")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk.hex(), want)
    except Exception:
        return False


def sign(data: str, secret: bytes = None, length: int = 22) -> str:
    secret = secret or config.app_secret()
    mac = hmac.new(secret, data.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")[:length]


# --- Unsubscribe tokens: "<recipient_id>.<hmac>" (never contains the email) ---
def make_unsub_token(recipient_id: int, email: str, secret: bytes = None) -> str:
    return f"{recipient_id}.{sign(f'unsub:{recipient_id}:{email.lower()}', secret)}"


def parse_unsub_token(token: str):
    """Returns recipient_id (int) or None if the token is malformed. Signature checked by verify_unsub_token."""
    try:
        rid, sig = token.split(".", 1)
        return int(rid) if sig else None
    except (ValueError, AttributeError):
        return None


def verify_unsub_token(token: str, email: str, secret: bytes = None) -> bool:
    rid = parse_unsub_token(token)
    if rid is None or not email:
        return False
    return hmac.compare_digest(make_unsub_token(rid, email, secret), token)
