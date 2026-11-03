"""The loop: keep the pipeline full, apply during recipients' office hours,
follow up once, stop for the day at the target, start again tomorrow."""

import csv
import logging
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

from . import scoring
from .discovery import discover
from .settings import DB_FILE, GMAIL_SCOPES, LOGS_DIR, TOKEN_FILE
from .store import Store, day_start, utcnow
from .writer import write_application, write_followup

log = logging.getLogger("internship_agent")

REPLY_CHECK_EVERY = timedelta(hours=1)
IDLE_SLEEP = 15 * 60  # nobody is in office hours / pipeline empty


class Agent:
    def __init__(self, cfg: dict, store: Store | None = None, client=None, mailer=None):
        self.cfg = cfg
        self.mode = cfg["engine"]["mode"]
        self.store = store or Store(DB_FILE)
        self.client = client or anthropic.Anthropic()
        self.mailer = mailer
        self._last_reply_check = None
        self._failures: dict[str, int] = {}
        self.exhausted = False
        if self.mode != "dry_run" and self.mailer is None:
            self.mailer = self._connect_gmail()

    def _connect_gmail(self):
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from gmail_auth import get_gmail_service  # reuse the repo's OAuth helper
        from .mailer import Mailer

        cv = self.cfg["candidate"]["cv_path"]
        if not cv.exists():
            raise FileNotFoundError(f"CV not found at {cv} — set candidate.cv_path in criteria.yaml")
        return Mailer(get_gmail_service(GMAIL_SCOPES, TOKEN_FILE), cv)

    # ── counters ─────────────────────────────────────────────────────────────
    def done_today(self) -> int:
        return self.store.count_today(self.mode, since=day_start())

    def target_reached(self) -> bool:
        s = self.cfg["schedule"]
        if self.mode == "send" and self.store.emails_out_since(day_start()) >= s["hard_daily_cap"]:
            return True
        return self.done_today() >= s["daily_target"]

    # ── pipeline ─────────────────────────────────────────────────────────────
    def add_companies(self, companies: list[dict], region: str = "manual") -> tuple[int, int]:
        added = qualified = 0
        for raw in companies:
            c = scoring.evaluate(raw, self.cfg)
            c.setdefault("region", region)
            if c["domain"] and self.store.add_company(c):
                added += 1
                qualified += c["status"] == "qualified"
                if c["status"] == "rejected":
                    log.debug("rejected %s: %s", c["domain"], c["reject_reason"])
        return added, qualified

    def run_discovery(self, region: dict | None = None) -> int:
        regions = self.cfg["market"]["regions"]
        region = region or regions[self.store.total_discovery_runs() % len(regions)]
        log.info("🔎 Discovery in %s …", region["name"])
        found = discover(self.client, self.cfg, region, self.store.known_domains(limit=400))
        added, qualified = self.add_companies(found, region["name"])
        self.store.record_discovery(region["name"], len(found), added)
        log.info("   %d found · %d new · %d qualified", len(found), added, qualified)
        return qualified

    def refill_pipeline(self):
        s = self.cfg["schedule"]
        while (len(self.store.qualified()) < s["pipeline_buffer"]
               and self.store.discovery_runs_today() < s["max_discovery_rounds_per_day"]):
            self.run_discovery()

    def pick_next(self, ignore_hours: bool):
        s, now = self.cfg["schedule"], utcnow()
        for c in self.store.qualified():
            if ignore_hours or scoring.in_office_hours(c["country"], now, s["recipient_hours"], s["weekdays_only"]):
                return c
        return None

    # ── actions ──────────────────────────────────────────────────────────────
    def apply(self, company) -> bool:
        lang = scoring.letter_language(company["country"], self.cfg["candidate"]["languages"])
        letter = write_application(self.client, self.cfg, company, lang)
        if letter is None:
            self.store.set_status(company["domain"], "rejected", "writer could not produce a valid letter")
            return False

        gmail_id = thread_id = None
        try:
            if self.mode == "send":
                r = self.mailer.send(company["contact_email"], letter.subject, letter.body)
                gmail_id, thread_id = r["id"], r["threadId"]
            elif self.mode == "draft":
                r = self.mailer.draft(company["contact_email"], letter.subject, letter.body)
                gmail_id, thread_id = r["id"], r["threadId"]
        except Exception as e:  # network / quota: keep the company queued, retry a few times
            n = self._failures[company["domain"]] = self._failures.get(company["domain"], 0) + 1
            log.error("✗ delivery failed for %s (attempt %d): %s", company["domain"], n, e)
            if n >= 3:
                self.store.set_status(company["domain"], "rejected", f"delivery failed: {e}")
            return False

        self.store.record_application(company["domain"], company["contact_email"], letter.subject,
                                      letter.body, lang, self.mode, gmail_id, thread_id,
                                      self.cfg["schedule"]["followup_after_days"])
        self._archive_letter(company, letter, lang)
        verb = {"send": "sent", "draft": "drafted", "dry_run": "previewed"}[self.mode]
        log.info("✓ %s %s → %s (%s, score %s, %s) [%d/%d today]", verb, company["name"],
                 company["contact_email"], company["country"], company["score"], lang,
                 self.done_today(), self.cfg["schedule"]["daily_target"])
        return True

    def _archive_letter(self, company, letter, lang):
        folder = LOGS_DIR / "letters" / datetime.now().strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{company['domain']}.txt").write_text(
            f"MODE: {self.mode}\nTO: {company['contact_email']}\nSOURCE: {company['contact_source']}\n"
            f"SCORE: {company['score']} · {company['segment']} · {company['country']} · {lang}\n"
            f"SUBJECT: {letter.subject}\n\n{letter.body}\n", encoding="utf-8")

    def check_replies(self):
        if self.mode != "send" or not self.mailer:
            return
        if self._last_reply_check and utcnow() - self._last_reply_check < REPLY_CHECK_EVERY:
            return
        self._last_reply_check = utcnow()
        for app in self.store.open_threads():
            try:
                status = self.mailer.thread_status(app["thread_id"])
            except Exception as e:
                log.warning("reply check failed for %s: %s", app["domain"], e)
                continue
            if status != "waiting":
                self.store.mark_replied(app["id"], app["domain"], status)
                log.info("📬 %s from %s (%s)", status.upper(), app["domain"], app["to_email"])

    def do_followups(self):
        if self.mode != "send":
            return
        s, now = self.cfg["schedule"], utcnow()
        for app in self.store.due_followups():
            if self.store.emails_out_since(day_start()) >= s["hard_daily_cap"]:
                return
            if not scoring.in_office_hours(app["country"], now, s["recipient_hours"], s["weekdays_only"]):
                continue
            # last-second reply check so we never nudge someone who just answered
            if self.mailer.thread_status(app["thread_id"]) != "waiting":
                self.store.mark_replied(app["id"], app["domain"])
                continue
            fu = write_followup(self.client, self.cfg, app, app["language"] or "English")
            if fu is None:
                self.store.mark_followed_up(app["id"])  # don't retry forever
                continue
            self.mailer.followup(app, fu.subject, fu.body)
            self.store.mark_followed_up(app["id"])
            log.info("↻ follow-up sent → %s (%s)", app["name"], app["to_email"])
            time.sleep(random.uniform(30, 90))

    # ── loop ─────────────────────────────────────────────────────────────────
    def tick(self, preview: bool = False) -> float | None:
        """One iteration. Returns seconds to wait, or None when today's target is met."""
        self.check_replies()
        self.do_followups()
        if self.target_reached():
            return None
        self.refill_pipeline()
        company = self.pick_next(ignore_hours=preview)
        self.exhausted = False
        if company is None:
            if not self.store.qualified():
                log.warning("Pipeline empty and discovery budget for today used up — "
                            "widen the market range or raise max_discovery_rounds_per_day.")
                self.exhausted = True
                return None if preview else IDLE_SLEEP
            log.info("No qualified company is in office hours right now; waiting.")
            return IDLE_SLEEP
        if not self.apply(company):
            return 60
        if preview:
            return 0
        s = self.cfg["schedule"]
        return random.uniform(s["min_gap_minutes"], s["max_gap_minutes"]) * 60

    def run(self, once: bool = False, limit: int | None = None):
        preview = self.mode == "dry_run"
        if limit:
            self.cfg["schedule"]["daily_target"] = min(limit, self.cfg["schedule"]["hard_daily_cap"])
        log.info("Agent started · mode=%s · target=%d/day", self.mode, self.cfg["schedule"]["daily_target"])
        while True:
            wait = self.tick(preview=preview)
            if once and self.exhausted:
                log.info("Nothing left to send today: %s", self.store.stats())
                return
            if wait is None:
                log.info("Daily target reached: %s", self.store.stats())
                if once or preview:
                    return
                wait = (day_start() + timedelta(days=1, minutes=5) - datetime.now().astimezone()).total_seconds()
                log.info("Sleeping until tomorrow (%.1f h).", wait / 3600)
            time.sleep(max(wait, 0))

    def seed_from_csv(self, path: Path) -> tuple[int, int]:
        rows = []
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                r["employees"] = int(r["employees"]) if (r.get("employees") or "").isdigit() else None
                r["internship_signal"] = (r.get("internship_signal") or "").strip().lower() in {"1", "true", "yes", "oui"}
                rows.append(r)
        return self.add_companies(rows, region="seed")
