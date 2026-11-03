"""Online job offers: find internship postings (LinkedIn Jobs, company career
sites, ATS pages like Greenhouse / Lever / Workday) and filter them.

The agent never submits an online application itself. LinkedIn forbids
automated applications, and career sites use logins and CAPTCHAs. Instead it
prepares everything (tailored cover letter, answers to the usual questions)
and lists each offer with its link in the daily digest. You apply in a few clicks.
"""

import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse, urlunparse

from . import scoring
from .discovery import search_and_submit

SUBMIT_OFFERS = {
    "name": "submit_offers",
    "description": "Submit the internship postings you verified. Call exactly once, at the end.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["offers"],
        "properties": {
            "offers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "company", "company_website", "location", "country", "url",
                                 "platform", "posted", "language", "requirements", "segment", "why_fit"],
                    "properties": {
                        "title": {"type": "string", "description": "Job title exactly as posted"},
                        "company": {"type": "string"},
                        "company_website": {"type": "string", "description": "Company homepage; empty if unknown"},
                        "location": {"type": "string", "description": "City, country"},
                        "country": {"type": "string", "description": "ISO 3166-1 alpha-2"},
                        "url": {"type": "string", "description": "Direct URL of the posting, exactly as found"},
                        "platform": {"type": "string", "enum": ["linkedin", "ats", "company_site", "job_board"]},
                        "posted": {"type": "string", "description": "Posting date YYYY-MM-DD if shown, else empty"},
                        "language": {"type": "string", "description": "Language the posting is written in"},
                        "requirements": {"type": "string", "description": "Key requirements in one or two sentences"},
                        "segment": {"type": "string", "description": (
                            "Finance function of the role, not the employer's industry: e.g. 'Audit', "
                            "'FP&A', 'Financial control', 'Transaction services', 'M&A advisory'")},
                        "why_fit": {"type": "string", "description": "Why it matches the candidate, one sentence"},
                    },
                },
            }
        },
    },
}


def _system(cfg: dict) -> str:
    m, c = cfg["market"], cfg["candidate"]
    return f"""You find open internship job postings for a finance student.

Market range: {m['description'].strip()}
Target segments: {', '.join(m['target_segments'])}. Never: {', '.join(m['excluded_segments'])}.
Internship sought: {c['internship']['duration']} starting {c['internship']['start']}.
Candidate languages: {', '.join(c['languages'])}.

Look on LinkedIn Jobs (linkedin.com/jobs/view/...), company career pages, and application systems
(Greenhouse, Lever, Workday, SmartRecruiters, Teamtailor, Welcome to the Jungle, JobTeaser).

Rules:
- Only report postings you actually saw in search results or on a page, with their exact URL.
- Internship / stage / off-cycle / summer analyst level only. No full-time graduate or experienced roles.
- Skip postings that say they are closed, expired or no longer accepting applications.
- Prefer postings from the last 30 days and start dates near {c['internship']['start']}.
- Never invent a URL or a posting date. Leave `posted` empty if no date is shown.
Call submit_offers once at the end."""


def find_offers(client, cfg: dict, region: dict, exclude_urls: list[str]) -> list[dict]:
    user = (f"Find up to 15 open finance internship postings in {region['name']} "
            f"(countries: {', '.join(region['countries'])}).\n"
            f"Already known, do not return:\n{chr(10).join(exclude_urls[:200]) or '(none)'}")
    data = search_and_submit(client, cfg, _system(cfg), user, SUBMIT_OFFERS, max_searches=20)
    return (data or {}).get("offers", [])


def clean_url(url: str) -> str:
    """Drop tracking query strings so the same posting isn't stored twice."""
    p = urlparse((url or "").strip())
    if not p.scheme.startswith("http"):
        return ""
    keep_query = p.query if "linkedin.com" not in p.netloc else ""
    if "linkedin.com" in p.netloc:  # normalise /jobs/view/<slug>-123456 → /jobs/view/123456
        m = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)", p.path)
        if m:
            return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", keep_query, ""))


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime((s or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def evaluate_offer(o: dict, cfg: dict, today: date | None = None) -> dict:
    oc, market = cfg["offers"], cfg["market"]
    today = today or date.today()
    o = dict(o)
    o["url"] = clean_url(o.get("url", ""))
    o["country"] = (o.get("country") or "").upper()
    o["company_domain"] = scoring.normalize_domain(o.get("company_website", "")) or None
    o["platform"] = "linkedin" if "linkedin.com" in o["url"] else o.get("platform")

    def reject(reason):
        o.update(status="rejected", reject_reason=reason, score=0)
        return o

    if not o["url"]:
        return reject("no valid URL")
    title = o.get("title", "").lower()
    if not any(k in title for k in oc["title_keywords"]):
        return reject("title is not an internship")
    if any(scoring.segment_matches(o.get("segment", ""), [x]) for x in market["excluded_segments"]):
        return reject("excluded segment")
    countries = {c for r in market["regions"] for c in r["countries"]}
    if o["country"] not in countries:
        return reject(f"country {o['country']} outside your regions")
    posted = _parse_date(o.get("posted", ""))
    if posted and posted < today - timedelta(days=oc["max_age_days"]):
        return reject(f"posted {posted} (older than {oc['max_age_days']} days)")
    lang = (o.get("language") or "English").strip().capitalize()
    if lang not in cfg["candidate"]["languages"]:
        return reject(f"posting in {lang}")

    score = 0
    if scoring.segment_matches(o.get("segment", ""), market["target_segments"]):
        score += 45
    if o["country"] in market.get("priority_countries", []):
        score += 20
    if posted and posted >= today - timedelta(days=10):
        score += 20
    elif not posted:
        score += 8
    else:
        score += 12
    score += 15 if o["platform"] in ("linkedin", "ats", "company_site") else 5
    o["score"] = score
    if score < oc["min_score"]:
        return reject(f"score {score} < {oc['min_score']}")
    o["status"] = "qualified"
    return o
