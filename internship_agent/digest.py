"""Daily digest: one email to yourself with everything that needs a human click.

  1. Replies from companies, each with a suggested answer waiting in Gmail Drafts
  2. Online job offers, each with its Apply link, a tailored cover letter and form answers
  3. LinkedIn kits for the best companies emailed today: links + connection note + message

Email clients don't run scripts, so text is laid out in boxes that are easy to
select and copy on a phone. The same page is saved to logs/digest/<date>.html.
"""

from datetime import datetime
from html import escape
from urllib.parse import quote_plus

KIND_LABEL = {
    "interview": "🎯 Interview / call proposed",
    "info_request": "📄 They ask for information",
    "rejection": "✉️ Not this time",
    "other": "💬 Reply",
    "bounce": "⚠️ Address bounced",
}

C = {  # email-safe inline colours
    "ink": "#1d2433", "muted": "#5b6578", "line": "#e3e6ec", "box": "#f5f7fa",
    "accent": "#1f5eff", "good": "#137a3f", "warn": "#a15c00",
}


def _box(text: str) -> str:
    return (f'<div style="background:{C["box"]};border:1px solid {C["line"]};border-radius:8px;'
            f'padding:12px 14px;margin:6px 0 12px;white-space:pre-wrap;font-size:14px;line-height:1.5;'
            f'color:{C["ink"]}">{escape(text)}</div>')


def _btn(url: str, label: str) -> str:
    return (f'<a href="{escape(url, quote=True)}" style="display:inline-block;background:{C["accent"]};'
            f'color:#fff;text-decoration:none;padding:8px 14px;border-radius:6px;font-size:14px;'
            f'font-weight:600;margin:4px 6px 4px 0">{escape(label)}</a>')


def _link(url: str, label: str) -> str:
    return f'<a href="{escape(url, quote=True)}" style="color:{C["accent"]}">{escape(label)}</a>'


def _h2(text: str, count: int) -> str:
    return (f'<h2 style="font-size:18px;margin:28px 0 8px;color:{C["ink"]};border-bottom:2px solid '
            f'{C["line"]};padding-bottom:6px">{escape(text)} <span style="color:{C["muted"]};'
            f'font-weight:400">({count})</span></h2>')


def _small(text: str) -> str:
    return f'<div style="font-size:13px;color:{C["muted"]};margin:2px 0">{escape(text)}</div>'


def build(cfg: dict, stats: dict, sent_today: list, replies: list, offers: list, linkedin: list,
          emailed_domains: set, new_offer_urls: set) -> tuple[str, str, str]:
    """Return (subject, html, plain_text)."""
    today = datetime.now().strftime("%A %d %B %Y")
    n_int = sum(r["kind"] == "interview" for r in replies)
    subject = (f"Internship agent · {len(sent_today)} sent · {len(offers)} offers to apply · "
               f"{len(linkedin)} LinkedIn" + (f" · 🎯 {n_int} interview" if n_int else ""))
    h, t = [], []

    h.append(f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:680px;'
             f'margin:0 auto;padding:16px;color:{C["ink"]}">')
    h.append(f'<h1 style="font-size:22px;margin:0 0 4px">Your internship search — {escape(today)}</h1>')
    h.append(_small(f"{len(sent_today)} applications emailed today · {stats.get('applied', 0)} companies "
                    f"contacted in total · replies from people so far: {stats.get('replies_human', 0)} · "
                    f"{stats.get('qualified_waiting', 0)} companies queued"))
    t.append(f"YOUR INTERNSHIP SEARCH — {today}\n{len(sent_today)} applications emailed today.\n")

    # 1. replies
    if replies:
        h.append(_h2("1. Companies that replied", len(replies)))
        t.append("\n1. COMPANIES THAT REPLIED\n")
        for r in replies:
            label = KIND_LABEL.get(r["kind"], "💬 Reply")
            h.append(f'<p style="margin:14px 0 2px;font-size:15px"><b>{escape(r["name"])}</b> — {label}</p>')
            h.append(_small(r["summary"] or ""))
            if r["draft_id"]:
                h.append(_small("A suggested answer is waiting in Gmail → Drafts, in the same thread. "
                                "Check it, adjust, send."))
            h.append(_btn("https://mail.google.com/mail/u/0/#drafts", "Open Gmail Drafts")
                     if r["draft_id"] else "")
            t.append(f"- {r['name']}: {label} — {r['summary']}"
                     + (" (draft ready in Gmail Drafts)" if r["draft_id"] else "") + "\n")

    # 2. online offers
    if offers:
        ans = cfg["candidate"].get("screening_answers", {})
        h.append(_h2("2. Job offers to apply online", len(offers)))
        h.append(_small("Click Apply, paste the cover letter and the answers below. LinkedIn Easy Apply "
                        "takes about 2 minutes. Each offer disappears from this list by itself once the "
                        "confirmation email arrives (or after "
                        f"{cfg['offers']['keep_in_digest_days']} days)."))
        if ans:
            h.append('<p style="margin:12px 0 2px;font-size:14px"><b>Answers for the usual form questions</b></p>')
            h.append(_box("\n".join(f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in ans.items())))
        t.append("\n2. JOB OFFERS TO APPLY ONLINE\n")
        for o in offers:
            tag = " 🆕" if o["url"] in new_offer_urls else ""
            emailed = " · already emailed" if o["company_domain"] in emailed_domains else ""
            h.append(f'<p style="margin:20px 0 2px;font-size:15px"><b>{escape(o["title"])}</b>{tag}<br>'
                     f'{escape(o["company"])} — {escape(o["location"] or "")}</p>')
            h.append(_small(f"#{o['ref']} · {o['platform']} · posted {o['posted'] or 'n/a'} · score "
                            f"{o['score']}{emailed}"))
            h.append(_btn(o["url"], "Apply" + (" on LinkedIn" if o["platform"] == "linkedin" else "")))
            h.append('<div style="font-size:13px;margin-top:8px"><b>“Why us?” answer</b></div>' + _box(o["why_answer"] or ""))
            h.append('<div style="font-size:13px"><b>Cover letter</b></div>' + _box(o["cover_letter"] or ""))
            t.append(f"\n#{o['ref']} {o['title']} — {o['company']} ({o['location']})\nApply: {o['url']}\n"
                     f"Why us: {o['why_answer']}\n\n{o['cover_letter']}\n")

    # 3. LinkedIn
    if linkedin:
        h.append(_h2("3. LinkedIn — second touch for your best companies", len(linkedin)))
        h.append(_small("For each: open the company, find someone in the deal team or HR, click Connect → "
                        "Add a note, paste the note. When they accept, send the message. "
                        f"Kept under {cfg['linkedin']['weekly_invites']} invitations a week."))
        t.append("\n3. LINKEDIN\n")
        for c in linkedin:
            company_url = c["linkedin_url"] or (
                "https://www.linkedin.com/search/results/companies/?keywords=" + quote_plus(c["name"]))
            people_url = ("https://www.linkedin.com/search/results/people/?keywords="
                          + quote_plus(f"{c['name']} recruiter OR talent OR analyst"))
            h.append(f'<p style="margin:20px 0 2px;font-size:15px"><b>{escape(c["name"])}</b> — '
                     f'{escape(c["segment"] or "")}, {escape(c["city"] or "")} · score {c["score"]}</p>')
            h.append(_btn(company_url, "Company page") + _btn(people_url, "Find people"))
            h.append('<div style="font-size:13px;margin-top:8px"><b>Connection note</b> '
                     f'<span style="color:{C["muted"]}">({len(c["linkedin_note"])}/300)</span></div>'
                     + _box(c["linkedin_note"]))
            h.append('<div style="font-size:13px"><b>Message after they accept</b></div>' + _box(c["linkedin_message"] or ""))
            t.append(f"\n{c['name']}\nCompany: {company_url}\nPeople: {people_url}\nNote: {c['linkedin_note']}\n"
                     f"Message: {c['linkedin_message']}\n")

    # sent today (compact)
    if sent_today:
        h.append(_h2("Emailed today", len(sent_today)))
        rows = "".join(
            f'<tr><td style="padding:4px 8px 4px 0">{escape(a["name"])}</td>'
            f'<td style="padding:4px 8px;color:{C["muted"]}">{escape(a["country"] or "")}</td>'
            f'<td style="padding:4px 8px;color:{C["muted"]}">{escape(a["segment"] or "")}</td>'
            f'<td style="padding:4px 0;color:{C["muted"]}">{escape(a["to_email"])}</td></tr>'
            for a in sent_today)
        h.append(f'<table style="border-collapse:collapse;font-size:13px">{rows}</table>')

    if not (replies or offers or linkedin or sent_today):
        h.append(_small("Nothing new today."))
    h.append(f'<p style="margin-top:28px;font-size:12px;color:{C["muted"]}">Sent by your internship agent. '
             f'Nothing on this page was submitted or sent on LinkedIn for you — those clicks are yours.</p></div>')
    return subject, "".join(h), "".join(t)
