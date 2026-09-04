"""Control-plane helpers for deterministic reporting settings."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
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
from nanobot.reporting.subscriptions import (
    ReportSubscriptionService,
    SubscriptionServiceError,
)
from nanobot.session.keys import session_key_for_channel
from nanobot.utils.helpers import _write_text_atomic
from nanobot.webui.http_utils import query_first

QueryParams = dict[str, list[str]]
_ADMIN_REPORT_PARAM_KEYS = frozenset(
    {
        "tenant_query", "tenant_scope", "tenant_labels", "tenants", "model", "models", "model_scope", "project",
        "endpoint", "provider", "providers", "provider_id", "all_tenants",
        "report_template", "report_family", "subscription_period", "report_selections",
        "cluster", "comparison", "granularity", "include_tpm", "breakdown",
        "report_variant", "multi_scope",
    }
)
_STRUCTURED_VALUES_KEY = "__reporting_values"
_GUIDED_FORM_KEYS = frozenset(
    {
        "template_id", "channel", "chat_id", "user_id", "tenant_scope", "tenants",
        "tenant_aliases", "model_scope", "models", "period", "recurrence", "send_time",
        "weekday", "month_day", "timezone", "project", "endpoint", "provider", "cluster",
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


def _restore_cron_job_or_raise(cron: CronService, job: Any) -> None:
    """Restore a Cron snapshot after a legacy settings mutation fails.

    The compatibility endpoint predates ``ReportSubscriptionService`` but must
    obey the same consistency invariant: a subscription row and its executable
    Cron job either change together or the scheduler is restored.  Failure to
    compensate is surfaced as a conflict rather than hidden behind a success
    response.
    """

    restore = getattr(cron, "restore_job", None)
    if not callable(restore):
        raise ReportingSettingsError(
            "subscription state changed and Cron recovery is unavailable", status=409
        )
    try:
        result = restore(deepcopy(job))
    except Exception as exc:
        raise ReportingSettingsError(
            "subscription state changed and Cron recovery failed", status=409
        ) from exc
    if result not in {"restored", "already_present"}:
        raise ReportingSettingsError(
            "subscription state changed and Cron recovery was rejected", status=409
        )


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


def _structured_values(query: QueryParams) -> dict[str, Any]:
    """Decode the bounded private form header merged by the HTTP route."""

    raw = str(query_first(query, _STRUCTURED_VALUES_KEY) or "").strip()
    if not raw:
        return {}
    if len(raw.encode("utf-8")) > 64 * 1024:
        raise ReportingSettingsError("structured reporting values are too large")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReportingSettingsError("structured reporting values must be valid JSON") from exc
    if not isinstance(values, dict):
        raise ReportingSettingsError("structured reporting values must be an object")
    unknown = set(values) - _GUIDED_FORM_KEYS - {
        "subscription_id", "revision", "expected_revision"
    }
    if unknown:
        raise ReportingSettingsError("structured reporting values contain unsupported fields")
    return values


def _guided_form(query: QueryParams) -> dict[str, Any]:
    """Combine private structured values with scalar compatibility parameters."""

    values = _structured_values(query)
    for key in _GUIDED_FORM_KEYS | {"subscription_id", "revision", "expected_revision"}:
        # A REST path is server-owned.  In particular, never let a stale or
        # forged private header replace ``/subscriptions/{id}`` with another
        # subscription.  Other fields retain the structured value so arrays
        # and explicit empty strings round-trip through the compatibility API.
        if key != "subscription_id" and key in values:
            continue
        value = query_first(query, key)
        if value is not None:
            values[key] = value
    return values


def _guided_form_requires_catalog(form: Mapping[str, Any]) -> bool:
    """Decide whether guided validation needs a live Cube catalog snapshot.

    Selected tenants always require catalog resolution.  An all-tenant form
    also needs the snapshot when a specific model is requested, because that
    model must be verified for every tenant before a Cron job is created.
    """

    tenant_scope = str(form.get("tenant_scope") or "selected").strip()
    model_scope = str(form.get("model_scope") or "summary").strip()
    return tenant_scope != "all" or model_scope == "selected"


def _form_required(
    form: dict[str, Any], name: str, *, max_length: int = 256
) -> str:
    """Read one bounded scalar from a guided form or return a safe 400 error."""

    value = str(form.get(name) or "").strip()
    if not value:
        raise ReportingSettingsError(f"missing {name}")
    if len(value) > max_length:
        raise ReportingSettingsError(f"{name} is too long")
    return value


def _form_integer(
    form: dict[str, Any], name: str, *, minimum: int = 0
) -> int:
    """Parse a guided-form integer while preserving valid zero values.

    Legacy subscriptions were created with revision ``0``.  Using the generic
    truthiness-based required-field helper here incorrectly converted that
    valid compare-and-swap version into ``missing revision``.  Keep absence and
    blank text as errors, but accept zero so legacy rows can be edited once and
    advance to the next revision atomically.
    """

    raw = form.get(name)
    if raw is None or isinstance(raw, bool):
        raise ReportingSettingsError(f"missing {name}")
    text = str(raw).strip()
    if not text:
        raise ReportingSettingsError(f"missing {name}")
    if len(text) > 16:
        raise ReportingSettingsError(f"{name} is too long")
    try:
        value = int(text)
    except ValueError as exc:
        raise ReportingSettingsError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ReportingSettingsError(f"{name} is outside the supported range")
    return value


def _run_async_blocking(coroutine: Any) -> Any:
    """Run one bounded adapter coroutine from the synchronous settings API.

    Reporting settings handlers execute in a worker thread.  The fallback for
    direct callers that already own an event loop uses a one-thread executor,
    preventing ``asyncio.run`` from nesting inside that loop.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


def _magik_resolvers(config: Any):
    """Build live Cube scope resolvers only when a real Cube origin is configured."""

    magik_config = getattr(config.tools, "magik_cube", None)
    if magik_config is None or not str(getattr(magik_config, "base_url", "")).strip():
        return None, None, []
    from nanobot.agent.tools.magik_cube import MagikCubeDailyReportTool

    tool = MagikCubeDailyReportTool(config=magik_config)

    def resolve_tenants(queries: list[str]):
        return _run_async_blocking(tool.resolve_tenant_queries(queries))

    def resolve_models(tenant_ids: list[str], models: list[str]):
        return _run_async_blocking(tool.resolve_models_for_tenants(tenant_ids, models))

    # The report fan-out limit is 20 selected tenants, not a limit on the
    # management catalog itself.  Loading the bounded catalog allows the UI to
    # search/select any live customer while the subscription service enforces
    # the smaller execution scope later.
    catalog = _run_async_blocking(tool.list_tenant_catalog())
    return resolve_tenants, resolve_models, catalog


def _reporting_registry_kwargs(config: Any) -> dict[str, Any]:
    """Return one flag-to-registry mapping for runtime and management views.

    The settings page and the channel runtime must describe the same enabled
    templates and renderers.  Keeping this mapping in one place prevents a
    policy editor from showing a capability that the live report registry did
    not actually load (or the inverse).  Configuration values only select
    built-in capability registration; credentials remain inside connector
    configuration and are never copied into the returned mapping.
    """

    tools = getattr(config, "tools", None)
    reporting = getattr(tools, "reporting", None)
    magik_config = getattr(tools, "magik_cube", None)

    def flag(name: str, default: bool = False) -> bool:
        return bool(getattr(reporting, name, default))

    return {
        "magik_enabled": bool(getattr(magik_config, "enable", False))
        and flag("cube_connector", True),
        "grafana_config": (
            getattr(reporting, "grafana", None) if flag("grafana_connector") else None
        ),
        "cube_config": magik_config,
        "cube_templates_enabled": flag("cube_template", True),
        "cube_health_template_enabled": flag("cube_health_connector")
        and flag("cube_health_template"),
        "cube_health_semantics_v2": flag("cube_health_semantics_v2"),
        "cube_health_card_v2": flag("cube_health_card_v2"),
        "cube_ttft_detail_enabled": flag("cube_ttft_detail"),
        "cube_usage_semantics_v2": flag("cube_usage_semantics_v2"),
        "cube_usage_brief_template_enabled": flag("cube_usage_brief_template", True),
        "cube_multi_scope_brief_enabled": flag("cube_multi_scope_brief"),
        "cube_machine_tpm_template_enabled": flag("cube_machine_tpm_report"),
        "cube_cost_template_enabled": flag("cube_cost_connector")
        and flag("cube_cost_template"),
        "cube_provider_quality_connector_enabled": flag("cube_provider_quality_connector"),
        "cube_provider_quality_template_enabled": flag("cube_provider_quality_connector")
        and flag("cube_provider_quality_template"),
        "cube_provider_quality_detail_enabled": flag("cube_provider_quality_detail"),
        "timezone": str(getattr(reporting, "timezone", "Asia/Shanghai")),
        "health_thresholds": getattr(reporting, "health_thresholds", None),
        "wecom_renderer_enabled": flag("wecom_renderer"),
        "dingtalk_renderer_enabled": flag("dingtalk_renderer"),
    }


def _subscription_service(
    config: Any,
    store: Any,
    registry: Any,
    *,
    load_catalog: bool = False,
) -> tuple[ReportSubscriptionService, list[dict[str, str]]]:
    """Construct the common subscription service and optional live catalog."""

    tenant_resolver = model_resolver = None
    catalog: list[dict[str, str]] = []
    if load_catalog:
        try:
            tenant_resolver, model_resolver, catalog = _magik_resolvers(config)
        except Exception as exc:
            raise ReportingSettingsError(
                "Cube 客户目录当前不可用，请稍后重试", status=503
            ) from exc
    return (
        ReportSubscriptionService(
            config=config,
            store=store,
            registry=registry,
            tenant_resolver=tenant_resolver,
            model_resolver=model_resolver,
            catalog_tenants=catalog,
        ),
        catalog,
    )


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
        "revision": item.revision,
        "report_params": safe_params,
        # The UI consumes this normalized view.  ``report_params`` remains in
        # the payload only for old clients and is never rendered as an editor.
        "scope_summary": _subscription_scope_summary(item),
        "schedule_label": _safe_schedule_label(item.schedule),
        "form": _subscription_form_snapshot(item),
        "updated_at": item.updated_at,
    }


def _safe_schedule_label(schedule: str) -> str:
    """Return a human schedule label without exposing Cron as the primary UI."""

    from nanobot.reporting.schedules import describe_subscription_schedule

    return describe_subscription_schedule(schedule)


def _subscription_scope_summary(item: ReportSubscription) -> str:
    params = item.report_params
    labels = [str(value).strip() for value in params.get("tenant_labels") or [] if str(value).strip()]
    if params.get("all_tenants"):
        tenant_text = "全部客户"
    else:
        tenant_text = "、".join(labels) or "、".join(
            str(value) for value in params.get("tenants") or [] if str(value).strip()
        ) or str(params.get("tenant_query") or "未指定客户")
    model_scope = str(params.get("model_scope") or "summary")
    if model_scope == "all":
        model_text = "全部模型"
    elif model_scope == "selected":
        model_text = "、".join(str(value) for value in params.get("models") or []) or "指定模型"
    else:
        model_text = "汇总"
    return f"{tenant_text} · {model_text}"


def _subscription_form_snapshot(item: ReportSubscription) -> dict[str, Any]:
    """Project stored safe fields into the guided editor shape."""

    params = item.report_params
    stored_tenants = list(params.get("tenants") or [])
    if not stored_tenants and params.get("tenant_query"):
        # Rows created before the normalized multi-scope fields used one
        # ``tenant_query`` value.  Recover that exact scope for editing rather
        # than presenting an empty form that could be saved as a broader query.
        stored_tenants = [str(params["tenant_query"])]
    stored_models = list(params.get("models") or [])
    if not stored_models and params.get("model"):
        stored_models = [str(params["model"])]
    stored_labels = list(params.get("tenant_labels") or [])
    if not stored_labels and stored_tenants:
        stored_labels = list(stored_tenants)
    schedule = item.schedule.split()
    minute = schedule[0] if len(schedule) == 5 and schedule[0].isdigit() else "0"
    hour = schedule[1] if len(schedule) == 5 and schedule[1].isdigit() else "9"
    weekday = 1
    recurrence = "workdays"
    if len(schedule) == 5:
        if schedule[4] == "*" and schedule[2] == "*":
            recurrence = "every_day"
        elif schedule[2] == "*" and schedule[4].isdigit():
            recurrence = "weekly"
            weekday = int(schedule[4])
        elif schedule[2].isdigit() and schedule[4] == "*":
            recurrence = "monthly"
    return {
        "template_id": item.template_id,
        "channel": item.channel,
        "chat_id": item.chat_id,
        "user_id": item.user_id,
        "tenant_scope": "all" if params.get("all_tenants") else "selected",
        "tenants": stored_tenants,
        "tenant_names": stored_labels,
        "model_scope": str(params.get("model_scope") or "summary"),
        "models": stored_models,
        "period": str(params.get("subscription_period") or "day"),
        "recurrence": recurrence,
        "send_time": f"{int(hour):02d}:{int(minute):02d}",
        "weekday": weekday,
        "month_day": int(schedule[2]) if len(schedule) == 5 and schedule[2].isdigit() else 1,
        "timezone": item.timezone,
        "project": str(params.get("project") or ""),
        "endpoint": str(params.get("endpoint") or ""),
        "provider": str(params.get("provider") or ""),
        "cluster": str(params.get("cluster") or ""),
        "revision": item.revision,
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
    registry = build_default_registry(**_reporting_registry_kwargs(config))
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
                "show_subscription_button": (
                    bool(stored["show_subscription_button"]) if stored else True
                ),
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
            "guided_ui_enabled": bool(
                getattr(config.tools.reporting, "report_subscription_guided_ui", False)
            ),
            "button_policy_enabled": bool(
                getattr(config.tools.reporting, "report_subscription_button_policy", False)
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


def _reporting_registry(config: Any):
    """Build the same capability registry used by the runtime report center."""

    return build_default_registry(**_reporting_registry_kwargs(config))


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
    guided_enabled = bool(
        getattr(config.tools.reporting, "report_subscription_guided_ui", False)
    )
    button_policy_enabled = bool(
        getattr(config.tools.reporting, "report_subscription_button_policy", False)
    )
    if action in {"subscription_options", "options"}:
        if not management_enabled:
            raise ReportingSettingsError("report management is disabled", status=404)
        registry = _reporting_registry(config)
        try:
            _tenant_resolver, _model_resolver, catalog = _magik_resolvers(config)
        except Exception as exc:
            raise ReportingSettingsError(
                "Cube 客户目录当前不可用，请稍后重试", status=503
            ) from exc
        payload = reporting_settings_payload(query)
        persisted_policies = {
            item["template_id"]: item for item in store.template_policies()
        }
        default_non_subscribable = {
            "usage_customer_model_daily_brief",
            "machine_tpm_peak",
        }

        def template_subscription_view(template_id: str) -> dict[str, Any]:
            """Keep editor options consistent with the persisted policy view."""

            stored = persisted_policies.get(template_id)
            enabled = bool(stored["enabled"]) if stored else True
            mode = (
                str(stored["subscription_mode"])
                if stored
                else "disabled"
                if template_id in default_non_subscribable
                else "all_authorized"
            )
            show_button = bool(stored["show_subscription_button"]) if stored else True
            return {
                "enabled": enabled,
                "subscription_mode": mode,
                "show_subscription_button": show_button,
                "subscribable": enabled and mode != "disabled",
                "revision": int(stored["revision"]) if stored else 0,
            }

        payload["subscription_options"] = {
            "templates": [
                {
                    "id": item.manifest.template_id,
                    "name": item.manifest.display_name,
                    "version": item.manifest.version,
                    "periods": sorted(item.manifest.periods),
                    **template_subscription_view(item.manifest.template_id),
                }
                for item in registry.templates()
                if item.manifest.lifecycle_state in {"publish", "canary"}
            ],
            "tenants": catalog,
            "timezones": ["Asia/Shanghai", "UTC"],
        }
        payload["last_action"] = {"ok": True, "action": "subscription_options"}
        return payload
    structured = _structured_values(query)
    if action in {
        "subscription_preview",
        "subscription_create_guided",
        "subscription_update",
        "subscription_enable",
        "subscription_disable",
        "subscription_delete",
    } or (action == "subscription_create" and bool(structured)):
        form = _guided_form(query)
        guided_requested = (
            action in {"subscription_preview", "subscription_create_guided"}
            or (action == "subscription_create" and bool(structured))
            or (action == "subscription_update" and bool(structured))
            or (
                action in {"subscription_enable", "subscription_disable", "subscription_delete"}
                and bool(structured)
            )
        )
        if guided_requested:
            if not management_enabled:
                raise ReportingSettingsError("report management is disabled", status=404)
            if not guided_enabled:
                raise ReportingSettingsError(
                    "guided subscription UI is disabled", status=404
                )
        if action in {
            "subscription_enable",
            "subscription_disable",
            "subscription_delete",
        } and (structured or query_first(query, "revision") is not None):
            if not management_enabled:
                raise ReportingSettingsError("report management is disabled", status=404)
            subscription_id = str(form.get("subscription_id") or "").strip()
            if not subscription_id or len(subscription_id) > 64:
                raise ReportingSettingsError("missing subscription_id")
            try:
                expected_revision = int(
                    form.get("revision", form.get("expected_revision"))
                )
            except (TypeError, ValueError) as exc:
                raise ReportingSettingsError("revision must be an integer") from exc
            registry = _reporting_registry(config)
            service, _catalog = _subscription_service(config, store, registry)
            try:
                if action == "subscription_delete":
                    service.delete(subscription_id, expected_revision=expected_revision)
                else:
                    service.set_enabled(
                        subscription_id,
                        enabled=action == "subscription_enable",
                        expected_revision=expected_revision,
                    )
            except SubscriptionServiceError as exc:
                raise ReportingSettingsError(exc.message, status=exc.status) from exc
            payload = reporting_settings_payload(query)
            payload["last_action"] = {"ok": True, "action": action}
            return payload
        if action == "subscription_update":
            subscription_id = _form_required(form, "subscription_id", max_length=64)
            revision_key = (
                "revision" if form.get("revision") is not None else "expected_revision"
            )
            expected_revision = _form_integer(form, revision_key)
            registry = _reporting_registry(config)
            service, _catalog = _subscription_service(
                config,
                store,
                registry,
                load_catalog=_guided_form_requires_catalog(form),
            )
            try:
                service.update(
                    subscription_id,
                    form,
                    expected_revision=expected_revision,
                )
            except SubscriptionServiceError as exc:
                raise ReportingSettingsError(exc.message, status=exc.status) from exc
            payload = reporting_settings_payload(query)
            payload["last_action"] = {"ok": True, "action": "subscription_update"}
            return payload
        if action == "subscription_preview":
            registry = _reporting_registry(config)
            service, _catalog = _subscription_service(
                config,
                store,
                registry,
                load_catalog=_guided_form_requires_catalog(form),
            )
            try:
                preview = service.preview(form)
            except SubscriptionServiceError as exc:
                raise ReportingSettingsError(exc.message, status=exc.status) from exc
            payload = reporting_settings_payload(query)
            payload["subscription_preview"] = preview
            payload["last_action"] = {"ok": True, "action": "subscription_preview"}
            return payload
        # ``subscription_create`` with the private structured header and the
        # explicit guided action share the same service.  The old query-based
        # JSON branch below remains available only when no structured values
        # are supplied, preserving existing clients during migration.
        if action == "subscription_create_guided" or (action == "subscription_create" and structured):
            if not management_enabled:
                raise ReportingSettingsError("report management is disabled", status=404)
            registry = _reporting_registry(config)
            service, _catalog = _subscription_service(
                config,
                store,
                registry,
                load_catalog=_guided_form_requires_catalog(form),
            )
            try:
                service.create(form)
            except SubscriptionServiceError as exc:
                raise ReportingSettingsError(exc.message, status=exc.status) from exc
            payload = reporting_settings_payload(query)
            payload["last_action"] = {"ok": True, "action": "subscription_create"}
            return payload
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
        registry = _reporting_registry(config)
        if registry.template(template_id) is None:
            raise ReportingSettingsError("unknown report template", status=404)
        try:
            show_button = (
                _bool_value(query, "show_subscription_button")
                if query_first(query, "show_subscription_button") is not None
                else None
            )
            if show_button is not None and not button_policy_enabled:
                raise ReportingSettingsError(
                    "subscription button policy is disabled", status=404
                )
            store.set_template_policy(
                template_id,
                enabled=_bool_value(query, "enabled"),
                subscription_mode=_required(query, "subscription_mode", max_length=32),
                updated_by="webui_admin",
                expected_revision=_integer_value(query, "revision"),
                show_subscription_button=show_button,
            )
        except ValueError as exc:
            status = 409 if "another operator" in str(exc) else 400
            raise ReportingSettingsError(str(exc), status=status) from exc
    elif action in {"subscription_enable", "subscription_disable", "subscription_delete"}:
        if not management_enabled:
            raise ReportingSettingsError("report management is disabled", status=404)
        # Guided clients send the row revision so a stale button cannot mutate
        # a newer subscription.  Legacy clients without a revision retain the
        # original behavior during the migration window.
        if query_first(query, "revision") is not None or query_first(
            query, "expected_revision"
        ) is not None:
            subscription_id = _required(query, "subscription_id", max_length=64)
            revision_key = "revision" if query_first(query, "revision") is not None else "expected_revision"
            expected_revision = _integer_value(query, revision_key)
            registry = _reporting_registry(config)
            service, _catalog = _subscription_service(config, store, registry)
            try:
                if action == "subscription_delete":
                    service.delete(subscription_id, expected_revision=expected_revision)
                else:
                    service.set_enabled(
                        subscription_id,
                        enabled=action == "subscription_enable",
                        expected_revision=expected_revision,
                    )
            except SubscriptionServiceError as exc:
                raise ReportingSettingsError(exc.message, status=exc.status) from exc
            payload = reporting_settings_payload(query)
            payload["last_action"] = {"ok": True, "action": action}
            return payload
        subscription_id = _required(query, "subscription_id", max_length=64)
        subscription = store.subscription(subscription_id)
        if subscription is None:
            raise ReportingSettingsError("report subscription not found", status=404)
        cron = _cron_service(config)
        before = {"enabled": subscription.enabled, "schedule": subscription.schedule}
        if action == "subscription_delete":
            job_snapshot = cron.get_job(subscription.cron_job_id)
            if job_snapshot is None:
                raise ReportingSettingsError(
                    "subscription Cron job is missing or protected", status=409
                )
            job_snapshot = deepcopy(job_snapshot)
            result = cron.remove_job(subscription.cron_job_id)
            if result != "removed":
                raise ReportingSettingsError("subscription Cron job is missing or protected", status=409)
            try:
                removed = store.remove_subscription(
                    subscription_id, channel=subscription.channel, user_id=subscription.user_id
                )
            except Exception as exc:
                _restore_cron_job_or_raise(cron, job_snapshot)
                raise ReportingSettingsError(
                    "订阅删除失败，已恢复原计划", status=503
                ) from exc
            if not removed:
                _restore_cron_job_or_raise(cron, job_snapshot)
                raise ReportingSettingsError("subscription state changed during deletion", status=409)
            after = {"deleted": True}
        else:
            enabled = action == "subscription_enable"
            old_enabled = subscription.enabled
            job = cron.enable_job(subscription.cron_job_id, enabled)
            if job is None:
                raise ReportingSettingsError("subscription Cron job was not found", status=409)
            try:
                updated = store.set_subscription_enabled(
                    subscription_id,
                    channel=subscription.channel,
                    user_id=subscription.user_id,
                    enabled=enabled,
                )
            except Exception as exc:
                cron.enable_job(subscription.cron_job_id, old_enabled)
                raise ReportingSettingsError(
                    "订阅状态保存失败，已恢复原计划", status=503
                ) from exc
            if updated is None:
                cron.enable_job(subscription.cron_job_id, old_enabled)
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
        job_snapshot = cron.get_job(subscription.cron_job_id)
        if job_snapshot is None:
            raise ReportingSettingsError("subscription Cron job is missing or protected", status=409)
        job_snapshot = deepcopy(job_snapshot)
        result = cron.update_job(
            subscription.cron_job_id,
            schedule=CronSchedule(kind="cron", expr=schedule, tz=timezone_name),
        )
        if result in {"not_found", "protected"}:
            raise ReportingSettingsError("subscription Cron job cannot be updated", status=409)
        try:
            updated = store.update_subscription_schedule(
                subscription_id, schedule=schedule, timezone_name=timezone_name
            )
        except Exception as exc:
            _restore_cron_job_or_raise(cron, job_snapshot)
            raise ReportingSettingsError(
                "订阅计划保存失败，已恢复原计划", status=503
            ) from exc
        if updated is None:
            _restore_cron_job_or_raise(cron, job_snapshot)
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
        registry = _reporting_registry(config)
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
        try:
            added = store.add_subscription(subscription, fingerprint)
        except Exception as exc:
            # Cron is created before the legacy row for the same reason as the
            # shared service: an executable orphan must never survive a failed
            # persistence write.  Keep the compatibility endpoint under the
            # same compensation invariant during migration.
            try:
                cron.remove_job(job.id)
            except Exception as cleanup_exc:
                raise ReportingSettingsError(
                    "subscription save failed and Cron cleanup failed", status=409
                ) from cleanup_exc
            raise ReportingSettingsError(
                "subscription save failed; no subscription was created", status=503
            ) from exc
        if not added:
            try:
                cron.remove_job(job.id)
            except Exception as cleanup_exc:
                raise ReportingSettingsError(
                    "duplicate subscription detected but Cron cleanup failed", status=409
                ) from cleanup_exc
            raise ReportingSettingsError(
                "an identical report subscription already exists", status=409
            )
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
