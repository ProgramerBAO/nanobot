"""SQLite state for onboarding, RBAC, report runs, and subscriptions."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ReportSubscription:
    subscription_id: str
    channel: str
    chat_id: str
    user_id: str
    connector_id: str
    template_id: str
    template_version: str
    schedule: str
    timezone: str
    report_params: dict[str, Any]
    cron_job_id: str
    enabled: bool
    created_at: str
    updated_at: str


class ReportStateStore:
    """Small local control-plane store with explicit production migration seams."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from nanobot.config.paths import get_runtime_subdir

            path = get_runtime_subdir("reports") / "state.db"
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS report_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS report_onboarding (
                    channel TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    seen_at TEXT NOT NULL,
                    PRIMARY KEY (channel, user_id)
                );
                CREATE TABLE IF NOT EXISTS report_grants (
                    channel TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (channel, user_id, resource_type, resource_id)
                );
                CREATE TABLE IF NOT EXISTS report_runs (
                    run_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    connector_id TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    template_version TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    quality TEXT NOT NULL,
                    error_type TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS report_runs_user_created
                    ON report_runs(channel, user_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS report_subscriptions (
                    subscription_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    connector_id TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    template_version TEXT NOT NULL,
                    schedule TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    report_params_json TEXT NOT NULL,
                    cron_job_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(channel, user_id, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS report_subscriptions_user
                    ON report_subscriptions(channel, user_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS report_deliveries (
                    idempotency_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS report_delivery_attempts (
                    idempotency_key TEXT NOT NULL,
                    part_index INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (idempotency_key, part_index, attempt)
                );
                CREATE TABLE IF NOT EXISTS report_template_policies (
                    template_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    subscription_mode TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS report_admin_audit (
                    audit_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    before_summary TEXT NOT NULL,
                    after_summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS report_admin_audit_created
                    ON report_admin_audit(created_at DESC);
                """
            )

    def setting(self, key: str, default: str = "") -> str:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT value FROM report_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        now = _utc_now()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO report_settings(key, value, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, now),
            )

    def onboarding_seen(self, channel: str, user_id: str, version: int) -> bool:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT version FROM report_onboarding WHERE channel=? AND user_id=?",
                (channel, user_id),
            ).fetchone()
        return bool(row and int(row["version"]) >= version)

    def mark_onboarding_seen(self, channel: str, user_id: str, version: int) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO report_onboarding(channel, user_id, version, seen_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(channel, user_id) DO UPDATE SET
                    version=excluded.version, seen_at=excluded.seen_at""",
                (channel, user_id, version, _utc_now()),
            )

    def rbac_enabled(self) -> bool:
        return self.setting("rbac_enabled", "false").casefold() == "true"

    def set_rbac_enabled(self, enabled: bool) -> None:
        self.set_setting("rbac_enabled", "true" if enabled else "false")

    def template_policies(self) -> list[dict[str, Any]]:
        """Return persisted template policies without exposing report parameters."""

        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM report_template_policies ORDER BY template_id"
            ).fetchall()
        return [
            {
                "template_id": str(row["template_id"]),
                "enabled": bool(row["enabled"]),
                "subscription_mode": str(row["subscription_mode"]),
                "revision": int(row["revision"]),
                "updated_at": str(row["updated_at"]),
                "updated_by": str(row["updated_by"]),
            }
            for row in rows
        ]

    def template_policy(self, template_id: str) -> dict[str, Any] | None:
        """Return one template policy for runtime enforcement, if configured."""

        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM report_template_policies WHERE template_id=?",
                (template_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "template_id": str(row["template_id"]),
            "enabled": bool(row["enabled"]),
            "subscription_mode": str(row["subscription_mode"]),
            "revision": int(row["revision"]),
            "updated_at": str(row["updated_at"]),
            "updated_by": str(row["updated_by"]),
        }

    def set_template_policy(
        self,
        template_id: str,
        *,
        enabled: bool,
        subscription_mode: str,
        updated_by: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Persist an exposure policy with optimistic concurrency and safe audit."""

        if not template_id or len(template_id) > 128:
            raise ValueError("invalid report template id")
        if subscription_mode not in {"all_authorized", "allowlist", "disabled"}:
            raise ValueError("unsupported report subscription mode")
        now = _utc_now()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM report_template_policies WHERE template_id=?",
                (template_id,),
            ).fetchone()
            revision = int(row["revision"]) if row else 0
            if expected_revision is not None and revision != expected_revision:
                raise ValueError("report template policy was updated by another operator")
            before = (
                {
                    "enabled": bool(row["enabled"]),
                    "subscription_mode": str(row["subscription_mode"]),
                    "revision": revision,
                }
                if row
                else {}
            )
            after = {
                "enabled": bool(enabled),
                "subscription_mode": subscription_mode,
                "revision": revision + 1,
            }
            db.execute(
                """INSERT INTO report_template_policies(
                    template_id, enabled, subscription_mode, revision, updated_at, updated_by
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(template_id) DO UPDATE SET enabled=excluded.enabled,
                    subscription_mode=excluded.subscription_mode, revision=excluded.revision,
                    updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
                (template_id, int(enabled), subscription_mode, revision + 1, now, updated_by),
            )
            db.execute(
                """INSERT INTO report_admin_audit(
                    audit_id, action, target_type, target_id, before_summary,
                    after_summary, created_at, updated_by
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    uuid.uuid4().hex,
                    "template_policy_update",
                    "template",
                    template_id,
                    json.dumps(before, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(after, ensure_ascii=False, separators=(",", ":")),
                    now,
                    updated_by[:256] or "webui_admin",
                ),
            )
        return {"template_id": template_id, **after, "updated_at": now}

    def all_subscriptions(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[ReportSubscription]:
        """Return a bounded subscription page for the authenticated admin surface."""

        limit = max(1, min(int(limit), 500))
        offset = max(0, min(int(offset), 100_000))
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM report_subscriptions ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._subscription_from_row(row) for row in rows]

    def update_subscription_schedule(
        self,
        subscription_id: str,
        *,
        schedule: str,
        timezone_name: str,
    ) -> ReportSubscription | None:
        """Update only scheduling metadata after the corresponding Cron job succeeds."""

        if not schedule.strip() or len(schedule) > 128:
            raise ValueError("invalid report subscription schedule")
        if not timezone_name.strip() or len(timezone_name) > 64:
            raise ValueError("invalid report subscription timezone")
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """UPDATE report_subscriptions SET schedule=?, timezone=?, updated_at=?
                WHERE subscription_id=?""",
                (schedule, timezone_name, _utc_now(), subscription_id),
            )
        return self.subscription(subscription_id) if cursor.rowcount else None

    def record_admin_audit(
        self,
        *,
        action: str,
        target_type: str,
        target_id: str,
        before_summary: dict[str, Any],
        after_summary: dict[str, Any],
        updated_by: str,
    ) -> None:
        """Record bounded control-plane metadata without report scope or credentials."""

        if not action or len(action) > 64 or not target_id or len(target_id) > 256:
            raise ValueError("invalid report admin audit target")
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO report_admin_audit(
                    audit_id, action, target_type, target_id, before_summary,
                    after_summary, created_at, updated_by
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    uuid.uuid4().hex,
                    action,
                    target_type[:64],
                    target_id,
                    json.dumps(before_summary, ensure_ascii=False, separators=(",", ":"))[:4096],
                    json.dumps(after_summary, ensure_ascii=False, separators=(",", ":"))[:4096],
                    _utc_now(),
                    updated_by[:256] or "webui_admin",
                ),
            )

    def grant(self, channel: str, user_id: str, resource_type: str, resource_id: str) -> None:
        if resource_type not in {
            "connector", "template", "tenant", "project", "model", "endpoint",
            "provider", "environment", "capability", "subscription_template",
        }:
            raise ValueError("unsupported report grant resource type")
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO report_grants(
                    channel, user_id, resource_type, resource_id, created_at
                ) VALUES(?, ?, ?, ?, ?)""",
                (channel, user_id, resource_type, resource_id, _utc_now()),
            )

    def revoke(self, channel: str, user_id: str, resource_type: str, resource_id: str) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """DELETE FROM report_grants
                WHERE channel=? AND user_id=? AND resource_type=? AND resource_id=?""",
                (channel, user_id, resource_type, resource_id),
            )
        return cursor.rowcount > 0

    def grants(self, channel: str, user_id: str) -> list[dict[str, str]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                """SELECT resource_type, resource_id, created_at FROM report_grants
                WHERE channel=? AND user_id=? ORDER BY resource_type, resource_id""",
                (channel, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def allowed(self, channel: str, user_id: str, resource_type: str, resource_id: str) -> bool:
        if not self.rbac_enabled():
            return True
        with self._lock, self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM report_grants WHERE channel=? AND user_id=?
                AND resource_type=? AND resource_id IN (?, '*') LIMIT 1""",
                (channel, user_id, resource_type, resource_id),
            ).fetchone()
        return row is not None

    def record_run(
        self,
        *,
        run_id: str,
        channel: str,
        chat_id: str,
        user_id: str,
        connector_id: str,
        template_id: str,
        template_version: str,
        request: dict[str, Any],
        status: str,
        duration_ms: int,
        quality: str = "complete",
        error_type: str = "",
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO report_runs VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    channel,
                    chat_id,
                    user_id,
                    connector_id,
                    template_id,
                    template_version,
                    json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                    status,
                    max(0, int(duration_ms)),
                    quality,
                    error_type,
                    _utc_now(),
                ),
            )

    def recent_runs(self, channel: str, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        with self._lock, self._connect() as db:
            rows = db.execute(
                """SELECT * FROM report_runs WHERE channel=? AND user_id=?
                ORDER BY created_at DESC LIMIT ?""",
                (channel, user_id, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["request"] = json.loads(item.pop("request_json"))
            result.append(item)
        return result

    def add_subscription(self, subscription: ReportSubscription, fingerprint: str) -> bool:
        values = asdict(subscription)
        with self._lock, self._connect() as db:
            try:
                db.execute(
                    """INSERT INTO report_subscriptions VALUES(
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )""",
                    (
                        values["subscription_id"], values["channel"], values["chat_id"],
                        values["user_id"], values["connector_id"], values["template_id"],
                        values["template_version"], values["schedule"], values["timezone"],
                        json.dumps(values["report_params"], ensure_ascii=False, separators=(",", ":")),
                        values["cron_job_id"], int(values["enabled"]), fingerprint,
                        values["created_at"], values["updated_at"],
                    ),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def subscriptions(self, channel: str, user_id: str) -> list[ReportSubscription]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                """SELECT * FROM report_subscriptions WHERE channel=? AND user_id=?
                ORDER BY updated_at DESC""",
                (channel, user_id),
            ).fetchall()
        return [self._subscription_from_row(row) for row in rows]

    def subscription(self, subscription_id: str) -> ReportSubscription | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM report_subscriptions WHERE subscription_id=?",
                (subscription_id,),
            ).fetchone()
        return self._subscription_from_row(row) if row else None

    def set_subscription_enabled(
        self, subscription_id: str, *, channel: str, user_id: str, enabled: bool
    ) -> ReportSubscription | None:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """UPDATE report_subscriptions SET enabled=?, updated_at=?
                WHERE subscription_id=? AND channel=? AND user_id=?""",
                (int(enabled), _utc_now(), subscription_id, channel, user_id),
            )
        return self.subscription(subscription_id) if cursor.rowcount else None

    def remove_subscription(self, subscription_id: str, *, channel: str, user_id: str) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """DELETE FROM report_subscriptions
                WHERE subscription_id=? AND channel=? AND user_id=?""",
                (subscription_id, channel, user_id),
            )
        return cursor.rowcount > 0

    def prune_runs(self, retention_days: int = 30) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=max(1, retention_days))).isoformat()
        with self._lock, self._connect() as db:
            cursor = db.execute("DELETE FROM report_runs WHERE created_at < ?", (cutoff,))
        return cursor.rowcount

    def claim_delivery(self, idempotency_key: str) -> bool:
        if not idempotency_key or len(idempotency_key) > 512:
            raise ValueError("invalid report delivery idempotency key")
        now = _utc_now()
        stale_before = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        with self._lock, self._connect() as db:
            inserted = db.execute(
                """INSERT OR IGNORE INTO report_deliveries(
                    idempotency_key, status, claimed_at, completed_at
                ) VALUES(?, 'running', ?, '')""",
                (idempotency_key, now),
            )
            if inserted.rowcount:
                return True
            reclaimed = db.execute(
                """UPDATE report_deliveries SET status='running', claimed_at=?, completed_at=''
                WHERE idempotency_key=? AND (status='error' OR (status='running' AND claimed_at < ?))""",
                (now, idempotency_key, stale_before),
            )
        return reclaimed.rowcount > 0

    def complete_delivery(self, idempotency_key: str, *, status: str) -> None:
        if status not in {"ok", "error"}:
            raise ValueError("report delivery status must be ok or error")
        with self._lock, self._connect() as db:
            db.execute(
                """UPDATE report_deliveries SET status=?, completed_at=?
                WHERE idempotency_key=?""",
                (status, _utc_now(), idempotency_key),
            )

    def record_delivery_attempt(
        self,
        idempotency_key: str,
        *,
        part_index: int,
        attempt: int,
        status: str,
        error_type: str = "",
    ) -> None:
        if not idempotency_key or part_index < 1 or attempt < 1:
            raise ValueError("invalid report delivery attempt")
        if status not in {"ok", "error"}:
            raise ValueError("report delivery attempt status must be ok or error")
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO report_delivery_attempts(
                    idempotency_key, part_index, attempt, status, error_type, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key, part_index, attempt) DO UPDATE SET
                    status=excluded.status, error_type=excluded.error_type,
                    created_at=excluded.created_at""",
                (idempotency_key, part_index, attempt, status, error_type[:128], _utc_now()),
            )

    @staticmethod
    def _subscription_from_row(row: sqlite3.Row) -> ReportSubscription:
        return ReportSubscription(
            subscription_id=str(row["subscription_id"]),
            channel=str(row["channel"]),
            chat_id=str(row["chat_id"]),
            user_id=str(row["user_id"]),
            connector_id=str(row["connector_id"]),
            template_id=str(row["template_id"]),
            template_version=str(row["template_version"]),
            schedule=str(row["schedule"]),
            timezone=str(row["timezone"]),
            report_params=json.loads(str(row["report_params_json"])),
            cron_job_id=str(row["cron_job_id"]),
            enabled=bool(row["enabled"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


class _PostgresConnection:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def __enter__(self) -> _PostgresConnection:
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        return self._connection.__exit__(exc_type, exc, traceback)

    @staticmethod
    def _query(sql: str) -> str:
        query = sql.replace("?", "%s")
        normalized = " ".join(query.split()).upper()
        if normalized.startswith("INSERT OR IGNORE"):
            query = query.replace("INSERT OR IGNORE", "INSERT", 1) + " ON CONFLICT DO NOTHING"
        elif normalized.startswith("INSERT OR REPLACE INTO REPORT_RUNS"):
            query = query.replace("INSERT OR REPLACE", "INSERT", 1)
            query += """ ON CONFLICT (run_id) DO UPDATE SET
                channel=EXCLUDED.channel, chat_id=EXCLUDED.chat_id,
                user_id=EXCLUDED.user_id, connector_id=EXCLUDED.connector_id,
                template_id=EXCLUDED.template_id, template_version=EXCLUDED.template_version,
                request_json=EXCLUDED.request_json, status=EXCLUDED.status,
                duration_ms=EXCLUDED.duration_ms, quality=EXCLUDED.quality,
                error_type=EXCLUDED.error_type, created_at=EXCLUDED.created_at"""
        return query

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        return self._connection.execute(self._query(sql), params)


class PostgresReportStateStore(ReportStateStore):
    """PostgreSQL state backend for multi-instance company deployments."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("reporting PostgreSQL DSN is empty")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL reporting requires the 'reporting-postgres' optional dependency"
            ) from exc
        self.path = Path("postgresql")
        self._dsn = dsn
        self._psycopg = psycopg
        self._row_factory = dict_row
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> _PostgresConnection:
        connection = self._psycopg.connect(self._dsn, row_factory=self._row_factory)
        return _PostgresConnection(connection)

    def _initialize(self) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS report_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS report_onboarding (
                channel TEXT NOT NULL, user_id TEXT NOT NULL, version INTEGER NOT NULL,
                seen_at TEXT NOT NULL, PRIMARY KEY (channel, user_id))""",
            """CREATE TABLE IF NOT EXISTS report_grants (
                channel TEXT NOT NULL, user_id TEXT NOT NULL, resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (channel, user_id, resource_type, resource_id))""",
            """CREATE TABLE IF NOT EXISTS report_runs (
                run_id TEXT PRIMARY KEY, channel TEXT NOT NULL, chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL, connector_id TEXT NOT NULL, template_id TEXT NOT NULL,
                template_version TEXT NOT NULL, request_json TEXT NOT NULL, status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL, quality TEXT NOT NULL, error_type TEXT NOT NULL,
                created_at TEXT NOT NULL)""",
            """CREATE INDEX IF NOT EXISTS report_runs_user_created
                ON report_runs(channel, user_id, created_at DESC)""",
            """CREATE TABLE IF NOT EXISTS report_subscriptions (
                subscription_id TEXT PRIMARY KEY, channel TEXT NOT NULL, chat_id TEXT NOT NULL,
                user_id TEXT NOT NULL, connector_id TEXT NOT NULL, template_id TEXT NOT NULL,
                template_version TEXT NOT NULL, schedule TEXT NOT NULL, timezone TEXT NOT NULL,
                report_params_json TEXT NOT NULL, cron_job_id TEXT NOT NULL, enabled INTEGER NOT NULL,
                fingerprint TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(channel, user_id, fingerprint))""",
            """CREATE INDEX IF NOT EXISTS report_subscriptions_user
                ON report_subscriptions(channel, user_id, updated_at DESC)""",
            """CREATE TABLE IF NOT EXISTS report_deliveries (
                idempotency_key TEXT PRIMARY KEY, status TEXT NOT NULL,
                claimed_at TEXT NOT NULL, completed_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS report_delivery_attempts (
                idempotency_key TEXT NOT NULL, part_index INTEGER NOT NULL,
                attempt INTEGER NOT NULL, status TEXT NOT NULL, error_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (idempotency_key, part_index, attempt))""",
            """CREATE TABLE IF NOT EXISTS report_template_policies (
                template_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
                subscription_mode TEXT NOT NULL, revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS report_admin_audit (
                audit_id TEXT PRIMARY KEY, action TEXT NOT NULL, target_type TEXT NOT NULL,
                target_id TEXT NOT NULL, before_summary TEXT NOT NULL,
                after_summary TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_by TEXT NOT NULL)""",
            """CREATE INDEX IF NOT EXISTS report_admin_audit_created
                ON report_admin_audit(created_at DESC)""",
        )
        with self._lock, self._connect() as db:
            for statement in statements:
                db.execute(statement)

    def add_subscription(self, subscription: ReportSubscription, fingerprint: str) -> bool:
        values = asdict(subscription)
        with self._lock, self._connect() as db:
            row = db.execute(
                """INSERT INTO report_subscriptions VALUES(
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                ) ON CONFLICT DO NOTHING RETURNING subscription_id""",
                (
                    values["subscription_id"], values["channel"], values["chat_id"],
                    values["user_id"], values["connector_id"], values["template_id"],
                    values["template_version"], values["schedule"], values["timezone"],
                    json.dumps(values["report_params"], ensure_ascii=False, separators=(",", ":")),
                    values["cron_job_id"], int(values["enabled"]), fingerprint,
                    values["created_at"], values["updated_at"],
                ),
            ).fetchone()
        return row is not None


def create_report_state_store(
    *,
    backend: str = "sqlite",
    postgres_dsn_env: str = "NANOBOT_REPORTING_POSTGRES_DSN",
    sqlite_path: Path | None = None,
) -> ReportStateStore:
    if backend == "sqlite":
        return ReportStateStore(sqlite_path)
    if backend != "postgresql":
        raise ValueError("reporting state backend must be sqlite or postgresql")
    dsn = os.environ.get(postgres_dsn_env, "")
    if not dsn:
        raise RuntimeError(f"reporting PostgreSQL DSN environment variable is not set: {postgres_dsn_env}")
    return PostgresReportStateStore(dsn)


@lru_cache(maxsize=4)
def get_report_state_store(
    backend: str = "sqlite",
    postgres_dsn_env: str = "NANOBOT_REPORTING_POSTGRES_DSN",
) -> ReportStateStore:
    """Return one process-local store per configured backend; config changes require restart."""

    return create_report_state_store(
        backend=backend,
        postgres_dsn_env=postgres_dsn_env,
    )


def configured_report_state_store() -> ReportStateStore:
    from nanobot.config.loader import load_config

    config = load_config().tools.reporting
    return get_report_state_store(
        backend=config.state_backend,
        postgres_dsn_env=config.postgres_dsn_env,
    )
