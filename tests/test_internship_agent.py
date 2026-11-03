"""Offline tests: fake Claude + fake Gmail. Run:  python -m unittest discover tests"""

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from internship_agent import agent as agent_mod
from internship_agent import scoring
from internship_agent.settings import load_settings
from internship_agent.store import Store, iso
from internship_agent.writer import Application

CFG = load_settings()


def company(i, **kw):
    c = {
        "name": f"Firm {i}", "website": f"https://www.firm{i}.com", "country": "FR", "city": "Paris",
        "segment": "M&A advisory", "employees": 60, "contact_email": f"careers@firm{i}.com",
        "contact_source": f"https://www.firm{i}.com/careers", "internship_signal": True,
        "why_fit": "Mid-market sell-side boutique", "recent_facts": "",
    }
    c.update(kw)
    return c


class FakeClient:
    """Mimics client.beta.messages.stream(...) and .parse(...)."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.discovery_calls = 0
        outer = self

        class _Stream:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def get_final_message(s):
                outer.discovery_calls += 1
                comps = outer.batches.pop(0) if outer.batches else []
                block = SimpleNamespace(type="tool_use", name="submit_companies", input={"companies": comps})
                return SimpleNamespace(stop_reason="tool_use", content=[block], stop_details=None)

        def parse(**kw):
            body = "Dear team,\n\n" + "I am writing to apply for an internship. " * 20 + "\n\nFirstname Lastname"
            return SimpleNamespace(stop_reason="end_turn", stop_details=None,
                                   parsed_output=Application(subject="Internship application", body=body))

        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: _Stream(), parse=parse))


class FakeMailer:
    def __init__(self):
        self.sent, self.followups, self.replied_threads = [], [], set()

    def send(self, to, subject, body, **kw):
        self.sent.append(to)
        return {"id": f"m{len(self.sent)}", "threadId": f"t{len(self.sent)}"}

    def draft(self, to, subject, body, **kw):
        return self.send(to, subject, body)

    def followup(self, app, subject, body):
        self.followups.append(app["to_email"])

    def thread_status(self, tid):
        return "replied" if tid in self.replied_threads else "waiting"


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


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.cfg = copy.deepcopy(CFG)
        self.cfg["engine"]["mode"] = "send"
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

    def make_agent(self, batches):
        self.mailer = FakeMailer()
        self.client = FakeClient(batches)
        return agent_mod.Agent(self.cfg, store=self.store, client=self.client, mailer=self.mailer)

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
        # 10 old applications due for follow-up
        for i in range(10):
            d = f"firm{1000 + i}.com"
            self.store.record_application(d, f"careers@{d}", "s", "b", "French", "send", f"g{i}", f"old{i}", 0)
            self.store.db.execute("UPDATE applications SET sent_at=? WHERE domain=?",
                                  (iso(datetime.now(timezone.utc) - timedelta(days=9)), d))
        self.store.db.commit()
        a.run(once=True)
        self.assertEqual(len(self.mailer.followups), 10)
        self.assertEqual(len(self.mailer.sent) + len(self.mailer.followups), 40)

    def test_no_followup_after_reply(self):
        a = self.make_agent([[company(1)]])
        a.add_companies([company(1)])
        a.apply(self.store.qualified()[0])
        self.store.db.execute("UPDATE applications SET followup_at=?", (iso(datetime.now(timezone.utc)),))
        self.store.db.commit()
        self.mailer.replied_threads.add("t1")
        a.do_followups()
        self.assertEqual(self.mailer.followups, [])
        self.assertEqual(self.store.stats()["replied"], 1)

    def test_dry_run_sends_nothing(self):
        self.cfg["engine"]["mode"] = "dry_run"
        a = self.make_agent([[company(i) for i in range(20)]])
        a.mailer = None
        a.run(limit=5)
        self.assertEqual(self.store.stats()["previewed"], 5)
        self.assertEqual(self.mailer.sent, [])


if __name__ == "__main__":
    unittest.main()
