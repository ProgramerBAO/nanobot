"""Deterministic report capability home, history, and subscription control.

Phase-5 structural split: configuration/schema live in ``schema``, phrase
regexes in ``phrases``, and the tool class assembles from responsibility
mixins (catalog / gating / execution / subscription flow / cron compile).
Core construction, routing, and dispatch stay here so tests patching
``report_center_module.<name>`` keep working; everything else is a pure move.
"""


from __future__ import annotations

# Intentional binding: some tests patch report_center_module.asyncio.sleep
# before driving the retry paths that now live in the mixin modules.
import asyncio as asyncio  # noqa: F401
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from nanobot.agent.reporting.cube_subscription_intent import (
    CubeSubscriptionIntent,
    classify_subscription_intent,
    is_subscription_intent_candidate,
    parse_deterministic_subscription_intent,
)
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.agent.tools.report_center.catalog import _CatalogReconciliationMixin
from nanobot.agent.tools.report_center.execution import _ReportExecutionMixin
from nanobot.agent.tools.report_center.gating import _ReportGatingMixin
from nanobot.agent.tools.report_center.schema import (
    _REPORT_CENTER_PARAMETERS,
    ReportCenterToolConfig,
)
from nanobot.agent.tools.report_center.subscription_compile import (
    _SubscriptionCompileMixin,
)
from nanobot.agent.tools.report_center.subscription_flow import (
    _SubscriptionFlowMixin,
)
from nanobot.bus.events import (
    OUTBOUND_META_AGENT_UI,
    OUTBOUND_META_REPORT_DELIVERY,
    OUTBOUND_META_REPORT_REFERENCE,
)
from nanobot.reporting import (
    CubeConnector,
    ReportDocument,
    ReportIntent,
    build_default_registry,
    default_registry_kwargs,
)
from nanobot.reporting.capabilities import (
    examples_document,
    home_document,
    recent_document,
    subscriptions_document,
    template_enabled,
)
from nanobot.reporting.store import get_report_state_store
from nanobot.reporting.subscriptions import SubscriptionServiceError

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
    r"^(?:请)?(?:打开|查看|生成)?(?:Cube\s*)?多客户多模型(?P<period>日报|周报)?简报[？?。！!]*$",
    re.IGNORECASE,
)
_MULTI_SCOPE_WEEKLY_RE = re.compile(
    r"^(?:请)?(?:打开|查看|生成)?(?:Cube\s*)?各客户模型周报(?:简报)?[？?。！!]*$",
    re.IGNORECASE,
)
_MULTI_SCOPE_NAMED_RE = re.compile(
    r"^(?P<tenants>.+?)(?:的)?(?:全部|所有|全量)模型(?:的)?多客户(?:多模型)?"
    r"(?P<period>日报|周报)简报[？?。！!]*$",
    re.IGNORECASE,
)
_CUSTOMER_MODEL_HOURLY_TPM_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成)?\s*(?P<tenants>.+?)\s*(?:的)?\s*"
    r"(?:全部|所有|全量)\s*模型\s*(?:的)?\s*"
    r"(?:上一|最近)\s*(?:完整)?\s*(?:一)?\s*小时\s*(?:TPM|tpm)\s*"
    r"(?:峰值和均值|均值和峰值|报告|报表)?[？?。！!]*$",
    re.IGNORECASE,
)
_CUSTOMER_MODEL_HOURLY_TPM_SELECTED_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成)?\s*(?P<tenants>.+?)\s+"
    r"(?P<models>[A-Za-z0-9][A-Za-z0-9._-]*(?:[、，,]\s*[A-Za-z0-9][A-Za-z0-9._-]*)*)\s*"
    r"(?:模型)?\s*(?:的)?\s*(?:上一|最近)\s*(?:完整)?\s*(?:一)?\s*小时\s*"
    r"(?:TPM|tpm)\s*(?:峰值和均值|均值和峰值|报告|报表)?[？?。！!]*$",
    re.IGNORECASE,
)
# This guard is intentionally broader than the deterministic parsers above.
# Hourly TPM requests that are incomplete must fail closed in ReportCenter
# instead of falling through to the legacy daily-usage matcher.
_HOURLY_TPM_SIGNAL_RE = re.compile(
    r"(?:上一|最近)\s*(?:完整)?\s*(?:一)?\s*小时.*(?:TPM|tpm)",
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
        # Hourly TPM subscriptions carry their template id so the cron-run
        # compiler can select the hourly intent branch without guessing from
        # the period alone.
        "report_template_id",
    }
)


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


# Per-template report switches retired by the 2026-09-16 consolidation
# (phase 2d): their semantics moved into the always-enforced
# report_template_policies store table. The config fields below stay
# accepted for one migration window so existing config.json files keep
# loading (a warning is logged when set non-default); run
# `nanobot reports policy migrate-flags` and remove them from config.


@tool_parameters(_REPORT_CENTER_PARAMETERS)
class ReportCenterTool(  # noqa: UP046
    _CatalogReconciliationMixin,
    _ReportGatingMixin,
    _ReportExecutionMixin,
    _SubscriptionFlowMixin,
    _SubscriptionCompileMixin,
    Tool,
):

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
            **default_registry_kwargs(
                config,
                cube_config,
                # Preserve the exact Gateway construction rule: the magik tool
                # instance must exist and the connector flag must be on.
                # Production create() guarantees tool-exists == magik enabled;
                # direct test constructions may pass a tool with
                # cube_config=None, which must still register the shell
                # connector rather than dropping it entirely.
                magik_enabled=magik_tool is not None and config.cube_connector,
            )
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
            (self._flag("cube_subscription_nlu_v2"))
            and self._flag("cube_subscription")
            and is_subscription_intent_candidate(text)
        )

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
            (self._flag("cube_subscription_nlu_v2"))
            and self._flag("cube_report_reference_subscription")
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
        named_multi = _MULTI_SCOPE_NAMED_RE.fullmatch(raw)
        if named_multi:
            tenant_values = [
                item.strip()
                for item in re.split(r"[、，,；;和与及]+", named_multi.group("tenants"))
                if item.strip()
            ]
            if tenant_values:
                period = "week" if named_multi.group("period") == "周报" else "day"
                return {
                    "action": "multi_scope_brief",
                    "period": period,
                    "interactive": False,
                    "tenants": tenant_values,
                    "report_selections": [
                        {"tenant_query": tenant, "model_scope": "all", "models": []}
                        for tenant in tenant_values
                    ],
                }
        if _MULTI_SCOPE_BRIEF_RE.fullmatch(raw):
            period_text = _MULTI_SCOPE_BRIEF_RE.fullmatch(raw).group("period")
            return {
                "action": "multi_scope_brief",
                "interactive": True,
                "period": "week" if period_text == "周报" else "day",
            }
        if _MULTI_SCOPE_WEEKLY_RE.fullmatch(raw):
            return {"action": "multi_scope_brief", "interactive": True, "period": "week"}
        hourly_tpm_match = _CUSTOMER_MODEL_HOURLY_TPM_RE.fullmatch(raw)
        if hourly_tpm_match:
            tenant_values = [
                item.strip()
                for item in re.split(r"[、，,；;和与及]+", hourly_tpm_match.group("tenants"))
                if item.strip()
            ]
            return {
                "action": "customer_model_hourly_tpm",
                "period": "recent1h",
                "tenants": tenant_values,
                "model_scope": "all",
                "interactive": False,
            }
        selected_hourly_match = _CUSTOMER_MODEL_HOURLY_TPM_SELECTED_RE.fullmatch(raw)
        if selected_hourly_match:
            tenant_values = [
                item.strip()
                for item in re.split(r"[、，,；;和与及]+", selected_hourly_match.group("tenants"))
                if item.strip()
            ]
            model_values = [
                item.strip()
                for item in re.split(r"[、，,；;和与及]+", selected_hourly_match.group("models"))
                if item.strip()
            ]
            return {
                "action": "customer_model_hourly_tpm",
                "period": "recent1h",
                "tenants": tenant_values,
                "models": model_values,
                "model_scope": "selected",
                "interactive": False,
            }
        if _HOURLY_TPM_SIGNAL_RE.search(raw):
            return {
                "action": "customer_model_hourly_tpm",
                "period": "recent1h",
                "interactive": True,
            }
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
            "customer_model_hourly_tpm"
            if intent.template_id == "usage_customer_model_hourly_tpm"
            else "customer_model_daily_brief"
            if intent.template_id == "usage_customer_model_daily_brief"
            else "usage_brief"
        )
        if report_variant in {"customer_model_daily_brief", "customer_model_hourly_tpm"}:
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
                # The template id lets the hourly cron-run compiler and quoted
                # subscriptions select the hourly intent branch without
                # guessing from the period alone.
                "report_template_id": intent.template_id,
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
    def _agent_ui_of(result: Any) -> dict[str, Any] | None:
        """Extract the agent-ui payload from a tool result without assuming shape.

        The legacy Magik tool legally returns plain strings (``ToolResult`` is
        a ``str`` subclass, so both shapes flow through the Tool contract);
        accessing ``result.metadata`` directly raises AttributeError on the
        plain-string paths (observed live 2026-09-15 on a scheduled legacy
        subscription).
        """

        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        ui = metadata.get(OUTBOUND_META_AGENT_UI)
        return ui if isinstance(ui, dict) else None

    @staticmethod
    def _with_delivery_metadata(
        result: ToolResult | str,
        *,
        idempotency_key: str,
        run_id: str,
        report_attempts: int,
    ) -> ToolResult:
        # Plain-string returns from the legacy compatibility tool are part of
        # the Tool contract; normalize before attaching delivery bookkeeping.
        normalized = result if isinstance(result, ToolResult) else ToolResult(str(result))
        normalized.metadata[OUTBOUND_META_REPORT_DELIVERY] = {
            "idempotency_key": idempotency_key,
            "run_id": run_id,
            "report_attempts": report_attempts,
        }
        return normalized

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
        hours: list[int] | None = None,
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
                    health_enabled=self.health_reports_enabled,
                    cost_enabled=self.cost_reports_enabled,
                    provider_quality_enabled=self.provider_quality_reports_enabled,
                    brief_default=self._usage_brief_default_enabled,
                    admin_skill_enabled=(
                        self._flag("cube_admin_skill_help") and self._magik_tool is not None
                    ),
                    management_enabled=self._flag("report_management_v1"),
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
                        self._flag("cube_admin_skill_help") and self._magik_tool is not None
                    ),
                    multi_scope_enabled=template_enabled(
                        self._store, "usage_customer_model_daily_brief"
                    ),
                    machine_tpm_enabled=template_enabled(
                        self._store, "machine_tpm_peak"
                    ),
                    hourly_tpm_enabled=template_enabled(
                        self._store, "usage_customer_model_hourly_tpm"
                    ),
                    subscription_nlu_enabled=self._flag("cube_subscription_nlu_v2"),
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
        if action in {"multi_scope_brief", "multi_scope_weekly_brief"}:
            if action == "multi_scope_weekly_brief":
                period = "week"
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
        if action == "customer_model_hourly_tpm":
            return await self._run_customer_model_hourly_tpm(
                tenants=tenants or ([tenant_query] if tenant_query else []),
                models=models or ([model] if model else []),
                all_tenants=all_tenants,
                interactive=interactive,
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
                hours=hours,
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
                hours=hours,
            )
        if action == "run_subscription":
            return await self._run_subscription(subscription_id, metadata)
        subscription = self._store.subscription(subscription_id)
        if subscription is None or subscription.channel != channel or subscription.user_id != user_id:
            return ToolResult.error("Error: report subscription not found")
        if action in {"subscription_enable", "subscription_disable"}:
            enabled = action == "subscription_enable"
            # Structured cards carry a CAS revision. The legacy text command
            # cannot, so it resolves the current row revision at execution
            # time — a typed one-shot command has no rendered stale state to
            # race against. Both entries share the same Cron-first
            # compensation service; the old inline no-CAS branch is gone.
            expected_revision = revision
            if expected_revision is None:
                expected_revision = subscription.revision
            try:
                updated = self._subscription_service_for_confirmed_scope().set_enabled(
                    subscription_id,
                    enabled=enabled,
                    expected_revision=expected_revision,
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
        if action == "subscription_remove":
            try:
                self._subscription_service_for_confirmed_scope().delete(
                    subscription_id,
                    # Same rule as enable/disable: typed commands resolve the
                    # current revision; card actions carry theirs.
                    expected_revision=(
                        revision if revision is not None else subscription.revision
                    ),
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
        return ToolResult.error("Error: unsupported report center action")


__all__ = [
    "CubeSubscriptionIntent",
    "ReportCenterTool",
    "ReportCenterToolConfig",
]
