"""SQLite pipeline: companies found, scores, applications sent, follow-ups."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    domain            TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    country           TEXT,
    city              TEXT,
    segment           TEXT,
    employees         INTEGER,
    website           TEXT,
    contact_email     TEXT,
    contact_type      TEXT,
    contact_source    TEXT,
    internship_signal INTEGER DEFAULT 0,
    why_fit           TEXT,
    recent_facts      TEXT,
    score             INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'new',   -- qualified | rejected | previewed | applied | replied | bounced
    reject_reason     TEXT,
    region            TEXT,
    linkedin_url      TEXT,
    found_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    domain        TEXT NOT NULL REFERENCES companies(domain),
    to_email      TEXT NOT NULL,
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    language      TEXT,
    mode          TEXT NOT NULL,           -- dry_run | draft | send
    gmail_id      TEXT,
    thread_id     TEXT,
    sent_at       TEXT NOT NULL,
    followup_at   TEXT,
    followed_up   INTEGER DEFAULT 0,
    followed_up_at TEXT,
    replied       INTEGER DEFAULT 0,
    linkedin_note    TEXT,
    linkedin_message TEXT,
    linkedin_listed_at TEXT                -- set when the LinkedIn kit went into a digest
);
CREATE TABLE IF NOT EXISTS replies (
    gmail_msg_id  TEXT PRIMARY KEY,
    app_id        INTEGER NOT NULL REFERENCES applications(id),
    kind          TEXT,                    -- interview | info_request | rejection | auto_ack | other | bounce
    summary       TEXT,
    draft_id      TEXT,
    received_at   TEXT NOT NULL,
    digested      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS offers (
    url           TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    company_domain TEXT,
    location      TEXT,
    country       TEXT,
    platform      TEXT,                    -- linkedin | ats | company_site | job_board
    posted        TEXT,
    language      TEXT,
    requirements  TEXT,
    why_fit       TEXT,
    score         INTEGER DEFAULT 0,
    status        TEXT NOT NULL,           -- qualified | rejected | ready | applied | expired
    reject_reason TEXT,
    cover_letter  TEXT,
    why_answer    TEXT,
    found_at      TEXT NOT NULL,
    listed_at     TEXT,
    done_at       TEXT
);
CREATE TABLE IF NOT EXISTS offer_runs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    region  TEXT, found INTEGER, added INTEGER, ran_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS discovery_runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    region    TEXT,
    found     INTEGER,
    added     INTEGER,
    ran_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_sent ON applications(sent_at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def day_start() -> datetime:
    """Midnight of the current day in the machine's local time zone."""
    return datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Add columns introduced after a database was first created."""
        wanted = {
            "companies": {"linkedin_url": "TEXT"},
            "applications": {"linkedin_note": "TEXT", "linkedin_message": "TEXT", "linkedin_listed_at": "TEXT"},
        }
        for table, cols in wanted.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.commit()

    # ── meta (small key/value state) ─────────────────────────────────────────
    def get_meta(self, key: str) -> str | None:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str):
        self.db.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value))
        self.db.commit()

    # ── companies ────────────────────────────────────────────────────────────
    def known_domains(self, limit: int | None = None) -> list[str]:
        q = "SELECT domain FROM companies ORDER BY found_at DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [r["domain"] for r in self.db.execute(q)]

    def has_domain(self, domain: str) -> bool:
        return self.db.execute("SELECT 1 FROM companies WHERE domain=?", (domain,)).fetchone() is not None

    def add_company(self, c: dict) -> bool:
        if self.has_domain(c["domain"]):
            return False
        cols = [
            "domain", "name", "country", "city", "segment", "employees", "website",
            "contact_email", "contact_type", "contact_source", "internship_signal",
            "why_fit", "recent_facts", "score", "status", "reject_reason", "region", "linkedin_url",
        ]
        values = [c.get(k) for k in cols] + [iso(utcnow())]
        self.db.execute(
            f"INSERT INTO companies ({','.join(cols)}, found_at) VALUES ({','.join('?' * (len(cols) + 1))})",
            values,
        )
        self.db.commit()
        return True

    def qualified(self) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM companies WHERE status='qualified' ORDER BY score DESC, found_at ASC"
        ))

    def set_status(self, domain: str, status: str, reason: str | None = None):
        self.db.execute("UPDATE companies SET status=?, reject_reason=COALESCE(?, reject_reason) WHERE domain=?",
                        (status, reason, domain))
        self.db.commit()

    def last_application_for(self, domain: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM applications WHERE domain=? ORDER BY sent_at DESC LIMIT 1", (domain,)
        ).fetchone()

    # ── applications ─────────────────────────────────────────────────────────
    def record_application(self, domain, to_email, subject, body, language, mode,
                           gmail_id, thread_id, followup_days,
                           linkedin_note=None, linkedin_message=None) -> int:
        now = utcnow()
        cur = self.db.execute(
            """INSERT INTO applications (domain, to_email, subject, body, language, mode,
                                         gmail_id, thread_id, sent_at, followup_at,
                                         linkedin_note, linkedin_message)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (domain, to_email, subject, body, language, mode, gmail_id, thread_id,
             iso(now), iso(now + timedelta(days=followup_days)), linkedin_note, linkedin_message),
        )
        # dry-run previews are parked so the loop moves on; `reset-previews` re-queues them
        self.set_status(domain, "previewed" if mode == "dry_run" else "applied")
        self.db.commit()
        return cur.lastrowid

    def emails_out_since(self, since: datetime) -> int:
        """Real emails that left the mailbox (applications + follow-ups) — for the hard cap."""
        a = self.db.execute("SELECT COUNT(*) FROM applications WHERE mode='send' AND sent_at >= ?",
                            (iso(since),)).fetchone()[0]
        f = self.db.execute("SELECT COUNT(*) FROM applications WHERE followed_up_at >= ?",
                            (iso(since),)).fetchone()[0]
        return a + f

    def count_today(self, mode: str | None = None, since: datetime | None = None) -> int:
        since = since or day_start()
        q, args = "SELECT COUNT(*) FROM applications WHERE sent_at >= ?", [iso(since)]
        if mode:
            q += " AND mode = ?"
            args.append(mode)
        return self.db.execute(q, args).fetchone()[0]

    def reset_previews(self) -> int:
        cur = self.db.execute("UPDATE companies SET status='qualified' WHERE status='previewed'")
        self.db.commit()
        return cur.rowcount

    def due_followups(self) -> list[sqlite3.Row]:
        return list(self.db.execute(
            """SELECT a.*, c.name, c.country FROM applications a JOIN companies c USING(domain)
               WHERE a.mode='send' AND a.followed_up=0 AND a.replied=0
                 AND a.thread_id IS NOT NULL AND a.followup_at <= ?""",
            (iso(utcnow()),),
        ))

    def open_threads(self, days: int = 45) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM applications WHERE mode IN ('send','draft') AND replied=0 AND thread_id IS NOT NULL "
            "AND sent_at >= ?",
            (iso(utcnow() - timedelta(days=days)),),
        ))

    def mark_followed_up(self, app_id: int):
        self.db.execute("UPDATE applications SET followed_up=1, followed_up_at=? WHERE id=?",
                        (iso(utcnow()), app_id))
        self.db.commit()

    def mark_replied(self, app_id: int, domain: str, status: str = "replied"):
        # replied=1 also stops follow-ups; status is 'replied' or 'bounced'
        self.db.execute("UPDATE applications SET replied=1 WHERE id=?", (app_id,))
        self.set_status(domain, status)

    def open_threads_by_id(self) -> dict[str, sqlite3.Row]:
        return {r["thread_id"]: r for r in self.open_threads()}

    def company_name(self, domain: str) -> str:
        r = self.db.execute("SELECT name FROM companies WHERE domain=?", (domain,)).fetchone()
        return r["name"] if r else domain

    def contacted_domains(self) -> set[str]:
        return {r[0] for r in self.db.execute(
            "SELECT domain FROM companies WHERE status IN ('applied','replied','bounced')")}

    def has_reply(self, app_id: int) -> bool:
        return self.db.execute("SELECT replied FROM applications WHERE id=?", (app_id,)).fetchone()[0] == 1

    # ── replies ──────────────────────────────────────────────────────────────
    def reply_seen(self, gmail_msg_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM replies WHERE gmail_msg_id=?", (gmail_msg_id,)).fetchone() is not None

    def record_reply(self, gmail_msg_id, app_id, kind, summary, draft_id):
        self.db.execute("INSERT OR IGNORE INTO replies VALUES (?,?,?,?,?,?,0)",
                        (gmail_msg_id, app_id, kind, summary, draft_id, iso(utcnow())))
        self.db.commit()

    def undigested_replies(self) -> list[sqlite3.Row]:
        return list(self.db.execute(
            """SELECT r.*, c.name, a.to_email FROM replies r JOIN applications a ON a.id=r.app_id
               JOIN companies c ON c.domain=a.domain WHERE r.digested=0 ORDER BY r.received_at"""))

    # ── LinkedIn outreach ────────────────────────────────────────────────────
    def linkedin_listed_since(self, since: datetime) -> int:
        return self.db.execute("SELECT COUNT(*) FROM applications WHERE linkedin_listed_at >= ?",
                               (iso(since),)).fetchone()[0]

    def linkedin_candidates(self, min_score: int, mode: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            """SELECT a.id, a.linkedin_note, a.linkedin_message, a.sent_at, c.* FROM applications a
               JOIN companies c USING(domain)
               WHERE a.mode=? AND a.linkedin_listed_at IS NULL AND a.linkedin_note IS NOT NULL
                 AND c.score >= ? ORDER BY c.score DESC, a.sent_at""", (mode, min_score)))

    # ── job offers ───────────────────────────────────────────────────────────
    def has_offer(self, url: str, company: str, title: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM offers WHERE url=? OR (lower(company)=lower(?) AND lower(title)=lower(?))",
            (url, company, title)).fetchone() is not None

    def add_offer(self, o: dict) -> bool:
        if self.has_offer(o["url"], o["company"], o["title"]):
            return False
        cols = ["url", "title", "company", "company_domain", "location", "country", "platform", "posted",
                "language", "requirements", "why_fit", "score", "status", "reject_reason"]
        self.db.execute(f"INSERT INTO offers ({','.join(cols)}, found_at) VALUES ({','.join('?' * (len(cols) + 1))})",
                        [o.get(k) for k in cols] + [iso(utcnow())])
        self.db.commit()
        return True

    def offers_with_status(self, status: str) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM offers WHERE status=? ORDER BY score DESC, found_at", (status,)))

    def set_offer_kit(self, url: str, cover_letter: str, why_answer: str):
        self.db.execute("UPDATE offers SET status='ready', cover_letter=?, why_answer=? WHERE url=?",
                        (cover_letter, why_answer, url))
        self.db.commit()

    def set_offer_status(self, url: str, status: str, reason: str | None = None):
        self.db.execute("UPDATE offers SET status=?, reject_reason=COALESCE(?, reject_reason), "
                        "done_at=CASE WHEN ? IN ('applied','expired') THEN ? ELSE done_at END WHERE url=?",
                        (status, reason, status, iso(utcnow()), url))
        self.db.commit()

    def offers_ready_since(self, since: datetime) -> int:
        return self.db.execute("SELECT COUNT(*) FROM offers WHERE status IN ('ready','applied') AND found_at >= ?",
                               (iso(since),)).fetchone()[0]

    def offer_by_ref(self, ref: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM offers WHERE rowid=? OR url=?", (ref, ref)).fetchone()

    def ready_offers(self) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT rowid AS ref, * FROM offers WHERE status='ready' ORDER BY score DESC, found_at"))

    def expire_offers(self, older_than_days: int) -> int:
        cur = self.db.execute("UPDATE offers SET status='expired', done_at=? WHERE status='ready' AND found_at < ?",
                              (iso(utcnow()), iso(utcnow() - timedelta(days=older_than_days))))
        self.db.commit()
        return cur.rowcount

    def known_offer_urls(self, limit: int = 300) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT url FROM offers ORDER BY found_at DESC LIMIT ?", (limit,))]

    def record_offer_run(self, region: str, found: int, added: int):
        self.db.execute("INSERT INTO offer_runs (region, found, added, ran_at) VALUES (?,?,?,?)",
                        (region, found, added, iso(utcnow())))
        self.db.commit()

    def offer_runs_today(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM offer_runs WHERE ran_at >= ?", (iso(day_start()),)).fetchone()[0]

    def total_offer_runs(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM offer_runs").fetchone()[0]

    # ── digest bookkeeping ───────────────────────────────────────────────────
    def mark_digested(self, reply_ids: list[str], app_ids: list[int], offer_urls: list[str]):
        now = iso(utcnow())
        self.db.executemany("UPDATE replies SET digested=1 WHERE gmail_msg_id=?", [(r,) for r in reply_ids])
        self.db.executemany("UPDATE applications SET linkedin_listed_at=? WHERE id=?", [(now, a) for a in app_ids])
        self.db.executemany("UPDATE offers SET listed_at=COALESCE(listed_at, ?) WHERE url=?",
                            [(now, u) for u in offer_urls])
        self.db.commit()

    def applications_since(self, since: datetime, mode: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            """SELECT a.*, c.name, c.country, c.segment, c.score FROM applications a JOIN companies c USING(domain)
               WHERE a.mode=? AND a.sent_at >= ? ORDER BY a.sent_at""", (mode, iso(since))))

    # ── discovery ────────────────────────────────────────────────────────────
    def record_discovery(self, region: str, found: int, added: int):
        self.db.execute("INSERT INTO discovery_runs (region, found, added, ran_at) VALUES (?,?,?,?)",
                        (region, found, added, iso(utcnow())))
        self.db.commit()

    def discovery_runs_today(self) -> int:
        since = day_start()
        return self.db.execute("SELECT COUNT(*) FROM discovery_runs WHERE ran_at >= ?", (iso(since),)).fetchone()[0]

    def total_discovery_runs(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM discovery_runs").fetchone()[0]

    # ── reporting ────────────────────────────────────────────────────────────
    def stats(self) -> dict:
        def one(q, *a):
            return self.db.execute(q, a).fetchone()[0]
        return {
            "companies_found": one("SELECT COUNT(*) FROM companies"),
            "qualified_waiting": one("SELECT COUNT(*) FROM companies WHERE status='qualified'"),
            "rejected": one("SELECT COUNT(*) FROM companies WHERE status='rejected'"),
            "previewed": one("SELECT COUNT(*) FROM companies WHERE status='previewed'"),
            "applied": one("SELECT COUNT(*) FROM companies WHERE status='applied'"),
            "replied": one("SELECT COUNT(*) FROM companies WHERE status='replied'"),
            "bounced": one("SELECT COUNT(*) FROM companies WHERE status='bounced'"),
            "sent_today": self.count_today("send"),
            "drafted_today": self.count_today("draft"),
            "dry_run_today": self.count_today("dry_run"),
            "followups_sent": one("SELECT COUNT(*) FROM applications WHERE followed_up=1"),
            "offers_ready": one("SELECT COUNT(*) FROM offers WHERE status='ready'"),
            "offers_applied": one("SELECT COUNT(*) FROM offers WHERE status='applied'"),
            "replies_human": one("SELECT COUNT(*) FROM replies WHERE kind NOT IN ('auto_ack','bounce')"),
        }
