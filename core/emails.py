"""Email normalisation and validation (syntax, role addresses, typos, MX)."""
import re
from concurrent.futures import ThreadPoolExecutor

FIND_RE = re.compile(r"[A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
LOCAL_RE = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~\-]+(\.[a-z0-9!#$%&'*+/=?^_`{|}~\-]+)*$")
LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")

ROLE_LOCALS = {
    "info", "admin", "administrator", "noreply", "no-reply", "donotreply", "do-not-reply", "support",
    "sales", "contact", "office", "hr", "jobs", "careers", "billing", "accounts", "webmaster", "postmaster",
    "abuse", "hostmaster", "marketing", "help", "mailer-daemon", "root", "privacy", "legal", "team",
}

TYPO_DOMAINS = {
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gamil.com": "gmail.com", "gmal.com": "gmail.com",
    "gnail.com": "gmail.com", "gmail.co": "gmail.com", "gmail.cm": "gmail.com", "gmail.om": "gmail.com",
    "gmaill.com": "gmail.com", "gmail.con": "gmail.com", "gmail.comm": "gmail.com", "gmailcom": "gmail.com",
    "gmsil.com": "gmail.com", "gmeil.com": "gmail.com", "g.mail.com": "gmail.com",
    "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com", "yahoo.co": "yahoo.com", "yhoo.com": "yahoo.com",
    "yahoo.con": "yahoo.com", "yaoo.com": "yahoo.com", "tahoo.com": "yahoo.com",
    "hotmial.com": "hotmail.com", "hotmal.com": "hotmail.com", "hotmail.co": "hotmail.com",
    "hotmail.con": "hotmail.com", "hotmaill.com": "hotmail.com",
    "outlok.com": "outlook.com", "outlook.co": "outlook.com", "outlook.con": "outlook.com",
    "iclod.com": "icloud.com", "icloud.co": "icloud.com", "icoud.com": "icloud.com",
    "aol.co": "aol.com", "aol.con": "aol.com", "aoll.com": "aol.com",
    "comcast.com": "comcast.net", "att.com": "att.net", "sbcglobal.com": "sbcglobal.net",
}

KNOWN_GOOD = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "live.com", "msn.com",
    "comcast.net", "att.net", "sbcglobal.net", "verizon.net", "me.com", "ymail.com", "rocketmail.com",
    "bellsouth.net", "charter.net", "cox.net", "protonmail.com", "proton.me", "mail.com", "gmx.com",
}


def normalize(raw) -> str:
    """Extract the first email-looking token and lowercase it. '' if none."""
    if raw is None:
        return ""
    m = FIND_RE.search(str(raw).strip())
    return m.group(0).lower().strip(".'-") if m else ""


def is_valid_syntax(email: str) -> bool:
    if not email or email.count("@") != 1 or len(email) > 254:
        return False
    local, domain = email.split("@")
    if not local or len(local) > 64 or not LOCAL_RE.match(local):
        return False
    labels = domain.split(".")
    if len(labels) < 2 or not all(LABEL_RE.match(lb) for lb in labels):
        return False
    return labels[-1].isalpha() and len(labels[-1]) >= 2


def is_role(email: str) -> bool:
    return email.split("@")[0] in ROLE_LOCALS


def typo_suggestion(email: str):
    local, _, domain = email.partition("@")
    fix = TYPO_DOMAINS.get(domain)
    return f"{local}@{fix}" if fix else None


def has_mail_server(domain: str) -> bool:
    """True if the domain has MX (or an A record as implicit MX). Unknown/DNS trouble => True (don't punish)."""
    if domain in KNOWN_GOOD:
        return True
    try:
        import dns.resolver
        import dns.exception
    except ImportError:
        return True
    res = dns.resolver.Resolver()
    res.lifetime = 6
    try:
        ans = res.resolve(domain, "MX")
        # A null MX ("0 .") means the domain accepts no mail.
        return not all(str(r.exchange) == "." for r in ans)
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        try:
            res.resolve(domain, "A")
            return True
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return False
        except dns.exception.DNSException:
            return True
    except dns.exception.DNSException:
        return True


def check_domains(domains, checker=has_mail_server) -> dict:
    domains = sorted(set(domains))
    if not domains:
        return {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(zip(domains, ex.map(checker, domains)))
