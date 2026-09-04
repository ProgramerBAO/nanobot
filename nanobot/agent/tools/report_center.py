"""Deterministic report capability home, history, and subscription control."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal
from zoneinfo import ZoneInfo

from loguru import logger
from pydantic import Field, field_validator

from nanobot.agent.reporting.cube_subscription_intent import (
    CubeSubscriptionIntent,
    classify_subscription_intent,
    is_subscription_intent_candidate,
    parse_deterministic_subscription_intent,
)
from nanobot.agent.reporting.magik_cube_intent import ReportIntent as MagikReportIntent
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.bus.events import (
    INBOUND_META_DIRECT_TOOL,
    OUTBOUND_META_AGENT_UI,
    OUTBOUND_META_REPORT_DELIVERY,
    OUTBOUND_META_REPORT_REFERENCE,
)
from nanobot.config_base import Base
from nanobot.cron.session_turns import CRON_TRIGGER_META
from nanobot.cron.types import CronSchedule
from nanobot.reporting import (
    CubeConnector,
    CubeProviderQualityConnector,
    ReportDocument,
    ReportIntent,
    ReportRunContext,
    ReportRunner,
    build_default_registry,
)
from nanobot.reporting.authorization import authorize_magik_params
from nanobot.reporting.capabilities import (
    ONBOARDING_VERSION,
    examples_document,
    home_document,
    recent_document,
    subscription_created_document,
    subscriptions_document,
)
from nanobot.reporting.contracts import ReportBlock
from nanobot.reporting.cube import normalize_health_thresholds
from nanobot.reporting.interactions import report_interactions
from nanobot.reporting.provider_quality import provider_quality_selector_document
from nanobot.reporting.schedules import build_subscription_schedule
from nanobot.reporting.store import ReportSubscription, get_report_state_store
from nanobot.reporting.subscriptions import (
    ReportSubscriptionService,
    SubscriptionServiceError,
)
from nanobot.utils.report_failures import is_transient_report_failure

_HOME_RE = re.compile(
    r"^(?:请)?(?:打开|显示|查看|进入)?(?:报表中心|报表菜单|功能菜单|菜单|帮助|你能做什么|你会什么|有哪些功能)[？?。！!]*$"
)
_RECENT_RE = re.compile(r"^(?:查看|打开|显示)?(?:我的)?最近报表[？?。！!]*$")
_SUBSCRIPTIONS_RE = re.compile(r"^(?:查看|打开|显示|管理)?(?:我的)?(?:报表)?订阅[？?。！!]*$")
_SUBSCRIPTION_CONTROL_RE = re.compile(
    r"^(?P<operation>启用|停用)订阅：(?P<subscription_id>[0-9a-f]{1,64})$"
)
_BRIEF_SUBSCRIPTION_RE = re.compile(
    r"^订阅(?P<period>日报|周报|月报)简报：客户 (?P<tenant>[^，（]{1,128})"
    r"(?:（ID (?P<tenant_id>[^）]{1,128})）)?，模型 (?P<models>[^，]{1,512})$"
)
_EXPLICIT_ALL_TENANTS_RE = re.compile(
    r"(?:全部|所有|全量|各个|每个|全体)\s*(?:客户|租户|用户)",
    re.IGNORECASE,
)
_EXPLICIT_ALL_MODELS_RE = re.compile(
    r"(?:全部|所有|全量|各个|每个|全体)\s*模型",
    re.IGNORECASE,
)
# A quoted report owns its verified scope.  Only these explicit entity words
# indicate that the user is asking to override that scope; schedule/recipient
# wording such as “工作日上午十点发送给我” must never make the classifier pick
# a new default tenant or template.  Keeping this boundary server-side avoids
# turning an LLM omission into a silently narrowed subscription.
_REFERENCE_SCOPE_OVERRIDE_RE = re.compile(
    r"(?:全部|所有|全量|指定|仅|只|改为|换成|换为)?\s*"
    r"(?:客户|租户|用户|模型|endpoint|项目|供应商)",
    re.IGNORECASE,
)
_HEALTH_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成|打开|显示)?(?:过去\s*15\s*分钟|近\s*15\s*分钟)?(?:平台)?健康(?:报告|情况)?[？?。！!]*$"
)
_COST_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成|打开|显示)?(?:成本|费用|账单|余额|账户)(?:报告|情况|概览)?[？?。！!]*$"
)
_PROVIDER_QUALITY_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成|打开|显示)?\s*"
    r"(?:(?:过去\s*15\s*分钟|近\s*15\s*分钟|昨天)\s*)?"
    r"(?:各\s*)?"
    r"(?:供应商质量|供应商性能|供应商详细情况|平台供应商质量)"
    r"(?:报告|情况|排行|对比)?[？?。！!]*$",
    re.IGNORECASE,
)
_NAMED_PROVIDER_QUALITY_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成|打开|显示)?\s*"
    r"供应商\s*(?P<provider>[A-Za-z0-9._-]+)\s*(?:的)?\s*"
    r"(?:质量|性能|详细情况)(?:报告|情况|排行|对比)?[？?。！!]*$",
    re.IGNORECASE,
)
_MODEL_PROVIDER_QUALITY_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成|打开|显示)?\s*"
    r"(?P<model>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:模型)?\s*"
    r"各供应商(?:质量|性能)(?:报告|情况|排行|对比)?[？?。！!]*$",
    re.IGNORECASE,
)
_PROVIDER_QUALITY_EMPTY_RE = re.compile(
    r"^查看(?:本次)?(?P<period>近\s*15\s*分钟|昨日|上一完整周|"
    r"自定义区间\s+(?P<start_date>\d{4}-\d{2}-\d{2})\s+至\s+(?P<end_date>\d{4}-\d{2}-\d{2}))"
    r"供应商无用量[？?。！!]*$"
)
_CUBE_PERIOD_RE = re.compile(
    r"^(?:请)?(?:我要|生成|查看|打开|显示)?\s*"
    r"(?P<template>简报|详细|完整)?(?P<period>日报|周报|月报)[？?。！!]*$"
)
_MODEL_CUBE_PERIOD_RE = re.compile(
    r"^(?:请)?(?:我要|我需要|给我|生成|查看|打开|显示)?\s*"
    r"(?P<model>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:模型)?\s*(?:的)?\s*"
    r"(?P<template>简报|详细|完整)?(?P<period>日报|周报|月报)\s*"
    r"(?:全部|所有|全体)?\s*(?:客户|用户|租户)?[？?。！!]*$",
    re.IGNORECASE,
)
_MULTI_SCOPE_BRIEF_RE = re.compile(
    r"^(?:请)?(?:打开|查看|生成)?(?:Cube\s*)?多客户多模型(?:日报)?简报[？?。！!]*$",
    re.IGNORECASE,
)
_MACHINE_TPM_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成)?\s*(?P<model>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"\s*(?:模型)?(?:的)?(?:单机|每台机器)\s*(?:折算)?\s*TPM"
    r"\s*(?:峰值)?\s*(?:报表)?[？?。！!]*$",
    re.IGNORECASE,
)
_FURTHER_ANALYSIS_RE = re.compile(
    r"^进一步分析（(?P<period>日报|周报|月报|区间报表)）："
    r"客户 (?P<tenant>[^，]{1,128})，模型 (?P<models>[^，]{1,512})，"
    r"日期 (?P<start>\d{4}-\d{2}-\d{2}) 至 (?P<end>\d{4}-\d{2}-\d{2})$"
)
_SAFE_CUBE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_ALLOWED_REPORT_PARAM_KEYS = frozenset(
    {
        "tenant_query",
        "tenants",
        "tenant_labels",
        "model",
        "models",
        "model_scope",
        "project",
        "endpoint",
        "provider",
        "all_tenants",
        "breakdown",
        "report_template",
        "granularity",
        "include_tpm",
        "report_selections",
        "comparison",
        "report_family",
        "subscription_period",
        "provider_id",
        "providers",
        "cluster",
        "report_variant",
        "tenant_scope",
    }
)
_PERIOD_TEMPLATES: dict[str, str] = {
    "day": "usage_daily_matrix",
    "week": "usage_weekly_matrix",
    "month": "usage_monthly_matrix",
    "recent7": "usage_custom_matrix",
    "range": "usage_custom_matrix",
}
_BRIEF_PERIOD_TEMPLATES: dict[str, str] = {
    "day": "usage_daily_brief",
    "week": "usage_weekly_brief",
    "month": "usage_monthly_brief",
    "recent7": "usage_custom_brief",
    "range": "usage_custom_brief",
}


@dataclass(frozen=True, slots=True)
class _TenantMentionResolution:
    """Result of reconciling natural-language tenant names with live Cube IDs.

    ``values`` contains only labels that can be resolved back to one live
    tenant.  ``error`` is intentionally a small machine-readable category so
    the caller can choose a recovery UI without exposing catalog responses or
    silently narrowing a requested multi-tenant scope.
    """

    values: tuple[str, ...]
    error: str | None = None
    unresolved: tuple[str, ...] = ()


class ReportCenterToolConfig(Base):
    enable: bool = True
    # Cube is the only production report path in this phase; the Magik tool
    # remains a separate compatibility entry point when its own flag is on.
    cube_connector: bool = True
    cube_template: bool = True
    cube_report_runner: bool = True
    cube_subscription: bool = True
    # Natural-language parsing is bounded to one schema-forced LLM call. Job
    # creation remains behind a server-validated confirmation action.
    cube_subscription_nlu_v2: bool = True
    # V3 adds route preemption and live-catalog scope reconciliation. V2 remains
    # the compatibility gate so existing deployments receive the safety fix
    # without requiring a configuration migration.
    cube_subscription_nlu_v3: bool = False
    cube_report_reference_subscription: bool = True
    cube_subscription_nlu_timeout_seconds: float = Field(default=3.0, ge=0.5, le=10.0)
    cube_report_reference_retention_days: int = Field(default=30, ge=1, le=90)
    # Health flags are deliberately opt-in; registering Cube usage does not
    # expose health data until all corresponding release gates are enabled.
    cube_health_connector: bool = False
    cube_health_template: bool = False
    cube_health_report: bool = False
    cube_health_subscription: bool = False
    cube_health_semantics_v2: bool = False
    cube_health_card_v2: bool = False
    cube_ttft_detail: bool = False
    cube_usage_semantics_v2: bool = False
    # Brief templates share the v2 metric semantics. The default route can be
    # disabled independently to restore existing matrix cards immediately.
    cube_usage_brief_template: bool = True
    cube_usage_brief_default: bool = True
    # These wider-scope capabilities are independently gated because they can
    # fan out across tenants or expose platform-level capacity information.
    cube_multi_scope_brief: bool = False
    cube_machine_tpm_report: bool = False
    report_management_v1: bool = False
    # Guided WebUI subscription editing and result-card policy are independent
    # rollout gates. The legacy settings endpoint remains available while the
    # guided surface is disabled.
    report_subscription_guided_ui: bool = False
    report_subscription_button_policy: bool = False
    cube_admin_skill_help: bool = True
    # Cost/account reports need both an enabled template and an independently
    # configured TokenAPI credential. The Admin JWT is never used as fallback.
    cube_cost_connector: bool = False
    cube_cost_template: bool = False
    cube_cost_report: bool = False
    cube_cost_subscription: bool = False
    cube_provider_quality_connector: bool = False
    cube_provider_quality_template: bool = False
    cube_provider_quality_report: bool = False
    cube_provider_quality_detail: bool = False
    cube_provider_quality_subscription: bool = False
    cube_provider_quality_selector: bool = True
    cube_provider_quality_empty_collapse: bool = True
    cube_scope_selector_v2: bool = False
    cube_transient_run_retry: bool = False
    cube_transient_retry_delay_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    # Cross-customer model reports can fan out across the full Cube catalog.
    # They stay disabled until an operator explicitly enables this feature.
    cube_model_all_tenant_report: bool = False
    # Shadow mode compares legacy/v2 calculations on the same normalized dataset
    # and persists only changed metric IDs, never values or raw Cube responses.
    cube_semantics_shadow: bool = False
    health_thresholds: dict[str, dict[str, float]] = Field(default_factory=dict)
    # Extension declarations are opt-in. They do not configure or contact a
    # real Grafana, WeCom, or DingTalk service by themselves.
    grafana_connector: bool = False
    wecom_renderer: bool = False
    dingtalk_renderer: bool = False
    onboarding_version: int = ONBOARDING_VERSION
    timezone: str = "Asia/Shanghai"
    rbac_enforced: bool = False
    run_retention_days: int = 30
    state_backend: Literal["sqlite", "postgresql"] = "sqlite"
    postgres_dsn_env: str = "NANOBOT_REPORTING_POSTGRES_DSN"
    # Grafana expressions are deployment-owned query definitions. Secrets must
    # be supplied as SecretRef objects, for example {"provider": "env", "key": "..."}.
    grafana: dict[str, Any] = Field(default_factory=dict)

    @field_validator("grafana")
    @classmethod
    def _require_grafana_secret_ref(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key in ("service_account_token", "serviceAccountToken"):
            secret = value.get(key)
            if isinstance(secret, str) and secret.strip():
                raise ValueError("Grafana service account credentials must use SecretRef")
        return value

    @field_validator("health_thresholds")
    @classmethod
    def _validate_health_thresholds(
        cls, value: dict[str, dict[str, float]]
    ) -> dict[str, dict[str, float]]:
        return normalize_health_thresholds(value)


_REPORT_CENTER_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "home",
                "cube_report",
                "health_report",
                "cost_report",
                "provider_quality_report",
                "multi_scope_brief",
                "machine_tpm_report",
                "examples",
                "recent",
                "subscriptions",
                "subscription_setup",
                "subscription_preview",
                "subscription_reference_missing",
                "subscription_parse_failed",
                "subscription_scope_failed",
                "subscribe",
                "subscription_enable",
                "subscription_disable",
                "subscription_remove",
                "run_subscription",
                "request_access",
            ],
        },
        "period": {"type": "string", "enum": ["day", "week", "month", "recent7", "recent15m", "range"]},
        "report_family": {
            "type": "string",
            "enum": ["usage", "health", "cost", "provider_quality", "capacity"],
        },
        "report_template": {
            "type": "string",
            "enum": ["brief", "matrix_card", "full"],
        },
        "report_type": {
            "type": "string",
            "enum": [
                "usage_daily_brief",
                "usage_weekly_brief",
                "usage_monthly_brief",
                "usage_customer_model_daily_brief",
                "inherit",
            ],
        },
        "tenant_scope": {"type": "string", "enum": ["selected", "all", "inherit"]},
        "tenant_aliases": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
            "maxItems": 20,
        },
        "model_scope": {
            "type": "string",
            "enum": ["summary", "all", "selected", "inherit"],
        },
        "inherit_report_scope": {"type": "boolean"},
        "reference_message_id": {"type": "string", "maxLength": 128},
        "catalog_unavailable": {"type": "boolean"},
        "scope_unresolved": {"type": "boolean"},
        "tenant_ambiguous": {"type": "boolean"},
        "subscription_error": {
            "type": "string",
            "enum": [
                "nlu_unavailable",
                "classifier_disabled",
                "catalog_unavailable",
                "tenant_ambiguous",
                "scope_unresolved",
                "reference_not_found_or_expired",
            ],
        },
        "unresolved_tenants": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
            "maxItems": 20,
        },
        "recurrence": {
            "type": "string",
            "enum": ["every_day", "workdays", "weekly", "monthly"],
        },
        "tenant_query": {"type": "string", "maxLength": 128},
        "tenants": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
            "maxItems": 20,
        },
        "project": {"type": "string", "maxLength": 128},
        "model": {"type": "string", "maxLength": 128},
        "models": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
            "maxItems": 20,
        },
        "report_selections": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "tenant_query": {"type": "string", "maxLength": 128},
                    "model_scope": {"type": "string", "enum": ["summary", "all", "selected"]},
                    "models": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 128},
                        "maxItems": 20,
                    },
                },
                "required": ["tenant_query", "model_scope", "models"],
                "additionalProperties": False,
            },
        },
        "breakdown": {"type": "string", "enum": ["summary", "model"]},
        "endpoint": {"type": "string", "maxLength": 128},
        "provider": {"type": "string", "maxLength": 128},
        "provider_id": {"type": "string", "maxLength": 64},
        "cluster": {"type": "string", "maxLength": 128},
        "providers": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
            "maxItems": 50,
        },
        "selection_confirmed": {"type": "boolean"},
        "include_empty": {"type": "boolean"},
        "start_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
        "end_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
        "interactive": {"type": "boolean"},
        "all_tenants": {"type": "boolean"},
        "send_time": {"type": "string", "maxLength": 5},
        "daily_mode": {"type": "string", "enum": ["workdays", "every_day"]},
        "weekday": {"type": "integer", "minimum": 1, "maximum": 7},
        "month_day": {"type": "integer", "minimum": 1, "maximum": 28},
        "report_params": {"type": "object"},
        "subscription_id": {"type": "string", "maxLength": 64},
        # Modern card actions carry the row revision so a stale enable/disable
        # or delete button cannot overwrite a newer subscription state.  It is
        # optional for the legacy text command during the migration window.
        "revision": {"type": "integer", "minimum": 0},
    },
    "required": ["action"],
    "additionalProperties": False,
}


@tool_parameters(_REPORT_CENTER_PARAMETERS)
class ReportCenterTool(Tool):
    """Render the report center and manage deterministic report subscriptions."""

    config_key = "reporting"

    def __init__(
        self,
        config: ReportCenterToolConfig,
        cron_service: Any,
        magik_tool: Tool | None,
        cube_config: Any | None = None,
    ):
        self._config = config
        self._cron = cron_service
        self._magik_tool = magik_tool
        self._cube_config = cube_config
        self._store = get_report_state_store(
            backend=config.state_backend,
            postgres_dsn_env=config.postgres_dsn_env,
        )
        self._registry = build_default_registry(
            magik_enabled=magik_tool is not None and config.cube_connector,
            grafana_config=(
                getattr(config, "grafana", None)
                if config.grafana_connector
                else None
            ),
            cube_config=cube_config,
            cube_templates_enabled=config.cube_template,
            cube_health_template_enabled=(
                config.cube_health_connector and config.cube_health_template
            ),
            cube_health_semantics_v2=config.cube_health_semantics_v2,
            cube_health_card_v2=config.cube_health_card_v2,
            cube_ttft_detail_enabled=config.cube_ttft_detail,
            cube_usage_semantics_v2=config.cube_usage_semantics_v2,
            cube_usage_brief_template_enabled=config.cube_usage_brief_template,
            cube_multi_scope_brief_enabled=config.cube_multi_scope_brief,
            cube_machine_tpm_template_enabled=config.cube_machine_tpm_report,
            cube_cost_template_enabled=(config.cube_cost_connector and config.cube_cost_template),
            cube_provider_quality_connector_enabled=config.cube_provider_quality_connector,
            cube_provider_quality_template_enabled=(
                config.cube_provider_quality_connector and config.cube_provider_quality_template
            ),
            cube_provider_quality_detail_enabled=config.cube_provider_quality_detail,
            timezone=config.timezone,
            health_thresholds=config.health_thresholds,
            wecom_renderer_enabled=config.wecom_renderer,
            dingtalk_renderer_enabled=config.dingtalk_renderer,
        )
        if config.rbac_enforced:
            self._store.set_rbac_enabled(True)

    @classmethod
    def config_cls(cls):
        return ReportCenterToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return bool(ctx.config.reporting.enable)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        magik_tool: Tool | None = None
        if getattr(ctx.config.magik_cube, "enable", False):
            from nanobot.agent.tools.magik_cube import MagikCubeDailyReportTool

            magik_tool = MagikCubeDailyReportTool.create(ctx)
        return cls(ctx.config.reporting, ctx.cron_service, magik_tool, ctx.config.magik_cube)

    @property
    def name(self) -> str:
        return "report_center"

    @property
    def description(self) -> str:
        return (
            "Open the deterministic report center, show recent reports or subscriptions, "
            "generate Cube reports, and manage report subscriptions."
        )

    @property
    def trusted_direct(self) -> bool:
        return True

    @property
    def fixed_cube_reports_enabled(self) -> bool:
        """Only prefer the new route after a real Cube connector is constructed."""

        return bool(
            self._config.cube_report_runner
            and isinstance(self._registry.connector("magik_cube"), CubeConnector)
        )

    @property
    def max_calls_per_turn(self) -> int | None:
        return 1

    def is_direct_intent_candidate(self, text: str) -> bool:
        """Use one schema-forced LLM call only for subscription-like language."""

        return bool(
            (self._config.cube_subscription_nlu_v2 or self._config.cube_subscription_nlu_v3)
            and self._config.cube_subscription
            and is_subscription_intent_candidate(text)
        )

    async def _load_catalog_tenant_mentions(
        self, text: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """Load only live catalog records that can be used to verify text mentions.

        The direct subscription path must fail closed when the catalog cannot
        be read.  A classifier result is not an identity proof, so retaining it
        after a failed scan would recreate the original one-customer-loss bug
        and could also turn an arbitrary phrase into a tenant selector.
        """

        finder = getattr(self._magik_tool, "find_tenant_mentions", None)
        loader = getattr(self._magik_tool, "list_tenant_catalog", None)
        if callable(finder):
            try:
                found = await finder(text, limit=20)
                if isinstance(found, list):
                    return [item for item in found if isinstance(item, dict)], True
            except Exception as exc:
                logger.warning(
                    "Cube subscription tenant mention scan failed: error_type={}",
                    type(exc).__name__,
                )
            return [], False
        if callable(loader):
            try:
                loaded = await loader(limit=20)
                if isinstance(loaded, list):
                    return [item for item in loaded if isinstance(item, dict)], True
            except Exception as exc:
                logger.warning(
                    "Cube subscription tenant mention scan failed: error_type={}",
                    type(exc).__name__,
                )
        return [], False

    async def _merge_catalog_tenant_mentions(
        self,
        text: str,
        aliases: tuple[str, ...],
        *,
        require_catalog: bool = False,
    ) -> tuple[str, ...] | None:
        """Reconcile classifier names with labels verified by the live catalog.

        ``require_catalog`` is used by natural-language subscription routing.
        It returns ``None`` when verification is unavailable, allowing the
        caller to present a recovery flow instead of silently executing a
        narrowed scope.  The default keeps the helper's historical tuple
        contract for compatibility adapters.
        """

        if require_catalog:
            # Keep the historical helper name for adapters, but route strict
            # callers through the same ambiguity/unresolved handling used by
            # natural-language subscriptions. Two strict implementations would
            # eventually reintroduce silent scope narrowing.
            resolution = await self._resolve_catalog_tenant_mentions(text, aliases)
            return None if resolution.error else resolution.values

        catalog, catalog_available = await self._load_catalog_tenant_mentions(text)

        classifier_values: list[str] = []
        for alias in aliases:
            classifier_values.extend(
                part.strip()
                for part in re.split(r"[,，、;；\n]+", str(alias))
                if part.strip()
            )

        configured = getattr(self._cube_config, "tenant_mappings", {}) or {}
        candidates: list[tuple[str, str]] = []
        for item in catalog:
            tenant_id = str(item.get("tenant_id") or item.get("tenantId") or "").strip()
            if not tenant_id:
                continue
            labels = [
                str(item.get("matched_label") or "").strip(),
                str(item.get("display_name") or item.get("displayName") or "").strip(),
                str(item.get("name") or "").strip(),
                tenant_id,
            ]
            labels.extend(
                str(alias).strip()
                for alias, target in configured.items()
                if str(target).strip() == tenant_id
            )
            for label in dict.fromkeys(label for label in labels if label):
                # Generic catalog tags such as “客户” can occur in the
                # surrounding sentence and are not safe tenant identities.
                if label.casefold() in {"客户", "租户", "用户", "customer", "tenant"}:
                    continue
                candidates.append((label, tenant_id))

        # Find catalog labels in their original order and choose the longest
        # non-overlapping label.  This prevents a short alias from masking a
        # longer customer name, while ensuring two explicitly named customers
        # are both retained when the LLM returned only one of them.
        folded = text.casefold()
        configured_aliases = {
            str(alias).strip().casefold(): str(target).strip()
            for alias, target in configured.items()
            if str(alias).strip() and str(target).strip()
        }
        occurrences: list[tuple[int, int, int, str, str]] = []
        for label, tenant_id in candidates:
            folded_label = label.casefold()
            start = folded.find(folded_label)
            if start >= 0:
                # A configured alias is the deterministic tie-breaker when
                # duplicate catalog records share the same display name. The
                # alias still has to point at a record returned by Cube.
                alias_priority = (
                    0 if configured_aliases.get(folded_label) == tenant_id else 1
                )
                occurrences.append((start, alias_priority, -len(label), label, tenant_id))
        selected_ids: set[str] = set()
        occupied: list[tuple[int, int]] = []
        # The compatibility mode historically returned classifier values even
        # when a test adapter did not expose a catalog.  Keep that behavior for
        # callers that explicitly opt out of strict verification; the direct
        # NLU path always passes ``require_catalog=True`` and starts empty.
        values: list[str] = list(dict.fromkeys(classifier_values)) if not require_catalog else []
        for start, _alias_priority, _negative_length, label, tenant_id in sorted(occurrences):
            end = start + len(label)
            if tenant_id in selected_ids or any(
                start < occupied_end and end > occupied_start
                for occupied_start, occupied_end in occupied
            ):
                continue
            selected_ids.add(tenant_id)
            occupied.append((start, end))
            values.append(label)
        return tuple(values)

    async def _resolve_catalog_tenant_mentions(
        self,
        text: str,
        aliases: tuple[str, ...],
    ) -> _TenantMentionResolution:
        """Resolve every named tenant against live IDs without narrowing scope.

        The classifier output is a hint rather than an identity proof.  The
        original sentence is scanned against live catalog labels so a model
        that returns only the last customer cannot drop earlier customers.
        Shared display names and tags fail closed unless a configured alias or
        exact tenant ID identifies one live record.
        """

        catalog, catalog_available = await self._load_catalog_tenant_mentions(text)
        if not catalog_available:
            return _TenantMentionResolution((), error="catalog_unavailable")

        classifier_values: list[str] = []
        for alias in aliases:
            classifier_values.extend(
                part.strip()
                for part in re.split(r"[,，、;；\n]+", str(alias))
                if part.strip()
            )
        classifier_values = list(dict.fromkeys(classifier_values))

        configured = getattr(self._cube_config, "tenant_mappings", {}) or {}
        configured_aliases = {
            str(alias).strip().casefold(): str(target).strip()
            for alias, target in configured.items()
            if str(alias).strip() and str(target).strip()
        }
        label_targets: dict[str, set[str]] = {}
        display_by_id: dict[str, str] = {}
        generic_labels = {"客户", "租户", "用户", "customer", "tenant"}
        for item in catalog:
            tenant_id = str(item.get("tenant_id") or item.get("tenantId") or "").strip()
            if not tenant_id:
                continue
            display = str(
                item.get("display_name")
                or item.get("displayName")
                or item.get("name")
                or tenant_id
            ).strip()
            display_by_id.setdefault(tenant_id, display or tenant_id)
            labels = [
                str(item.get("matched_label") or "").strip(),
                str(item.get("display_name") or item.get("displayName") or "").strip(),
                str(item.get("name") or "").strip(),
                tenant_id,
            ]
            labels.extend(
                str(alias).strip()
                for alias, target in configured.items()
                if str(target).strip() == tenant_id
            )
            for label in dict.fromkeys(label for label in labels if label):
                if label.casefold() not in generic_labels:
                    label_targets.setdefault(label.casefold(), set()).add(tenant_id)

        # Validate every classifier value.  A hallucinated or stale value must
        # be reported instead of being silently discarded when other names do
        # happen to match the sentence.  Some providers, however, serialize a
        # long Chinese list together with the report suffix (for example
        # ``阳春面、豆汁、佛跳墙全部模型日报简报``).  Treating that whole string
        # as one unknown alias would reproduce the original one-customer-loss
        # bug, so embedded live labels are reconciled before failing closed.
        classifier_targets: dict[str, set[str]] = {}
        unresolved: list[str] = []
        for value in classifier_values:
            folded_value = value.casefold()
            targets = set(label_targets.get(folded_value, set()))
            configured_target = configured_aliases.get(folded_value)
            if configured_target in targets:
                targets = {configured_target}
            if not targets:
                embedded: list[tuple[int, int, str, set[str]]] = []
                for label, label_targets_for_value in label_targets.items():
                    start = folded_value.find(label)
                    if start >= 0:
                        embedded.append(
                            (start, -len(label), label, set(label_targets_for_value))
                        )
                embedded.sort(key=lambda item: (item[0], item[1], item[2]))
                occupied: list[tuple[int, int]] = []
                embedded_targets: list[tuple[str, set[str]]] = []
                for start, _negative_length, label, label_target_set in embedded:
                    end = start + len(label)
                    if any(
                        start < occupied_end and end > occupied_start
                        for occupied_start, occupied_end in occupied
                    ):
                        continue
                    occupied.append((start, end))
                    embedded_targets.append((label, label_target_set))
                ambiguous = next(
                    (
                        label
                        for label, label_target_set in embedded_targets
                        if len(label_target_set) > 1
                    ),
                    None,
                )
                if ambiguous is not None:
                    return _TenantMentionResolution(
                        (), error="tenant_ambiguous", unresolved=(ambiguous,)
                    )
                if embedded_targets:
                    # Strip only known report/list glue from the remainder.
                    # Any other residual word remains unresolved, preserving
                    # fail-closed behavior for a value that mixes a live name
                    # with a hallucinated customer.
                    remainder = folded_value
                    for label, _label_target_set in embedded_targets:
                        remainder = remainder.replace(label, " ", 1)
                    remainder = re.sub(
                        r"(?:全部模型|所有模型|全模型|多客户|多模型|日报简报|日报|周报简报|周报|月报简报|月报|区间报表|报表|简报|客户|租户|模型|以及|并且|发送|推送|给我|每天|工作日|每周|每月|的|和|与|及|[\s,，、;；:：()（）\[\]【】])",
                        "",
                        remainder,
                        flags=re.IGNORECASE,
                    )
                    if remainder.strip():
                        unresolved.append(value)
                    else:
                        for label, label_target_set in embedded_targets:
                            classifier_targets[label] = set(label_target_set)
                else:
                    unresolved.append(value)
            elif len(targets) > 1:
                return _TenantMentionResolution(
                    (), error="tenant_ambiguous", unresolved=(value,)
                )
            else:
                classifier_targets[folded_value] = targets

        folded_text = text.casefold()
        occurrences: list[tuple[int, int, int, str, str]] = []
        for folded_label, raw_targets in label_targets.items():
            targets = set(raw_targets)
            preferred = configured_aliases.get(folded_label)
            if preferred in targets:
                targets = {preferred}
            classifier_hint = classifier_targets.get(folded_label)
            if classifier_hint and len(classifier_hint) == 1:
                targets &= classifier_hint
            if len(targets) > 1:
                if folded_label in folded_text:
                    return _TenantMentionResolution(
                        (), error="tenant_ambiguous", unresolved=(folded_label,)
                    )
                continue
            if not targets:
                continue
            tenant_id = next(iter(targets))
            start = folded_text.find(folded_label)
            while start >= 0:
                priority = 0 if preferred == tenant_id or classifier_hint else 1
                occurrences.append(
                    (start, priority, -len(folded_label), folded_label, tenant_id)
                )
                start = folded_text.find(
                    folded_label, start + max(1, len(folded_label))
                )

        selected_ids: set[str] = set()
        occupied: list[tuple[int, int]] = []
        selected: list[tuple[int, str]] = []
        for start, _priority, _negative_length, folded_label, tenant_id in sorted(occurrences):
            end = start + len(folded_label)
            if tenant_id in selected_ids or any(
                start < occupied_end and end > occupied_start
                for occupied_start, occupied_end in occupied
            ):
                continue
            selected_ids.add(tenant_id)
            occupied.append((start, end))
            selected.append((start, tenant_id))

        if unresolved:
            return _TenantMentionResolution(
                (), error="scope_unresolved", unresolved=tuple(unresolved)
            )
        # A valid classifier hint may be a real ID or a normalized label that
        # is not found byte-for-byte in the sentence.  It is safe to retain
        # because it already mapped to a live catalog record.
        for targets in classifier_targets.values():
            selected_ids.update(targets)
        if not selected_ids:
            return _TenantMentionResolution((), error="scope_unresolved")

        ordered_ids: list[str] = []
        for _start, tenant_id in sorted(selected):
            if tenant_id not in ordered_ids:
                ordered_ids.append(tenant_id)
        for tenant_id in sorted(selected_ids - set(ordered_ids)):
            ordered_ids.append(tenant_id)
        values = tuple(
            next(
                (
                    alias
                    for alias, target in configured.items()
                    if str(target).strip() == tenant_id
                ),
                display_by_id.get(tenant_id, tenant_id),
            )
            for tenant_id in ordered_ids
        )
        return _TenantMentionResolution(values)

    @staticmethod
    def _subscription_preview_params(
        intent: CubeSubscriptionIntent, *, reference_message_id: str = ""
    ) -> dict[str, Any]:
        """Compile validated NLU output to the ReportCenter's public schema."""

        return {
            "action": "subscription_preview",
            "report_type": intent.report_type,
            "tenant_scope": intent.tenant_scope,
            "tenant_aliases": list(intent.tenant_aliases),
            "model_scope": intent.model_scope,
            "models": list(intent.models),
            "recurrence": intent.recurrence,
            "send_time": intent.send_time,
            "weekday": intent.weekday,
            "month_day": intent.month_day,
            "inherit_report_scope": intent.inherit_report_scope,
            "reference_message_id": reference_message_id,
        }

    async def classify_direct_request(self, text: str, runtime: Any) -> dict[str, Any] | None:
        """Parse a direct subscription request without allowing the LLM to execute it."""

        deterministic_intent = parse_deterministic_subscription_intent(text)
        if deterministic_intent is not None:
            logger.info("Cube subscription intent parsed deterministically: mode=direct")
            return await self._compile_subscription_intent(
                text, deterministic_intent, reference_message_id=""
            )
        intent = await classify_subscription_intent(
            text,
            runtime,
            timeout_seconds=self._config.cube_subscription_nlu_timeout_seconds,
        )
        if intent is None:
            logger.warning("Cube subscription intent parse failed: mode=direct reason=nlu_unavailable")
            return {"action": "subscription_parse_failed", "subscription_error": "nlu_unavailable"}
        return await self._compile_subscription_intent(text, intent, reference_message_id="")

    async def _compile_subscription_intent(
        self,
        text: str,
        intent: CubeSubscriptionIntent,
        *,
        reference_message_id: str,
    ) -> dict[str, Any]:
        """Resolve a validated intent against live Cube catalog data.

        This boundary owns identity reconciliation and grouped-template
        promotion.  The classifier, deterministic parser, and references may
        provide only human-facing scope hints; no caller can inject a tenant
        ID or broaden a subscription here.
        """

        tenant_resolution = await self._resolve_catalog_tenant_mentions(
            text, intent.tenant_aliases
        )
        explicit_all_tenants = bool(_EXPLICIT_ALL_TENANTS_RE.search(text))
        if tenant_resolution.error == "catalog_unavailable":
            return {
                "action": "subscription_scope_failed",
                "subscription_error": "catalog_unavailable",
                "catalog_unavailable": True,
            }
        # An explicit all-customer request does not need individual name
        # matching.  Ignore incidental duplicate tags or classifier noise, but
        # never ignore an unavailable catalog because the resulting scope would
        # no longer be verifiable at execution time.
        if tenant_resolution.error and not explicit_all_tenants:
            marker: dict[str, Any] = {
                "action": "subscription_scope_failed",
                "subscription_error": tenant_resolution.error,
                tenant_resolution.error: True,
            }
            if tenant_resolution.unresolved:
                marker["unresolved_tenants"] = list(tenant_resolution.unresolved)
            return marker
        merged_aliases = tenant_resolution.values
        # The original sentence is authoritative for an explicitly named
        # scope.  A classifier may return ``all`` or only its last entity when
        # a long Chinese list is present; live catalog matches repair that
        # loss.  Conversely, an explicit “全部客户” phrase must not be turned
        # into a selected subset merely because a name-like token was emitted.
        if explicit_all_tenants:
            intent = replace(intent, tenant_scope="all", tenant_aliases=())
        elif intent.tenant_scope == "all" and not merged_aliases:
            # ``tenant_scope=all`` from the classifier is only a hint.  Without
            # an explicit all-customer phrase or a live named match, accepting
            # it would broaden a natural-language request to every tenant.
            return {
                "action": "subscription_scope_failed",
                "subscription_error": "scope_unresolved",
                "scope_unresolved": True,
            }
        elif merged_aliases:
            intent = replace(
                intent,
                tenant_scope="selected",
                tenant_aliases=merged_aliases,
            )
        elif intent.tenant_scope == "selected":
            # The classifier supplied a name, but no live catalog record
            # matched it.  Do not let the later resolver guess from stale or
            # synthetic aliases; return the bounded recovery path instead.
            return {
                "action": "subscription_scope_failed",
                "subscription_error": "scope_unresolved",
                "scope_unresolved": True,
            }
        # A long scope sentence is often truncated by an LLM before it reaches
        # the schema boundary.  The explicit phrase in the original message is
        # authoritative for model scope, so a single accidentally extracted
        # model must never narrow an ``all models`` subscription.
        if _EXPLICIT_ALL_MODELS_RE.search(text):
            intent = replace(intent, model_scope="all", models=())
        if (
            intent.report_type in {"usage_daily_brief", "usage_customer_model_daily_brief"}
            and (
                intent.report_type == "usage_customer_model_daily_brief"
                or len(merged_aliases) > 1
                or (_EXPLICIT_ALL_TENANTS_RE.search(text) and _EXPLICIT_ALL_MODELS_RE.search(text))
            )
        ):
            # Keep the grouped template for a multi-customer daily subscription;
            # the legacy daily brief has a single ``tenant_query`` slot and
            # would silently retain only one customer.
            intent = replace(
                intent,
                report_type="usage_customer_model_daily_brief",
                model_scope="all" if _EXPLICIT_ALL_MODELS_RE.search(text) else intent.model_scope,
                models=() if _EXPLICIT_ALL_MODELS_RE.search(text) else intent.models,
            )
        return self._subscription_preview_params(
            intent, reference_message_id=reference_message_id
        )

    async def classify_referenced_subscription(
        self,
        text: str,
        runtime: Any,
        *,
        channel: str,
        chat_id: str,
        reference_message_id: str,
    ) -> dict[str, Any] | None:
        """Parse schedule language using only a verified reference summary."""

        if not (
            (self._config.cube_subscription_nlu_v2 or self._config.cube_subscription_nlu_v3)
            and self._config.cube_report_reference_subscription
            and is_subscription_intent_candidate(text)
        ):
            return None
        reference = self._store.message_reference(
            channel=channel,
            chat_id=chat_id,
            message_id=reference_message_id,
        )
        if reference is None:
            return {
                "action": "subscription_reference_missing",
                "subscription_error": "reference_not_found_or_expired",
                "reference_message_id": reference_message_id,
            }
        deterministic_intent = parse_deterministic_subscription_intent(
            text, referenced_report=True
        )
        if deterministic_intent is not None:
            logger.info("Cube subscription intent parsed deterministically: mode=reference")
            return self._subscription_preview_params(
                deterministic_intent, reference_message_id=reference_message_id
            )
        intent = await classify_subscription_intent(
            text,
            runtime,
            timeout_seconds=self._config.cube_subscription_nlu_timeout_seconds,
            referenced_report={
                "template_id": reference.template_id,
                "period": reference.period,
            },
        )
        if intent is None:
            logger.warning(
                "Cube subscription intent parse failed: mode=reference reason=nlu_unavailable"
            )
            return {
                "action": "subscription_parse_failed",
                "subscription_error": "nlu_unavailable",
                "reference_message_id": reference_message_id,
            }
        if not _REFERENCE_SCOPE_OVERRIDE_RE.search(text):
            # The reference is the only trusted source of report scope.  The
            # LLM is intentionally used for cadence/time extraction, but it
            # may return a generic daily intent even when the user only said
            # “subscribe this report”.  Force inheritance in that case so the
            # later preview cannot fall back to the default tenant or lose a
            # multi-customer/all-model selection.  Explicit entity wording is
            # left for the normal resolver path to validate as an override.
            intent = replace(
                intent,
                report_type="inherit",
                tenant_scope="inherit",
                tenant_aliases=(),
                model_scope="inherit",
                models=(),
                inherit_report_scope=True,
            )
        return self._subscription_preview_params(
            intent, reference_message_id=reference_message_id
        )

    def fallback_direct_request(self, text: str) -> dict[str, Any] | None:
        """Fail closed when subscription NLU is unavailable or invalid."""

        if not is_subscription_intent_candidate(text):
            return None
        logger.warning("Cube subscription intent parse failed: mode=direct reason=classifier_disabled")
        return {
            "action": "subscription_parse_failed",
            "subscription_error": "classifier_disabled",
        }

    def match_direct_request(self, text: str) -> dict[str, Any] | None:
        raw = text.strip()
        subscription_control = _SUBSCRIPTION_CONTROL_RE.fullmatch(raw)
        if subscription_control:
            operation = "enable" if subscription_control.group("operation") == "启用" else "disable"
            return {
                "action": f"subscription_{operation}",
                "subscription_id": subscription_control.group("subscription_id"),
            }
        brief_subscription = _BRIEF_SUBSCRIPTION_RE.fullmatch(raw)
        if brief_subscription:
            tenant_display = brief_subscription.group("tenant").strip()
            tenant_id = str(brief_subscription.group("tenant_id") or "").strip()
            models_text = brief_subscription.group("models").strip()
            all_tenants = tenant_display == "全部客户"
            tenant = (
                ""
                if all_tenants or tenant_display == "默认客户范围"
                else tenant_id or tenant_display
            )
            models = [] if models_text in {"汇总", "全部模型"} else models_text.split("、")
            model_scope = "all" if models_text == "全部模型" else "selected" if models else "summary"
            return {
                "action": "subscription_setup",
                "period": {"日报": "day", "周报": "week", "月报": "month"}[brief_subscription.group("period")],
                "report_family": "usage",
                "report_params": {
                    "report_template": "brief",
                    "tenant_query": tenant,
                    "models": models,
                    "all_tenants": all_tenants,
                    "model_scope": model_scope,
                    "breakdown": "model" if model_scope in {"all", "selected"} else "summary",
                    "report_selections": (
                        [{"tenant_query": tenant, "model_scope": model_scope, "models": models}]
                        if tenant else []
                    ),
                },
            }
        # A subscription candidate must never fall through to the ordinary
        # daily-report matcher.  AgentLoop performs the one bounded classifier
        # call before reaching this method; this guard also protects callers
        # that invoke the deterministic matcher directly.
        if is_subscription_intent_candidate(raw):
            return None
        detail_match = _FURTHER_ANALYSIS_RE.fullmatch(raw)
        if detail_match:
            period = {
                "日报": "day",
                "周报": "week",
                "月报": "month",
                "区间报表": "range",
            }[detail_match.group("period")]
            tenant_text = detail_match.group("tenant").strip()
            models_text = detail_match.group("models").strip()
            all_tenants = tenant_text == "全部客户"
            tenant = "" if tenant_text in {"全部客户", "默认客户范围"} else tenant_text
            models = [] if models_text in {"汇总", "全部模型"} else models_text.split("、")
            model_scope = (
                "all" if models_text == "全部模型" else "selected" if models else "summary"
            )
            report_selections = (
                []
                if all_tenants or not tenant
                else [{"tenant_query": tenant, "model_scope": model_scope, "models": models}]
            )
            return {
                "action": "cube_report",
                "period": period,
                "report_template": "matrix_card",
                "tenant_query": tenant,
                "model": models[0] if len(models) == 1 else "",
                "models": models,
                "all_tenants": all_tenants,
                "breakdown": "model" if model_scope in {"all", "selected"} else "summary",
                "start_date": detail_match.group("start"),
                "end_date": detail_match.group("end"),
                "interactive": False,
                "report_selections": report_selections,
            }
        if _HOME_RE.fullmatch(raw):
            return {"action": "home"}
        if _MULTI_SCOPE_BRIEF_RE.fullmatch(raw):
            return {"action": "multi_scope_brief", "interactive": True, "period": "day"}
        machine_tpm_match = _MACHINE_TPM_RE.fullmatch(raw)
        if machine_tpm_match:
            return {
                "action": "machine_tpm_report",
                "period": "day",
                "model": machine_tpm_match.group("model"),
            }
        if _RECENT_RE.fullmatch(raw):
            return {"action": "recent"}
        if _SUBSCRIPTIONS_RE.fullmatch(raw):
            return {"action": "subscriptions"}
        empty_provider_match = _PROVIDER_QUALITY_EMPTY_RE.fullmatch(raw)
        if empty_provider_match:
            period_text = empty_provider_match.group("period").replace(" ", "")
            period = (
                "recent15m" if "15" in period_text else
                "day" if period_text == "昨日" else
                "week" if period_text == "上一完整周" else "range"
            )
            result = {
                "action": "provider_quality_report",
                "period": period,
                "provider": "",
                "selection_confirmed": True,
                "include_empty": True,
            }
            if period == "range":
                result["start_date"] = empty_provider_match.group("start_date")
                result["end_date"] = empty_provider_match.group("end_date")
            return result
        model_provider_match = _MODEL_PROVIDER_QUALITY_RE.fullmatch(raw)
        if model_provider_match:
            return {
                "action": "provider_quality_report",
                "period": "recent15m",
                "model": model_provider_match.group("model"),
            }
        named_provider_quality_match = _NAMED_PROVIDER_QUALITY_RE.fullmatch(raw)
        provider_quality_match = _PROVIDER_QUALITY_RE.fullmatch(raw)
        if named_provider_quality_match:
            return {
                "action": "provider_quality_report",
                "period": "recent15m",
                "provider": named_provider_quality_match.group("provider"),
            }
        if provider_quality_match:
            return {
                "action": "provider_quality_report",
                "period": "day" if "昨天" in raw else "recent15m",
                "provider": "",
            }
        health_match = _HEALTH_RE.fullmatch(raw)
        if health_match:
            return {
                "action": "health_report",
                "period": "recent15m" if "15" in raw else "recent15m",
            }
        if _COST_RE.fullmatch(raw):
            return {"action": "cost_report", "period": "month", "interactive": True}
        model_period_match = _MODEL_CUBE_PERIOD_RE.fullmatch(raw)
        if model_period_match:
            period = {"日报": "day", "周报": "week", "月报": "month"}[
                model_period_match.group("period")
            ]
            return {
                "action": "cube_report",
                "period": period,
                "model": model_period_match.group("model"),
                "all_tenants": True,
                "report_template": self._requested_usage_template(
                    model_period_match.group("template")
                ),
            }
        period_match = _CUBE_PERIOD_RE.fullmatch(raw)
        if period_match:
            period = {"日报": "day", "周报": "week", "月报": "month"}[
                period_match.group("period")
            ]
            return {
                "action": "cube_report",
                "period": period,
                "interactive": True,
                "report_template": self._requested_usage_template(
                    period_match.group("template")
                ),
            }
        translated = self._translate_legacy_usage_request(raw)
        if translated is not None:
            return translated
        return None

    def _requested_usage_template(self, requested: str | None) -> str:
        """Map user-facing depth words to stable compatibility template names."""

        if requested == "完整":
            return "full"
        if requested == "详细":
            return "matrix_card"
        if requested == "简报" and self._config.cube_usage_brief_template:
            return "brief"
        if self._usage_brief_default_enabled:
            return "brief"
        return "matrix_card"

    @property
    def _usage_brief_default_enabled(self) -> bool:
        """Require template registration and routing flags to enable the default safely."""

        return (
            self._config.cube_usage_brief_template
            and self._config.cube_usage_brief_default
        )

    def _translate_legacy_usage_request(self, raw: str) -> dict[str, Any] | None:
        """Reuse the mature Cube parser, then execute standard reports through ReportRunner."""

        if self._magik_tool is None:
            return None
        params = self._magik_tool.match_direct_request(raw)
        if not isinstance(params, dict):
            return None
        legacy_template = str(params.get("report_template") or "")
        if legacy_template in {"full", "usage_total"}:
            return None
        start_date = str(params.get("start_date") or "")
        end_date = str(params.get("end_date") or "")
        if not start_date or not end_date:
            return None
        if "月报" in raw or "上月" in raw or "上个月" in raw:
            period = "month"
        elif "周报" in raw or "上周" in raw:
            period = "week"
        elif start_date == end_date:
            period = "day"
        else:
            period = "range"
        result = {
            "action": "cube_report",
            "period": period,
            "report_template": self._requested_usage_template(
                "详细" if re.search(r"(?:详细|明细)", raw) else None
            ),
            "tenant_query": str(params.get("tenant_query") or ""),
            "model": str(params.get("model") or ""),
            "models": list(params.get("models") or []),
            "breakdown": str(params.get("breakdown") or "summary"),
            "start_date": start_date,
            "end_date": end_date,
            "interactive": bool(params.get("interactive", False)),
            "report_selections": list(params.get("report_selections") or []),
        }
        return result

    def _cube_period_dates(self, period: str, today: date) -> tuple[date, date]:
        yesterday = today - timedelta(days=1)
        if period == "day":
            return yesterday, yesterday
        if period == "week":
            start = today - timedelta(days=today.weekday() + 7)
            return start, start + timedelta(days=6)
        if period == "month":
            end = today.replace(day=1) - timedelta(days=1)
            return end.replace(day=1), end
        if period == "recent7":
            return yesterday - timedelta(days=6), yesterday
        raise ValueError("unsupported Cube report period")

    def _canonical_cube_models(self, values: tuple[str, ...]) -> tuple[str, ...]:
        """Resolve only configured shorthand; API model names otherwise pass through unchanged."""

        aliases = getattr(self._cube_config, "model_aliases", {}) or {}
        canonical: list[str] = []
        for value in values:
            normalized = value.strip()
            if not normalized:
                continue
            resolved = next(
                (
                    str(target).strip()
                    for alias, target in aliases.items()
                    if str(alias).strip().casefold() == normalized.casefold()
                ),
                normalized,
            )
            if resolved and resolved not in canonical:
                canonical.append(resolved)
        return tuple(canonical)

    @property
    def health_connector_enabled(self) -> bool:
        return bool(
            self._config.cube_health_connector
            and self._config.cube_health_template
            and isinstance(self._registry.connector("magik_cube"), CubeConnector)
            and self._registry.template("health_sre") is not None
        )

    @property
    def health_reports_enabled(self) -> bool:
        return self.health_connector_enabled and self._config.cube_health_report

    @property
    def health_subscriptions_enabled(self) -> bool:
        return self.health_connector_enabled and self._config.cube_health_subscription

    @property
    def cost_connector_enabled(self) -> bool:
        connector = self._registry.connector("magik_cube")
        return bool(
            self._config.cube_cost_connector
            and self._config.cube_cost_template
            and isinstance(connector, CubeConnector)
            and connector.account_configured
            and self._registry.template("cost_account") is not None
        )

    @property
    def cost_reports_enabled(self) -> bool:
        return self.cost_connector_enabled and self._config.cube_cost_report

    @property
    def cost_subscriptions_enabled(self) -> bool:
        return self.cost_connector_enabled and self._config.cube_cost_subscription

    @property
    def provider_quality_connector_enabled(self) -> bool:
        return bool(
            self._config.cube_provider_quality_connector
            and isinstance(
                self._registry.connector("cube_provider_quality"),
                CubeProviderQualityConnector,
            )
            and self._registry.template("provider_quality") is not None
        )

    @property
    def provider_quality_reports_enabled(self) -> bool:
        return self.provider_quality_connector_enabled and self._config.cube_provider_quality_report

    @property
    def provider_quality_subscriptions_enabled(self) -> bool:
        return self.provider_quality_connector_enabled and self._config.cube_provider_quality_subscription

    async def _run_health_report(self, *, period: str) -> ToolResult:
        if not self.health_reports_enabled:
            return ToolResult.error("Error: Cube health report is not enabled")
        if period not in {"recent15m", "day", "week"}:
            return ToolResult.error("Error: health report period must be recent15m, day, or week")
        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        timezone_info = ZoneInfo(self._config.timezone)
        now = datetime.now(timezone_info)
        intent_kwargs: dict[str, Any] = {
            "connector_id": "magik_cube",
            "template_id": "health_sre",
            "period": period,
            "filters": {},
        }
        if period == "recent15m":
            end_time = now.replace(second=0, microsecond=0)
            start_time = end_time - timedelta(minutes=15)
            intent_kwargs.update(
                start_date=start_time.date(),
                end_date=end_time.date(),
                start_time=start_time,
                end_time=end_time,
                comparison_start_time=start_time - timedelta(minutes=15),
                comparison_end_time=end_time - timedelta(minutes=15),
            )
        else:
            start_date, end_date = self._cube_period_dates(period, now.date())
            intent_kwargs.update(start_date=start_date, end_date=end_date)
        intent = ReportIntent(**intent_kwargs)
        template = self._registry.template("health_sre")
        if template is None:
            return ToolResult.error("Error: Cube health template is unavailable")
        trace_id = uuid.uuid4().hex
        context = ReportRunContext(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            timezone=self._config.timezone,
            trace_id=trace_id,
            template_version=template.manifest.version,
            metadata=metadata,
        )
        try:
            outcome = await ReportRunner(
                self._registry,
                self._store,
                semantic_shadow_enabled=self._config.cube_semantics_shadow,
                template_policy_enforced=self._config.report_management_v1,
            ).run(intent, context)
        except PermissionError:
            return ToolResult.error("当前账号没有执行 Cube 健康报告的权限，请联系管理员授权。")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: Cube health report unavailable: {exc}")
        return self._result(outcome.document)

    async def _run_provider_quality_report(
        self,
        *,
        period: str,
        provider: str,
        providers: list[str] | None,
        provider_id: str,
        model: str,
        endpoint: str,
        selection_confirmed: bool = False,
        include_empty: bool = False,
        start_date: str = "",
        end_date: str = "",
    ) -> ToolResult:
        if not self.provider_quality_reports_enabled:
            return ToolResult.error("Error: Cube provider quality report is not enabled")
        if period not in {"recent15m", "day", "week", "range"}:
            return ToolResult.error("Error: provider quality supports recent15m, day, week, or range")
        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        if (
            self._config.cube_provider_quality_selector
            and not selection_confirmed
            and not provider.strip()
            and not providers
            and not provider_id.strip()
            and not model.strip()
            and not endpoint.strip()
        ):
            return await self._run_provider_quality_selector(
                channel=channel,
                chat_id=chat_id,
                user_id=user_id,
                timezone=self._config.timezone,
            )
        timezone_info = ZoneInfo(self._config.timezone)
        now = datetime.now(timezone_info)
        intent_kwargs: dict[str, Any] = {
            "connector_id": "cube_provider_quality",
            "template_id": "provider_quality",
            "period": period,
            "provider": provider.strip(),
            "endpoint": endpoint.strip(),
            "models": self._canonical_cube_models((model,)) if model.strip() else (),
            "filters": {
                "provider": provider.strip(),
                "providers": [item.strip() for item in (providers or []) if item.strip()],
                "provider_id": provider_id.strip(),
                "model": model.strip(),
                "endpoint": endpoint.strip(),
                "include_empty": include_empty,
                "start_date": start_date,
                "end_date": end_date,
            },
        }
        if period == "recent15m":
            end_time = now.replace(second=0, microsecond=0)
            start_time = end_time - timedelta(minutes=15)
            intent_kwargs.update(
                start_date=start_time.date(),
                end_date=end_time.date(),
                start_time=start_time,
                end_time=end_time,
                comparison_start_time=start_time - timedelta(minutes=15),
                comparison_end_time=end_time - timedelta(minutes=15),
            )
        else:
            if period == "range":
                try:
                    period_start_date = date.fromisoformat(start_date)
                    period_end_date = date.fromisoformat(end_date)
                except ValueError:
                    return ToolResult.error("自定义区间需要使用 YYYY-MM-DD 日期格式。")
                if period_end_date < period_start_date:
                    return ToolResult.error("自定义区间的结束日期不能早于开始日期。")
                if (period_end_date - period_start_date).days >= 90:
                    return ToolResult.error("自定义区间最多支持 90 天。")
            else:
                period_start_date, period_end_date = self._cube_period_dates(period, now.date())
            intent_kwargs.update(
                start_date=period_start_date,
                end_date=period_end_date,
                comparison_start_time=None,
                comparison_end_time=None,
            )
        intent = ReportIntent(**intent_kwargs)
        template = self._registry.template("provider_quality")
        if template is None:
            return ToolResult.error("Error: Cube provider quality template is unavailable")
        context = ReportRunContext(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            timezone=self._config.timezone,
            trace_id=uuid.uuid4().hex,
            template_version=template.manifest.version,
            metadata=metadata,
        )
        try:
            outcome = await ReportRunner(
                self._registry,
                self._store,
                semantic_shadow_enabled=self._config.cube_semantics_shadow,
                template_policy_enforced=self._config.report_management_v1,
            ).run(intent, context)
        except PermissionError:
            return ToolResult.error("当前账号没有执行 Cube 供应商质量报告的权限，请联系管理员授权。")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: Cube provider quality report unavailable: {exc}")
        return self._result(outcome.document)

    async def _run_provider_quality_selector(
        self,
        *,
        channel: str,
        chat_id: str,
        user_id: str,
        timezone: str,
    ) -> ToolResult:
        connector = self._registry.connector("cube_provider_quality")
        if not isinstance(connector, CubeProviderQualityConnector):
            return ToolResult.error("Error: Cube provider quality connector is unavailable")
        if not self._store.allowed(channel, user_id, "connector", "cube_provider_quality"):
            return ToolResult.error("当前账号没有执行 Cube 供应商质量报告的权限，请联系管理员授权。")
        if not self._store.allowed(channel, user_id, "template", "provider_quality"):
            return ToolResult.error("当前账号没有执行 Cube 供应商质量报告的权限，请联系管理员授权。")
        catalog, warnings = await connector.list_provider_catalog()
        providers = sorted(
            {
                str(item.get("provider") or "").strip()
                for item in catalog
                if str(item.get("provider") or "").strip()
            },
            key=str.casefold,
        )
        if not providers:
            reason = "；".join(warnings[:2]) if warnings else "Cube 未返回供应商目录"
            return ToolResult.error(f"暂时无法加载供应商列表：{reason}")
        interaction = report_interactions().create(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            options={
                secrets.token_urlsafe(12): provider
                for provider in providers
            },
        )
        return self._result(
            provider_quality_selector_document(
                interaction,
                catalog,
                timezone=timezone,
                warnings=warnings,
            )
        )

    async def _run_cost_report(
        self,
        *,
        period: str,
        tenant_query: str,
        project: str,
        model: str,
        endpoint: str,
        interactive: bool,
    ) -> ToolResult:
        if not self.cost_reports_enabled:
            return ToolResult.error("Error: Cube cost/account report is not enabled")
        if period != "month":
            return ToolResult.error("Error: Cube cost/account reports support month only")
        if interactive and not tenant_query.strip():
            return await self._run_scope_selector(period=period, report_family="cost")
        if not tenant_query.strip():
            return ToolResult.error("请选择一个已授权客户后再查询成本与账户。")
        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        today = datetime.now(ZoneInfo(self._config.timezone)).date()
        start_date, end_date = self._cube_period_dates("month", today)
        intent = ReportIntent(
            connector_id="magik_cube",
            template_id="cost_account",
            period="month",
            tenant=tenant_query.strip(),
            project=project.strip(),
            endpoint=endpoint.strip(),
            model_scope="selected" if model.strip() else "summary",
            models=(model.strip(),) if model.strip() else (),
            start_date=start_date,
            end_date=end_date,
            filters={
                "tenant": tenant_query.strip(),
                "project": project.strip(),
                "endpoint": endpoint.strip(),
                "models": [model.strip()] if model.strip() else [],
                "model_scope": "selected" if model.strip() else "summary",
            },
        )
        template = self._registry.template("cost_account")
        if template is None:
            return ToolResult.error("Error: Cube cost/account template is unavailable")
        context = ReportRunContext(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            timezone=self._config.timezone,
            trace_id=uuid.uuid4().hex,
            template_version=template.manifest.version,
            metadata=metadata,
        )
        try:
            outcome = await ReportRunner(
                self._registry,
                self._store,
                semantic_shadow_enabled=self._config.cube_semantics_shadow,
                template_policy_enforced=self._config.report_management_v1,
            ).run(intent, context)
        except PermissionError:
            return ToolResult.error("当前账号没有执行 Cube 成本与账户报表的权限，请联系管理员授权。")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: Cube cost/account report unavailable: {exc}")
        return self._result(outcome.document)

    async def _run_cube_report(
        self,
        *,
        period: str,
        tenant_query: str,
        model: str,
        models: list[str] | None,
        breakdown: str,
        project: str,
        endpoint: str,
        provider: str,
        interactive: bool,
        all_tenants: bool,
        report_template: str,
        start_date: str = "",
        end_date: str = "",
        report_selections: list[dict[str, Any]] | None = None,
    ) -> ToolResult:
        if (
            not self._config.cube_report_runner
            or self._magik_tool is None
            or self._registry.connector("magik_cube") is None
        ):
            return ToolResult.error("Error: Magik Cube connector is unavailable")
        if period not in {"day", "week", "month", "recent7", "range"}:
            return ToolResult.error("Error: unsupported Cube report period")
        if report_template not in {"brief", "matrix_card", "full"}:
            return ToolResult.error("Error: unsupported Cube report template")
        if breakdown not in {"summary", "model"}:
            return ToolResult.error("Error: breakdown must be summary or model")
        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        selections = [item for item in report_selections or [] if isinstance(item, dict)]
        if len(selections) > 1:
            return ToolResult.error("统一选择器当前一次仅支持一个客户，请分开生成报表。")
        selected = selections[0] if selections else {}
        selected_tenant = str(selected.get("tenant_query") or tenant_query).strip()
        selected_scope = str(selected.get("model_scope") or "").strip()
        selected_values = selected.get("models") if isinstance(selected.get("models"), list) else []
        selected_models = self._canonical_cube_models(tuple(
            dict.fromkeys(
                item.strip()
                for item in (
                    ([model] if model else (models or []))
                    if not selected_values
                    else selected_values
                )
                if isinstance(item, str) and item.strip()
            )
        ))
        if all_tenants:
            if not self._config.cube_model_all_tenant_report:
                return ToolResult.error("管理员尚未开启全部客户模型报表。")
            if selected_tenant:
                return ToolResult.error("全部客户模型报表不能同时选择单个客户。")
            if len(selected_models) != 1:
                return ToolResult.error("请选择一个模型后再生成全部客户报表。")
        if interactive and not selected_tenant and not selected_models:
            return await self._run_scope_selector(
                period=period,
                report_family="usage",
                report_template=report_template,
            )
        if interactive and selected_scope == "selected" and not selected_models:
            return await self._run_selector_model_stage(
                period=period,
                tenant_query=selected_tenant,
                report_template=report_template,
            )
        today = datetime.now(ZoneInfo(self._config.timezone)).date()
        if start_date or end_date:
            try:
                period_start = date.fromisoformat(start_date)
                period_end = date.fromisoformat(end_date)
            except ValueError:
                return ToolResult.error("报表日期需要使用 YYYY-MM-DD 格式。")
            if period_end < period_start or (period_end - period_start).days >= 365:
                return ToolResult.error("报表日期范围无效或超过 365 天。")
        else:
            if period == "range":
                return ToolResult.error("自定义区间必须提供开始和结束日期。")
            period_start, period_end = self._cube_period_dates(period, today)
        if report_template == "full":
            legacy_params: dict[str, Any] = {
                "start_date": period_start.isoformat(),
                "end_date": period_end.isoformat(),
                "comparison": "previous_period",
                "report_template": "full",
                "tenant_query": selected_tenant,
                "model": selected_models[0] if len(selected_models) == 1 else "",
                "breakdown": "model" if selected_scope in {"all", "selected"} else breakdown,
                "include_tpm": True,
                "save_snapshot": False,
            }
            return await self._magik_tool.execute(**legacy_params)
        template_id = (
            _BRIEF_PERIOD_TEMPLATES[period]
            if report_template == "brief"
            else _PERIOD_TEMPLATES[period]
        )
        intent = ReportIntent(
            connector_id="magik_cube",
            template_id=template_id,
            period=period,  # type: ignore[arg-type]
            tenant=selected_tenant,
            project=project.strip(),
            endpoint=endpoint.strip(),
            provider=provider.strip(),
            model_scope=(selected_scope if selected_scope in {"summary", "all", "selected"} else "selected" if selected_models else "summary"),
            models=selected_models,
            start_date=period_start,
            end_date=period_end,
            filters={
                "tenant": selected_tenant,
                "project": project.strip(),
                "endpoint": endpoint.strip(),
                "provider": provider.strip(),
                "models": list(selected_models),
                "all_tenants": all_tenants,
                "model_scope": (
                    selected_scope
                    if selected_scope in {"summary", "all", "selected"}
                    else "selected" if selected_models else "summary"
                ),
            },
        )
        template = self._registry.template(template_id)
        if template is None:
            return ToolResult.error(f"Error: Cube report template unavailable: {template_id}")
        trace_id = uuid.uuid4().hex
        context = ReportRunContext(
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            timezone=self._config.timezone,
            trace_id=trace_id,
            template_version=template.manifest.version,
            metadata=metadata,
        )
        try:
            outcome = await ReportRunner(
                self._registry,
                self._store,
                semantic_shadow_enabled=self._config.cube_semantics_shadow,
                template_policy_enforced=self._config.report_management_v1,
            ).run(intent, context)
        except PermissionError:
            return ToolResult.error("当前账号没有执行该 Cube 报表的权限，请联系管理员授权。")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: Cube report unavailable: {exc}")
        document = self._filter_usage_subscription_actions(
            outcome.document,
            channel=channel,
            user_id=user_id,
            template_id=template_id,
        )
        return self._result(
            document,
            report_reference=self._report_reference_payload(
                intent, document=document, run_id=trace_id
            ),
        )

    async def _load_tenant_model_catalog(
        self, tenant_ids: list[str], *, start_date: date, end_date: date
    ) -> dict[str, list[str]]:
        """Load explicit model names for all-model multi-tenant execution.

        The Cube usage endpoint may collapse an omitted model into a tenant
        aggregate, so every manual and scheduled run expands the live catalog
        before entering ReportRunner.
        """

        if self._magik_tool is None:
            raise LookupError("Cube 模型目录当前不可用")

        # Cron and other non-interactive callers cannot consume the legacy
        # selector's ``agent_ui`` response.  The concrete Cube tool exposes a
        # direct catalog method for this path; keep the old selector fallback
        # only for compatibility adapters and test doubles that predate it.
        direct_loader = getattr(self._magik_tool, "list_models_for_tenants", None)
        native_loader = getattr(type(self._magik_tool), "list_models_for_tenants", None)
        if callable(direct_loader) and native_loader is not None:
            try:
                loaded = await direct_loader(tenant_ids)
            except Exception as exc:
                raise LookupError("Cube 实时模型目录未返回可用模型，请检查目录查询权限") from exc
            if not isinstance(loaded, dict):
                raise LookupError("Cube 实时模型目录返回格式无效")
            catalog_models = {
                str(tenant_id).strip(): list(
                    dict.fromkeys(
                        str(model).strip()
                        for model in models
                        if str(model).strip()
                    )
                )
                for tenant_id, models in loaded.items()
                if str(tenant_id).strip() and isinstance(models, (list, tuple))
            }
            missing = [tenant_id for tenant_id in tenant_ids if not catalog_models.get(tenant_id)]
            if missing:
                raise LookupError("Cube 实时模型目录未返回可用模型，请检查客户模型配置")
            if sum(len(catalog_models[tenant_id]) for tenant_id in tenant_ids) > 200:
                raise ValueError("客户模型组合超过 200 个，请缩小订阅范围")
            return {tenant_id: catalog_models[tenant_id] for tenant_id in tenant_ids}

        catalog_result = await self._magik_tool.execute(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            comparison="none",
            include_tpm=False,
            report_template="matrix_card",
            granularity="day",
            interactive=True,
            report_selections=[
                {
                    "tenant_query": tenant_id,
                    "model_scope": "selected",
                    "models": [],
                }
                for tenant_id in tenant_ids
            ],
            save_snapshot=False,
            _trusted_selection_limit=20,
        )
        catalog_ui = (
            catalog_result.metadata.get(OUTBOUND_META_AGENT_UI)
            if catalog_result.metadata
            else None
        )
        catalog_entries = (
            catalog_ui.get("tenant_models", [])
            if isinstance(catalog_ui, dict) and catalog_ui.get("phase") == "models"
            else []
        )
        catalog_models = {
            str(item.get("tenant_query") or "").strip(): [
                str(model_name).strip()
                for model_name in item.get("models") or []
                if str(model_name).strip()
            ]
            for item in catalog_entries
            if isinstance(item, dict) and str(item.get("tenant_query") or "").strip()
        }
        missing = [tenant_id for tenant_id in tenant_ids if not catalog_models.get(tenant_id)]
        if getattr(catalog_result, "is_error", False) or missing:
            raise LookupError(
                "Cube 实时模型目录未返回可用模型，请检查客户模型配置或目录查询权限"
            )
        if sum(len(catalog_models[tenant_id]) for tenant_id in tenant_ids) > 200:
            raise ValueError("客户模型组合超过 200 个，请缩小订阅范围")
        return {tenant_id: catalog_models[tenant_id] for tenant_id in tenant_ids}

    async def _run_multi_scope_brief(
        self,
        *,
        period: str,
        tenants: list[str],
        models: list[str],
        all_tenants: bool,
        interactive: bool,
        start_date: str,
        end_date: str,
        report_selections: list[dict[str, Any]] | None = None,
    ) -> ToolResult:
        """Run the explicit multi-customer/model brief after scope selection."""

        if not self._config.cube_multi_scope_brief:
            return ToolResult.error("Error: multi-customer model brief is not enabled")
        if period != "day":
            return ToolResult.error("Error: multi-customer model brief supports day only")
        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        if interactive and not tenants and not report_selections:
            if self._magik_tool is None:
                return ToolResult.error("Error: Cube scope selector is unavailable")
            today = datetime.now(ZoneInfo(self._config.timezone)).date()
            selected_day, _ = self._cube_period_dates("day", today)
            result = await self._magik_tool.execute(
                start_date=selected_day.isoformat(),
                end_date=selected_day.isoformat(),
                comparison="none",
                include_tpm=False,
                report_template="matrix_card",
                granularity="day",
                interactive=True,
                save_snapshot=False,
            )
            ui = result.metadata.get(OUTBOUND_META_AGENT_UI) if result.metadata else None
            if isinstance(ui, dict) and ui.get("kind") == "magik_report_form":
                ui["title"] = "选择多客户多模型日报范围"
                ui["base_params"] = {
                    "action": "multi_scope_brief",
                    "period": "day",
                    "interactive": False,
                    "start_date": selected_day.isoformat(),
                    "end_date": selected_day.isoformat(),
                    # The form UI comes from the legacy Cube tool, but every callback
                    # in this workflow must resume the multi-scope ReportRunner action.
                    "_report_center_selector": True,
                }
                ui["max_tenants"] = 20
            return result
        selections = [item for item in report_selections or [] if isinstance(item, dict)]
        needs_model_selection = interactive and any(
            str(item.get("model_scope") or "") == "selected" and not item.get("models")
            for item in selections
        )
        if needs_model_selection:
            if self._magik_tool is None:
                return ToolResult.error("Error: Cube model selector is unavailable")
            result = await self._magik_tool.execute(
                start_date=start_date,
                end_date=end_date,
                comparison="none",
                include_tpm=False,
                report_template="matrix_card",
                granularity="day",
                interactive=True,
                report_selections=selections,
                save_snapshot=False,
                # Tool-schema callers cannot submit this private control. It only
                # raises the internal selector bound for the explicitly gated
                # multi-scope workflow; ordinary compatibility calls keep their cap.
                _trusted_selection_limit=20,
            )
            ui = result.metadata.get(OUTBOUND_META_AGENT_UI) if result.metadata else None
            if isinstance(ui, dict) and ui.get("kind") == "magik_report_form":
                ui["title"] = "选择多客户日报模型"
                ui["base_params"] = {
                    "action": "multi_scope_brief",
                    "period": "day",
                    "interactive": False,
                    "start_date": start_date,
                    "end_date": end_date,
                    "_report_center_selector": True,
                }
                ui["max_tenants"] = 20
            return result
        if selections:
            tenants = [
                str(item.get("tenant_query") or "").strip()
                for item in selections
                if str(item.get("tenant_query") or "").strip()
            ]
            tenant_models = {
                tenant: [str(model).strip() for model in item.get("models") or [] if str(model).strip()]
                for item in selections
                for tenant in [str(item.get("tenant_query") or "").strip()]
                if tenant and str(item.get("model_scope") or "") == "selected"
            }
            all_model_tenants = [
                str(item.get("tenant_query") or "").strip()
                for item in selections
                if str(item.get("model_scope") or "") == "all"
                and str(item.get("tenant_query") or "").strip()
            ]
            all_models = bool(all_model_tenants)
        else:
            tenant_models = {}
            all_model_tenants = []
            all_models = False
        if not tenants and not all_tenants:
            return ToolResult.error("请选择至少一个客户")
        if not all_models and not models and not tenant_models:
            return ToolResult.error("请选择至少一个模型")
        try:
            target_start = date.fromisoformat(start_date) if start_date else None
            target_end = date.fromisoformat(end_date) if end_date else None
        except ValueError:
            return ToolResult.error("报表日期需要使用 YYYY-MM-DD 格式。")
        if target_start is None or target_end is None:
            target_start, target_end = self._cube_period_dates(
                "day", datetime.now(ZoneInfo(self._config.timezone)).date()
            )
        if target_start != target_end:
            return ToolResult.error("多客户多模型简报只支持单日")
        if all_model_tenants:
            try:
                catalog_models = await self._load_tenant_model_catalog(
                    all_model_tenants,
                    start_date=target_start,
                    end_date=target_end,
                )
            except (LookupError, ValueError) as exc:
                return ToolResult.error(
                    f"{exc}。"
                )
            tenant_models.update(
                {tenant_id: catalog_models[tenant_id] for tenant_id in all_model_tenants}
            )
        template = self._registry.template("usage_customer_model_daily_brief")
        if template is None:
            return ToolResult.error("Error: multi-customer model brief template is unavailable")
        scoped_model_values = tuple(
            dict.fromkeys(
                [*models]
                + [
                    model_name
                    for tenant_values in tenant_models.values()
                    for model_name in tenant_values
                ]
            )
        )
        selected_models = self._canonical_cube_models(scoped_model_values)
        if len(selected_models) > 20 and not all_models:
            return ToolResult.error("多客户多模型简报最多支持 20 个模型，请缩小范围。")
        canonical_by_name = {item.casefold(): item for item in selected_models}
        tenant_models = {
            tenant_id: list(
                dict.fromkeys(
                    canonical_by_name.get(model_name.casefold(), model_name)
                    for model_name in tenant_values
                )
            )
            for tenant_id, tenant_values in tenant_models.items()
        }
        # Keep the shared Intent's explicit-selection cap intact. Catalog-expanded
        # "all" models remain in tenant_models, which ReportRunner authorizes item
        # by item and CubeConnector bounds to 200 tenant/model combinations.
        intent_models = () if all_models else selected_models
        intent = ReportIntent(
            connector_id="magik_cube",
            template_id=template.manifest.template_id,
            period="day",
            tenant_scope="all" if all_tenants else "selected",
            tenants=tuple(dict.fromkeys(tenants)),
            models=intent_models,
            start_date=target_start,
            end_date=target_end,
            filters={
                "tenants": list(dict.fromkeys(tenants)),
                "tenant_scope": "all" if all_tenants else "selected",
                "models": list(intent_models),
                "model_scope": "all" if all_models else "selected",
                "all_tenants": all_tenants,
                "tenant_models": tenant_models,
                "multi_scope": True,
            },
        )
        trace_id = uuid.uuid4().hex
        try:
            outcome = await ReportRunner(
                self._registry,
                self._store,
                semantic_shadow_enabled=self._config.cube_semantics_shadow,
                template_policy_enforced=self._config.report_management_v1,
            ).run(
                intent,
                ReportRunContext(
                    channel=channel,
                    chat_id=chat_id,
                    user_id=user_id,
                    timezone=self._config.timezone,
                    trace_id=trace_id,
                    template_version=template.manifest.version,
                    metadata=metadata,
                ),
            )
        except PermissionError:
            return ToolResult.error("当前账号没有执行多客户多模型简报的权限，请联系管理员授权。")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: multi-customer model brief unavailable: {exc}")
        return self._result(
            outcome.document,
            report_reference=self._report_reference_payload(
                intent, document=outcome.document, run_id=trace_id
            ),
        )

    async def _run_machine_tpm_report(
        self,
        *,
        period: str,
        model: str,
        cluster: str,
        start_date: str,
        end_date: str,
    ) -> ToolResult:
        """Generate the read-only machine TPM report for one validated model."""

        if not self._config.cube_machine_tpm_report:
            return ToolResult.error("Error: machine TPM report is not enabled")
        if not model.strip():
            return ToolResult("请指定一个模型，例如：Kimi-K3 单机 TPM 峰值。")
        if period not in {"day", "week", "range"}:
            return ToolResult.error("Error: machine TPM report supports day, week, or range")
        try:
            if start_date or end_date:
                target_start = date.fromisoformat(start_date)
                target_end = date.fromisoformat(end_date)
            else:
                target_start, target_end = self._cube_period_dates(
                    period, datetime.now(ZoneInfo(self._config.timezone)).date()
                )
        except ValueError:
            return ToolResult.error("报表日期需要使用 YYYY-MM-DD 格式。")
        if target_end < target_start or (target_end - target_start).days >= 90:
            return ToolResult.error("机器 TPM 报表日期范围无效或超过 90 天。")
        selected_model = self._canonical_cube_models((model.strip(),))
        if len(selected_model) != 1:
            return ToolResult.error("请选择一个有效模型")
        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        template = self._registry.template("machine_tpm_peak")
        if template is None:
            return ToolResult.error("Error: machine TPM report template is unavailable")
        intent = ReportIntent(
            connector_id="magik_cube",
            template_id=template.manifest.template_id,
            period=period,  # type: ignore[arg-type]
            models=selected_model,
            start_date=target_start,
            end_date=target_end,
            filters={"models": list(selected_model), "cluster": cluster.strip()},
        )
        try:
            outcome = await ReportRunner(
                self._registry,
                self._store,
                semantic_shadow_enabled=self._config.cube_semantics_shadow,
                template_policy_enforced=self._config.report_management_v1,
            ).run(
                intent,
                ReportRunContext(
                    channel=channel,
                    chat_id=chat_id,
                    user_id=user_id,
                    timezone=self._config.timezone,
                    trace_id=uuid.uuid4().hex,
                    template_version=template.manifest.version,
                    metadata=metadata,
                ),
            )
        except PermissionError:
            return ToolResult.error("当前账号没有执行机器 TPM 报表的权限，请联系管理员授权。")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: machine TPM report unavailable: {exc}")
        return self._result(outcome.document)

    async def _run_scope_selector(
        self,
        *,
        period: str,
        report_family: str,
        report_template: str = "matrix_card",
    ) -> ToolResult:
        """Reuse the proven catalog card while returning to ReportRunner for execution."""

        if self._magik_tool is None:
            return ToolResult.error("Error: Cube scope selector is unavailable")
        today = datetime.now(ZoneInfo(self._config.timezone)).date()
        start_date, end_date = self._cube_period_dates(period, today)
        granularity = "week" if period == "month" else "day"
        result = await self._magik_tool.execute(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            comparison="previous_period",
            include_tpm=True,
            report_template="matrix_card",
            granularity=granularity,
            interactive=True,
            save_snapshot=False,
        )
        if (
            report_family == "usage"
            and report_template != "brief"
            and not self._config.cube_scope_selector_v2
        ):
            return result
        ui = result.metadata.get(OUTBOUND_META_AGENT_UI) if result.metadata else None
        if not isinstance(ui, dict) or ui.get("kind") != "magik_report_form":
            return result
        ui["title"] = "选择成本账户范围" if report_family == "cost" else "选择 Cube 报表范围"
        ui["base_params"] = {
            "action": "cost_report" if report_family == "cost" else "cube_report",
            "period": period,
            "report_family": report_family,
            "report_template": report_template,
            "_report_center_selector": True,
        }
        ui["max_tenants"] = 1
        if report_family == "cost":
            ui["scope_options"] = [{"value": "summary", "label": "账务汇总"}]
        elif report_family == "usage":
            ui["report_template_options"] = [
                {"value": "brief", "label": "简报（默认）"},
                {"value": "matrix_card", "label": "详细分析"},
                {"value": "full", "label": "完整报表"},
            ]
        return result

    async def _run_selector_model_stage(
        self, *, period: str, tenant_query: str, report_template: str
    ) -> ToolResult:
        """Load one authorized tenant's model catalog, then resume ReportRunner."""

        if not tenant_query or self._magik_tool is None:
            return ToolResult.error("Error: Cube model selector is unavailable")
        today = datetime.now(ZoneInfo(self._config.timezone)).date()
        start_date, end_date = self._cube_period_dates(period, today)
        result = await self._magik_tool.execute(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            comparison="previous_period",
            include_tpm=True,
            report_template="matrix_card",
            granularity="week" if period == "month" else "day",
            interactive=True,
            report_selections=[
                {"tenant_query": tenant_query, "model_scope": "selected", "models": []}
            ],
            save_snapshot=False,
        )
        ui = result.metadata.get(OUTBOUND_META_AGENT_UI) if result.metadata else None
        if isinstance(ui, dict) and ui.get("kind") == "magik_report_form":
            ui["base_params"] = {
                "action": "cube_report",
                "period": period,
                "report_family": "usage",
                "report_template": report_template,
                "_report_center_selector": True,
            }
            ui["max_tenants"] = 1
        return result

    @staticmethod
    def _request_identity() -> tuple[str, str, str, str, dict[str, Any]]:
        ctx = current_request_context()
        if ctx is None or not ctx.channel or not ctx.chat_id:
            raise ValueError("report center requires a routed request context")
        return (
            ctx.channel,
            ctx.chat_id,
            ctx.sender_id or "",
            ctx.session_key or f"{ctx.channel}:{ctx.chat_id}",
            dict(ctx.metadata or {}),
        )

    def _report_reference_payload(
        self, intent: ReportIntent, *, document: ReportDocument, run_id: str
    ) -> dict[str, Any]:
        """Build the minimal safe scope needed to recreate a subscription."""

        model_scope = str(intent.filters.get("model_scope") or intent.model_scope or "summary")
        tenants = list(dict.fromkeys(intent.tenants or ((intent.tenant,) if intent.tenant else ())))
        configured_aliases = getattr(self._cube_config, "tenant_mappings", {}) or {}
        tenant_labels = {
            tenant: next(
                (alias for alias, tenant_id in configured_aliases.items() if tenant_id == tenant),
                tenant,
            )
            for tenant in tenants
        }
        report_variant = (
            "customer_model_daily_brief"
            if intent.template_id == "usage_customer_model_daily_brief"
            else "usage_brief"
        )
        if report_variant == "customer_model_daily_brief":
            tenant_models = intent.filters.get("tenant_models")
            report_selections = [
                {
                    "tenant_query": tenant,
                    "model_scope": model_scope,
                    "models": (
                        []
                        if model_scope == "all"
                        else list(tenant_models.get(tenant, intent.models))
                        if isinstance(tenant_models, dict)
                        else list(intent.models)
                    ),
                }
                for tenant in tenants
            ]
        else:
            report_selections = (
                [
                    {
                        "tenant_query": intent.tenant,
                        "model_scope": model_scope,
                        "models": list(intent.models),
                    }
                ]
                if intent.tenant
                else []
            )
        return {
            "run_id": run_id,
            "document_id": document.document_id,
            "connector_id": intent.connector_id,
            "template_id": intent.template_id,
            "period": intent.period,
            "expires_at": (
                datetime.now(UTC)
                + timedelta(days=self._config.cube_report_reference_retention_days)
            ).isoformat(),
            "scope": {
                "report_variant": report_variant,
                "tenant_scope": intent.tenant_scope or "selected",
                "tenant_query": intent.tenant,
                "tenants": tenants,
                "tenant_labels": tenant_labels,
                "all_tenants": intent.filters.get("all_tenants") is True,
                "model_scope": model_scope,
                "models": list(intent.models),
                "report_selections": report_selections,
                "project": intent.project,
                "endpoint": intent.endpoint,
                "provider": intent.provider,
                "report_template": "brief",
                "breakdown": "model" if model_scope in {"all", "selected"} else "summary",
            },
        }

    @staticmethod
    def _result(
        document: Any, *, report_reference: dict[str, Any] | None = None
    ) -> ToolResult:
        metadata = {OUTBOUND_META_AGENT_UI: document.to_agent_ui()}
        if report_reference:
            metadata[OUTBOUND_META_REPORT_REFERENCE] = report_reference
        return ToolResult(
            document.fallback_text,
            metadata=metadata,
        )

    @staticmethod
    def _with_delivery_metadata(
        result: ToolResult,
        *,
        idempotency_key: str,
        run_id: str,
        report_attempts: int,
    ) -> ToolResult:
        result.metadata[OUTBOUND_META_REPORT_DELIVERY] = {
            "idempotency_key": idempotency_key,
            "run_id": run_id,
            "report_attempts": report_attempts,
        }
        return result

    def _authorized_for_magik(self, channel: str, user_id: str) -> bool:
        return self._magik_tool is not None and self._store.allowed(
            channel, user_id, "connector", "magik_cube"
        )

    def _subscription_policy_denial(
        self,
        *,
        channel: str,
        user_id: str,
        template_id: str,
    ) -> str | None:
        """Return a safe denial for a template before subscription side effects.

        The management plane is deliberately the single source of truth for
        lifecycle, audience, and opt-in policy.  When that plane is disabled,
        the legacy subscription behavior remains available for backwards
        compatibility; once enabled, wider-scope templates require an explicit
        policy row instead of inheriting an accidental default.
        """

        template = self._registry.template(template_id)
        if template is None:
            return "Error: report subscription template is unavailable"
        if template.manifest.lifecycle_state not in {"publish", "canary"}:
            return "Error: this report template is not available"
        if not self._config.report_management_v1:
            return None

        if self._store.rbac_enabled():
            required_grants = [
                ("capability", "subscriptions"),
                ("template", template_id),
            ]
            required_grants.extend(
                ("connector", connector_id)
                for connector_id in template.manifest.connector_ids
            )
            if any(
                not self._store.allowed(channel, user_id, resource_type, resource_id)
                for resource_type, resource_id in required_grants
            ):
                return "Error: no permission to subscribe to this report template"

        policy = self._store.template_policy(template_id)
        if policy is None and template_id in {
            "usage_customer_model_daily_brief",
            "machine_tpm_peak",
        }:
            return "Error: this report template does not allow subscriptions"
        if policy is None:
            return None
        if not policy["enabled"]:
            return "Error: this report template is disabled"
        mode = str(policy["subscription_mode"])
        if mode == "disabled":
            return "Error: this report template does not allow subscriptions"
        if mode == "allowlist" and not self._store.allowed(
            channel, user_id, "subscription_template", template_id
        ):
            return "Error: no permission to subscribe to this report template"
        return None

    def _filter_usage_subscription_actions(
        self,
        document: Any,
        *,
        channel: str,
        user_id: str,
        template_id: str,
    ) -> Any:
        """Hide brief subscription actions unless the full server capability is usable.

        The action remains independently authorized during setup and creation. This
        presentation gate prevents offering a control that the current Gateway,
        template lifecycle, or user grants cannot execute.
        """

        template = self._registry.template(template_id)
        policy = (
            self._store.template_policy(template_id)
            if self._config.report_subscription_button_policy
            else None
        )
        policy_denial = self._subscription_policy_denial(
            channel=channel,
            user_id=user_id,
            template_id=template_id,
        )
        allowed = bool(
            self._cron is not None
            and self._config.cube_subscription
            and user_id
            and self._authorized_for_magik(channel, user_id)
            and self._store.allowed(channel, user_id, "capability", "subscriptions")
            and template is not None
            and template.manifest.lifecycle_state in {"publish", "canary"}
            and self._store.allowed(channel, user_id, "template", template_id)
            and policy_denial is None
            and (policy is None or bool(policy.get("show_subscription_button", True)))
        )
        if allowed:
            return document

        blocks = []
        for block in document.blocks:
            if block.kind != "actions":
                blocks.append(block)
                continue
            actions = block.data.get("actions")
            if not isinstance(actions, list):
                blocks.append(block)
                continue
            visible_actions = [
                action
                for action in actions
                if not (isinstance(action, dict) and self._is_subscription_action(action))
            ]
            if visible_actions:
                blocks.append(replace(block, data={**block.data, "actions": visible_actions}))
        return replace(document, blocks=tuple(blocks))

    @staticmethod
    def _is_subscription_action(action: dict[str, Any]) -> bool:
        """Recognize legacy and current subscription controls for policy hiding."""

        action_id = str(action.get("action_id") or "").casefold()
        if action_id.startswith(("usage_subscription_setup:", "subscribe:", "subscription:")):
            return True
        params = action.get("params")
        return isinstance(params, dict) and str(params.get("action") or "").casefold() in {
            "subscription_setup",
            "subscription_preview",
            "subscribe",
        }

    @staticmethod
    def _subscription_unavailable_document(message: str) -> ReportDocument:
        """Return a safe recovery path when NLU or a quoted reference is unavailable."""

        return ReportDocument(
            title="无法创建报表订阅",
            fallback_text=message,
            quality="missing",
            blocks=(
                ReportBlock("note", {"content": message}),
                ReportBlock(
                    "actions",
                    {
                        "actions": [
                            {
                                "action_id": "subscriptions",
                                "label": "打开订阅中心",
                                "style": "primary",
                            }
                        ]
                    },
                ),
            ),
        )

    async def _subscription_preview(
        self,
        *,
        report_type: str,
        tenant_scope: str,
        tenant_aliases: list[str],
        model_scope: str,
        models: list[str],
        recurrence: str,
        send_time: str,
        weekday: int,
        month_day: int,
        inherit_report_scope: bool,
        reference_message_id: str,
    ) -> ToolResult:
        """Resolve NLU output and produce an explicit, opaque confirmation action."""

        channel, chat_id, user_id, _session_key, metadata = self._request_identity()
        if not user_id or not self._store.allowed(
            channel, user_id, "capability", "subscriptions"
        ):
            return ToolResult.error("Error: no permission to manage report subscriptions")
        reference = None
        if reference_message_id:
            if (
                not self._config.cube_report_reference_subscription
                or str(metadata.get("parent_id") or "") != reference_message_id
            ):
                return self._result(
                    self._subscription_unavailable_document(
                        "无法从该卡片恢复可验证的报表范围。请重新生成报表，或在订阅中心选择客户和模型。"
                    )
                )
            reference = self._store.message_reference(
                channel=channel,
                chat_id=chat_id,
                message_id=reference_message_id,
            )
            if reference is None:
                return self._result(
                    self._subscription_unavailable_document(
                        "引用报表已过期或不是可订阅的结构化报表。请重新生成报表后再引用。"
                    )
                )

        safe_report_types = {
            "usage_daily_brief": ("day", "usage_brief"),
            "usage_weekly_brief": ("week", "usage_brief"),
            "usage_monthly_brief": ("month", "usage_brief"),
            "usage_customer_model_daily_brief": ("day", "customer_model_daily_brief"),
        }
        unresolved: list[dict[str, str]] = []
        display_names: list[str] = []

        async def resolve_tenants(
            queries: list[str],
        ) -> tuple[list[dict[str, str]], list[dict[str, str]]] | None:
            resolver = getattr(self._magik_tool, "resolve_tenant_queries", None)
            if not callable(resolver):
                return None
            try:
                return await resolver(queries)
            except Exception as exc:
                logger.warning(
                    "Cube subscription tenant resolution failed: error_type={}",
                    type(exc).__name__,
                )
                return None

        def unique_resolved(
            items: list[dict[str, str]],
        ) -> list[dict[str, str]]:
            """Collapse aliases that resolve to one tenant before fan-out."""

            result: list[dict[str, str]] = []
            seen: set[str] = set()
            for item in items:
                tenant_id = str(item.get("tenant_id") or "").strip()
                if not tenant_id or tenant_id in seen:
                    continue
                seen.add(tenant_id)
                result.append(item)
            return result

        def apply_tenant_scope(
            target: dict[str, Any],
            resolved: list[dict[str, str]],
            *,
            selected_model_scope: str,
            selected_models: list[str],
        ) -> None:
            tenant_ids = [item["tenant_id"] for item in resolved]
            target["tenants"] = tenant_ids
            # Persist display labels next to the verified IDs.  Labels are only
            # presentation metadata; every later query and authorization check
            # continues to use the exact Cube tenant ID.
            target["tenant_labels"] = [
                str(item.get("display_name") or item.get("tenant_id") or "").strip()
                for item in resolved
            ]
            target["tenant_scope"] = "selected"
            target["all_tenants"] = False
            target["report_selections"] = [
                {
                    "tenant_query": tenant_id,
                    "model_scope": selected_model_scope,
                    "models": selected_models if selected_model_scope == "selected" else [],
                }
                for tenant_id in tenant_ids
            ]

        if inherit_report_scope:
            if reference is None or report_type != "inherit":
                return self._result(
                    self._subscription_unavailable_document(
                        "没有找到可继承的引用报表范围，请重新引用报表卡片。"
                    )
                )
            if reference.template_id not in {
                "usage_daily_brief",
                "usage_weekly_brief",
                "usage_monthly_brief",
                "usage_customer_model_daily_brief",
            }:
                return self._result(
                    self._subscription_unavailable_document("该报表类型当前不允许创建订阅。")
                )
            data_period = reference.period
            params = {
                key: value
                for key, value in reference.scope.items()
                if key in _ALLOWED_REPORT_PARAM_KEYS
            }
            if tenant_scope == "all" or (
                tenant_scope == "inherit" and params.get("all_tenants") is True
            ):
                params.update(
                    {
                        "tenant_scope": "all",
                        "all_tenants": True,
                        "tenants": [],
                        "report_selections": [],
                    }
                )
                display_names = ["全部客户"]
            else:
                tenant_queries = (
                    list(tenant_aliases)
                    if tenant_scope == "selected"
                    else [str(item) for item in params.get("tenants") or []]
                )
                resolution = await resolve_tenants(tenant_queries)
                if resolution is None:
                    return self._result(
                        self._subscription_unavailable_document(
                            "Cube 客户目录当前不可用，请稍后重试或打开订阅中心。"
                        )
                    )
                resolved, unresolved = resolution
                resolved = unique_resolved(resolved)
                if not resolved:
                    details = "、".join(
                        f"{item['query']}（{item['reason']}）" for item in unresolved
                    )
                    return self._result(
                        self._subscription_unavailable_document(
                            f"没有匹配到可订阅客户：{details or '请重新选择客户'}"
                        )
                    )
                effective_model_scope = (
                    model_scope
                    if model_scope != "inherit"
                    else str(params.get("model_scope") or "summary")
                )
                effective_models = (
                    list(models)
                    if model_scope == "selected"
                    else [str(item) for item in params.get("models") or []]
                )
                apply_tenant_scope(
                    params,
                    resolved,
                    selected_model_scope=effective_model_scope,
                    selected_models=effective_models,
                )
                display_names = [item["display_name"] for item in resolved]
            if model_scope != "inherit":
                params["model_scope"] = model_scope
                params["models"] = list(models) if model_scope == "selected" else []
                for selection in params.get("report_selections") or []:
                    if isinstance(selection, dict):
                        selection["model_scope"] = model_scope
                        selection["models"] = (
                            list(models) if model_scope == "selected" else []
                        )
        else:
            if report_type not in safe_report_types:
                return self._result(
                    self._subscription_unavailable_document("未识别出可订阅的 Cube 报表类型。")
                )
            data_period, report_variant = safe_report_types[report_type]
            # A long natural-language customer list is a product-level scope
            # signal.  The classifier may truncate that list, but once the
            # server has resolved more than one live tenant the subscription
            # must use the grouped template; otherwise the legacy one-tenant
            # brief silently drops all but the last selection.
            multi_scope_requested = (
                report_type == "usage_customer_model_daily_brief"
                or (tenant_scope == "selected" and len(tenant_aliases) > 1)
                or (tenant_scope == "all" and model_scope == "all")
            )
            if multi_scope_requested and data_period != "day":
                return self._result(
                    self._subscription_unavailable_document(
                        "多客户多模型订阅当前仅支持日报，请改为日报或分别创建周期订阅。"
                    )
                )
            if multi_scope_requested:
                report_type = "usage_customer_model_daily_brief"
                data_period, report_variant = safe_report_types[report_type]
            params = {
                "report_variant": report_variant,
                "tenant_scope": tenant_scope,
                "model_scope": model_scope,
                "models": list(models),
                "report_template": "brief",
                "breakdown": "model" if model_scope in {"all", "selected"} else "summary",
            }
            if tenant_scope == "all":
                params["all_tenants"] = True
                params["tenants"] = []
                display_names = ["全部客户"]
            else:
                resolution = await resolve_tenants(tenant_aliases)
                if resolution is None:
                    return self._result(
                        self._subscription_unavailable_document(
                            "Cube 客户目录当前不可用，请稍后重试或打开订阅中心。"
                        )
                    )
                resolved, unresolved = resolution
                resolved = unique_resolved(resolved)
                tenant_ids = [item["tenant_id"] for item in resolved]
                display_names = [item["display_name"] for item in resolved]
                if not tenant_ids:
                    details = "、".join(
                        f"{item['query']}（{item['reason']}）" for item in unresolved
                    )
                    return self._result(
                        self._subscription_unavailable_document(
                            f"没有匹配到可订阅客户：{details or '请重新选择客户'}"
                        )
                    )
                apply_tenant_scope(
                    params,
                    resolved,
                    selected_model_scope=model_scope,
                    selected_models=list(models),
                )
                if len(tenant_ids) == 1 and report_variant == "usage_brief":
                    params["tenant_query"] = tenant_ids[0]

        if params.get("model_scope") == "selected":
            selected_tenants = [
                str(item).strip()
                for item in params.get("tenants") or []
                if str(item).strip()
            ]
            selected_models = [
                str(item).strip()
                for item in params.get("models") or []
                if str(item).strip()
            ]
            model_resolver = getattr(
                self._magik_tool, "resolve_models_for_tenants", None
            )
            if selected_tenants and selected_models:
                if not callable(model_resolver):
                    return self._result(
                        self._subscription_unavailable_document(
                            "Cube 模型目录当前不可用，请稍后重试或打开订阅中心。"
                        )
                    )
                try:
                    tenant_models, unresolved_models = await model_resolver(
                        selected_tenants,
                        selected_models,
                    )
                except Exception as exc:
                    logger.warning(
                        "Cube subscription model resolution failed: error_type={}",
                        type(exc).__name__,
                    )
                    return self._result(
                        self._subscription_unavailable_document(
                            "Cube 模型目录当前不可用，请稍后重试或打开订阅中心。"
                        )
                    )
                if unresolved_models:
                    details = "、".join(
                        f"{item['tenant_id']} / {item['model']}（{item['reason']}）"
                        for item in unresolved_models
                    )
                    return self._result(
                        self._subscription_unavailable_document(
                            f"指定模型无法通过实时目录校验：{details}"
                        )
                    )
                params["models"] = list(
                    dict.fromkeys(
                        model
                        for tenant_id in selected_tenants
                        for model in tenant_models.get(tenant_id, [])
                    )
                )
                for selection in params.get("report_selections") or []:
                    if not isinstance(selection, dict):
                        continue
                    tenant_id = str(selection.get("tenant_query") or "")
                    selection["models"] = list(tenant_models.get(tenant_id, []))

        params["subscription_period"] = data_period
        if str(params.get("report_variant") or "") == "customer_model_daily_brief":
            if not self._config.cube_multi_scope_brief:
                return ToolResult.error("Error: multi-customer model brief is not enabled")
            params["report_template"] = "brief"
        try:
            params = self._safe_report_params(params)
        except ValueError as exc:
            return ToolResult.error(f"Error: {exc}")
        denial = authorize_magik_params(
            self._store, channel=channel, user_id=user_id, params=params
        )
        if denial:
            return ToolResult.error(denial)

        template_id = (
            "usage_customer_model_daily_brief"
            if str(params.get("report_variant") or "") == "customer_model_daily_brief"
            else _BRIEF_PERIOD_TEMPLATES[data_period]
        )
        policy_denial = self._subscription_policy_denial(
            channel=channel,
            user_id=user_id,
            template_id=template_id,
        )
        if policy_denial:
            return ToolResult.error(policy_denial)

        schedule_period = {
            "every_day": "day",
            "workdays": "day",
            "weekly": "week",
            "monthly": "month",
        }[recurrence]
        subscribe_params = {
            "action": "subscribe",
            "period": schedule_period,
            "report_family": "usage",
            "report_params": params,
            "send_time": send_time,
            "daily_mode": "workdays" if recurrence == "workdays" else "every_day",
            "weekday": weekday,
            "month_day": month_day,
        }
        unresolved_text = ""
        if unresolved:
            unresolved_text = "\n**未包含**：" + "、".join(
                f"{item['query']}（{item['reason']}）" for item in unresolved
            )
        model_text = (
            "每个客户的全部模型"
            if params.get("model_scope") == "all"
            else "、".join(str(item) for item in params.get("models") or [])
            if params.get("model_scope") == "selected"
            else "汇总"
        )
        recurrence_text = {
            "every_day": "每天",
            "workdays": "每个工作日",
            "weekly": f"每周{'一二三四五六日'[weekday - 1]}",
            "monthly": f"每月 {month_day} 日",
        }[recurrence]
        content = (
            f"**客户**：{'、'.join(display_names)}\n"
            f"**模型**：{model_text}\n"
            f"**发送计划**：{recurrence_text} {send_time}\n"
            f"**时区**：{self._config.timezone}{unresolved_text}"
        )
        if reference is not None:
            content += "\n**说明**：引用卡片的历史日期不会固化，发送时使用最近完整周期。"
        action_label = "确认仅订阅已匹配客户" if unresolved else "确认创建订阅"
        document = ReportDocument(
            title="确认 Cube 报表订阅",
            subtitle=f"{recurrence_text} {send_time}｜{self._config.timezone}",
            fallback_text=content,
            blocks=(
                ReportBlock("markdown", {"content": content}),
                ReportBlock(
                    "actions",
                    {
                        "actions": [
                            {
                                "action_id": "subscription_confirm",
                                "label": action_label,
                                "style": "primary",
                                "tool_name": "report_center",
                                "params": subscribe_params,
                                "content": "确认创建 Cube 报表订阅",
                            },
                            {
                                "action_id": "subscriptions",
                                "label": "取消并打开订阅中心",
                                "style": "default",
                            },
                        ]
                    },
                ),
            ),
        )
        return self._result(document)

    def _safe_report_params(self, value: Any) -> dict[str, Any]:
        """Normalize subscription scope while keeping internal controls server-owned.

        Subscription forms round-trip the already normalized parameters. Internal
        controls such as ``save_snapshot`` must therefore be removed before the
        external allowlist check and re-applied with the safe fixed value below.
        """

        if not isinstance(value, dict):
            raise ValueError("report_params must be an object")
        candidate = dict(value)
        candidate.pop("save_snapshot", None)
        unknown = set(candidate) - _ALLOWED_REPORT_PARAM_KEYS
        if unknown:
            raise ValueError("unsupported report subscription parameters")
        params = {
            key: candidate[key]
            for key in candidate
            if key in _ALLOWED_REPORT_PARAM_KEYS
        }
        params.setdefault(
            "report_template",
            "brief" if self._usage_brief_default_enabled else "matrix_card",
        )
        params["save_snapshot"] = False
        params.pop("start_date", None)
        params.pop("end_date", None)
        return params

    @staticmethod
    def _period_from_template(template_id: str) -> str:
        if "daily" in template_id:
            return "day"
        if "monthly" in template_id:
            return "month"
        return "week"

    @staticmethod
    def _subscription_period(subscription: ReportSubscription) -> str:
        saved_period = str(subscription.report_params.get("subscription_period") or "")
        if saved_period in {"day", "week", "month"}:
            return saved_period
        return ReportCenterTool._period_from_template(subscription.template_id)

    def _subscription_service_for_confirmed_scope(
        self,
        *,
        tenant_records: list[dict[str, str]] | None = None,
        model_records: dict[str, list[str]] | None = None,
        catalog_tenants: list[dict[str, Any]] | None = None,
    ) -> ReportSubscriptionService:
        """Build the shared service for a server-resolved channel confirmation.

        ``ReportCenterTool`` owns the async Cube adapters, while
        ``ReportSubscriptionService`` owns synchronous persistence and Cron
        mutation.  The small config adapter keeps that dependency direction
        explicit and prevents the service from depending on channel code.
        ``tenant_records`` and ``model_records`` are bounded snapshots used
        only to re-check a confirmation; scheduled executions still refresh
        ``all`` model scopes from Cube.
        """

        tenant_by_id = {
            str(item.get("tenant_id") or item.get("tenantId") or "").strip(): item
            for item in (tenant_records or [])
            if str(item.get("tenant_id") or item.get("tenantId") or "").strip()
        }

        def resolve_tenants(requested: list[str]):
            resolved: list[dict[str, str]] = []
            unresolved: list[dict[str, str]] = []
            for value in requested:
                record = tenant_by_id.get(value)
                if record is None:
                    unresolved.append({"query": value, "reason": "客户不在已验证目录中"})
                    continue
                resolved.append(
                    {
                        "query": value,
                        "tenant_id": value,
                        "display_name": str(
                            record.get("display_name")
                            or record.get("displayName")
                            or record.get("name")
                            or value
                        ).strip(),
                    }
                )
            return resolved, unresolved

        def resolve_models(tenant_ids: list[str], requested: list[str]):
            resolved: dict[str, list[str]] = {}
            unresolved: list[dict[str, str]] = []
            for tenant_id in tenant_ids:
                available = set(model_records.get(tenant_id, ())) if model_records else set()
                selected = [model for model in requested if model in available]
                resolved[tenant_id] = selected
                unresolved.extend(
                    {
                        "tenant_id": tenant_id,
                        "model": model,
                        "reason": "模型不在确认时的实时目录中",
                    }
                    for model in requested
                    if model not in available
                )
            return resolved, unresolved

        adapter = SimpleNamespace(
            workspace_path=None,
            tools=SimpleNamespace(reporting=self._config),
            agents=SimpleNamespace(
                defaults=SimpleNamespace(unified_session=False),
            ),
        )
        return ReportSubscriptionService(
            config=adapter,
            store=self._store,
            registry=self._registry,
            cron=self._cron,
            tenant_resolver=resolve_tenants if tenant_by_id else None,
            model_resolver=resolve_models if model_records is not None else None,
            catalog_tenants=catalog_tenants or tenant_records or [],
        )

    @staticmethod
    def _subscription_service_template_id(
        *,
        data_period: str,
        report_template: str,
        report_variant: str,
    ) -> str:
        if report_variant == "customer_model_daily_brief":
            return "usage_customer_model_daily_brief"
        if report_template == "brief":
            return _BRIEF_PERIOD_TEMPLATES[data_period]
        return _PERIOD_TEMPLATES[data_period]

    async def _revalidate_confirmed_usage_scope(
        self, params: dict[str, Any]
    ) -> tuple[
        list[dict[str, str]],
        dict[str, list[str]] | None,
        list[dict[str, Any]],
        str | None,
    ]:
        """Re-check IDs in a confirmation without trusting rendered card text.

        The preview already resolves human names, but the confirmation action
        is still client-controlled.  A real Cube adapter result is therefore
        preferred; a bounded opaque-ID fallback is retained only for legacy
        adapters that do not expose a resolver.  It rejects aliases, URLs and
        control characters rather than assuming a particular Cube ID prefix.
        """

        all_tenants = params.get("all_tenants") is True or params.get("tenant_scope") == "all"
        requested = [
            str(item).strip()
            for item in params.get("tenants") or []
            if str(item).strip()
        ]
        if not requested and params.get("tenant_query"):
            requested = [str(params["tenant_query"]).strip()]
        records: list[dict[str, str]] = []
        catalog: list[dict[str, Any]] = []
        resolver = getattr(self._magik_tool, "resolve_tenant_queries", None)
        # Normalized confirmations carry ``tenants``/``tenant_scope``.  A
        # legacy row may contain only ``tenant_query`` and is intentionally
        # allowed to use the bounded opaque-ID fallback during the migration
        # window; probing an absent/old adapter in that path would turn a
        # compatibility subscription into a false catalog outage.
        normalized_scope = bool(
            "tenants" in params
            or "tenant_scope" in params
            or "report_variant" in params
        )
        if not all_tenants and requested and callable(resolver) and normalized_scope:
            try:
                response = await resolver(requested)
            except Exception as exc:
                logger.warning(
                    "Cube subscription confirmation tenant check failed: error_type={}",
                    type(exc).__name__,
                )
                return [], None, [], "客户目录在确认时不可用，请重新选择客户"
            if not (isinstance(response, tuple) and len(response) == 2):
                return [], None, [], "客户范围在确认时无法验证，请重新选择客户"
            if isinstance(response, tuple) and len(response) == 2:
                resolved, unresolved = response
                if unresolved:
                    return [], None, [], "客户范围在确认时已变化，请重新选择客户"
                if isinstance(resolved, list):
                    records = [item for item in resolved if isinstance(item, dict)]
                    requested_values = list(dict.fromkeys(requested))
                    resolved_ids = [
                        str(item.get("tenant_id") or item.get("tenantId") or "").strip()
                        for item in records
                    ]
                    query_values = [
                        str(item.get("query") or "").strip().casefold() for item in records
                    ]
                    query_match_valid = True
                    ids_are_requested_values = set(resolved_ids) == set(requested_values)
                    if any(query_values) and not ids_are_requested_values:
                        query_match_valid = (
                            all(query_values)
                            and len(set(query_values)) == len(query_values)
                            and set(query_values) == {item.casefold() for item in requested_values}
                        )
                    if (
                        len(records) != len(requested_values)
                        or any(not _SAFE_CUBE_ID_RE.fullmatch(item) for item in resolved_ids)
                        or len(set(resolved_ids)) != len(resolved_ids)
                        or not query_match_valid
                    ):
                        return [], None, [], "客户范围在确认时未能完整验证，请重新选择客户"
                    params["tenants"] = [
                        str(item.get("tenant_id") or item.get("tenantId") or "").strip()
                        for item in records
                    ]
                    params["tenant_labels"] = [
                        str(
                            item.get("display_name")
                            or item.get("displayName")
                            or item.get("name")
                            or item.get("tenant_id")
                            or item.get("tenantId")
                            or ""
                        ).strip()
                        for item in records
                    ]
                    requested = list(params["tenants"])
        if not records and not all_tenants:
            if not requested:
                return [], None, [], "没有可验证的客户范围，请重新选择客户"
            if any(not _SAFE_CUBE_ID_RE.fullmatch(value) for value in requested):
                return [], None, [], "客户身份无法再次验证，请重新生成选择器"
            records = [
                {
                    "tenant_id": value,
                    "display_name": str(
                        (params.get("tenant_labels") or [])[index]
                        if index < len(params.get("tenant_labels") or [])
                        else value
                    ),
                }
                for index, value in enumerate(requested)
            ]
        if all_tenants:
            loader = getattr(self._magik_tool, "list_tenant_catalog", None)
            if callable(loader):
                try:
                    loaded = await loader(limit=20)
                except Exception as exc:
                    logger.warning(
                        "Cube subscription confirmation catalog check failed: error_type={}",
                        type(exc).__name__,
                    )
                    return [], None, [], "Cube 客户目录在确认时不可用，请重新选择客户"
                if not isinstance(loaded, list):
                    return [], None, [], "Cube 客户目录在确认时无法验证，请重新选择客户"
                catalog = [item for item in loaded if isinstance(item, dict)]
                if not catalog:
                    return [], None, [], "Cube 客户目录在确认时为空，请重新选择客户"

        model_records: dict[str, list[str]] | None = None
        selected_models = [
            str(item).strip()
            for item in params.get("models") or []
            if str(item).strip()
        ]
        if str(params.get("model_scope") or "") == "selected" and selected_models:
            tenant_ids = requested or [
                str(item.get("tenant_id") or item.get("tenantId") or "").strip()
                for item in catalog
            ]
            model_resolver = getattr(self._magik_tool, "resolve_models_for_tenants", None)
            if tenant_ids and callable(model_resolver):
                try:
                    response = await model_resolver(tenant_ids, selected_models)
                except Exception as exc:
                    logger.warning(
                        "Cube subscription confirmation model check failed: error_type={}",
                        type(exc).__name__,
                    )
                    response = None
                if isinstance(response, tuple) and len(response) == 2:
                    resolved_models, unresolved_models = response
                    if unresolved_models:
                        return [], None, [], "模型范围在确认时已变化，请重新选择模型"
                    if isinstance(resolved_models, dict):
                        model_records = {
                            str(tenant_id): [
                                str(model).strip()
                                for model in values
                                if str(model).strip()
                            ]
                            for tenant_id, values in resolved_models.items()
                            if isinstance(values, (list, tuple, set))
                        }
                        params["models"] = list(
                            dict.fromkeys(
                                model
                                for values in model_records.values()
                                for model in values
                            )
                        )
                        for selection in params.get("report_selections") or []:
                            if isinstance(selection, dict):
                                tenant_id = str(selection.get("tenant_query") or "")
                                selection["models"] = list(model_records.get(tenant_id, ()))
        return records, model_records, catalog, None

    async def _subscribe_usage_via_service(
        self,
        *,
        period: str,
        report_params: dict[str, Any],
        channel: str,
        chat_id: str,
        user_id: str,
        send_time: str,
        daily_mode: str,
        weekday: int,
        month_day: int,
    ) -> ToolResult:
        """Create usage subscriptions through the shared guided service."""

        if period not in {"day", "week", "month"}:
            return ToolResult.error("Error: subscription period must be day, week, or month")
        params = dict(report_params)
        records, model_records, catalog, error = await self._revalidate_confirmed_usage_scope(
            params
        )
        if error:
            return ToolResult.error(f"Error: {error}")
        data_period = str(params.get("subscription_period") or period)
        if data_period not in {"day", "week", "month"}:
            return ToolResult.error("Error: invalid report data period")
        report_variant = str(params.get("report_variant") or "")
        report_template = str(params.get("report_template") or "brief")
        template_id = self._subscription_service_template_id(
            data_period=data_period,
            report_template=report_template,
            report_variant=report_variant,
        )
        if report_variant == "customer_model_daily_brief" and not self._config.cube_multi_scope_brief:
            return ToolResult.error("Error: multi-customer model brief is not enabled")
        if period == "week":
            recurrence = "weekly"
        elif period == "month":
            recurrence = "monthly"
        else:
            recurrence = "workdays" if daily_mode == "workdays" else "every_day"
        tenant_scope = (
            "all"
            if params.get("all_tenants") is True or params.get("tenant_scope") == "all"
            else "selected"
        )
        tenants = [
            str(item).strip()
            for item in params.get("tenants") or []
            if str(item).strip()
        ]
        if not tenants and params.get("tenant_query"):
            tenants = [str(params["tenant_query"]).strip()]
        model_scope = str(params.get("model_scope") or "")
        if not model_scope:
            model_scope = "selected" if params.get("model") else "summary"
        models = [
            str(item).strip()
            for item in params.get("models") or []
            if str(item).strip()
        ]
        if not models and params.get("model"):
            models = [str(params["model"]).strip()]
        form = {
            "template_id": template_id,
            "channel": channel,
            "chat_id": chat_id,
            "user_id": user_id,
            "tenant_scope": tenant_scope,
            "tenants": tenants,
            "tenant_labels": params.get("tenant_labels") or [],
            "model_scope": model_scope,
            "models": models,
            "period": data_period,
            "recurrence": recurrence,
            "send_time": send_time,
            "weekday": weekday,
            "month_day": month_day,
            "timezone": self._config.timezone,
            "project": params.get("project", ""),
            "endpoint": params.get("endpoint", ""),
            "provider": params.get("provider", ""),
            "cluster": params.get("cluster", ""),
        }
        service = self._subscription_service_for_confirmed_scope(
            tenant_records=records,
            model_records=model_records,
            catalog_tenants=catalog,
        )
        try:
            subscription = service.create(form, updated_by=user_id)
        except SubscriptionServiceError as exc:
            if exc.status == 409 and "identical" in exc.message:
                return ToolResult("相同报表和发送计划的订阅已经存在。")
            return ToolResult.error(f"Error: {exc.message}")
        return self._result(subscription_created_document(subscription))

    def _dynamic_magik_params(self, subscription: ReportSubscription) -> dict[str, Any]:
        period = self._subscription_period(subscription)
        intent = MagikReportIntent(report_kind=period)  # type: ignore[arg-type]
        params = intent.to_tool_params(
            today=datetime.now(ZoneInfo(subscription.timezone)).date()
        )
        saved = dict(subscription.report_params)
        params.update(saved)
        params.setdefault(
            "report_template",
            "brief" if self._usage_brief_default_enabled else "matrix_card",
        )
        params["save_snapshot"] = False
        params.pop("report_family", None)
        params.pop("subscription_period", None)
        params.pop("calculation_version", None)
        params.pop("threshold_version", None)
        return params

    def _subscription_cube_intent(
        self,
        subscription: ReportSubscription,
        *,
        tenant_models: dict[str, list[str]] | None = None,
    ) -> ReportIntent | None:
        """Compile the compatible one-scope subscription into a safe Intent."""

        params = self._dynamic_magik_params(subscription)
        family = str(subscription.report_params.get("report_family") or "usage")
        period = self._subscription_period(subscription)
        if subscription.template_id == "machine_tpm_peak":
            if not self._config.cube_machine_tpm_report:
                return None
            try:
                start_date = date.fromisoformat(str(params["start_date"]))
                end_date = date.fromisoformat(str(params["end_date"]))
            except (KeyError, TypeError, ValueError):
                return None
            model = str(params.get("model") or "").strip()
            if not model:
                return None
            return ReportIntent(
                connector_id="magik_cube",
                template_id="machine_tpm_peak",
                period=period,  # type: ignore[arg-type]
                models=self._canonical_cube_models((model,)),
                start_date=start_date,
                end_date=end_date,
                filters={
                    "models": [model],
                    "model_scope": "selected",
                    "cluster": str(params.get("cluster") or "").strip(),
                },
            )
        if subscription.template_id == "usage_customer_model_daily_brief":
            if not self._config.cube_multi_scope_brief or period != "day":
                return None
            try:
                start_date = date.fromisoformat(str(params["start_date"]))
                end_date = date.fromisoformat(str(params["end_date"]))
            except (KeyError, TypeError, ValueError):
                return None
            tenants = tuple(
                dict.fromkeys(
                    str(item).strip()
                    for item in params.get("tenants") or []
                    if str(item).strip()
                )
            )
            selections = [
                item
                for item in params.get("report_selections") or []
                if isinstance(item, dict)
            ]
            if not tenants:
                tenants = tuple(
                    dict.fromkeys(
                        str(item.get("tenant_query") or "").strip()
                        for item in selections
                        if str(item.get("tenant_query") or "").strip()
                    )
                )
            model_scope = str(params.get("model_scope") or "selected")
            models = self._canonical_cube_models(
                tuple(
                    dict.fromkeys(
                        str(item).strip()
                        for item in params.get("models") or []
                        if str(item).strip()
                    )
                )
            )
            if model_scope == "all":
                if not tenants or tenant_models is None:
                    return None
                # An all-model subscription resolves its live per-tenant catalog
                # immediately before each run; the static model tuple stays empty.
                models = ()
            elif model_scope != "selected" or not tenants or not models:
                return None
            return ReportIntent(
                connector_id="magik_cube",
                template_id="usage_customer_model_daily_brief",
                period="day",
                tenant_scope=(
                    "all" if params.get("all_tenants") is True else "selected"
                ),
                tenants=tenants,
                models=models,
                start_date=start_date,
                end_date=end_date,
                filters={
                    "tenants": list(tenants),
                    "tenant_scope": (
                        "all" if params.get("all_tenants") is True else "selected"
                    ),
                    "models": list(models),
                    "model_scope": model_scope,
                    "tenant_models": tenant_models or {},
                    "multi_scope": True,
                },
            )
        if family == "provider_quality":
            if not self.provider_quality_subscriptions_enabled or period not in {"day", "week"}:
                return None
            try:
                start_date = date.fromisoformat(str(params["start_date"]))
                end_date = date.fromisoformat(str(params["end_date"]))
            except (KeyError, TypeError, ValueError):
                return None
            model = str(params.get("model") or "").strip()
            providers = tuple(
                dict.fromkeys(
                    str(item).strip()
                    for item in (params.get("providers") or [])
                    if str(item).strip()
                )
            )
            return ReportIntent(
                connector_id=subscription.connector_id,
                template_id="provider_quality",
                period=period,  # type: ignore[arg-type]
                models=self._canonical_cube_models((model,)) if model else (),
                start_date=start_date,
                end_date=end_date,
                provider=str(params.get("provider") or "").strip(),
                endpoint=str(params.get("endpoint") or "").strip(),
                filters={
                    "provider_quality": True,
                    "provider": str(params.get("provider") or "").strip(),
                    "providers": list(providers),
                    "provider_id": str(params.get("provider_id") or "").strip(),
                    "model": model,
                    "endpoint": str(params.get("endpoint") or "").strip(),
                },
            )
        if family == "health":
            if not self.health_subscriptions_enabled:
                return None
            try:
                start_date = date.fromisoformat(str(params["start_date"]))
                end_date = date.fromisoformat(str(params["end_date"]))
            except (KeyError, TypeError, ValueError):
                return None
            return ReportIntent(
                connector_id=subscription.connector_id,
                template_id="health_sre",
                period=period,  # type: ignore[arg-type]
                start_date=start_date,
                end_date=end_date,
                filters={},
            )
        if family == "cost":
            if not self.cost_subscriptions_enabled or period != "month":
                return None
            try:
                start_date = date.fromisoformat(str(params["start_date"]))
                end_date = date.fromisoformat(str(params["end_date"]))
            except (KeyError, TypeError, ValueError):
                return None
            tenant = str(params.get("tenant_query") or "").strip()
            if not tenant:
                return None
            model = str(params.get("model") or "").strip()
            return ReportIntent(
                connector_id=subscription.connector_id,
                template_id="cost_account",
                period="month",
                tenant=tenant,
                project=str(params.get("project") or "").strip(),
                endpoint=str(params.get("endpoint") or "").strip(),
                model_scope="selected" if model else "summary",
                models=(model,) if model else (),
                start_date=start_date,
                end_date=end_date,
                filters={
                    "tenant": tenant,
                    "project": str(params.get("project") or "").strip(),
                    "endpoint": str(params.get("endpoint") or "").strip(),
                    "models": [model] if model else [],
                    "model_scope": "selected" if model else "summary",
                },
            )
        selections = params.get("report_selections")
        if isinstance(selections, list) and len(selections) > 1:
            # The legacy matrix supports several tenant/model scopes. Keep it on
            # the compatibility path until Cube has a bulk-scope contract.
            return None
        selection = selections[0] if isinstance(selections, list) and selections else {}
        if not isinstance(selection, dict):
            selection = {}
        try:
            start_date = date.fromisoformat(str(params["start_date"]))
            end_date = date.fromisoformat(str(params["end_date"]))
        except (KeyError, TypeError, ValueError):
            return None
        tenant = str(selection.get("tenant_query") or params.get("tenant_query") or "").strip()
        model = str(params.get("model") or "").strip()
        models = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in (selection.get("models") or params.get("models") or [])
                if str(item).strip()
            )
        )
        if model and not models:
            models = (model,)
        models = self._canonical_cube_models(models)
        model_scope = str(
            selection.get("model_scope") or params.get("model_scope") or "summary"
        )
        if model_scope not in {"summary", "all", "selected"}:
            return None
        all_tenants = params.get("all_tenants", False)
        if not isinstance(all_tenants, bool):
            return None
        if all_tenants and (tenant or len(models) != 1):
            return None
        return ReportIntent(
            connector_id=subscription.connector_id,
            template_id=subscription.template_id,
            period=period,  # type: ignore[arg-type]
            tenant=tenant,
            models=models,
            model_scope=model_scope,  # type: ignore[arg-type]
            start_date=start_date,
            end_date=end_date,
            filters={
                "tenant": tenant,
                "models": list(models),
                "all_tenants": all_tenants,
                "model_scope": model_scope,
            },
        )

    async def _run_cube_subscription(
        self,
        subscription: ReportSubscription,
        *,
        run_id: str,
        idempotency_key: str,
    ) -> ToolResult:
        params = self._dynamic_magik_params(subscription)
        tenant_models: dict[str, list[str]] | None = None
        if (
            subscription.template_id == "usage_customer_model_daily_brief"
            and str(params.get("model_scope") or "") == "all"
        ):
            tenants = [
                str(item).strip()
                for item in params.get("tenants") or []
                if str(item).strip()
            ]
            if params.get("all_tenants") is True:
                catalog_loader = getattr(self._magik_tool, "list_tenant_catalog", None)
                if not callable(catalog_loader):
                    raise LookupError("Cube 客户目录当前不可用")
                tenant_catalog = await catalog_loader(limit=20)
                tenants = [str(item["tenant_id"]) for item in tenant_catalog]
                params["tenants"] = tenants
                params["report_selections"] = [
                    {
                        "tenant_query": tenant_id,
                        "model_scope": "all",
                        "models": [],
                    }
                    for tenant_id in tenants
                ]
                subscription = replace(subscription, report_params=params)
            try:
                start_date = date.fromisoformat(str(params["start_date"]))
                end_date = date.fromisoformat(str(params["end_date"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("subscription has an invalid dynamic report window") from exc
            tenant_models = await self._load_tenant_model_catalog(
                tenants,
                start_date=start_date,
                end_date=end_date,
            )
        intent = self._subscription_cube_intent(
            subscription,
            tenant_models=tenant_models,
        )
        if intent is None:
            raise ValueError("subscription requires the legacy Magik compatibility path")
        template = self._registry.template(intent.template_id)
        if template is None:
            raise LookupError(f"Cube report template unavailable: {intent.template_id}")
        context = ReportRunContext(
            channel=subscription.channel,
            chat_id=subscription.chat_id,
            user_id=subscription.user_id,
            timezone=subscription.timezone,
            trace_id=run_id,
            template_version=subscription.template_version or template.manifest.version,
            metadata={"subscription_id": subscription.subscription_id},
        )
        runner = ReportRunner(
            self._registry,
            self._store,
            semantic_shadow_enabled=self._config.cube_semantics_shadow,
            template_policy_enforced=self._config.report_management_v1,
        )
        outcome = await runner.run(intent, context)
        report_attempts = 1
        if (
            self._config.cube_transient_run_retry
            and outcome.quality == "missing"
            and any(is_transient_report_failure(item) for item in outcome.document.warnings)
        ):
            logger.warning(
                "Cube subscription transient failure; retrying once: subscription_id={} delay_seconds={}",
                subscription.subscription_id,
                self._config.cube_transient_retry_delay_seconds,
            )
            await asyncio.sleep(self._config.cube_transient_retry_delay_seconds)
            outcome = await runner.run(intent, context)
            report_attempts = 2
        return self._with_delivery_metadata(
            self._result(
                outcome.document,
                report_reference=self._report_reference_payload(
                    intent,
                    document=outcome.document,
                    run_id=run_id,
                ),
            ),
            idempotency_key=idempotency_key,
            run_id=run_id,
            report_attempts=report_attempts,
        )

    async def _subscribe(
        self,
        *,
        period: str,
        report_family: str,
        report_params: Any,
        channel: str,
        chat_id: str,
        user_id: str,
        session_key: str,
        metadata: dict[str, Any],
        send_time: str,
        daily_mode: str,
        weekday: int,
        month_day: int,
    ) -> ToolResult:
        if self._cron is None:
            return ToolResult.error("Error: report subscriptions require the Gateway Cron service")
        if not user_id or not self._store.allowed(
            channel, user_id, "capability", "subscriptions"
        ):
            return ToolResult.error("Error: no permission to manage report subscriptions")
        params = self._safe_report_params(report_params)
        saved_family = str(params.get("report_family") or "")
        if saved_family == "health" and report_family == "usage":
            report_family = saved_family
        report_family = report_family or saved_family or "usage"
        if report_family not in {"usage", "health", "cost", "provider_quality"}:
            return ToolResult.error("Error: unsupported report family")
        if report_family == "provider_quality":
            if not self.provider_quality_subscriptions_enabled:
                return ToolResult.error("Error: Cube provider quality subscription is not enabled")
            if period not in {"day", "week"}:
                return ToolResult.error("Error: provider quality subscriptions support day or week only")
        elif report_family == "health":
            if not self.health_subscriptions_enabled:
                return ToolResult.error("Error: Cube health subscription is not enabled")
            if period not in {"day", "week"}:
                return ToolResult.error("Error: health subscriptions support day or week only")
        elif report_family == "cost":
            if not self.cost_subscriptions_enabled:
                return ToolResult.error("Error: Cube cost/account subscription is not enabled")
            if period != "month":
                return ToolResult.error("Error: cost/account subscriptions support month only")
        elif report_family == "usage":
            if not self._config.cube_subscription:
                return ToolResult.error("Error: Cube usage subscription is not enabled")
            if period not in _PERIOD_TEMPLATES:
                return ToolResult.error("Error: subscription period must be day, week, or month")
            # Day/week/month confirmations now use the same typed compiler as
            # the WebUI. Keep the bounded legacy path for custom windows, whose
            # historical schedule semantics still need migration.
            if period in {"day", "week", "month"} and not (
                len(
                    [
                        item
                        for item in params.get("report_selections") or []
                        if isinstance(item, dict)
                    ]
                )
                > 1
                and str(params.get("report_variant") or "")
                != "customer_model_daily_brief"
            ):
                if not user_id or not self._authorized_for_magik(channel, user_id):
                    return ToolResult.error("Error: no permission for the Magik Cube connector")
                params.setdefault(
                    "report_template",
                    "brief" if self._usage_brief_default_enabled else "matrix_card",
                )
                return await self._subscribe_usage_via_service(
                    period=period,
                    report_params=params,
                    channel=channel,
                    chat_id=chat_id,
                    user_id=user_id,
                    send_time=send_time,
                    daily_mode=daily_mode,
                    weekday=weekday,
                    month_day=month_day,
                )
        # Delivery cadence and report data period are independent. For example,
        # a daily report can be delivered on workdays or once every Monday.
        data_period = str(params.get("subscription_period") or period)
        if data_period not in {"day", "week", "month"}:
            return ToolResult.error("Error: invalid report data period")
        if report_family == "provider_quality":
            if not user_id or not self.provider_quality_connector_enabled:
                return ToolResult.error("Error: no permission for the Cube provider quality connector")
            if not self._store.allowed(channel, user_id, "connector", "cube_provider_quality"):
                return ToolResult.error("Error: no permission for the Cube provider quality connector")
            if not self._store.allowed(channel, user_id, "template", "provider_quality"):
                return ToolResult.error("Error: no permission for the Cube provider quality template")
            params["report_family"] = report_family
            params["subscription_period"] = period
        elif not user_id or not self._authorized_for_magik(channel, user_id):
            return ToolResult.error("Error: no permission for the Magik Cube connector")
        if report_family in {"health", "cost"}:
            params["report_family"] = report_family
            params.setdefault("subscription_period", data_period)
        elif report_family == "usage" and params.get("report_template") == "brief":
            params.setdefault("subscription_period", data_period)
        denial = None if report_family == "provider_quality" else authorize_magik_params(
            self._store, channel=channel, user_id=user_id, params=params
        )
        if denial:
            return ToolResult.error(denial)
        try:
            cron_expr = build_subscription_schedule(
                period,
                send_time=send_time,
                daily_mode=daily_mode,
                weekday=weekday,
                month_day=month_day,
            )
        except ValueError as exc:
            return ToolResult.error(f"Error: invalid subscription schedule: {exc}")
        report_variant = str(params.get("report_variant") or "")
        if report_variant == "customer_model_daily_brief":
            if not self._config.cube_multi_scope_brief or data_period != "day":
                return ToolResult.error(
                    "Error: multi-customer model subscriptions require the daily brief"
                )
            template_id = "usage_customer_model_daily_brief"
        else:
            template_id = (
                "health_sre"
                if report_family == "health"
                else "cost_account"
                if report_family == "cost"
                else "provider_quality"
                if report_family == "provider_quality"
                else _BRIEF_PERIOD_TEMPLATES[data_period]
                if params.get("report_template") == "brief"
                else _PERIOD_TEMPLATES[data_period]
            )
        template = self._registry.template(template_id)
        if (
            report_family == "usage"
            and params.get("report_template") == "brief"
            and (
                template is None
                or template.manifest.lifecycle_state not in {"publish", "canary"}
            )
        ):
            return ToolResult.error("Error: Cube usage brief template is not available")
        if template is not None:
            params["calculation_version"] = template.manifest.version
        policy_denial = self._subscription_policy_denial(
            channel=channel,
            user_id=user_id,
            template_id=template_id,
        )
        if policy_denial:
            return ToolResult.error(policy_denial)
        if report_family == "health":
            params["threshold_version"] = "health-default-v1"
        fingerprint_payload = json.dumps(
            [channel, user_id, template_id, cron_expr, params],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
        subscription_id = uuid.uuid4().hex[:16]
        origin_metadata = {
            key: value
            for key, value in metadata.items()
            if key not in {OUTBOUND_META_AGENT_UI, INBOUND_META_DIRECT_TOOL}
        }
        origin_metadata[INBOUND_META_DIRECT_TOOL] = {
            "name": "report_center",
            "params": {"action": "run_subscription", "subscription_id": subscription_id},
        }
        origin_metadata["direct_request_text"] = "执行固定报表订阅"
        job = self._cron.add_job(
            name=f"固定{period}报订阅",
            schedule=CronSchedule(kind="cron", expr=cron_expr, tz=self._config.timezone),
            message="执行固定报表订阅",
            session_key=session_key,
            origin_channel=channel,
            origin_chat_id=chat_id,
            origin_metadata=origin_metadata,
        )
        now = datetime.now().astimezone().isoformat()
        subscription = ReportSubscription(
            subscription_id=subscription_id,
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            connector_id=("cube_provider_quality" if report_family == "provider_quality" else "magik_cube"),
            template_id=template_id,
            template_version=template.manifest.version if template is not None else "1.0",
            schedule=cron_expr,
            timezone=self._config.timezone,
            report_params=params,
            cron_job_id=job.id,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        if not self._store.add_subscription(subscription, fingerprint):
            self._cron.remove_job(job.id)
            return ToolResult("相同报表和发送计划的订阅已经存在。")
        return self._result(subscription_created_document(subscription))

    def _subscription_setup(
        self,
        *,
        period: str,
        report_family: str,
        report_params: Any,
        channel: str,
        user_id: str,
    ) -> ToolResult:
        if self._cron is None:
            return ToolResult.error("Error: report subscriptions require the Gateway Cron service")
        if not user_id or not self._store.allowed(
            channel, user_id, "capability", "subscriptions"
        ):
            return ToolResult.error("Error: no permission to manage report subscriptions")
        if report_family == "provider_quality":
            if not self.provider_quality_subscriptions_enabled:
                return ToolResult.error("Error: Cube provider quality subscription is not enabled")
            if period not in {"day", "week"}:
                return ToolResult.error("Error: provider quality subscriptions support day or week only")
        elif report_family == "health":
            if not self.health_subscriptions_enabled:
                return ToolResult.error("Error: Cube health subscription is not enabled")
            if period not in {"day", "week"}:
                return ToolResult.error("Error: health subscriptions support day or week only")
        elif report_family == "cost":
            if not self.cost_subscriptions_enabled:
                return ToolResult.error("Error: Cube cost/account subscription is not enabled")
            if period != "month":
                return ToolResult.error("Error: cost/account subscriptions support month only")
        elif report_family == "usage":
            if not self._config.cube_subscription:
                return ToolResult.error("Error: Cube usage subscription is not enabled")
            if period not in _PERIOD_TEMPLATES:
                return ToolResult.error("Error: subscription period must be day, week, or month")
        else:
            return ToolResult.error("Error: unsupported report family")
        if report_family == "provider_quality":
            authorized = bool(
                user_id
                and self.provider_quality_connector_enabled
                and self._store.allowed(channel, user_id, "connector", "cube_provider_quality")
                and self._store.allowed(channel, user_id, "template", "provider_quality")
            )
        else:
            authorized = bool(user_id and self._authorized_for_magik(channel, user_id))
        if not authorized:
            return ToolResult.error(
                "Error: no permission for the Cube provider quality connector"
                if report_family == "provider_quality"
                else "Error: no permission for the Magik Cube connector"
            )
        params = self._safe_report_params(report_params)
        if report_family in {"health", "cost", "provider_quality"}:
            params["report_family"] = report_family
            params["subscription_period"] = period
        elif params.get("report_template") == "brief":
            params["subscription_period"] = period
        report_variant = str(params.get("report_variant") or "")
        if report_variant == "customer_model_daily_brief":
            if not self._config.cube_multi_scope_brief or period != "day":
                return ToolResult.error(
                    "Error: multi-customer model subscriptions require the daily brief"
                )
            template_id = "usage_customer_model_daily_brief"
        else:
            template_id = (
                "health_sre"
                if report_family == "health"
                else "cost_account"
                if report_family == "cost"
                else "provider_quality"
                if report_family == "provider_quality"
                else _BRIEF_PERIOD_TEMPLATES[period]
                if params.get("report_template") == "brief"
                else _PERIOD_TEMPLATES[period]
            )
        policy_denial = self._subscription_policy_denial(
            channel=channel,
            user_id=user_id,
            template_id=template_id,
        )
        if policy_denial:
            return ToolResult.error(policy_denial)
        denial = None if report_family == "provider_quality" else authorize_magik_params(
            self._store, channel=channel, user_id=user_id, params=params
        )
        if denial:
            return ToolResult.error(denial)
        return ToolResult(
            "请选择报表发送周期和时间。",
            metadata={
                OUTBOUND_META_AGENT_UI: {
                    "kind": "report_subscription_form",
                    "version": 1,
                    "title": "设置报表订阅",
                    "default_period": period,
                    "default_time": "10:00",
                    "timezone": self._config.timezone,
                    "report_params": params,
                }
            },
        )

    async def _run_subscription(self, subscription_id: str, metadata: dict[str, Any]) -> Any:
        ctx = current_request_context()
        trigger = metadata.get(CRON_TRIGGER_META)
        if ctx is None or ctx.sender_id != "cron" or not isinstance(trigger, dict):
            return ToolResult.error("Error: run_subscription is restricted to Cron")
        subscription = self._store.subscription(subscription_id)
        if subscription is None or not subscription.enabled:
            return ToolResult.error("Error: report subscription is missing or disabled")
        if self._config.report_management_v1:
            policy = self._store.template_policy(subscription.template_id)
            if policy is not None and not policy["enabled"]:
                return ToolResult.error("Error: report template is disabled")
        family = str(subscription.report_params.get("report_family") or "usage")
        if self._magik_tool is None and family != "provider_quality":
            return ToolResult.error("Error: Magik Cube connector is unavailable")
        scheduled_at = trigger.get("scheduled_at_ms")
        if not isinstance(scheduled_at, int):
            return ToolResult.error("Error: Cron trigger is missing scheduled_at_ms")
        idempotency_key = (
            f"{subscription.subscription_id}:{scheduled_at}:{subscription.template_version}"
        )
        if not self._store.claim_delivery(idempotency_key):
            return ToolResult("该计划周期的报表已经处理，已跳过重复发送。")
        run_id = str(trigger.get("run_id") or uuid.uuid4().hex)
        connector = self._registry.connector(subscription.connector_id)
        cube_family_enabled = (
            (family == "usage" and self._config.cube_subscription)
            or (family == "health" and self._config.cube_health_subscription)
            or (family == "cost" and self._config.cube_cost_subscription)
            or (family == "provider_quality" and self._config.cube_provider_quality_subscription)
        )
        if (
            cube_family_enabled
            and isinstance(connector, (CubeConnector, CubeProviderQualityConnector))
            and (
                subscription.template_id == "usage_customer_model_daily_brief"
                or self._subscription_cube_intent(subscription) is not None
            )
        ):
            try:
                return await self._run_cube_subscription(
                    subscription,
                    run_id=run_id,
                    idempotency_key=idempotency_key,
                )
            except Exception:
                self._store.complete_delivery(idempotency_key, status="error")
                raise
        started = time.perf_counter()
        try:
            result = await self._magik_tool.execute(**self._dynamic_magik_params(subscription))
        except Exception:
            self._store.complete_delivery(idempotency_key, status="error")
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        self._store.record_run(
            run_id=run_id,
            channel=subscription.channel,
            chat_id=subscription.chat_id,
            user_id=subscription.user_id,
            connector_id=subscription.connector_id,
            template_id=subscription.template_id,
            template_version=subscription.template_version,
            request={
                "subscription_id": subscription_id,
                "calculation_version": subscription.report_params.get(
                    "calculation_version", subscription.template_version
                ),
            },
            status="error" if getattr(result, "is_error", False) else "ok",
            duration_ms=duration_ms,
            quality="partial" if getattr(result, "is_error", False) else "complete",
            error_type="tool_error" if getattr(result, "is_error", False) else "",
        )
        return self._with_delivery_metadata(
            result,
            idempotency_key=idempotency_key,
            run_id=run_id,
            report_attempts=1,
        )

    async def execute(
        self,
        action: str,
        period: str = "week",
        send_time: str = "10:00",
        daily_mode: str = "workdays",
        weekday: int = 1,
        month_day: int = 1,
        report_params: dict[str, Any] | None = None,
        report_family: str = "usage",
        report_template: str = "",
        report_type: str = "",
        subscription_id: str = "",
        tenant_query: str = "",
        tenant_scope: str = "selected",
        tenant_aliases: list[str] | None = None,
        model: str = "",
        models: list[str] | None = None,
        model_scope: str = "summary",
        breakdown: str = "summary",
        project: str = "",
        endpoint: str = "",
        provider: str = "",
        provider_id: str = "",
        providers: list[str] | None = None,
        tenants: list[str] | None = None,
        cluster: str = "",
        selection_confirmed: bool = False,
        include_empty: bool = False,
        start_date: str = "",
        end_date: str = "",
        interactive: bool = False,
        all_tenants: bool = False,
        report_selections: list[dict[str, Any]] | None = None,
        recurrence: str = "workdays",
        inherit_report_scope: bool = False,
        reference_message_id: str = "",
        revision: int | None = None,
        **_kwargs: Any,
    ) -> Any:
        channel, chat_id, user_id, session_key, metadata = self._request_identity()
        self._store.prune_runs(self._config.run_retention_days)
        if action == "home":
            return self._result(
                home_document(
                    self._registry,
                    self._store,
                    channel=channel,
                    user_id=user_id,
                    health_enabled=self._config.cube_health_report,
                    cost_enabled=self.cost_reports_enabled,
                    provider_quality_enabled=self.provider_quality_reports_enabled,
                    brief_default=self._usage_brief_default_enabled,
                    admin_skill_enabled=(
                        self._config.cube_admin_skill_help and self._magik_tool is not None
                    ),
                    management_enabled=self._config.report_management_v1,
                )
            )
        if action == "examples":
            return self._result(
                examples_document(
                    self._authorized_for_magik(channel, user_id),
                    cost_enabled=self.cost_reports_enabled,
                    all_tenant_model_enabled=self._config.cube_model_all_tenant_report,
                    provider_quality_enabled=self.provider_quality_reports_enabled,
                    admin_skill_enabled=(
                        self._config.cube_admin_skill_help and self._magik_tool is not None
                    ),
                    multi_scope_enabled=self._config.cube_multi_scope_brief,
                    machine_tpm_enabled=self._config.cube_machine_tpm_report,
                    subscription_nlu_enabled=(
                        self._config.cube_subscription_nlu_v2
                        or self._config.cube_subscription_nlu_v3
                    ),
                )
            )
        if action == "recent":
            return self._result(
                recent_document(self._store.recent_runs(channel, user_id))
            )
        if action == "cube_report":
            selected_template = report_template or (
                "brief" if self._usage_brief_default_enabled else "matrix_card"
            )
            return await self._run_cube_report(
                period=period,
                tenant_query=tenant_query,
                model=model,
                models=models,
                breakdown=breakdown,
                project=project,
                endpoint=endpoint,
                provider=provider,
                interactive=interactive,
                all_tenants=all_tenants,
                report_template=selected_template,
                start_date=start_date,
                end_date=end_date,
                report_selections=report_selections,
            )
        if action == "multi_scope_brief":
            return await self._run_multi_scope_brief(
                period=period,
                tenants=tenants or [],
                models=models or ([model] if model else []),
                all_tenants=all_tenants,
                interactive=interactive,
                start_date=start_date,
                end_date=end_date,
                report_selections=report_selections,
            )
        if action == "machine_tpm_report":
            return await self._run_machine_tpm_report(
                period=period,
                model=model,
                cluster=cluster,
                start_date=start_date,
                end_date=end_date,
            )
        if action == "health_report":
            return await self._run_health_report(period=period)
        if action == "provider_quality_report":
            return await self._run_provider_quality_report(
                period=period,
                provider=provider,
                providers=providers,
                provider_id=provider_id,
                model=model,
                endpoint=endpoint,
                selection_confirmed=selection_confirmed,
                include_empty=include_empty,
                start_date=start_date,
                end_date=end_date,
            )
        if action == "cost_report":
            selection = next(
                (item for item in report_selections or [] if isinstance(item, dict)),
                {},
            )
            return await self._run_cost_report(
                period=period,
                tenant_query=str(selection.get("tenant_query") or tenant_query),
                project=project,
                model=model,
                endpoint=endpoint,
                interactive=interactive,
            )
        if action == "subscriptions":
            return self._result(
                subscriptions_document(self._store.subscriptions(channel, user_id))
            )
        if action == "subscription_reference_missing":
            # This action is reserved for a missing, expired, or untrusted
            # quoted-message reference.  Parser and catalog failures use
            # separate actions so users are not told to regenerate a card that
            # the server has already resolved successfully.
            message = (
                "无法从该卡片恢复可验证的报表范围。请重新生成报表，或在订阅中心选择客户和模型。"
                if reference_message_id
                else "未能安全识别订阅范围和发送计划。请换一种说法，或在订阅中心选择客户和模型。"
            )
            return self._result(self._subscription_unavailable_document(message))
        if action == "subscription_parse_failed":
            logger.warning(
                "Cube subscription request rejected: stage=parse error_code={}",
                str(_kwargs.get("subscription_error") or "nlu_unavailable_or_invalid"),
            )
            message = (
                "已找到引用报表，但发送计划未能识别。请使用“每天上午十点发送给我”这类格式重试。"
                if reference_message_id
                else "未能识别发送计划。请使用“每天上午十点发送阳春面、豆汁、佛跳墙全部模型的多客户日报简报”这类格式重试。"
            )
            return self._result(self._subscription_unavailable_document(message))
        if action == "subscription_scope_failed":
            error_code = str(_kwargs.get("subscription_error") or "scope_unresolved")
            if _kwargs.get("catalog_unavailable") is True:
                message = "Cube 客户目录当前不可用，无法验证订阅范围。请稍后重试或在订阅中心重新选择客户。"
            elif _kwargs.get("tenant_ambiguous") is True:
                unresolved = [
                    str(item).strip()
                    for item in (_kwargs.get("unresolved_tenants") or [])
                    if str(item).strip()
                ]
                detail = f"：{'、'.join(unresolved[:5])}" if unresolved else ""
                message = f"客户名称或标签匹配到多个实时客户{detail}。请使用客户选择器或真实 tenant ID。"
            elif _kwargs.get("scope_unresolved") is True:
                message = "原文中的客户未能在 Cube 实时目录中确认。请重新选择客户后再创建订阅。"
            else:
                message = f"订阅范围校验失败（{error_code}）。请在订阅中心重新选择客户和模型。"
            return self._result(
                self._subscription_unavailable_document(message)
            )
        if action == "subscription_preview":
            return await self._subscription_preview(
                report_type=report_type,
                tenant_scope=tenant_scope,
                tenant_aliases=tenant_aliases or [],
                model_scope=model_scope,
                models=models or ([model] if model else []),
                recurrence=recurrence,
                send_time=send_time,
                weekday=weekday,
                month_day=month_day,
                inherit_report_scope=inherit_report_scope,
                reference_message_id=reference_message_id,
            )
        if action == "request_access":
            return ToolResult("请联系报表平台管理员，为当前飞书账号配置所需数据范围。")
        if action == "subscription_setup":
            return self._subscription_setup(
                period=period,
                report_family=report_family,
                report_params=report_params or {},
                channel=channel,
                user_id=user_id,
            )
        if action == "subscribe":
            return await self._subscribe(
                period=period,
                report_family=report_family,
                report_params=report_params or {},
                channel=channel,
                chat_id=chat_id,
                user_id=user_id,
                session_key=session_key,
                metadata=metadata,
                send_time=send_time,
                daily_mode=daily_mode,
                weekday=weekday,
                month_day=month_day,
            )
        if action == "run_subscription":
            return await self._run_subscription(subscription_id, metadata)
        subscription = self._store.subscription(subscription_id)
        if subscription is None or subscription.channel != channel or subscription.user_id != user_id:
            return ToolResult.error("Error: report subscription not found")
        if action in {"subscription_enable", "subscription_disable"}:
            enabled = action == "subscription_enable"
            if revision is not None:
                # Structured cards carry a CAS revision.  Route those actions
                # through the same Cron-first compensation service used by the
                # WebUI, while retaining the legacy branch below for older
                # text commands that cannot provide a revision.
                try:
                    updated = self._subscription_service_for_confirmed_scope().set_enabled(
                        subscription_id,
                        enabled=enabled,
                        expected_revision=revision,
                        updated_by=user_id,
                    )
                except SubscriptionServiceError as exc:
                    return ToolResult.error(f"Error: {exc.message}")
                except Exception as exc:
                    logger.warning(
                        "Report subscription control failed: action={} error_type={}",
                        action,
                        type(exc).__name__,
                    )
                    return ToolResult.error("Error: subscription state could not be updated")
                return self._result(
                    subscriptions_document(self._store.subscriptions(channel, user_id))
                ) if updated else ToolResult.error("Error: subscription state could not be updated")
            job = self._cron.enable_job(subscription.cron_job_id, enabled) if self._cron else None
            if job is None:
                return ToolResult.error("Error: subscription Cron job not found")
            self._store.set_subscription_enabled(
                subscription_id, channel=channel, user_id=user_id, enabled=enabled
            )
            return self._result(
                subscriptions_document(self._store.subscriptions(channel, user_id))
            )
        if action == "subscription_remove":
            if revision is not None:
                try:
                    self._subscription_service_for_confirmed_scope().delete(
                        subscription_id,
                        expected_revision=revision,
                        updated_by=user_id,
                    )
                except SubscriptionServiceError as exc:
                    return ToolResult.error(f"Error: {exc.message}")
                except Exception as exc:
                    logger.warning(
                        "Report subscription delete failed: error_type={}",
                        type(exc).__name__,
                    )
                    return ToolResult.error("Error: subscription could not be deleted")
                return ToolResult("订阅已删除。")
            if self._cron:
                self._cron.remove_job(subscription.cron_job_id)
            self._store.remove_subscription(subscription_id, channel=channel, user_id=user_id)
            return ToolResult("订阅已删除。")
        return ToolResult.error("Error: unsupported report center action")
