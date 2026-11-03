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
    replied       INTEGER DEFAULT 0
);
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
            "why_fit", "recent_facts", "score", "status", "reject_reason", "region",
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
                           gmail_id, thread_id, followup_days) -> int:
        now = utcnow()
        cur = self.db.execute(
            """INSERT INTO applications (domain, to_email, subject, body, language, mode,
                                         gmail_id, thread_id, sent_at, followup_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (domain, to_email, subject, body, language, mode, gmail_id, thread_id,
             iso(now), iso(now + timedelta(days=followup_days))),
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
            "SELECT * FROM applications WHERE mode='send' AND replied=0 AND thread_id IS NOT NULL AND sent_at >= ?",
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
        }
