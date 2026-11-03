"""Company discovery: Claude searches the web for firms inside the market range.

Claude uses the server-side web search tool, then hands its findings back
through a strict `submit_companies` tool so the output is always valid JSON.
Scoring/filtering happens afterwards in scoring.py.
"""

import json
import logging

import anthropic

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

SUBMIT_TOOL = {
    "name": "submit_companies",
    "description": (
        "Submit the companies you verified. Call this exactly once, at the end, "
        "with every company that has a publicly listed email address."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["companies"],
        "properties": {
            "companies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "name", "website", "country", "city", "segment", "employees",
                        "contact_email", "contact_source", "internship_signal",
                        "why_fit", "recent_facts",
                    ],
                    "properties": {
                        "name": {"type": "string"},
                        "website": {"type": "string", "description": "Official homepage URL"},
                        "country": {"type": "string", "description": "ISO 3166-1 alpha-2 code of the office you would join"},
                        "city": {"type": "string"},
                        "segment": {"type": "string", "description": "Main business line, e.g. 'M&A advisory'"},
                        "employees": {"type": "integer", "description": "Approximate headcount; 0 if unknown"},
                        "contact_email": {"type": "string", "description": "Email address exactly as published"},
                        "contact_source": {"type": "string", "description": "URL of the page where the email is published"},
                        "internship_signal": {
                            "type": "boolean",
                            "description": "True if the firm's site or a job board shows it hires interns/analysts",
                        },
                        "why_fit": {"type": "string", "description": "One or two sentences: why this firm fits the market range"},
                        "recent_facts": {
                            "type": "string",
                            "description": "1-3 concrete, verifiable recent facts (deal, fund close, mandate) with dates",
                        },
                    },
                },
            }
        },
    },
}


def _system_prompt(cfg: dict) -> str:
    m, c = cfg["market"], cfg["candidate"]
    return f"""You research companies for a finance student looking for an internship.

Market range to stay inside:
{m['description'].strip()}
Target segments: {', '.join(m['target_segments'])}
Never include: {', '.join(m['excluded_segments'])}
Headcount range: {m['employees']['min']}–{m['employees']['max']} employees.
Internship sought: {c['internship']['duration']} starting {c['internship']['start']}.

Rules for contact emails — these matter more than the number of results:
- Only report an email address you actually read on a web page, and give that page's URL.
- Never construct, guess or pattern-match an address (no "firstname.lastname@" inference).
- Prefer a careers / recruitment / HR inbox; a general inbox (info@, contact@) is acceptable.
- {'A named recruiter address is acceptable only if the company itself publishes it.' if cfg['engine'].get('allow_named_contacts') else 'Do not report addresses of named individuals.'}
- The email domain must belong to the company's own website domain.
- Skip a company rather than report an address you are unsure about.

Facts in why_fit and recent_facts must come from your searches; leave recent_facts empty if you found nothing.
When you are done, call submit_companies once with all verified companies."""


def discover(client: anthropic.Anthropic, cfg: dict, region: dict, exclude_domains: list[str]) -> list[dict]:
    eng = cfg["engine"]
    n = eng["companies_per_discovery"]
    user = (
        f"Find {n} companies in {region['name']} (countries: {', '.join(region['countries'])}) "
        f"that fit the market range and publish an email address where an internship application can be sent.\n"
        f"Mix well-known and lesser-known firms; smaller boutiques reply more often.\n"
        f"Already in the pipeline — do not return these domains:\n{', '.join(exclude_domains) or '(none)'}"
    )
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 25},
        SUBMIT_TOOL,
    ]
    messages = [{"role": "user", "content": user}]
    extra = {}
    if eng.get("refusal_fallbacks", True):
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}

    nudged = False
    for _ in range(8):  # pause_turn continuations + one nudge
        with client.beta.messages.stream(
            model=eng["model"],
            max_tokens=32000,
            system=_system_prompt(cfg),
            output_config={"effort": eng.get("discovery_effort", "medium")},
            tools=tools,
            messages=messages,
            **extra,
        ) as stream:
            resp = stream.get_final_message()

        if resp.stop_reason == "refusal":
            log.warning("Discovery declined for %s: %s", region["name"], resp.stop_details)
            return []

        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_companies":
                data = block.input if isinstance(block.input, dict) else json.loads(block.input)
                companies = data.get("companies", [])
                for comp in companies:
                    comp["region"] = region["name"]
                    if comp.get("employees") == 0:
                        comp["employees"] = None
                return companies

        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue  # server-side search loop paused; resend to resume
        if resp.stop_reason == "max_tokens" or nudged:
            break
        nudged = True
        messages.append({"role": "user", "content": "Please call submit_companies now with the companies you verified."})

    log.warning("Discovery for %s ended without results (stop_reason=%s)", region["name"], resp.stop_reason)
    return []
