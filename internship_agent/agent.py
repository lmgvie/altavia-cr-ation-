"""The loop: keep the pipeline full, apply during recipients' office hours,
follow up once, stop for the day at the target, start again tomorrow."""

import csv
import logging
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

from . import digest, scoring
from .discovery import discover
from .offers import evaluate_offer, find_offers
from .settings import DB_FILE, GMAIL_SCOPES, LOGS_DIR, TOKEN_FILE
from .store import Store, day_start, utcnow
from .writer import analyse_reply, write_application, write_followup, write_offer_kit

log = logging.getLogger("internship_agent")

REPLY_CHECK_EVERY = timedelta(hours=1)
IDLE_SLEEP = 15 * 60  # nobody is in office hours / pipeline empty
AFTER_TARGET_SLEEP = 30 * 60  # target met: keep checking replies and send the digest

CONFIRMATION_QUERY = (
    'newer_than:14d (from:linkedin.com subject:("application was sent" OR "candidature a été envoyée") '
    'OR subject:("thank you for applying" OR "thank you for your application" OR "application received" '
    'OR "merci pour votre candidature" OR "nous avons bien reçu votre candidature"))')


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
        self._target_logged = None
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
                                      self.cfg["schedule"]["followup_after_days"],
                                      letter.linkedin_note or None, letter.linkedin_message or None)
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

    def check_replies(self, force: bool = False):
        """Find new answers, classify them, save a suggested reply in Gmail Drafts."""
        if self.mode == "dry_run" or not self.mailer:
            return
        if not force and self._last_reply_check and utcnow() - self._last_reply_check < REPLY_CHECK_EVERY:
            return
        self._last_reply_check = utcnow()
        threads = self.store.open_threads_by_id()
        if threads:
            for tid in self.mailer.recent_inbound_thread_ids(days=3) & threads.keys():
                try:
                    self._process_thread(threads[tid])
                except Exception as e:
                    log.warning("reply handling failed for %s: %s", threads[tid]["domain"], e)
        self.detect_offer_confirmations()

    def _process_thread(self, app):
        for m in self.mailer.inbound_messages(app["thread_id"]):
            if self.store.reply_seen(m["id"]):
                continue
            if m["from"].startswith(("mailer-daemon", "postmaster")):
                self.store.record_reply(m["id"], app["id"], "bounce", f"Delivery to {app['to_email']} failed.", None)
                self.store.mark_replied(app["id"], app["domain"], "bounced")
                log.info("⚠️ BOUNCE %s (%s)", app["domain"], app["to_email"])
                continue
            name = self.store.company_name(app["domain"])
            res = analyse_reply(self.client, self.cfg, app, name, m["text"])
            kind = res.kind if res else "other"
            summary = res.summary if res else "Reply received (could not be analysed — read it in Gmail)."
            draft_id = None
            if (res and kind != "auto_ack" and res.draft_reply.strip()
                    and self.cfg.get("replies", {}).get("draft_responses", True)):
                draft_id = self.mailer.draft_reply(app["thread_id"], m["from"], m["subject"] or app["subject"],
                                                   res.draft_reply, m["message_id"])
            self.store.record_reply(m["id"], app["id"], kind, summary, draft_id)
            if kind != "auto_ack":  # an automatic acknowledgement doesn't stop the follow-up
                self.store.mark_replied(app["id"], app["domain"])
            log.info("📬 %s from %s%s", kind.upper(), name, " — draft ready in Gmail" if draft_id else "")

    # ── online job offers ────────────────────────────────────────────────────
    def offers_due(self) -> bool:
        oc = self.cfg.get("offers", {})
        return (oc.get("enabled", False)
                and self.store.offers_ready_since(day_start()) < oc["daily_target"]
                and self.store.offer_runs_today() < oc["max_search_rounds_per_day"])

    def run_offer_search(self, region: dict | None = None) -> int:
        regions = self.cfg["market"]["regions"]
        region = region or regions[self.store.total_offer_runs() % len(regions)]
        log.info("🧾 Searching job offers in %s …", region["name"])
        found = find_offers(self.client, self.cfg, region, self.store.known_offer_urls())
        added = qualified = 0
        for raw in found:
            o = evaluate_offer(raw, self.cfg)
            if o["url"] and self.store.add_offer(o):
                added += 1
                qualified += o["status"] == "qualified"
        self.store.record_offer_run(region["name"], len(found), added)
        log.info("   %d offers found · %d new · %d qualified", len(found), added, qualified)
        return qualified

    def prepare_offer_kits(self):
        missing = self.cfg["offers"]["daily_target"] - self.store.offers_ready_since(day_start())
        for o in self.store.offers_with_status("qualified")[:max(missing, 0)]:
            lang = (o["language"] or "English").capitalize()
            kit = write_offer_kit(self.client, self.cfg, o, lang)
            if kit is None:
                self.store.set_offer_status(o["url"], "rejected", "writer could not produce a kit")
                continue
            self.store.set_offer_kit(o["url"], kit.cover_letter, kit.why_answer)
            log.info("📝 offer ready: %s — %s", o["title"], o["company"])

    def detect_offer_confirmations(self):
        """Mark offers as applied when LinkedIn / the company confirms by email."""
        ready = self.store.ready_offers()
        if not ready:
            return
        subjects = [scoring._norm(x) for x in self.mailer.search_subjects(CONFIRMATION_QUERY)]
        for o in ready:
            name = scoring._norm(o["company"])
            if name and any(name in subj for subj in subjects):
                self.store.set_offer_status(o["url"], "applied")
                log.info("✅ applied online: %s — %s", o["title"], o["company"])

    # ── daily digest ─────────────────────────────────────────────────────────
    def maybe_send_digest(self, force: bool = False) -> bool:
        dg = self.cfg.get("digest", {})
        if not dg.get("enabled", False):
            return False
        key = "digest:" + datetime.now().strftime("%Y-%m-%d")
        if self.store.get_meta(key) or (not force and datetime.now().hour < dg.get("hour", 18)):
            return False

        self.store.expire_offers(self.cfg["offers"]["keep_in_digest_days"])
        all_replies = self.store.undigested_replies()
        replies = [r for r in all_replies if r["kind"] != "auto_ack"]
        offers = self.store.ready_offers() if self.cfg["offers"].get("enabled") else []
        linkedin = []
        li = self.cfg.get("linkedin", {})
        if li.get("enabled"):
            budget = li["weekly_invites"] - self.store.linkedin_listed_since(utcnow() - timedelta(days=7))
            linkedin = self.store.linkedin_candidates(li["min_score"], self.mode)[:max(min(budget, 20), 0)]

        subject, html, text = digest.build(
            self.cfg, self.store.stats(), self.store.applications_since(day_start(), self.mode),
            replies, offers, linkedin, self.store.contacted_domains(),
            {o["url"] for o in offers if not o["listed_at"]})

        folder = LOGS_DIR / "digest"
        folder.mkdir(parents=True, exist_ok=True)
        page = folder / f"{datetime.now():%Y-%m-%d}.html"
        page.write_text(f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
                        f'content="width=device-width,initial-scale=1"><title>Internship digest</title></head>'
                        f'<body style="background:#fff;margin:0">{html}</body></html>', encoding="utf-8")
        if self.mode != "dry_run" and self.mailer:
            self.mailer.send_html_to_self(subject, html, text)
        self.store.mark_digested([r["gmail_msg_id"] for r in all_replies], [c["id"] for c in linkedin],
                                 [o["url"] for o in offers])
        self.store.set_meta(key, utcnow().isoformat())
        log.info("📨 digest %s: %s", "emailed to you" if self.mode != "dry_run" else "saved", page)
        return True

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
            self._process_thread(app)
            if self.store.has_reply(app["id"]):
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
        self.maybe_send_digest()
        if self.offers_due():
            self.run_offer_search()
            self.prepare_offer_kits()
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
        errors = 0
        while True:
            try:
                wait = self.tick(preview=preview)
                errors = 0
            except KeyboardInterrupt:
                raise
            except Exception:
                # Unattended mode: an API outage, network drop or expired token must not
                # kill the loop. Back off 5 min, 10 min, … up to 1 h, then keep retrying.
                errors += 1
                wait = min(300 * errors, 3600)
                log.exception("Iteration failed (%d in a row); retrying in %d min", errors, wait // 60)
                time.sleep(wait)
                continue
            if once and self.exhausted:
                log.info("Nothing left to send today: %s", self.store.stats())
                self.maybe_send_digest(force=True)
                return
            if wait is None:
                today = datetime.now().strftime("%Y-%m-%d")
                if self._target_logged != today:
                    log.info("Daily target reached: %s", self.store.stats())
                    self._target_logged = today
                if once or preview:
                    self.maybe_send_digest(force=True)
                    return
                # keep watching replies and send the evening digest, then resume tomorrow
                to_tomorrow = (day_start() + timedelta(days=1, minutes=5) - datetime.now().astimezone()).total_seconds()
                wait = min(AFTER_TARGET_SLEEP, to_tomorrow)
            time.sleep(max(wait, 0))

    def seed_from_csv(self, path: Path) -> tuple[int, int]:
        rows = []
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                r["employees"] = int(r["employees"]) if (r.get("employees") or "").isdigit() else None
                r["internship_signal"] = (r.get("internship_signal") or "").strip().lower() in {"1", "true", "yes", "oui"}
                rows.append(r)
        return self.add_companies(rows, region="seed")
