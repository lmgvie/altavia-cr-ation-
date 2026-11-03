"""Writes one tailored application email per company (and the follow-up)."""

import logging
import re

import anthropic
from pydantic import BaseModel, Field

from .discovery import FALLBACK_BETA

log = logging.getLogger(__name__)


class Application(BaseModel):
    subject: str = Field(description="Email subject, under 90 characters")
    body: str = Field(description="Plain-text email body including greeting and the signature")


PLACEHOLDER_RE = re.compile(r"\[[^\]]{2,}\]|\{[a-z_]+\}|lorem ipsum", re.I)


def _system(cfg: dict) -> str:
    c = cfg["candidate"]
    highlights = "\n".join(f"- {h}" for h in c["highlights"])
    return f"""You write short, specific internship application emails for a finance student.

Candidate (the only facts you may state about them):
Name: {c['first_name']} {c['last_name']}
Programme: {c['degree']}, {c['school']}, graduating {c['graduation']}
Languages: {', '.join(c['languages'])}
Internship sought: {c['internship']['duration']} from {c['internship']['start']}{' (dates flexible)' if c['internship'].get('flexible') else ''}
Highlights:
{highlights}
Motivation: {c['motivation'].strip()}

Signature, to be used verbatim at the end:
{c['email_signature'].strip()}

How to write it:
- 120–180 words, plain text, no markdown, no bullet lists.
- Open with one sentence about the firm itself, grounded in the facts supplied (a deal, a fund, its focus). If no fact is supplied, refer to its segment and city instead — never invent news.
- Then two or three sentences linking the candidate's highlights to the firm's work.
- State the dates and that the CV is attached. End with a simple request for a short call or the right contact.
- Write in the requested language with the formal register used in that country.
- Never claim experience, grades or skills that are not in the candidate facts. No placeholders."""


def _call(client, cfg, prompt: str, min_words: int = 90) -> Application | None:
    eng = cfg["engine"]
    extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if eng.get("refusal_fallbacks", True) else {}
    for attempt in range(2):
        resp = client.beta.messages.parse(
            model=eng["model"],
            max_tokens=8000,
            system=_system(cfg),
            output_config={"effort": eng.get("writing_effort", "medium")},
            output_format=Application,
            messages=[{"role": "user", "content": prompt}],
            **extra,
        )
        if resp.stop_reason == "refusal":
            log.warning("Writer declined: %s", resp.stop_details)
            return None
        app = resp.parsed_output
        if app and not PLACEHOLDER_RE.search(app.subject + app.body) and len(app.body.split()) >= min_words:
            return app
        prompt += "\n\nThe previous draft was too short or contained placeholders. Rewrite it completely."
    return None


def write_application(client: anthropic.Anthropic, cfg: dict, company, language: str) -> Application | None:
    prompt = f"""Write the application email in {language}.

Firm: {company['name']} — {company['segment']}, {company['city']} ({company['country']})
Why it fits: {company['why_fit']}
Verified recent facts: {company['recent_facts'] or '(none found)'}
Recipient: {company['contact_email']} ({'recruiting team' if company['contact_type'] == 'recruiting' else 'general inbox — ask them to forward it to the team in charge of internships'})"""
    return _call(client, cfg, prompt)


def write_followup(client: anthropic.Anthropic, cfg: dict, app_row, language: str) -> Application | None:
    prompt = f"""Write a polite follow-up in {language}, 50–80 words, to the email below that got no reply.
Do not repeat the whole pitch; restate availability ({cfg['candidate']['internship']['start']}) and ask whether
the application could be passed to the right person. Subject: keep the original subject prefixed with "Re: ".
Overrides the 120–180 word rule for this message only.

Original subject: {app_row['subject']}
Original email:
{app_row['body']}"""
    return _call(client, cfg, prompt, min_words=35)
