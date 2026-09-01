"""Control-plane helpers for deterministic reporting settings."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nanobot.config.loader import load_config
from nanobot.config.paths import get_runtime_subdir
from nanobot.reporting import build_default_registry, get_report_state_store
from nanobot.utils.helpers import _write_text_atomic
from nanobot.webui.http_utils import query_first

QueryParams = dict[str, list[str]]


class ReportingSettingsError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _required(query: QueryParams, name: str, *, max_length: int = 256) -> str:
    value = str(query_first(query, name) or "").strip()
    if not value:
        raise ReportingSettingsError(f"missing {name}")
    if len(value) > max_length:
        raise ReportingSettingsError(f"{name} is too long")
    return value


def _bool_value(query: QueryParams, name: str) -> bool:
    value = _required(query, name, max_length=8).casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ReportingSettingsError(f"{name} must be true or false")


def reporting_settings_payload(query: QueryParams | None = None) -> dict[str, Any]:
    query = query or {}
    config = load_config()
    store = get_report_state_store(
        config.tools.reporting.state_backend,
        config.tools.reporting.postgres_dsn_env,
    )
    magik_enabled = bool(getattr(config.tools.magik_cube, "enable", False))
    registry = build_default_registry(
        magik_enabled=magik_enabled,
        grafana_config=getattr(config.tools.reporting, "grafana", None),
        cube_config=config.tools.magik_cube,
    )
    channel = str(query_first(query, "channel") or "").strip()
    user_id = str(query_first(query, "user_id") or "").strip()
    payload: dict[str, Any] = {
        "catalog": registry.public_catalog(),
        "policy": {
            "rbac_enabled": store.rbac_enabled(),
            "resource_types": [
                "connector", "template", "tenant", "project", "model", "endpoint",
                "provider", "environment", "capability",
            ],
        },
        "storage": {
            "backend": config.tools.reporting.state_backend,
            "retention_days": config.tools.reporting.run_retention_days,
        },
        "onboarding_version": config.tools.reporting.onboarding_version,
        "grants": [],
        "recent_runs": [],
        "subscriptions": [],
    }
    if channel and user_id:
        payload["grants"] = store.grants(channel, user_id)
        payload["recent_runs"] = store.recent_runs(channel, user_id, limit=10)
        payload["subscriptions"] = [
            {
                "subscription_id": item.subscription_id,
                "connector_id": item.connector_id,
                "template_id": item.template_id,
                "template_version": item.template_version,
                "schedule": item.schedule,
                "timezone": item.timezone,
                "enabled": item.enabled,
                "updated_at": item.updated_at,
            }
            for item in store.subscriptions(channel, user_id)
        ]
    return payload


def _export_catalog() -> Path:
    payload = reporting_settings_payload()
    declaration = {
        "schema_version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "catalog": payload["catalog"],
        "policy": payload["policy"],
        "onboarding_version": payload["onboarding_version"],
    }
    path = get_runtime_subdir("reports") / "declarations" / "catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(path, json.dumps(declaration, ensure_ascii=False, indent=2) + "\n")
    return path


def reporting_settings_action(action: str | None, query: QueryParams) -> dict[str, Any]:
    if action is None:
        return reporting_settings_payload(query)
    config = load_config()
    store = get_report_state_store(
        config.tools.reporting.state_backend,
        config.tools.reporting.postgres_dsn_env,
    )
    if action == "rbac":
        store.set_rbac_enabled(_bool_value(query, "enabled"))
    elif action in {"grant", "revoke"}:
        channel = _required(query, "channel", max_length=32)
        user_id = _required(query, "user_id")
        resource_type = _required(query, "resource_type", max_length=32)
        resource_id = _required(query, "resource_id")
        try:
            if action == "grant":
                store.grant(channel, user_id, resource_type, resource_id)
            else:
                store.revoke(channel, user_id, resource_type, resource_id)
        except ValueError as exc:
            raise ReportingSettingsError(str(exc)) from exc
    elif action == "export":
        path = _export_catalog()
        payload = reporting_settings_payload(query)
        payload["last_action"] = {"ok": True, "action": "export", "path": str(path)}
        return payload
    else:
        raise ReportingSettingsError("unsupported reporting settings action", status=404)
    payload = reporting_settings_payload(query)
    payload["last_action"] = {"ok": True, "action": action}
    return payload
