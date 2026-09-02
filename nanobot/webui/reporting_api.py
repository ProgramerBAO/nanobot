"""Control-plane helpers for deterministic reporting settings."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nanobot.bus.events import INBOUND_META_DIRECT_TOOL
from nanobot.config.loader import load_config
from nanobot.config.paths import get_runtime_subdir
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule
from nanobot.reporting import build_default_registry, get_report_state_store
from nanobot.reporting.store import ReportSubscription
from nanobot.session.keys import session_key_for_channel
from nanobot.utils.helpers import _write_text_atomic
from nanobot.webui.http_utils import query_first

QueryParams = dict[str, list[str]]
_ADMIN_REPORT_PARAM_KEYS = frozenset(
    {
        "tenant_query", "tenants", "model", "models", "model_scope", "project",
        "endpoint", "provider", "providers", "provider_id", "all_tenants",
        "report_template", "report_family", "subscription_period", "report_selections",
        "cluster", "comparison", "granularity", "include_tpm", "breakdown",
    }
)


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


def _integer_value(query: QueryParams, name: str, *, minimum: int = 0) -> int:
    try:
        value = int(_required(query, name, max_length=16))
    except ValueError as exc:
        raise ReportingSettingsError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ReportingSettingsError(f"{name} is outside the supported range")
    return value


def _cron_service(config: Any) -> CronService:
    """Open the workspace Cron store through its cross-process action protocol."""

    return CronService(config.workspace_path / "cron" / "jobs.json")


def _safe_admin_report_params(raw: str) -> dict[str, Any]:
    if len(raw) > 16_384:
        raise ReportingSettingsError("report_params_json is too large")
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ReportingSettingsError("report_params_json must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ReportingSettingsError("report_params_json must be an object")
    unknown = set(value) - _ADMIN_REPORT_PARAM_KEYS
    if unknown:
        raise ReportingSettingsError("report_params_json contains unsupported fields")
    serialized = json.dumps(value, ensure_ascii=False).casefold()
    forbidden = ("http://", "https://", "bearer ", "password", "api_key", "apikey", "secret")
    if any(item in serialized for item in forbidden):
        raise ReportingSettingsError("report_params_json contains forbidden content")
    return value


def _subscription_payload(item: ReportSubscription) -> dict[str, Any]:
    """Expose bounded subscription scope; credentials are never valid report parameters."""

    safe_params = {
        key: value
        for key, value in item.report_params.items()
        if key in _ADMIN_REPORT_PARAM_KEYS
    }
    return {
        "subscription_id": item.subscription_id,
        "channel": item.channel,
        "chat_id": item.chat_id,
        "user_id": item.user_id,
        "connector_id": item.connector_id,
        "template_id": item.template_id,
        "template_version": item.template_version,
        "schedule": item.schedule,
        "timezone": item.timezone,
        "enabled": item.enabled,
        "report_params": safe_params,
        "updated_at": item.updated_at,
    }


def _pagination_value(
    query: QueryParams, name: str, *, default: int, minimum: int, maximum: int
) -> int:
    """Parse bounded list pagination without turning malformed input into HTTP 500."""

    raw = str(query_first(query, name) or default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ReportingSettingsError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ReportingSettingsError(f"{name} is outside the supported range")
    return min(value, maximum)


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
        cube_multi_scope_brief_enabled=bool(
            getattr(config.tools.reporting, "cube_multi_scope_brief", False)
        ),
        cube_machine_tpm_template_enabled=bool(
            getattr(config.tools.reporting, "cube_machine_tpm_report", False)
        ),
    )
    channel = str(query_first(query, "channel") or "").strip()
    user_id = str(query_first(query, "user_id") or "").strip()
    persisted_policies = {
        item["template_id"]: item for item in store.template_policies()
    }
    default_non_subscribable = {
        "usage_customer_model_daily_brief", "machine_tpm_peak"
    }
    template_policies = []
    for item in registry.public_catalog().get("templates", []):
        stored = persisted_policies.get(str(item.get("id") or ""))
        template_policies.append(
            {
                **item,
                "enabled": bool(stored["enabled"]) if stored else True,
                "subscription_mode": (
                    str(stored["subscription_mode"])
                    if stored
                    else "disabled"
                    if item.get("id") in default_non_subscribable
                    else "all_authorized"
                ),
                "revision": int(stored["revision"]) if stored else 0,
                "updated_at": str(stored["updated_at"]) if stored else "",
            }
        )
    payload: dict[str, Any] = {
        "catalog": registry.public_catalog(),
        "policy": {
            "rbac_enabled": store.rbac_enabled(),
            "management_enabled": bool(
                getattr(config.tools.reporting, "report_management_v1", False)
            ),
            "resource_types": [
                "connector", "template", "tenant", "project", "model", "endpoint",
            "provider", "environment", "capability", "subscription_template",
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
        "template_policies": template_policies,
    }
    if payload["policy"]["management_enabled"]:
        limit = _pagination_value(query, "limit", default=100, minimum=1, maximum=500)
        offset = _pagination_value(
            query, "offset", default=0, minimum=0, maximum=1_000_000
        )
        payload["subscriptions"] = [
            _subscription_payload(item)
            for item in store.all_subscriptions(limit=limit, offset=offset)
        ]
    if channel and user_id:
        payload["grants"] = store.grants(channel, user_id)
        payload["recent_runs"] = store.recent_runs(channel, user_id, limit=10)
        if not payload["policy"]["management_enabled"]:
            payload["subscriptions"] = [
                _subscription_payload(item) for item in store.subscriptions(channel, user_id)
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
    management_enabled = bool(getattr(config.tools.reporting, "report_management_v1", False))
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
    elif action == "template_policy":
        if not management_enabled:
            raise ReportingSettingsError("report management is disabled", status=404)
        template_id = _required(query, "template_id", max_length=128)
        registry = build_default_registry(
            magik_enabled=bool(getattr(config.tools.magik_cube, "enable", False)),
            grafana_config=getattr(config.tools.reporting, "grafana", None),
            cube_config=config.tools.magik_cube,
            cube_multi_scope_brief_enabled=bool(
                getattr(config.tools.reporting, "cube_multi_scope_brief", False)
            ),
            cube_machine_tpm_template_enabled=bool(
                getattr(config.tools.reporting, "cube_machine_tpm_report", False)
            ),
        )
        if registry.template(template_id) is None:
            raise ReportingSettingsError("unknown report template", status=404)
        try:
            store.set_template_policy(
                template_id,
                enabled=_bool_value(query, "enabled"),
                subscription_mode=_required(query, "subscription_mode", max_length=32),
                updated_by="webui_admin",
                expected_revision=_integer_value(query, "revision"),
            )
        except ValueError as exc:
            status = 409 if "another operator" in str(exc) else 400
            raise ReportingSettingsError(str(exc), status=status) from exc
    elif action in {"subscription_enable", "subscription_disable", "subscription_delete"}:
        if not management_enabled:
            raise ReportingSettingsError("report management is disabled", status=404)
        subscription_id = _required(query, "subscription_id", max_length=64)
        subscription = store.subscription(subscription_id)
        if subscription is None:
            raise ReportingSettingsError("report subscription not found", status=404)
        cron = _cron_service(config)
        before = {"enabled": subscription.enabled, "schedule": subscription.schedule}
        if action == "subscription_delete":
            result = cron.remove_job(subscription.cron_job_id)
            if result != "removed":
                raise ReportingSettingsError("subscription Cron job is missing or protected", status=409)
            if not store.remove_subscription(
                subscription_id, channel=subscription.channel, user_id=subscription.user_id
            ):
                raise ReportingSettingsError("subscription state changed during deletion", status=409)
            after = {"deleted": True}
        else:
            enabled = action == "subscription_enable"
            job = cron.enable_job(subscription.cron_job_id, enabled)
            if job is None:
                raise ReportingSettingsError("subscription Cron job was not found", status=409)
            updated = store.set_subscription_enabled(
                subscription_id,
                channel=subscription.channel,
                user_id=subscription.user_id,
                enabled=enabled,
            )
            if updated is None:
                cron.enable_job(subscription.cron_job_id, subscription.enabled)
                raise ReportingSettingsError("subscription state changed during update", status=409)
            after = {"enabled": enabled, "schedule": subscription.schedule}
        store.record_admin_audit(
            action=action,
            target_type="subscription",
            target_id=subscription_id,
            before_summary=before,
            after_summary=after,
            updated_by="webui_admin",
        )
    elif action == "subscription_schedule":
        if not management_enabled:
            raise ReportingSettingsError("report management is disabled", status=404)
        subscription_id = _required(query, "subscription_id", max_length=64)
        schedule = _required(query, "schedule", max_length=128)
        timezone_name = _required(query, "timezone", max_length=64)
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:
            raise ReportingSettingsError("invalid subscription timezone") from exc
        subscription = store.subscription(subscription_id)
        if subscription is None:
            raise ReportingSettingsError("report subscription not found", status=404)
        cron = _cron_service(config)
        result = cron.update_job(
            subscription.cron_job_id,
            schedule=CronSchedule(kind="cron", expr=schedule, tz=timezone_name),
        )
        if result in {"not_found", "protected"}:
            raise ReportingSettingsError("subscription Cron job cannot be updated", status=409)
        updated = store.update_subscription_schedule(
            subscription_id, schedule=schedule, timezone_name=timezone_name
        )
        if updated is None:
            cron.update_job(
                subscription.cron_job_id,
                schedule=CronSchedule(
                    kind="cron", expr=subscription.schedule, tz=subscription.timezone
                ),
            )
            raise ReportingSettingsError("subscription state changed during update", status=409)
        store.record_admin_audit(
            action=action,
            target_type="subscription",
            target_id=subscription_id,
            before_summary={"schedule": subscription.schedule, "timezone": subscription.timezone},
            after_summary={"schedule": schedule, "timezone": timezone_name},
            updated_by="webui_admin",
        )
    elif action == "subscription_create":
        if not management_enabled:
            raise ReportingSettingsError("report management is disabled", status=404)
        registry = build_default_registry(
            magik_enabled=bool(getattr(config.tools.magik_cube, "enable", False)),
            grafana_config=getattr(config.tools.reporting, "grafana", None),
            cube_config=config.tools.magik_cube,
            cube_multi_scope_brief_enabled=bool(
                getattr(config.tools.reporting, "cube_multi_scope_brief", False)
            ),
            cube_machine_tpm_template_enabled=bool(
                getattr(config.tools.reporting, "cube_machine_tpm_report", False)
            ),
        )
        template_id = _required(query, "template_id", max_length=128)
        template = registry.template(template_id)
        if template is None:
            raise ReportingSettingsError("unknown report template", status=404)
        policy = next(
            (item for item in store.template_policies() if item["template_id"] == template_id),
            None,
        )
        default_disabled = template_id in {
            "usage_customer_model_daily_brief", "machine_tpm_peak"
        }
        if (
            policy
            and (not policy["enabled"] or policy["subscription_mode"] == "disabled")
        ) or (policy is None and default_disabled):
            raise ReportingSettingsError("this report template does not allow subscriptions", status=403)
        if policy and policy["subscription_mode"] == "allowlist" and not store.allowed(
            _required(query, "channel", max_length=32),
            _required(query, "user_id"),
            "subscription_template",
            template_id,
        ):
            raise ReportingSettingsError("user is not allowed to subscribe to this template", status=403)
        channel = _required(query, "channel", max_length=32)
        chat_id = _required(query, "chat_id")
        user_id = _required(query, "user_id")
        schedule = _required(query, "schedule", max_length=128)
        timezone_name = _required(query, "timezone", max_length=64)
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:
            raise ReportingSettingsError("invalid subscription timezone") from exc
        params = _safe_admin_report_params(
            str(query_first(query, "report_params_json") or "{}")
        )
        subscription_id = uuid.uuid4().hex[:16]
        supported_periods = sorted(template.manifest.periods)
        params.setdefault(
            "subscription_period",
            "day" if "day" in template.manifest.periods else supported_periods[0],
        )
        fingerprint_source = [channel, user_id, template_id, schedule, params]
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_source, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        direct_tool = {
            "name": "report_center",
            "params": {"action": "run_subscription", "subscription_id": subscription_id},
        }
        cron = _cron_service(config)
        job = cron.add_job(
            name=f"固定报表订阅 {template.manifest.display_name}",
            schedule=CronSchedule(kind="cron", expr=schedule, tz=timezone_name),
            message="执行固定报表订阅",
            session_key=session_key_for_channel(
                channel,
                chat_id,
                unified_session=config.agents.defaults.unified_session,
            ),
            origin_channel=channel,
            origin_chat_id=chat_id,
            origin_metadata={
                INBOUND_META_DIRECT_TOOL: direct_tool,
                "direct_request_text": "执行固定报表订阅",
            },
        )
        now = datetime.now(UTC).isoformat()
        subscription = ReportSubscription(
            subscription_id=subscription_id,
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            connector_id=next(iter(template.manifest.connector_ids)),
            template_id=template_id,
            template_version=template.manifest.version,
            schedule=schedule,
            timezone=timezone_name,
            report_params=params,
            cron_job_id=job.id,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        if not store.add_subscription(subscription, fingerprint):
            cron.remove_job(job.id)
            raise ReportingSettingsError("an identical report subscription already exists", status=409)
        store.record_admin_audit(
            action=action,
            target_type="subscription",
            target_id=subscription_id,
            before_summary={},
            after_summary={"template_id": template_id, "enabled": True, "schedule": schedule},
            updated_by="webui_admin",
        )
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
