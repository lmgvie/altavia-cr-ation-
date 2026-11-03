"""Offline tests: fake Claude + fake Gmail. Run:  python -m unittest discover tests"""

import copy
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from internship_agent import agent as agent_mod
from internship_agent import scoring
from internship_agent.offers import clean_url, evaluate_offer
from internship_agent.settings import load_settings
from internship_agent.store import Store, iso
from internship_agent.writer import Application, OfferKit, ReplyAnalysis

CFG = load_settings(use_local=False)  # tests never depend on personal settings


def company(i, **kw):
    c = {
        "name": f"Firm {i}", "website": f"https://www.firm{i}.com", "country": "FR", "city": "Paris",
        "segment": "M&A advisory", "employees": 60, "contact_email": f"careers@firm{i}.com",
        "contact_source": f"https://www.firm{i}.com/careers", "internship_signal": True,
        "why_fit": "Mid-market sell-side boutique", "recent_facts": "", "linkedin_url": "",
    }
    c.update(kw)
    return c


def offer(i, **kw):
    o = {
        "title": f"M&A Intern {i} (6 months, Jan 2027)", "company": f"Bank {i}",
        "company_website": f"https://bank{i}.com", "location": "Paris, France", "country": "FR",
        "url": f"https://www.linkedin.com/jobs/view/ma-intern-at-bank-{1000 + i}?trk=abc",
        "platform": "linkedin", "posted": date.today().isoformat(), "language": "English",
        "requirements": "Excel, valuation", "segment": "M&A advisory", "why_fit": "Mid-market M&A",
    }
    o.update(kw)
    return o


class FakeClient:
    """Mimics client.beta.messages.stream(...) (search) and .parse(...) (writing)."""

    def __init__(self, company_batches, offer_batches=None, reply_kind="interview"):
        self.company_batches = list(company_batches)
        self.offer_batches = list(offer_batches or [])
        self.reply_kind = reply_kind
        self.discovery_calls = self.offer_calls = 0
        outer = self

        class _Stream:
            def __init__(s, kw):
                s.tool = kw["tools"][1]["name"]

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def get_final_message(s):
                if s.tool == "submit_companies":
                    outer.discovery_calls += 1
                    data = {"companies": outer.company_batches.pop(0) if outer.company_batches else []}
                else:
                    outer.offer_calls += 1
                    data = {"offers": outer.offer_batches.pop(0) if outer.offer_batches else []}
                block = SimpleNamespace(type="tool_use", name=s.tool, input=data)
                return SimpleNamespace(stop_reason="tool_use", content=[block], stop_details=None)

        def parse(**kw):
            words = "I am writing to apply for an internship in your team. " * 20
            cls = kw["output_format"]
            if cls is Application:
                out = Application(subject="Internship application", body="Dear team,\n\n" + words,
                                  linkedin_note="Hello, I just emailed your team about a 6-month internship. " * 8,
                                  linkedin_message="Thanks for connecting. " * 10)
            elif cls is OfferKit:
                out = OfferKit(cover_letter=words, why_answer="Because of your mid-market focus.")
            else:
                out = ReplyAnalysis(kind=outer.reply_kind, summary="They propose a call next week.",
                                    draft_reply="" if outer.reply_kind == "auto_ack" else "Thank you, I am available.")
            return SimpleNamespace(stop_reason="end_turn", stop_details=None, parsed_output=out)

        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: _Stream(kw), parse=parse))


class FakeMailer:
    def __init__(self):
        self.sent, self.followups, self.drafts, self.digests = [], [], [], []
        self.inbox: dict[str, list[dict]] = {}   # thread_id -> inbound messages
        self.confirmation_subjects: list[str] = []

    def send(self, to, subject, body, **kw):
        self.sent.append(to)
        return {"id": f"m{len(self.sent)}", "threadId": f"t{len(self.sent)}"}

    def draft(self, to, subject, body, **kw):
        return self.send(to, subject, body)

    def followup(self, app, subject, body):
        self.followups.append(app["to_email"])

    def recent_inbound_thread_ids(self, days=3):
        return set(self.inbox)

    def inbound_messages(self, tid):
        return self.inbox.get(tid, [])

    def draft_reply(self, thread_id, to, subject, body, in_reply_to):
        self.drafts.append((thread_id, to, body))
        return f"d{len(self.drafts)}"

    def send_html_to_self(self, subject, html, text):
        self.digests.append((subject, html))

    def search_subjects(self, q, limit=100):
        return self.confirmation_subjects


def inbound(mid, sender="anna@firm1.com", text="Could we have a call on Tuesday?"):
    return {"id": mid, "from": sender, "subject": "Re: Internship application", "message_id": f"<{mid}>", "text": text}


class ScoringTests(unittest.TestCase):
    def test_good_company_qualifies(self):
        c = scoring.evaluate(company(1), CFG)
        self.assertEqual(c["status"], "qualified")
        self.assertEqual(c["contact_type"], "recruiting")
        self.assertGreaterEqual(c["score"], CFG["scoring"]["min_score"])

    def test_email_domain_must_match_website(self):
        c = scoring.evaluate(company(1, contact_email="jobs@gmail.com"), CFG)
        self.assertEqual(c["status"], "rejected")

    def test_named_contacts_blocked_by_default(self):
        c = scoring.evaluate(company(1, contact_email="jean.dupont@firm1.com"), CFG)
        self.assertEqual(c["status"], "rejected")

    def test_noreply_and_missing_source_rejected(self):
        self.assertEqual(scoring.evaluate(company(1, contact_email="noreply@firm1.com"), CFG)["status"], "rejected")
        self.assertEqual(scoring.evaluate(company(1, contact_source=""), CFG)["status"], "rejected")

    def test_empty_segment_does_not_match(self):
        self.assertFalse(scoring.segment_matches("", ["M&A advisory"]))

    def test_excluded_segment(self):
        self.assertEqual(scoring.evaluate(company(1, segment="Retail banking"), CFG)["status"], "rejected")

    def test_off_target_segment_scores_low(self):
        c = scoring.evaluate(company(1, segment="Restaurant chain", internship_signal=False,
                                     contact_email="info@firm1.com"), CFG)
        self.assertEqual(c["status"], "rejected")

    def test_uk_subdomains(self):
        c = scoring.evaluate(company(1, website="https://www.firm.co.uk", contact_email="careers@firm.co.uk",
                                     country="GB"), CFG)
        self.assertEqual(c["status"], "qualified")

    def test_language_and_office_hours(self):
        self.assertEqual(scoring.letter_language("FR", ["English", "French"]), "French")
        self.assertEqual(scoring.letter_language("DE", ["English", "French"]), "English")
        wed_10_paris = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
        self.assertTrue(scoring.in_office_hours("FR", wed_10_paris, [9, 17], True))
        self.assertFalse(scoring.in_office_hours("SG", wed_10_paris, [9, 17], True))   # 17:00 in SG
        sat = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
        self.assertFalse(scoring.in_office_hours("FR", sat, [9, 17], True))


class OfferFilterTests(unittest.TestCase):
    def test_good_offer_qualifies_and_url_is_cleaned(self):
        o = evaluate_offer(offer(1), CFG)
        self.assertEqual(o["status"], "qualified")
        self.assertEqual(o["url"], "https://www.linkedin.com/jobs/view/1001/")

    def test_same_posting_with_tracking_params_is_one_url(self):
        a = clean_url("https://www.linkedin.com/jobs/view/4012345678/?refId=x&trackingId=y")
        b = clean_url("https://fr.linkedin.com/jobs/view/analyste-m-a-4012345678")
        self.assertEqual(a, b)

    def test_rejections(self):
        old = (date.today() - timedelta(days=60)).isoformat()
        cases = {
            "title": offer(1, title="Senior Associate, Private Equity"),
            "age": offer(1, posted=old),
            "country": offer(1, country="BR"),
            "language": offer(1, language="German"),
            "segment": offer(1, segment="Retail banking"),
            "url": offer(1, url="not a url"),
        }
        for name, o in cases.items():
            with self.subTest(name):
                self.assertEqual(evaluate_offer(o, CFG)["status"], "rejected")


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.cfg = copy.deepcopy(CFG)
        self.cfg["engine"]["mode"] = "send"
        self.cfg["offers"]["enabled"] = False
        patches = [
            mock.patch.object(agent_mod.time, "sleep", lambda s: None),
            mock.patch.object(agent_mod.scoring, "in_office_hours", lambda *a: True),
            mock.patch.object(agent_mod, "LOGS_DIR", Path(self.tmp.name)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def make_agent(self, batches, offer_batches=None, reply_kind="interview"):
        self.mailer = FakeMailer()
        self.client = FakeClient(batches, offer_batches, reply_kind)
        return agent_mod.Agent(self.cfg, store=self.store, client=self.client, mailer=self.mailer)

    def sent_one(self, a, i=1):
        a.add_companies([company(i)])
        a.apply(self.store.qualified()[0])
        return self.store.db.execute("SELECT * FROM applications ORDER BY id DESC LIMIT 1").fetchone()

    # ── email loop ───────────────────────────────────────────────────────────
    def test_reaches_daily_target_without_duplicates(self):
        batches = [[company(i) for i in range(b * 15, b * 15 + 15)] for b in range(6)]
        batches[0].append(company(0))  # duplicate inside discovery output
        a = self.make_agent(batches)
        a.run(once=True)
        self.assertEqual(len(self.mailer.sent), self.cfg["schedule"]["daily_target"])
        self.assertGreater(len(self.mailer.sent), 30)
        self.assertEqual(len(set(self.mailer.sent)), len(self.mailer.sent))

    def test_discovery_budget_stops_runaway(self):
        a = self.make_agent([[] for _ in range(50)])   # discovery never finds anything
        a.run(once=True)
        self.assertEqual(self.client.discovery_calls, self.cfg["schedule"]["max_discovery_rounds_per_day"])
        self.assertEqual(self.mailer.sent, [])

    def test_hard_cap_counts_followups(self):
        self.cfg["schedule"].update(daily_target=40, hard_daily_cap=40)
        a = self.make_agent([[company(i) for i in range(100)]])
        a.add_companies([company(1000 + i) for i in range(10)])
        for i in range(10):  # 10 old applications due for follow-up
            d = f"firm{1000 + i}.com"
            self.store.record_application(d, f"careers@{d}", "s", "b", "French", "send", f"g{i}", f"old{i}", 0)
            self.store.db.execute("UPDATE applications SET sent_at=? WHERE domain=?",
                                  (iso(datetime.now(timezone.utc) - timedelta(days=9)), d))
        self.store.db.commit()
        a.run(once=True)
        self.assertEqual(len(self.mailer.followups), 10)
        self.assertEqual(len(self.mailer.sent) + len(self.mailer.followups), 40)

    def test_loop_survives_errors(self):
        a = self.make_agent([[company(i) for i in range(50)]])
        real_tick, calls = a.tick, {"n": 0}

        def flaky(preview=False):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ConnectionError("network down")
            return real_tick(preview)

        a.tick = flaky
        a.run(once=True)
        self.assertEqual(len(self.mailer.sent), self.cfg["schedule"]["daily_target"])

    def test_dry_run_sends_nothing(self):
        self.cfg["engine"]["mode"] = "dry_run"
        a = self.make_agent([[company(i) for i in range(20)]])
        a.mailer = None
        a.run(limit=5)
        self.assertEqual(self.store.stats()["previewed"], 5)
        self.assertEqual(self.mailer.sent, [])
        self.assertTrue(any((Path(self.tmp.name) / "digest").glob("*.html")))  # preview digest saved

    # ── replies ──────────────────────────────────────────────────────────────
    def test_reply_gets_draft_and_stops_followup(self):
        a = self.make_agent([])
        app = self.sent_one(a)
        self.mailer.inbox[app["thread_id"]] = [inbound("r1")]
        a.check_replies(force=True)
        self.assertEqual(len(self.mailer.drafts), 1)
        self.assertEqual(self.mailer.drafts[0][0], app["thread_id"])  # same thread
        self.assertEqual(self.store.stats()["replied"], 1)
        self.store.db.execute("UPDATE applications SET followup_at=?", (iso(datetime.now(timezone.utc)),))
        self.store.db.commit()
        a.do_followups()
        self.assertEqual(self.mailer.followups, [])
        a.check_replies(force=True)                 # same message again → not re-processed
        self.assertEqual(len(self.mailer.drafts), 1)

    def test_auto_ack_does_not_stop_followup(self):
        a = self.make_agent([], reply_kind="auto_ack")
        app = self.sent_one(a)
        self.mailer.inbox[app["thread_id"]] = [inbound("r1", text="We received your application.")]
        self.store.db.execute("UPDATE applications SET followup_at=?", (iso(datetime.now(timezone.utc)),))
        self.store.db.commit()
        a.do_followups()
        self.assertEqual(self.mailer.drafts, [])
        self.assertEqual(len(self.mailer.followups), 1)

    def test_bounce(self):
        a = self.make_agent([])
        app = self.sent_one(a)
        self.mailer.inbox[app["thread_id"]] = [inbound("b1", sender="mailer-daemon@googlemail.com")]
        a.check_replies(force=True)
        self.assertEqual(self.store.stats()["bounced"], 1)
        self.assertEqual(self.mailer.drafts, [])

    # ── offers, LinkedIn, digest ─────────────────────────────────────────────
    def test_offers_kits_and_confirmation(self):
        self.cfg["offers"]["enabled"] = True
        a = self.make_agent([[company(i) for i in range(60)]],
                            offer_batches=[[offer(i) for i in range(10)], [offer(i) for i in range(10, 25)]])
        a.run(once=True)
        self.assertEqual(self.store.stats()["offers_ready"], self.cfg["offers"]["daily_target"])
        self.assertLessEqual(self.client.offer_calls, self.cfg["offers"]["max_search_rounds_per_day"])
        self.mailer.confirmation_subjects = ["Firstname, your application was sent to Bank 3"]
        a.check_replies(force=True)
        self.assertEqual(self.store.stats()["offers_applied"], 1)

    def test_digest_once_a_day_with_everything(self):
        self.cfg["offers"]["enabled"] = True
        a = self.make_agent([[company(i) for i in range(60)]], offer_batches=[[offer(i) for i in range(15)]])
        a.run(once=True)                               # sends digest at the end
        self.assertEqual(len(self.mailer.digests), 1)
        subject, html = self.mailer.digests[0]
        self.assertIn("Job offers to apply online", html)
        self.assertIn("LinkedIn", html)
        self.assertIn("linkedin.com/jobs/view/", html)
        self.assertFalse(a.maybe_send_digest(force=True))   # never twice the same day
        notes = [r[0] for r in self.store.db.execute("SELECT linkedin_note FROM applications")]
        self.assertTrue(all(len(n) <= 300 for n in notes))

    def test_linkedin_weekly_budget(self):
        self.cfg["linkedin"].update(weekly_invites=5, min_score=0)
        a = self.make_agent([[company(i) for i in range(40)]])
        a.run(once=True)
        listed = self.store.db.execute(
            "SELECT COUNT(*) FROM applications WHERE linkedin_listed_at IS NOT NULL").fetchone()[0]
        self.assertEqual(listed, 5)


if __name__ == "__main__":
    unittest.main()
