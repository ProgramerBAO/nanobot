"""Deterministic report capability home, history, and subscription control."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from loguru import logger
from pydantic import Field, field_validator

from nanobot.agent.reporting.magik_cube_intent import ReportIntent as MagikReportIntent
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.bus.events import (
    INBOUND_META_DIRECT_TOOL,
    OUTBOUND_META_AGENT_UI,
    OUTBOUND_META_REPORT_DELIVERY,
)
from nanobot.config_base import Base
from nanobot.cron.session_turns import CRON_TRIGGER_META
from nanobot.cron.types import CronSchedule
from nanobot.reporting import (
    CubeConnector,
    CubeProviderQualityConnector,
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
from nanobot.reporting.cube import normalize_health_thresholds
from nanobot.reporting.interactions import report_interactions
from nanobot.reporting.provider_quality import provider_quality_selector_document
from nanobot.reporting.schedules import build_subscription_schedule
from nanobot.reporting.store import ReportSubscription, get_report_state_store
from nanobot.utils.report_failures import is_transient_report_failure

_HOME_RE = re.compile(
    r"^(?:请)?(?:打开|显示|查看|进入)?(?:报表中心|报表菜单|功能菜单|菜单|帮助|你能做什么|你会什么|有哪些功能)[？?。！!]*$"
)
_RECENT_RE = re.compile(r"^(?:查看|打开|显示)?(?:我的)?最近报表[？?。！!]*$")
_SUBSCRIPTIONS_RE = re.compile(r"^(?:查看|打开|显示|管理)?(?:我的)?(?:报表)?订阅[？?。！!]*$")
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
    r"^(?:请)?(?:我要|生成|查看|打开|显示)?\s*(日报|周报|月报)[？?。！!]*$"
)
_MODEL_CUBE_PERIOD_RE = re.compile(
    r"^(?:请)?(?:我要|我需要|给我|生成|查看|打开|显示)?\s*"
    r"(?P<model>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:模型)?\s*(?:的)?\s*"
    r"(?P<period>日报|周报|月报)\s*(?:全部|所有|全体)?\s*(?:客户|用户|租户)?[？?。！!]*$",
    re.IGNORECASE,
)

_ALLOWED_REPORT_PARAM_KEYS = frozenset(
    {
        "tenant_query",
        "model",
        "models",
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
    }
)
_PERIOD_TEMPLATES: dict[str, str] = {
    "day": "usage_daily_matrix",
    "week": "usage_weekly_matrix",
    "month": "usage_monthly_matrix",
}


class ReportCenterToolConfig(Base):
    enable: bool = True
    # Cube is the only production report path in this phase; the Magik tool
    # remains a separate compatibility entry point when its own flag is on.
    cube_connector: bool = True
    cube_template: bool = True
    cube_report_runner: bool = True
    cube_subscription: bool = True
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
                "examples",
                "recent",
                "subscriptions",
                "subscription_setup",
                "subscribe",
                "subscription_enable",
                "subscription_disable",
                "subscription_remove",
                "run_subscription",
                "request_access",
            ],
        },
        "period": {"type": "string", "enum": ["day", "week", "month", "recent7", "recent15m"]},
        "report_family": {
            "type": "string",
            "enum": ["usage", "health", "cost", "provider_quality"],
        },
        "tenant_query": {"type": "string", "maxLength": 128},
        "project": {"type": "string", "maxLength": 128},
        "model": {"type": "string", "maxLength": 128},
        "models": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
            "maxItems": 20,
        },
        "report_selections": {
            "type": "array",
            "maxItems": 1,
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

    def match_direct_request(self, text: str) -> dict[str, Any] | None:
        raw = text.strip()
        if _HOME_RE.fullmatch(raw):
            return {"action": "home"}
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
            }
        period_match = _CUBE_PERIOD_RE.fullmatch(raw)
        if period_match:
            period = {"日报": "day", "周报": "week", "月报": "month"}[period_match.group(1)]
            return {"action": "cube_report", "period": period, "interactive": True}
        return None

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
        report_selections: list[dict[str, Any]] | None = None,
    ) -> ToolResult:
        if (
            not self._config.cube_report_runner
            or self._magik_tool is None
            or self._registry.connector("magik_cube") is None
        ):
            return ToolResult.error("Error: Magik Cube connector is unavailable")
        if period not in {"day", "week", "month", "recent7"}:
            return ToolResult.error("Error: unsupported Cube report period")
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
            return await self._run_scope_selector(period=period, report_family="usage")
        if interactive and selected_scope == "selected" and not selected_models:
            return await self._run_selector_model_stage(period=period, tenant_query=selected_tenant)
        today = datetime.now(ZoneInfo(self._config.timezone)).date()
        start_date, end_date = self._cube_period_dates(period, today)
        template_id = {
            "day": "usage_daily_matrix",
            "week": "usage_weekly_matrix",
            "month": "usage_monthly_matrix",
            "recent7": "usage_weekly_matrix",
        }[period]
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
            start_date=start_date,
            end_date=end_date,
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
            ).run(intent, context)
        except PermissionError:
            return ToolResult.error("当前账号没有执行该 Cube 报表的权限，请联系管理员授权。")
        except (LookupError, ValueError) as exc:
            return ToolResult.error(f"Error: Cube report unavailable: {exc}")
        return self._result(outcome.document)

    async def _run_scope_selector(self, *, period: str, report_family: str) -> ToolResult:
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
        if report_family == "usage" and not self._config.cube_scope_selector_v2:
            return result
        ui = result.metadata.get(OUTBOUND_META_AGENT_UI) if result.metadata else None
        if not isinstance(ui, dict) or ui.get("kind") != "magik_report_form":
            return result
        ui["title"] = "选择成本账户范围" if report_family == "cost" else "选择 Cube 报表范围"
        ui["base_params"] = {
            "action": "cost_report" if report_family == "cost" else "cube_report",
            "period": period,
            "report_family": report_family,
            "_report_center_selector": True,
        }
        ui["max_tenants"] = 1
        if report_family == "cost":
            ui["scope_options"] = [{"value": "summary", "label": "账务汇总"}]
        return result

    async def _run_selector_model_stage(self, *, period: str, tenant_query: str) -> ToolResult:
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

    @staticmethod
    def _result(document: Any) -> ToolResult:
        return ToolResult(
            document.fallback_text,
            metadata={OUTBOUND_META_AGENT_UI: document.to_agent_ui()},
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

    def _safe_report_params(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("report_params must be an object")
        unknown = set(value) - _ALLOWED_REPORT_PARAM_KEYS
        if unknown:
            raise ValueError("unsupported report subscription parameters")
        params = {key: value[key] for key in value if key in _ALLOWED_REPORT_PARAM_KEYS}
        params["report_template"] = "matrix_card"
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

    def _dynamic_magik_params(self, subscription: ReportSubscription) -> dict[str, Any]:
        period = self._subscription_period(subscription)
        intent = MagikReportIntent(report_kind=period)  # type: ignore[arg-type]
        params = intent.to_tool_params(
            today=datetime.now(ZoneInfo(subscription.timezone)).date()
        )
        saved = dict(subscription.report_params)
        params.update(saved)
        params["report_template"] = "matrix_card"
        params["save_snapshot"] = False
        params.pop("report_family", None)
        params.pop("subscription_period", None)
        params.pop("calculation_version", None)
        params.pop("threshold_version", None)
        return params

    def _subscription_cube_intent(
        self, subscription: ReportSubscription
    ) -> ReportIntent | None:
        """Compile the compatible one-scope subscription into a safe Intent."""

        params = self._dynamic_magik_params(subscription)
        family = str(subscription.report_params.get("report_family") or "usage")
        period = self._subscription_period(subscription)
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
        intent = self._subscription_cube_intent(subscription)
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
            self._result(outcome.document),
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
        elif period not in _PERIOD_TEMPLATES:
            return ToolResult.error("Error: subscription period must be day, week, or month")
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
            params["subscription_period"] = period
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
        template_id = (
            "health_sre"
            if report_family == "health"
            else "cost_account"
            if report_family == "cost"
            else "provider_quality"
            if report_family == "provider_quality"
            else _PERIOD_TEMPLATES[period]
        )
        template = self._registry.template(template_id)
        if template is not None:
            params["calculation_version"] = template.manifest.version
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
        elif report_family != "usage" or period not in _PERIOD_TEMPLATES:
            return ToolResult.error("Error: subscription period must be day, week, or month")
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
            and self._subscription_cube_intent(subscription) is not None
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
        subscription_id: str = "",
        tenant_query: str = "",
        model: str = "",
        models: list[str] | None = None,
        breakdown: str = "summary",
        project: str = "",
        endpoint: str = "",
        provider: str = "",
        provider_id: str = "",
        providers: list[str] | None = None,
        selection_confirmed: bool = False,
        include_empty: bool = False,
        start_date: str = "",
        end_date: str = "",
        interactive: bool = False,
        all_tenants: bool = False,
        report_selections: list[dict[str, Any]] | None = None,
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
                )
            )
        if action == "examples":
            return self._result(
                examples_document(
                    self._authorized_for_magik(channel, user_id),
                    cost_enabled=self.cost_reports_enabled,
                    all_tenant_model_enabled=self._config.cube_model_all_tenant_report,
                    provider_quality_enabled=self.provider_quality_reports_enabled,
                )
            )
        if action == "recent":
            return self._result(
                recent_document(self._store.recent_runs(channel, user_id))
            )
        if action == "cube_report":
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
                report_selections=report_selections,
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
            job = self._cron.enable_job(subscription.cron_job_id, enabled) if self._cron else None
            if job is None:
                return ToolResult.error("Error: subscription Cron job not found")
            self._store.set_subscription_enabled(
                subscription_id, channel=channel, user_id=user_id, enabled=enabled
            )
            return ToolResult("订阅已启用。" if enabled else "订阅已停用。")
        if action == "subscription_remove":
            if self._cron:
                self._cron.remove_job(subscription.cron_job_id)
            self._store.remove_subscription(subscription_id, channel=channel, user_id=user_id)
            return ToolResult("订阅已删除。")
        return ToolResult.error("Error: unsupported report center action")
