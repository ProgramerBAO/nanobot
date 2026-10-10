"""Bounded SQLite inbox/outbox; durable receipt is distinct from Feishu delivery.

One receiver process owns this store. Sending/unknown rows are never retried
automatically after a crash; human reconciliation avoids duplicate side effects.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from filelock import FileLock


class InboxFullError(Exception):
    """Capacity exceeded: reject receipt so Trail keeps responsibility for retry."""


class InboxStore:
    """Own one connection; serialize short transactions across worker threads."""

    def __init__(self, path: Path, max_rows: int = 10000):
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite operations/cleanup run in worker threads; ownership must be
        # process-wide, otherwise releasing from another thread leaks the lock.
        self._process_lock = FileLock(str(path) + ".lock", thread_local=False)
        self._process_lock.acquire(timeout=0)
        self._db = sqlite3.connect(path, timeout=2, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._closed = False
        self._max_rows = max_rows
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS inbox (
                event_id TEXT NOT NULL, recipient TEXT NOT NULL, envelope TEXT NOT NULL,
                received_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'received',
                attempts INTEGER NOT NULL DEFAULT 0, next_retry_at REAL NOT NULL DEFAULT 0,
                message_id TEXT, reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(event_id, recipient)
            );
            CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(status,next_retry_at);
        """)
        with self._db:
            self._db.execute("UPDATE inbox SET status='unknown',reason='process_restarted_during_send' WHERE status='sending'")
            self._db.execute("UPDATE inbox SET status='received' WHERE status='checking'")

    def close(self) -> None:
        """Close only after HTTP acceptance and the worker have stopped."""
        with self._lock:
            if self._closed:
                return
            try:
                self._db.close()
            finally:
                self._process_lock.release()
                self._closed = True

    def receive(self, envelope: dict[str, Any], recipient: str, now: float) -> bool:
        """Commit minimal event before ACK; duplicates do not consume capacity."""
        with self._lock, self._db:
            existing = self._db.execute("SELECT 1 FROM inbox WHERE event_id=? AND recipient=?", (envelope["event_id"], recipient)).fetchone()
            if existing:
                return False
            # Seven-day terminal retention exceeds the accepted 24h event age.
            # Keep unresolved/unknown rows for manual reconciliation.
            self._db.execute("DELETE FROM inbox WHERE received_at<? AND status IN ('sent','cancelled','failed')", (now - 7 * 86400,))
            if self._db.execute("SELECT count(*) FROM inbox").fetchone()[0] >= self._max_rows:
                raise InboxFullError()
            self._db.execute("INSERT INTO inbox(event_id,recipient,envelope,received_at) VALUES(?,?,?,?)", (envelope["event_id"], recipient, json.dumps(envelope), now))
        return True

    def claim(self, now: float) -> dict[str, Any] | None:
        """Atomically claim one ready job; checks can retry, sends cannot replay."""
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM inbox WHERE status='received' AND next_retry_at<=? ORDER BY received_at,event_id LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            self._db.execute("UPDATE inbox SET status='checking',attempts=attempts+1 WHERE event_id=? AND recipient=?", (row["event_id"], row["recipient"]))
            out = dict(row)
            out["attempts"] += 1
            out["envelope"] = json.loads(out["envelope"])
            return out

    def finish(self, job: dict[str, Any], status: str, reason: str = "", *, message_id: str | None = None, next_retry_at: float = 0) -> None:
        """Persist a controlled state transition; never store raw exception text."""
        if status not in {"received", "sending", "sent", "cancelled", "failed", "unknown"}:
            raise ValueError("invalid Trail inbox status")
        with self._lock, self._db:
            self._db.execute("UPDATE inbox SET status=?,reason=?,message_id=?,next_retry_at=? WHERE event_id=? AND recipient=?", (status, reason, message_id, next_retry_at, job["event_id"], job["recipient"]))

    def snapshot(self, now: float) -> dict[str, Any]:
        """Low-cardinality queue health without event bodies or recipient IDs."""
        with self._lock:
            counts = {row[0]: row[1] for row in self._db.execute("SELECT status,count(*) FROM inbox GROUP BY status")}
            oldest = self._db.execute("SELECT min(received_at) FROM inbox WHERE status IN ('received','checking')").fetchone()[0]
        return {"counts": counts, "oldest_pending_seconds": max(0, now - oldest) if oldest is not None else 0}
