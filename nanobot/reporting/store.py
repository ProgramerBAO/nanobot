"""SQLite state for onboarding, RBAC, report runs, and subscriptions."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from nanobot.config.paths import get_runtime_subdir


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
        self.path = path or get_runtime_subdir("reports") / "state.db"
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

    def grant(self, channel: str, user_id: str, resource_type: str, resource_id: str) -> None:
        if resource_type not in {"connector", "template", "tenant", "model", "capability"}:
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
