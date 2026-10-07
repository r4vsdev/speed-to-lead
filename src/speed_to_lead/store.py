"""Lead log in SQLite: dedupes resent webhooks and records how long each lead took."""

import sqlite3
import statistics
import threading
import time
from pathlib import Path

SCHEMA = """
create table if not exists leads (
  key text primary key,
  email text not null,
  crm text not null,
  status text not null,          -- processing | done | failed
  received_at real not null,     -- unix seconds
  finished_at real,
  ms integer,                    -- form received -> CRM fully written
  contact_id text, company_id text, deal_id text,
  contact_created integer,
  error text
);
create table if not exists companies (
  crm text not null,
  domain text not null,
  company_id text not null,
  primary key (crm, domain)
);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def claim(self, key: str, email: str, crm: str, received_at: float | None = None) -> bool:
        """Record a new submission. False if this key was already seen (resent webhook): skip it.

        The insert is atomic, so two copies of the same webhook arriving together can't both win.
        """
        with self.lock, self.db:
            cur = self.db.execute(
                "insert or ignore into leads (key, email, crm, status, received_at) values (?, ?, ?, 'processing', ?)",
                (key, email, crm, received_at or time.time()))
            return cur.rowcount == 1

    def finish(self, key: str, result) -> int:
        with self.lock, self.db:
            row = self.db.execute("select received_at from leads where key = ?", (key,)).fetchone()
            now = time.time()
            ms = int((now - row["received_at"]) * 1000)
            self.db.execute(
                "update leads set status='done', finished_at=?, ms=?, contact_id=?, company_id=?, deal_id=?, "
                "contact_created=? where key=?",
                (now, ms, result.contact_id, result.company_id, result.deal_id, int(result.contact_created), key))
            return ms

    def fail(self, key: str, error: str) -> None:
        with self.lock, self.db:
            self.db.execute("update leads set status='failed', finished_at=?, error=? where key=?",
                            (time.time(), error[:2000], key))

    def company_id(self, crm: str, domain: str) -> str | None:
        row = self.db.execute("select company_id from companies where crm=? and domain=?", (crm, domain)).fetchone()
        return row["company_id"] if row else None

    def remember_company(self, crm: str, domain: str, company_id: str) -> None:
        with self.lock, self.db:
            self.db.execute("insert or replace into companies values (?, ?, ?)", (crm, domain, company_id))

    def get(self, key: str) -> dict | None:
        row = self.db.execute("select * from leads where key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict:
        rows = self.db.execute("select status, ms from leads").fetchall()
        ms = sorted(r["ms"] for r in rows if r["status"] == "done")
        by_status = {s: sum(1 for r in rows if r["status"] == s) for s in ("done", "failed", "processing")}
        return {
            "leads": len(rows),
            **by_status,
            "median_ms": int(statistics.median(ms)) if ms else None,
            "p95_ms": ms[min(len(ms) - 1, int(len(ms) * 0.95))] if ms else None,
        }
