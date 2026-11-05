"""Local SQLite database: one row per lead handled, plus an append-only event log."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .timeutil import iso, utcnow

# queued -> (sent | scheduled | skipped | error); scheduled -> (sent | skipped | error); sent -> error
FINAL_STATUSES = ("queued", "scheduled", "sent", "error")
# Leads the service leaves alone: the above, plus "baseline" (already in the view before go-live).
SETTLED_STATUSES = FINAL_STATUSES + ("baseline",)

LEAD_COLUMNS = {
    "lead_id": "TEXT PRIMARY KEY",
    "lead_name": "TEXT",
    "contact_id": "TEXT",
    "phone": "TEXT",
    "timezone": "TEXT",
    "status": "TEXT NOT NULL",  # queued | scheduled | sent | skipped | error | baseline
    "message_version": "INTEGER",
    "message_text": "TEXT",
    "skip_rule": "TEXT",
    "skip_reason": "TEXT",
    "sms_activity_id": "TEXT",
    "error_message": "TEXT",
    "source": "TEXT",  # webhook | sweep | dry-run
    "attempts": "INTEGER DEFAULT 0",
    "scheduled_for": "TEXT",
    "morning_checked_at": "TEXT",
    "verify_after": "TEXT",
    "verified_at": "TEXT",
    "verify_attempts": "INTEGER DEFAULT 0",
    "note_created": "INTEGER DEFAULT 0",
    "duplicate_task_created": "INTEGER DEFAULT 0",
    "error_task_created": "INTEGER DEFAULT 0",
    "first_seen_at": "TEXT NOT NULL",
    "updated_at": "TEXT NOT NULL",
    "last_checked_at": "TEXT",
    "queued_at": "TEXT",
    "sent_at": "TEXT",
    "error_at": "TEXT",
}

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS leads ("
    + ", ".join(f"{name} {kind}" for name, kind in LEAD_COLUMNS.items())
    + ");\n"
    """
    CREATE INDEX IF NOT EXISTS leads_status ON leads(status);
    CREATE INDEX IF NOT EXISTS leads_sms ON leads(sms_activity_id);
    CREATE INDEX IF NOT EXISTS leads_phone ON leads(phone);
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        lead_id TEXT,
        event TEXT NOT NULL,
        rule TEXT,
        detail TEXT
    );
    CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
    CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
    """
)

SEND_EVENTS = ("sent", "send_failed")


class Db:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._migrate()

    @classmethod
    def snapshot(cls, path: str | Path) -> "Db":
        """In-memory copy of an existing database (for the one-shot dry run)."""
        db = cls(":memory:")
        if Path(path).exists():
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            src.backup(db.conn)
            src.close()
            db._migrate()
        return db

    def _migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(leads)")}
        for name, kind in LEAD_COLUMNS.items():
            if name not in existing:
                kind = kind.replace(" NOT NULL", "")
                self.conn.execute(f"ALTER TABLE leads ADD COLUMN {name} {kind}")

    # ---- leads -------------------------------------------------------------
    def get(self, lead_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None

    def upsert(self, lead_id: str, **fields) -> None:
        unknown = set(fields) - set(LEAD_COLUMNS)
        if unknown:
            raise ValueError(f"unknown columns: {sorted(unknown)}")
        now = iso(utcnow())
        fields["updated_at"] = now
        with self._lock:
            exists = self.conn.execute("SELECT 1 FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if exists:
                assignments = ", ".join(f"{k} = ?" for k in fields)
                self.conn.execute(f"UPDATE leads SET {assignments} WHERE lead_id = ?", (*fields.values(), lead_id))
            else:
                fields.setdefault("status", "skipped")
                fields.setdefault("first_seen_at", now)
                cols = ["lead_id", *fields]
                marks = ", ".join("?" for _ in cols)
                self.conn.execute(
                    f"INSERT INTO leads ({', '.join(cols)}) VALUES ({marks})", (lead_id, *fields.values())
                )

    def by_sms_id(self, sms_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM leads WHERE sms_activity_id = ?", (sms_id,)).fetchone()
        return dict(row) if row else None

    def next_queued(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM leads WHERE status = 'queued' ORDER BY queued_at, first_seen_at LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def other_lead_with_phone(self, phone: str, exclude_lead_id: str) -> dict | None:
        marks = ", ".join("?" for _ in FINAL_STATUSES)
        row = self.conn.execute(
            f"SELECT * FROM leads WHERE phone = ? AND lead_id != ? AND status IN ({marks}) LIMIT 1",
            (phone, exclude_lead_id, *FINAL_STATUSES),
        ).fetchone()
        return dict(row) if row else None

    def rows(self, status: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM leads WHERE status = ?", (status,))]

    def scheduled_times(self, start: str, end: str) -> list[str]:
        cur = self.conn.execute(
            "SELECT scheduled_for FROM leads WHERE status IN ('scheduled', 'sent') "
            "AND scheduled_for >= ? AND scheduled_for <= ?",
            (start, end),
        )
        return [r[0] for r in cur]

    def due_for_verification(self, now: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM leads WHERE status IN ('sent', 'scheduled') AND sms_activity_id IS NOT NULL "
            "AND verified_at IS NULL AND verify_after IS NOT NULL AND verify_after <= ? ORDER BY verify_after",
            (now,),
        )
        return [dict(r) for r in cur]

    def due_for_morning_check(self, cutoff: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM leads WHERE status = 'scheduled' AND morning_checked_at IS NULL "
            "AND scheduled_for <= ? ORDER BY scheduled_for",
            (cutoff,),
        )
        return [dict(r) for r in cur]

    def errors_since(self, since: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM leads WHERE status = 'error' AND error_at >= ?", (since,)
        ).fetchone()[0]

    def texted_since(self, since: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM leads WHERE status = 'sent' AND sent_at >= ? ORDER BY sent_at", (since,)
        )
        return [dict(r) for r in cur]

    def status_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT status, COUNT(*) FROM leads GROUP BY status")}

    # ---- events ------------------------------------------------------------
    def log(self, event: str, *, lead_id=None, rule=None, detail=None, ts: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, lead_id, event, rule, detail) VALUES (?, ?, ?, ?, ?)",
            (ts or iso(utcnow()), lead_id, event, rule, detail),
        )

    def send_event_times_since(self, since: str) -> list[str]:
        marks = ", ".join("?" for _ in SEND_EVENTS)
        cur = self.conn.execute(
            f"SELECT ts FROM events WHERE event IN ({marks}) AND ts >= ? ORDER BY ts", (*SEND_EVENTS, since)
        )
        return [r[0] for r in cur]

    def count_events_since(self, event: str, since: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event = ? AND ts >= ?", (event, since)
        ).fetchone()[0]

    def skip_counts_since(self, since: str) -> dict[str, int]:
        cur = self.conn.execute(
            "SELECT rule, COUNT(DISTINCT lead_id) FROM events WHERE event IN ('skipped', 'unscheduled') "
            "AND ts >= ? GROUP BY rule",
            (since,),
        )
        return {r[0]: r[1] for r in cur}

    def last_event(self, event: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM events WHERE event = ? ORDER BY id DESC LIMIT 1", (event,)
        ).fetchone()
        return dict(row) if row else None

    # ---- key/value ---------------------------------------------------------
    def kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
