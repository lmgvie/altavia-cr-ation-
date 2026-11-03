"""Deterministic filtering & scoring of discovered companies against criteria.yaml.

The language model *finds* companies; this module *decides*. Keeping the
decision in plain code means the criteria are applied identically every time.
"""

import re
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")

# Inboxes meant for applications — the best possible recipient.
RECRUITING_LOCALPARTS = {
    "careers", "career", "jobs", "job", "recruitment", "recruiting", "recrutement",
    "recruit", "talent", "internship", "internships", "stage", "stages", "hr", "rh",
    "graduates", "graduate", "campus", "apply", "applications", "candidature",
    "candidatures", "people", "join", "joinus", "work", "karriere", "bewerbung",
}
GENERAL_LOCALPARTS = {"info", "contact", "hello", "office", "enquiries", "inquiries", "admin", "mail"}
NEVER_LOCALPARTS = {"noreply", "no-reply", "donotreply", "privacy", "gdpr", "dpo", "abuse",
                    "press", "media", "investor", "investors", "ir", "compliance", "legal",
                    "complaints", "support", "billing", "accounts"}

# Main business language per country (used to pick the letter language).
COUNTRY_LANGUAGE = {
    "FR": "French", "BE": "French", "LU": "French", "MC": "French", "CH": "French",
    "GB": "English", "IE": "English", "US": "English", "CA": "English", "AU": "English",
    "NZ": "English", "SG": "English", "HK": "English", "AE": "English", "SA": "English",
    "QA": "English", "IN": "English", "ZA": "English",
    "DE": "German", "AT": "German", "ES": "Spanish", "IT": "Italian", "PT": "Portuguese",
    "NL": "English", "SE": "English", "DK": "English", "NO": "English", "FI": "English",
    "JP": "English",
}

COUNTRY_TZ = {
    "FR": "Europe/Paris", "BE": "Europe/Brussels", "LU": "Europe/Luxembourg", "MC": "Europe/Monaco",
    "CH": "Europe/Zurich", "GB": "Europe/London", "IE": "Europe/Dublin", "DE": "Europe/Berlin",
    "AT": "Europe/Vienna", "NL": "Europe/Amsterdam", "ES": "Europe/Madrid", "IT": "Europe/Rome",
    "PT": "Europe/Lisbon", "SE": "Europe/Stockholm", "DK": "Europe/Copenhagen", "NO": "Europe/Oslo",
    "FI": "Europe/Helsinki", "PL": "Europe/Warsaw", "AE": "Asia/Dubai", "SA": "Asia/Riyadh",
    "QA": "Asia/Qatar", "US": "America/New_York", "CA": "America/Toronto", "SG": "Asia/Singapore",
    "HK": "Asia/Hong_Kong", "AU": "Australia/Sydney", "JP": "Asia/Tokyo", "IN": "Asia/Kolkata",
    "ZA": "Africa/Johannesburg", "NZ": "Pacific/Auckland",
}


def normalize_domain(url_or_domain: str) -> str:
    s = (url_or_domain or "").strip().lower()
    if "://" not in s:
        s = "http://" + s
    host = urlparse(s).hostname or ""
    return host.removeprefix("www.")


def _registrable(host: str) -> str:
    """Rough eTLD+1 (good enough for matching an email domain to a website)."""
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def classify_email(email: str, company_domain: str) -> tuple[str | None, str]:
    """Return (contact_type, reason). contact_type is None when the address is unusable."""
    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        return None, "invalid or missing email"
    local, dom = email.lower().split("@", 1)
    if _registrable(dom) != _registrable(company_domain):
        return None, f"email domain {dom} does not match company website {company_domain}"
    base = re.split(r"[.+_-]", local)[0]
    if local in NEVER_LOCALPARTS or base in NEVER_LOCALPARTS:
        return None, f"{local}@ is not an application inbox"
    if local in RECRUITING_LOCALPARTS or base in RECRUITING_LOCALPARTS:
        return "recruiting", "dedicated recruiting inbox"
    if local in GENERAL_LOCALPARTS:
        return "general", "general company inbox"
    return "named", "named person"


def letter_language(country: str, candidate_languages: list[str]) -> str:
    lang = COUNTRY_LANGUAGE.get((country or "").upper(), "English")
    return lang if lang in candidate_languages else "English"


def local_now(country: str, now_utc: datetime) -> datetime:
    return now_utc.astimezone(ZoneInfo(COUNTRY_TZ.get((country or "").upper(), "UTC")))


def in_office_hours(country: str, now_utc: datetime, hours: list[int], weekdays_only: bool) -> bool:
    t = local_now(country, now_utc)
    if weekdays_only and t.weekday() >= 5:
        return False
    return hours[0] <= t.hour < hours[1]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def segment_matches(segment: str, targets: list[str]) -> bool:
    seg = _norm(segment)
    if not seg:
        return False
    for t in targets:
        target = _norm(t)
        tokens = [w for w in target.split() if len(w) > 2]
        if target in seg or seg in target or (len(tokens) >= 2 and all(w in seg for w in tokens[:2])):
            return True
    return False


def evaluate(company: dict, cfg: dict) -> dict:
    """Fill in domain, contact_type, score, status (+reject_reason). Returns the company dict."""
    market, weights = cfg["market"], cfg["scoring"]["weights"]
    candidate, engine = cfg["candidate"], cfg["engine"]

    c = dict(company)
    c["domain"] = normalize_domain(c.get("website", ""))
    c["country"] = (c.get("country") or "").upper()

    def reject(reason):
        c.update(status="rejected", reject_reason=reason, score=0)
        return c

    if not c["domain"]:
        return reject("no website")
    excluded = {normalize_domain(x) if "." in x else _norm(x) for x in market.get("excluded_companies", [])}
    if c["domain"] in excluded or _norm(c.get("name", "")) in excluded:
        return reject("on exclusion list")
    if any(segment_matches(c.get("segment", ""), [x]) for x in market.get("excluded_segments", [])):
        return reject(f"excluded segment: {c.get('segment')}")

    ctype, why = classify_email(c.get("contact_email", ""), c["domain"])
    if ctype is None:
        return reject(why)
    if ctype == "named" and not engine.get("allow_named_contacts", False):
        return reject("named personal address (allow_named_contacts is false)")
    if not (c.get("contact_source") or "").startswith("http"):
        return reject("no public source URL for the email address")
    c["contact_type"] = ctype

    score = 0
    if segment_matches(c.get("segment", ""), market["target_segments"]):
        score += weights["segment_match"]
    emp = c.get("employees")
    if isinstance(emp, int) and market["employees"]["min"] <= emp <= market["employees"]["max"]:
        score += weights["size_in_range"]
    elif emp is None:
        score += weights["size_in_range"] // 2          # unknown size: half credit
    if c.get("internship_signal"):
        score += weights["internship_signal"]
    if c["country"] in market.get("priority_countries", []):
        score += weights["priority_country"]
    score += {"recruiting": 1.0, "general": 0.5, "named": 0.7}[ctype] * weights["contact_quality"]
    if COUNTRY_LANGUAGE.get(c["country"], "English") in candidate["languages"]:
        score += weights["language_match"]

    c["score"] = int(round(score))
    if c["score"] < cfg["scoring"]["min_score"]:
        c.update(status="rejected", reject_reason=f"score {c['score']} < {cfg['scoring']['min_score']}")
    else:
        c["status"] = "qualified"
    c["internship_signal"] = int(bool(c.get("internship_signal")))
    return c
