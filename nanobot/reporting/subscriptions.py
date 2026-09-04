"""Guided report-subscription compilation and lifecycle operations.

The WebUI and future channel flows submit a small, typed form rather than a
raw Cron expression or arbitrary report JSON.  This module owns the boundary
between that form and the existing ``ReportSubscription`` persistence model:
it validates scope, template policy, schedule semantics, and bounded report
parameters before any Cron side effect is created.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from nanobot.bus.events import INBOUND_META_DIRECT_TOOL
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule
from nanobot.reporting.authorization import authorize_magik_params
from nanobot.reporting.registry import ReportPluginRegistry
from nanobot.reporting.schedules import build_subscription_schedule, describe_subscription_schedule
from nanobot.reporting.store import ReportStateStore, ReportSubscription
from nanobot.session.keys import session_key_for_channel

TenantResolver = Callable[[list[str]], tuple[list[dict[str, str]], list[dict[str, str]]]]
ModelResolver = Callable[
    [list[str], list[str]], tuple[dict[str, list[str]], list[dict[str, str]]]
]

_SAFE_REPORT_PARAM_KEYS = frozenset(
    {
        "tenant_query",
        # The normalized scope marker distinguishes a bounded tenant list from
        # the live all-tenant scope for both guided and confirmed subscriptions.
        "tenant_scope",
        "tenants",
        "tenant_labels",
        "model",
        "models",
        "model_scope",
        "project",
        "endpoint",
        "provider",
        "providers",
        "provider_id",
        "all_tenants",
        "report_template",
        "report_family",
        "subscription_period",
        "report_selections",
        "cluster",
        "comparison",
        "granularity",
        "include_tpm",
        "breakdown",
        "report_variant",
        "multi_scope",
    }
)
_ALLOWED_CHANNELS = frozenset({"feishu", "wecom", "dingtalk", "webhook", "text"})
_UNSAFE_TEXT_RE = re.compile(r"(?:https?://|bearer\s+|password|api[_-]?key|secret)", re.I)
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_SAFE_CUBE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class SubscriptionServiceError(ValueError):
    """A safe, user-facing subscription control-plane error."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True, slots=True)
class CompiledSubscriptionForm:
    """Validated form plus the internal values required to create a job."""

    template_id: str
    connector_id: str
    template_version: str
    channel: str
    chat_id: str
    user_id: str
    period: str
    recurrence: str
    send_time: str
    timezone: str
    weekday: int
    month_day: int
    schedule: str
    report_params: dict[str, Any]
    tenant_names: tuple[str, ...]
    models: tuple[str, ...]

    @property
    def schedule_label(self) -> str:
        return describe_subscription_schedule(self.schedule)

    def to_form(self, *, revision: int = 0) -> dict[str, Any]:
        """Return a frontend-safe representation without raw JSON or secrets."""

        params = self.report_params
        tenant_scope = "all" if params.get("all_tenants") else "selected"
        model_scope = str(params.get("model_scope") or "summary")
        return {
            "template_id": self.template_id,
            "channel": self.channel,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "tenant_scope": tenant_scope,
            "tenants": list(params.get("tenants") or []),
            "tenant_names": list(self.tenant_names),
            "model_scope": model_scope,
            "models": list(params.get("models") or self.models),
            "period": self.period,
            "recurrence": self.recurrence,
            "send_time": self.send_time,
            "weekday": self.weekday,
            "month_day": self.month_day,
            "timezone": self.timezone,
            "project": str(params.get("project") or ""),
            "endpoint": str(params.get("endpoint") or ""),
            "provider": str(params.get("provider") or ""),
            "cluster": str(params.get("cluster") or ""),
            "revision": revision,
        }


def _text(value: Any, name: str, *, required: bool = False, maximum: int = 256) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise SubscriptionServiceError(f"missing {name}")
    if len(result) > maximum:
        raise SubscriptionServiceError(f"{name} is too long")
    if _UNSAFE_TEXT_RE.search(result):
        raise SubscriptionServiceError(f"{name} contains forbidden content")
    return result


def _values(value: Any, name: str, *, maximum_items: int = 20) -> list[str]:
    """Accept UI arrays and human-friendly comma/顿号 separated values."""

    if value is None or value == "":
        return []
    raw: list[Any]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise SubscriptionServiceError(f"{name} must be a list") from exc
            if not isinstance(decoded, list):
                raise SubscriptionServiceError(f"{name} must be a list")
            raw = decoded
        else:
            raw = re.split(r"[,，、;；\n]+", stripped)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw = list(value)
    else:
        raise SubscriptionServiceError(f"{name} must be a list")
    result = list(dict.fromkeys(_text(item, name, maximum=128) for item in raw if str(item).strip()))
    if len(result) > maximum_items:
        raise SubscriptionServiceError(f"{name} supports at most {maximum_items} items")
    return result


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SubscriptionServiceError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise SubscriptionServiceError(f"{name} is outside the supported range")
    return parsed


def _period_for_template(template_id: str, periods: set[str], requested: Any) -> str:
    aliases = {
        "日报": "day",
        "日报简报": "day",
        "周报": "week",
        "周报简报": "week",
        "月报": "month",
        "月报简报": "month",
        "区间": "range",
        "区间报表": "range",
    }
    value = aliases.get(str(requested or "").strip(), str(requested or "").strip())
    if not value:
        value = "day" if "day" in periods else sorted(periods)[0] if periods else "day"
    if value == "recent15m" or value not in periods:
        raise SubscriptionServiceError("该报表不支持当前订阅周期")
    if value == "range":
        raise SubscriptionServiceError("自定义历史区间不能创建周期订阅")
    return value


def _recurrence(value: Any) -> str:
    aliases = {
        "daily": "every_day",
        "每天": "every_day",
        "workday": "workdays",
        "workdays": "workdays",
        "工作日": "workdays",
        "weekly": "weekly",
        "每周": "weekly",
        "monthly": "monthly",
        "每月": "monthly",
    }
    result = aliases.get(str(value or "").strip(), str(value or "").strip() or "workdays")
    if result not in {"every_day", "workdays", "weekly", "monthly"}:
        raise SubscriptionServiceError("不支持的订阅频率")
    return result


def _schedule_period(recurrence: str) -> str:
    return {
        "every_day": "day",
        "workdays": "day",
        "weekly": "week",
        "monthly": "month",
    }[recurrence]


class ReportSubscriptionService:
    """Compile and mutate subscriptions through one validated control plane.

    ``tenant_resolver`` and ``model_resolver`` are injected at the adapter
    boundary so the service remains independent of Cube HTTP clients.  A
    production caller supplies resolvers backed by the live catalog; tests can
    provide deterministic fixtures.  Cron is changed before the database and
    restored when a CAS or persistence operation fails.
    """

    def __init__(
        self,
        *,
        config: Any,
        store: ReportStateStore,
        registry: ReportPluginRegistry,
        cron: CronService | None = None,
        tenant_resolver: TenantResolver | None = None,
        model_resolver: ModelResolver | None = None,
        catalog_tenants: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.registry = registry
        self.cron = cron or CronService(config.workspace_path / "cron" / "jobs.json")
        self.tenant_resolver = tenant_resolver
        self.model_resolver = model_resolver
        # Keep a bounded, non-secret snapshot only for validating a selected
        # model against an all-customer form.  The scheduled report still
        # resolves its live model catalog at execution time.
        self.catalog_tenants = tuple(
            dict(item) for item in (catalog_tenants or ()) if isinstance(item, Mapping)
        )

    def _management_enabled(self) -> bool:
        reporting_config = getattr(getattr(self.config, "tools", None), "reporting", self.config)
        return bool(getattr(reporting_config, "report_management_v1", False))

    def _template(self, template_id: str):
        template = self.registry.template(template_id)
        if template is None:
            raise SubscriptionServiceError("unknown report template", status=404)
        return template

    def _check_template_policy(self, template_id: str, *, channel: str, user_id: str) -> None:
        """Enforce lifecycle and subscription audience before side effects."""

        template = self._template(template_id)
        if template.manifest.lifecycle_state not in {"publish", "canary"}:
            raise SubscriptionServiceError("this report template is not available", status=403)
        if self.store.rbac_enabled():
            required_grants = [
                ("capability", "subscriptions"),
                ("template", template_id),
            ]
            required_grants.extend(
                ("connector", connector_id)
                for connector_id in template.manifest.connector_ids
            )
            for resource_type, resource_id in required_grants:
                if not self.store.allowed(channel, user_id, resource_type, resource_id):
                    raise SubscriptionServiceError(
                        "user is not authorized for this report subscription", status=403
                    )
        # The management policy is an additive control plane.  When it is not
        # enabled, legacy subscription APIs retain their historical behavior;
        # RBAC and template lifecycle checks above still apply.
        if not self._management_enabled():
            return
        policy = self.store.template_policy(template_id)
        default_disabled = template_id in {
            "usage_customer_model_daily_brief",
            "machine_tpm_peak",
        }
        if policy is None:
            if default_disabled:
                raise SubscriptionServiceError(
                    "this report template does not allow subscriptions", status=403
                )
            return
        if not policy["enabled"]:
            raise SubscriptionServiceError("this report template is disabled", status=403)
        mode = str(policy["subscription_mode"])
        if mode == "disabled":
            raise SubscriptionServiceError(
                "this report template does not allow subscriptions", status=403
            )
        if mode == "allowlist" and not self.store.allowed(
            channel, user_id, "subscription_template", template_id
        ):
            raise SubscriptionServiceError(
                "user is not allowed to subscribe to this template", status=403
            )

    def _authorize_compiled_scope(self, compiled: CompiledSubscriptionForm) -> None:
        """Re-check normalized tenant/model scope before a Cron side effect.

        A guided form can come from an untrusted browser or from a legacy row
        being edited.  Authorization therefore runs after catalog resolution,
        against the exact IDs and model names that will be stored and replayed
        by Cron.  Other connectors keep their own grant boundary.
        """

        if compiled.connector_id != "magik_cube":
            return
        denial = authorize_magik_params(
            self.store,
            channel=compiled.channel,
            user_id=compiled.user_id,
            params=compiled.report_params,
        )
        if denial:
            raise SubscriptionServiceError(denial, status=403)

    @staticmethod
    def _form_value(
        form: Mapping[str, Any], previous: Mapping[str, Any], key: str, default: Any = None
    ) -> Any:
        """Read an explicit form value without treating an empty value as omission.

        This distinction lets an operator deliberately clear an optional
        project/endpoint/provider filter during an edit instead of silently
        carrying the old value back into the subscription.
        """

        if key in form:
            return form[key]
        return previous.get(key, default)

    def _resolve_tenants(
        self, requested: list[str], *, tenant_scope: str
    ) -> tuple[list[str], tuple[str, ...]]:
        if tenant_scope == "all":
            return [], ("全部客户",)
        requested_values = list(dict.fromkeys(str(item).strip() for item in requested if str(item).strip()))
        if not requested_values:
            raise SubscriptionServiceError("请选择至少一个客户")
        if self.tenant_resolver is None:
            # The guided UI normally submits IDs obtained from the options
            # endpoint.  Accepting only the stable Cube ID here prevents a
            # hand-written display alias from becoming an unverified scope.
            if not all(_SAFE_CUBE_ID_RE.fullmatch(item) for item in requested_values):
                raise SubscriptionServiceError("Cube 客户目录当前不可用，请刷新客户选项")
            return requested_values, tuple(requested_values)
        try:
            resolved, unresolved = self.tenant_resolver(requested_values)
        except SubscriptionServiceError:
            raise
        except Exception as exc:
            raise SubscriptionServiceError("Cube 客户目录当前不可用，请稍后重试", status=503) from exc
        if not isinstance(resolved, list) or not isinstance(unresolved, list):
            raise SubscriptionServiceError("Cube 客户目录当前返回无效，请稍后重试", status=503)
        if unresolved:
            details = "、".join(
                f"{item.get('query', '客户')}（{item.get('reason', '未匹配')}）"
                for item in unresolved[:5]
            )
            raise SubscriptionServiceError(f"客户未能全部匹配：{details}", status=422)
        resolved_records = [item for item in resolved if isinstance(item, Mapping)]
        ids = [
            str(item.get("tenant_id") or item.get("tenantId") or "").strip()
            for item in resolved_records
        ]
        if (
            not ids
            or len(resolved_records) != len(resolved)
            or len(ids) != len(requested_values)
            or any(not _SAFE_CUBE_ID_RE.fullmatch(item) for item in ids)
            or len(set(ids)) != len(ids)
        ):
            raise SubscriptionServiceError(
                "Cube 客户目录未能完整验证所选客户，请刷新后重试", status=503
            )
        # A resolver may translate a display alias (for example 佛跳墙) to a
        # different stable Cube ID (for example tenant-...).  Comparing the
        # returned ID set with the requested values would reject that valid
        # translation.  Instead, verify a one-to-one response: when the
        # adapter returns ``query``, every requested value must be represented;
        # older adapters without that field are checked positionally.
        query_values = [
            str(item.get("query") or "").strip().casefold() for item in resolved_records
        ]
        ids_are_requested_values = set(ids) == set(requested_values)
        if any(query_values) and not ids_are_requested_values:
            expected_queries = {item.casefold() for item in requested_values}
            if (
                any(not value for value in query_values)
                or len(set(query_values)) != len(query_values)
                or set(query_values) != expected_queries
            ):
                raise SubscriptionServiceError(
                    "Cube 客户目录未能完整验证所选客户，请刷新后重试", status=503
                )
        names = tuple(
            str(
                item.get("display_name")
                or item.get("displayName")
                or item.get("name")
                or item.get("tenant_id")
                or item.get("tenantId")
                or ""
            ).strip()
            for item in resolved_records
        )
        if not all(names):
            raise SubscriptionServiceError("没有匹配到可订阅客户", status=422)
        return ids, names

    def _resolve_models(
        self,
        tenant_ids: list[str],
        models: list[str],
        *,
        validation_tenant_ids: list[str] | None = None,
    ) -> list[str]:
        if not models:
            return []
        if self.model_resolver is None:
            if validation_tenant_ids and not tenant_ids:
                raise SubscriptionServiceError(
                    "Cube 模型目录当前不可用，请刷新客户和模型选项", status=503
                )
            return models
        resolver_tenant_ids = list(validation_tenant_ids or tenant_ids)
        if not resolver_tenant_ids:
            # The live adapter intentionally rejects an unbounded fan-out.  A
            # guided all-customer selected-model form must first carry the
            # bounded catalog snapshot into this validation step.
            raise SubscriptionServiceError(
                "Cube 客户目录当前不可用，请刷新客户选项", status=503
            )
        try:
            resolved, unresolved = self.model_resolver(resolver_tenant_ids, models)
        except SubscriptionServiceError:
            raise
        except Exception as exc:
            raise SubscriptionServiceError("Cube 模型目录当前不可用，请稍后重试", status=503) from exc
        if not isinstance(resolved, dict) or not isinstance(unresolved, list):
            raise SubscriptionServiceError("Cube 模型目录当前返回无效，请稍后重试", status=503)
        if unresolved:
            details = "、".join(
                f"{item.get('tenant_id', '客户')} / {item.get('model', '模型')}"
                for item in unresolved[:5]
            )
            raise SubscriptionServiceError(f"模型未能全部匹配：{details}", status=422)
        # A selected model must exist for every selected tenant.  The internal
        # subscription representation keeps the union; the runner re-expands
        # per-tenant selections at execution time.  Missing tenant keys are a
        # malformed/partial resolver response, not permission to fall back to
        # the user text: silently doing so would store an unverified model.
        if any(tenant_id not in resolved for tenant_id in resolver_tenant_ids):
            raise SubscriptionServiceError(
                "Cube 模型目录未能完整验证所选模型，请刷新后重试", status=503
            )
        if any(
            not isinstance(resolved.get(tenant_id), list)
            for tenant_id in resolver_tenant_ids
        ):
            raise SubscriptionServiceError("Cube 模型目录当前返回无效，请稍后重试", status=503)
        union = list(
            dict.fromkeys(
                model
                for tenant_id in resolver_tenant_ids
                for model in resolved.get(tenant_id, [])
            )
        )
        if not union:
            raise SubscriptionServiceError(
                "Cube 模型目录未返回所选模型，请刷新后重试", status=422
            )
        return union

    def compile_form(
        self,
        form: Mapping[str, Any],
        *,
        existing: ReportSubscription | None = None,
    ) -> CompiledSubscriptionForm:
        """Validate a guided form and produce allowlisted internal parameters."""

        if not isinstance(form, Mapping):
            raise SubscriptionServiceError("subscription form must be an object")
        previous = existing.report_params if existing else {}
        template_id = _text(
            self._form_value(form, {}, "template_id", existing.template_id if existing else ""),
            "template_id",
            required=True,
            maximum=128,
        )
        template = self._template(template_id)
        channel = _text(
            self._form_value(form, {}, "channel", existing.channel if existing else ""),
            "channel",
            required=True,
            maximum=32,
        )
        if channel not in _ALLOWED_CHANNELS:
            raise SubscriptionServiceError("unsupported subscription channel")
        chat_id = _text(
            self._form_value(form, {}, "chat_id", existing.chat_id if existing else ""),
            "chat_id",
            required=True,
        )
        user_id = _text(
            self._form_value(form, {}, "user_id", existing.user_id if existing else ""),
            "user_id",
            required=True,
        )
        period = _period_for_template(
            template_id,
            set(template.manifest.periods),
            self._form_value(form, previous, "period", previous.get("subscription_period")),
        )
        recurrence = _recurrence(self._form_value(form, previous, "recurrence", "workdays"))
        send_time = _text(
            self._form_value(form, previous, "send_time", "10:00"),
            "send_time",
            maximum=5,
        )
        if not _TIME_RE.fullmatch(send_time):
            raise SubscriptionServiceError("send_time must use HH:MM")
        timezone_name = _text(
            self._form_value(
                form,
                previous,
                "timezone",
                getattr(
                    getattr(getattr(self.config, "tools", None), "reporting", self.config),
                    "timezone",
                    "Asia/Shanghai",
                ),
            ),
            "timezone",
            maximum=64,
        )
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:
            raise SubscriptionServiceError("invalid subscription timezone") from exc
        weekday = _integer(
            self._form_value(form, previous, "weekday", 1), "weekday", minimum=1, maximum=7
        )
        month_day = _integer(
            self._form_value(form, previous, "month_day", 1),
            "month_day",
            minimum=1,
            maximum=28,
        )
        inferred_tenant_scope = (
            "all"
            if previous.get("all_tenants") is True
            else "selected"
            if previous.get("tenant_query") or previous.get("tenants")
            else "selected"
        )
        tenant_scope = _text(
            self._form_value(form, previous, "tenant_scope", inferred_tenant_scope),
            "tenant_scope",
            maximum=16,
        )
        if tenant_scope not in {"all", "selected"}:
            raise SubscriptionServiceError("tenant_scope must be all or selected")
        if "tenants" in form:
            tenant_input = form["tenants"]
        elif "tenant_aliases" in form:
            tenant_input = form["tenant_aliases"]
        else:
            tenant_input = previous.get("tenants")
            if tenant_input is None and previous.get("tenant_query"):
                tenant_input = [previous["tenant_query"]]
        requested_tenants = _values(tenant_input, "tenants")
        tenant_ids, tenant_names = self._resolve_tenants(
            requested_tenants, tenant_scope=tenant_scope
        )
        inferred_model_scope = str(previous.get("model_scope") or "").strip()
        if not inferred_model_scope:
            inferred_model_scope = (
                "selected"
                if previous.get("model") or previous.get("models")
                else "summary"
            )
        model_scope = _text(
            self._form_value(form, previous, "model_scope", inferred_model_scope),
            "model_scope",
            maximum=16,
        )
        if model_scope not in {"all", "selected", "summary"}:
            raise SubscriptionServiceError("model_scope must be all, selected, or summary")
        if "models" in form:
            model_input = form["models"]
        else:
            model_input = previous.get("models")
            if model_input is None and previous.get("model"):
                model_input = [previous["model"]]
        requested_models = _values(model_input, "models")
        if model_scope == "selected" and not requested_models:
            raise SubscriptionServiceError("请选择至少一个模型")
        if model_scope != "selected":
            requested_models = []
        validation_tenant_ids = tenant_ids
        if tenant_scope == "all" and model_scope == "selected":
            validation_tenant_ids = list(
                dict.fromkeys(
                    str(item.get("tenant_id") or item.get("tenantId") or "").strip()
                    for item in self.catalog_tenants
                    if str(item.get("tenant_id") or item.get("tenantId") or "").strip()
                )
            )
            if not validation_tenant_ids:
                raise SubscriptionServiceError(
                    "Cube 客户目录当前不可用，请刷新客户和模型选项", status=503
                )
        requested_models = self._resolve_models(
            tenant_ids,
            requested_models,
            validation_tenant_ids=validation_tenant_ids,
        )

        schedule = build_subscription_schedule(
            _schedule_period(recurrence),
            send_time=send_time,
            daily_mode="workdays" if recurrence == "workdays" else "every_day",
            weekday=weekday,
            month_day=month_day,
        )
        connector_id = next(iter(template.manifest.connector_ids), "magik_cube")
        if template_id.endswith("_brief"):
            report_template = "brief"
        elif template_id.endswith("_matrix"):
            report_template = "matrix_card"
        else:
            report_template = "full"
        params: dict[str, Any] = {
            "report_family": "usage" if template.manifest.category == "usage" else template.manifest.category,
            "report_template": report_template,
            "subscription_period": period,
            "tenant_scope": tenant_scope,
            "all_tenants": tenant_scope == "all",
            "tenants": tenant_ids,
            "tenant_labels": list(tenant_names),
            "model_scope": model_scope,
            "models": requested_models,
            "breakdown": "model" if model_scope in {"all", "selected"} else "summary",
            "report_selections": [
                {
                    "tenant_query": tenant_id,
                    "model_scope": model_scope,
                    "models": requested_models if model_scope == "selected" else [],
                }
                for tenant_id in tenant_ids
            ],
        }
        if template_id == "usage_customer_model_daily_brief":
            params.update(
                {
                    "report_variant": "customer_model_daily_brief",
                    "multi_scope": True,
                    "report_template": "brief",
                }
            )
        if len(tenant_ids) == 1:
            params["tenant_query"] = tenant_ids[0]
        for key in ("project", "endpoint", "provider", "cluster"):
            value = _text(self._form_value(form, previous, key, ""), key, maximum=128)
            if value:
                params[key] = value
        params["save_snapshot"] = False
        unknown = set(params) - _SAFE_REPORT_PARAM_KEYS - {"save_snapshot"}
        if unknown:
            raise SubscriptionServiceError("unsupported report subscription parameters")
        return CompiledSubscriptionForm(
            template_id=template_id,
            connector_id=connector_id,
            template_version=template.manifest.version,
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            period=period,
            recurrence=recurrence,
            send_time=send_time,
            timezone=timezone_name,
            weekday=weekday,
            month_day=month_day,
            schedule=schedule,
            report_params=params,
            tenant_names=tenant_names,
            models=tuple(requested_models),
        )

    def preview(self, form: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a form without creating a Cron job or database row."""

        compiled = self.compile_form(form)
        self._check_template_policy(
            compiled.template_id,
            channel=compiled.channel,
            user_id=compiled.user_id,
        )
        self._authorize_compiled_scope(compiled)
        return {
            "form": compiled.to_form(),
            "schedule_label": compiled.schedule_label,
            "template_id": compiled.template_id,
            "template_version": compiled.template_version,
        }

    @staticmethod
    def _fingerprint(compiled: CompiledSubscriptionForm) -> str:
        value = [
            compiled.channel,
            compiled.chat_id,
            compiled.user_id,
            compiled.template_id,
            compiled.schedule,
            compiled.timezone,
            compiled.report_params,
        ]
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

    def _new_job(self, compiled: CompiledSubscriptionForm, subscription_id: str):
        direct_tool = {
            "name": "report_center",
            "params": {"action": "run_subscription", "subscription_id": subscription_id},
        }
        return self.cron.add_job(
            name=f"{compiled.tenant_names[0] if len(compiled.tenant_names) == 1 else 'Cube'} {compiled.template_id}",
            schedule=CronSchedule(kind="cron", expr=compiled.schedule, tz=compiled.timezone),
            message="执行固定报表订阅",
            session_key=session_key_for_channel(
                compiled.channel,
                compiled.chat_id,
                unified_session=getattr(
                    getattr(getattr(self.config, "agents", None), "defaults", None),
                    "unified_session",
                    False,
                ),
            ),
            origin_channel=compiled.channel,
            origin_chat_id=compiled.chat_id,
            origin_metadata={
                INBOUND_META_DIRECT_TOOL: direct_tool,
                "direct_request_text": "执行固定报表订阅",
            },
        )

    def _remove_created_job(self, job_id: str) -> None:
        """Remove a just-created Cron job and fail loudly if compensation fails."""

        try:
            result = self.cron.remove_job(job_id)
        except Exception as exc:
            logger.error(
                "Failed to remove orphan Cron job: job_id={} error_type={}",
                job_id,
                type(exc).__name__,
            )
            raise SubscriptionServiceError(
                "订阅保存失败且 Cron 清理失败，请检查订阅状态", status=409
            ) from exc
        if result not in {"removed", "not_found"}:
            logger.error(
                "Cron rejected orphan subscription cleanup: job_id={} result={}",
                job_id,
                result,
            )
            raise SubscriptionServiceError(
                "订阅保存失败且 Cron 清理被拒绝，请检查订阅状态", status=409
            )

    def create(
        self, form: Mapping[str, Any], *, updated_by: str = "webui_admin"
    ) -> ReportSubscription:
        """Create one guided subscription after policy and scope validation."""

        compiled = self.compile_form(form)
        self._check_template_policy(
            compiled.template_id,
            channel=compiled.channel,
            user_id=compiled.user_id,
        )
        self._authorize_compiled_scope(compiled)
        subscription_id = uuid.uuid4().hex[:16]
        job = self._new_job(compiled, subscription_id)
        now = datetime.now(UTC).isoformat()
        subscription = ReportSubscription(
            subscription_id=subscription_id,
            channel=compiled.channel,
            chat_id=compiled.chat_id,
            user_id=compiled.user_id,
            connector_id=compiled.connector_id,
            template_id=compiled.template_id,
            template_version=compiled.template_version,
            schedule=compiled.schedule,
            timezone=compiled.timezone,
            report_params=compiled.report_params,
            cron_job_id=job.id,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        try:
            added = self.store.add_subscription(subscription, self._fingerprint(compiled))
        except Exception as exc:
            # Cron is deliberately created first so a persisted subscription
            # always has an executable job.  If persistence fails, compensate
            # the side effect and surface a bounded control-plane error.
            self._remove_created_job(job.id)
            raise SubscriptionServiceError(
                "订阅保存失败，未创建订阅", status=503
            ) from exc
        if not added:
            self._remove_created_job(job.id)
            raise SubscriptionServiceError(
                "an identical report subscription already exists", status=409
            )
        self.store.record_admin_audit(
            action="subscription_create",
            target_type="subscription",
            target_id=subscription_id,
            before_summary={},
            after_summary={"template_id": compiled.template_id, "enabled": True},
            updated_by=updated_by[:256] or "webui_admin",
        )
        return subscription

    def update(
        self,
        subscription_id: str,
        form: Mapping[str, Any],
        *,
        expected_revision: int,
        updated_by: str = "webui_admin",
    ) -> ReportSubscription:
        """Replace editable fields atomically with Cron/database compensation."""

        current = self.store.subscription(subscription_id)
        if current is None:
            raise SubscriptionServiceError("report subscription not found", status=404)
        if current.revision != expected_revision:
            raise SubscriptionServiceError("subscription was updated by another operator", status=409)
        compiled = self.compile_form(form, existing=current)
        self._check_template_policy(
            compiled.template_id,
            channel=compiled.channel,
            user_id=compiled.user_id,
        )
        self._authorize_compiled_scope(compiled)
        job = self.cron.get_job(current.cron_job_id)
        if job is None:
            raise SubscriptionServiceError("subscription Cron job was not found", status=409)
        old_schedule = job.schedule
        old_name = job.name
        old_session = job.payload.session_key
        old_origin_channel = job.payload.origin_channel
        old_origin_chat = job.payload.origin_chat_id
        old_origin_metadata = dict(job.payload.origin_metadata)
        updated_job = self.cron.update_job(
            current.cron_job_id,
            name=f"{compiled.tenant_names[0] if len(compiled.tenant_names) == 1 else 'Cube'} {compiled.template_id}",
            schedule=CronSchedule(kind="cron", expr=compiled.schedule, tz=compiled.timezone),
            session_key=session_key_for_channel(
                compiled.channel,
                compiled.chat_id,
                unified_session=getattr(
                    getattr(getattr(self.config, "agents", None), "defaults", None),
                    "unified_session",
                    False,
                ),
            ),
            origin_channel=compiled.channel,
            origin_chat_id=compiled.chat_id,
            origin_metadata={
                INBOUND_META_DIRECT_TOOL: {
                    "name": "report_center",
                    "params": {"action": "run_subscription", "subscription_id": subscription_id},
                },
                "direct_request_text": "执行固定报表订阅",
            },
        )
        if isinstance(updated_job, str):
            raise SubscriptionServiceError("subscription Cron job cannot be updated", status=409)
        try:
            updated = self.store.update_subscription(
                subscription_id,
                channel=compiled.channel,
                chat_id=compiled.chat_id,
                user_id=compiled.user_id,
                connector_id=compiled.connector_id,
                template_id=compiled.template_id,
                template_version=compiled.template_version,
                schedule=compiled.schedule,
                timezone_name=compiled.timezone,
                report_params=compiled.report_params,
                fingerprint=self._fingerprint(compiled),
                expected_revision=expected_revision,
            )
        except Exception:
            self._restore_job(
                current.cron_job_id,
                old_schedule,
                old_name,
                old_session,
                old_origin_channel,
                old_origin_chat,
                old_origin_metadata,
            )
            raise
        if updated is None:
            self._restore_job(
                current.cron_job_id,
                old_schedule,
                old_name,
                old_session,
                old_origin_channel,
                old_origin_chat,
                old_origin_metadata,
            )
            raise SubscriptionServiceError("subscription was updated by another operator", status=409)
        self.store.record_admin_audit(
            action="subscription_update",
            target_type="subscription",
            target_id=subscription_id,
            before_summary={"template_id": current.template_id, "revision": current.revision},
            after_summary={"template_id": updated.template_id, "revision": updated.revision},
            updated_by=updated_by[:256] or "webui_admin",
        )
        return updated

    def _restore_job(
        self,
        job_id: str,
        schedule: CronSchedule,
        name: str,
        session_key: str | None,
        origin_channel: str | None,
        origin_chat_id: str | None,
        origin_metadata: dict[str, Any],
    ) -> None:
        """Best-effort compensation; the original database row remains authoritative."""

        restored = self.cron.update_job(
            job_id,
            name=name,
            schedule=schedule,
            session_key=session_key,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            origin_metadata=origin_metadata,
        )
        if isinstance(restored, str):
            logger.error(
                "Failed to restore Cron job after subscription mutation: job_id={} result={}",
                job_id,
                restored,
            )
            raise SubscriptionServiceError(
                "订阅状态未能恢复，请检查 Cron 与订阅记录", status=409
            )

    def set_enabled(
        self,
        subscription_id: str,
        *,
        enabled: bool,
        expected_revision: int,
        updated_by: str = "webui_admin",
    ) -> ReportSubscription:
        current = self.store.subscription(subscription_id)
        if current is None:
            raise SubscriptionServiceError("report subscription not found", status=404)
        if current.revision != expected_revision:
            raise SubscriptionServiceError("subscription was updated by another operator", status=409)
        job = self.cron.enable_job(current.cron_job_id, enabled)
        if job is None:
            raise SubscriptionServiceError("subscription Cron job was not found", status=409)
        try:
            updated = self.store.set_subscription_enabled(
                subscription_id,
                channel=current.channel,
                user_id=current.user_id,
                enabled=enabled,
                expected_revision=expected_revision,
            )
        except Exception as exc:
            self._restore_enabled_state(current.cron_job_id, current.enabled)
            raise SubscriptionServiceError(
                "订阅状态保存失败，已恢复原计划", status=503
            ) from exc
        if updated is None:
            self._restore_enabled_state(current.cron_job_id, current.enabled)
            raise SubscriptionServiceError("subscription was updated by another operator", status=409)
        self.store.record_admin_audit(
            action="subscription_enable" if enabled else "subscription_disable",
            target_type="subscription",
            target_id=subscription_id,
            before_summary={"enabled": current.enabled, "revision": current.revision},
            after_summary={"enabled": enabled, "revision": updated.revision},
            updated_by=updated_by[:256] or "webui_admin",
        )
        return updated

    def _restore_enabled_state(self, job_id: str, enabled: bool) -> None:
        """Compensate a Cron toggle and surface inconsistency if it fails.

        The database CAS is authoritative only after the scheduler mutation is
        committed.  Ignoring a failed rollback would leave a visible
        subscription row and the actual Cron execution state disagreeing.
        """

        try:
            restored = self.cron.enable_job(job_id, enabled)
        except Exception as exc:
            logger.error(
                "Failed to restore Cron enabled state: job_id={} error_type={}",
                job_id,
                type(exc).__name__,
            )
            raise SubscriptionServiceError(
                "订阅状态保存失败且 Cron 恢复失败，请检查订阅状态", status=409
            ) from exc
        if restored is None:
            raise SubscriptionServiceError(
                "订阅状态保存失败且 Cron 恢复失败，请检查订阅状态", status=409
            )

    def delete(
        self,
        subscription_id: str,
        *,
        expected_revision: int,
        updated_by: str = "webui_admin",
    ) -> None:
        current = self.store.subscription(subscription_id)
        if current is None:
            raise SubscriptionServiceError("report subscription not found", status=404)
        if current.revision != expected_revision:
            raise SubscriptionServiceError("subscription was updated by another operator", status=409)
        job = self.cron.get_job(current.cron_job_id)
        if job is None:
            raise SubscriptionServiceError("subscription Cron job is missing or protected", status=409)
        job_snapshot = deepcopy(job)
        result = self.cron.remove_job(current.cron_job_id)
        if result != "removed":
            raise SubscriptionServiceError("subscription Cron job is missing or protected", status=409)
        try:
            removed = self.store.remove_subscription(
                subscription_id,
                channel=current.channel,
                user_id=current.user_id,
                expected_revision=expected_revision,
            )
        except Exception:
            self._restore_deleted_job(job_snapshot)
            raise
        if not removed:
            self._restore_deleted_job(job_snapshot)
            raise SubscriptionServiceError("subscription was updated by another operator", status=409)
        self.store.record_admin_audit(
            action="subscription_delete",
            target_type="subscription",
            target_id=subscription_id,
            before_summary={"template_id": current.template_id, "revision": current.revision},
            after_summary={"deleted": True},
            updated_by=updated_by[:256] or "webui_admin",
        )

    def _restore_deleted_job(self, job: Any) -> None:
        """Compensate a Cron-first delete when the database mutation did not commit."""

        restore = getattr(self.cron, "restore_job", None)
        if not callable(restore):
            # Older test doubles/embedded Cron adapters do not expose the new
            # primitive.  Keep the failure explicit rather than pretending the
            # scheduler and database are consistent.
            raise SubscriptionServiceError(
                "subscription state changed but the Cron job could not be restored",
                status=409,
            )
        try:
            result = restore(job)
        except Exception as exc:
            logger.error(
                "Failed to restore Cron job after subscription delete failure: error_type={}",
                type(exc).__name__,
            )
            raise SubscriptionServiceError(
                "subscription state changed and Cron recovery failed; inspect the subscription",
                status=409,
            ) from exc
        if result not in {"restored", "already_present"}:
            raise SubscriptionServiceError(
                "subscription state changed and Cron recovery was rejected",
                status=409,
            )
