"""Runtime configuration, read from environment (systemd EnvironmentFile on the VM, .env locally)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env_file(Path(os.environ.get("OUTREACH_ENV_FILE", BASE_DIR / ".env")))


def get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def flag(name: str) -> bool:
    return get(name, "0").strip().lower() in ("1", "true", "yes", "on")


def data_dir() -> Path:
    p = Path(get("DATA_DIR", str(BASE_DIR / "data")))
    p.mkdir(parents=True, exist_ok=True)
    return p


def app_secret() -> bytes:
    s = get("APP_SECRET")
    if len(s) < 32:
        raise RuntimeError("APP_SECRET is missing or too short (run install.sh or see .env.example)")
    return s.encode()


def fernet_key() -> str:
    k = get("FERNET_KEY")
    if not k:
        raise RuntimeError("FERNET_KEY is missing (run install.sh or see .env.example)")
    return k


def public_base_url() -> str:
    return get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


def dry_run() -> bool:
    return flag("DRY_RUN")


def dry_run_fast() -> bool:
    """Only for local demos: in dry-run mode, ignore the sending window and gaps."""
    return dry_run() and flag("DRY_RUN_FAST")


def google_sa_file() -> str:
    return get("GOOGLE_SA_FILE", "")


def cookie_secure() -> bool:
    return get("COOKIE_SECURE", "1") != "0"
