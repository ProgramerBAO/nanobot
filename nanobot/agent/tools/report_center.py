"""Deterministic report capability home, history, and subscription control."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from nanobot.agent.reporting.magik_cube_intent import ReportIntent as MagikReportIntent
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context
from nanobot.bus.events import INBOUND_META_DIRECT_TOOL, OUTBOUND_META_AGENT_UI
from nanobot.config_base import Base
from nanobot.cron.session_turns import CRON_TRIGGER_META
from nanobot.cron.types import CronSchedule
from nanobot.reporting import build_default_registry
from nanobot.reporting.authorization import authorize_magik_params
from nanobot.reporting.capabilities import (
    ONBOARDING_VERSION,
    examples_document,
    home_document,
    recent_document,
    subscription_created_document,
    subscriptions_document,
)
from nanobot.reporting.schedules import build_subscription_schedule
from nanobot.reporting.store import ReportSubscription, get_report_state_store

_HOME_RE = re.compile(
    r"^(?:请)?(?:打开|显示|查看|进入)?(?:报表中心|报表菜单|功能菜单|菜单|帮助|你能做什么|你会什么|有哪些功能)[？?。！!]*$"
)
_RECENT_RE = re.compile(r"^(?:查看|打开|显示)?(?:我的)?最近报表[？?。！!]*$")
_SUBSCRIPTIONS_RE = re.compile(r"^(?:查看|打开|显示|管理)?(?:我的)?(?:报表)?订阅[？?。！!]*$")

_ALLOWED_REPORT_PARAM_KEYS = frozenset(
    {
        "tenant_query",
        "breakdown",
        "report_template",
        "granularity",
        "include_tpm",
        "report_selections",
        "comparison",
    }
)
_PERIOD_TEMPLATES: dict[str, str] = {
    "day": "usage_daily_matrix",
    "week": "usage_weekly_matrix",
    "month": "usage_monthly_matrix",
}


class ReportCenterToolConfig(Base):
    enable: bool = True
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


_REPORT_CENTER_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "home",
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
        "period": {"type": "string", "enum": ["day", "week", "month"]},
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

    def __init__(self, config: ReportCenterToolConfig, cron_service: Any, magik_tool: Tool | None):
        self._config = config
        self._cron = cron_service
        self._magik_tool = magik_tool
        self._store = get_report_state_store(
            backend=config.state_backend,
            postgres_dsn_env=config.postgres_dsn_env,
        )
        self._registry = build_default_registry(
            magik_enabled=magik_tool is not None,
            grafana_config=getattr(config, "grafana", None),
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
        return cls(ctx.config.reporting, ctx.cron_service, magik_tool)

    @property
    def name(self) -> str:
        return "report_center"

    @property
    def description(self) -> str:
        return (
            "Open the deterministic report center, show recent reports or subscriptions, "
            "and manage report subscriptions. Normal report generation remains in "
            "magik_cube_daily_report."
        )

    @property
    def trusted_direct(self) -> bool:
        return True

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

    @staticmethod
    def _result(document: Any) -> ToolResult:
        return ToolResult(
            document.fallback_text,
            metadata={OUTBOUND_META_AGENT_UI: document.to_agent_ui()},
        )

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

    def _dynamic_magik_params(self, subscription: ReportSubscription) -> dict[str, Any]:
        period = self._period_from_template(subscription.template_id)
        intent = MagikReportIntent(report_kind=period)  # type: ignore[arg-type]
        params = intent.to_tool_params(
            today=datetime.now(ZoneInfo(subscription.timezone)).date()
        )
        saved = dict(subscription.report_params)
        params.update(saved)
        params["report_template"] = "matrix_card"
        params["save_snapshot"] = False
        return params

    async def _subscribe(
        self,
        *,
        period: str,
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
        if period not in _PERIOD_TEMPLATES:
            return ToolResult.error("Error: subscription period must be day, week, or month")
        if not user_id or not self._authorized_for_magik(channel, user_id):
            return ToolResult.error("Error: no permission for the Magik Cube connector")
        params = self._safe_report_params(report_params)
        denial = authorize_magik_params(
            self._store,
            channel=channel,
            user_id=user_id,
            params=params,
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
        template_id = _PERIOD_TEMPLATES[period]
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
            connector_id="magik_cube",
            template_id=template_id,
            template_version="1.0",
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
        report_params: Any,
        channel: str,
        user_id: str,
    ) -> ToolResult:
        if self._cron is None:
            return ToolResult.error("Error: report subscriptions require the Gateway Cron service")
        if period not in _PERIOD_TEMPLATES:
            return ToolResult.error("Error: subscription period must be day, week, or month")
        if not user_id or not self._authorized_for_magik(channel, user_id):
            return ToolResult.error("Error: no permission for the Magik Cube connector")
        params = self._safe_report_params(report_params)
        denial = authorize_magik_params(
            self._store,
            channel=channel,
            user_id=user_id,
            params=params,
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
        if self._magik_tool is None:
            return ToolResult.error("Error: Magik Cube connector is unavailable")
        scheduled_at = trigger.get("scheduled_at_ms")
        if not isinstance(scheduled_at, int):
            return ToolResult.error("Error: Cron trigger is missing scheduled_at_ms")
        idempotency_key = (
            f"{subscription.subscription_id}:{scheduled_at}:{subscription.template_version}"
        )
        if not self._store.claim_delivery(idempotency_key):
            return ToolResult("该计划周期的报表已经处理，已跳过重复发送。")
        started = time.perf_counter()
        try:
            result = await self._magik_tool.execute(**self._dynamic_magik_params(subscription))
        except Exception:
            self._store.complete_delivery(idempotency_key, status="error")
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        run_id = str(trigger.get("run_id") or uuid.uuid4().hex)
        self._store.record_run(
            run_id=run_id,
            channel=subscription.channel,
            chat_id=subscription.chat_id,
            user_id=subscription.user_id,
            connector_id=subscription.connector_id,
            template_id=subscription.template_id,
            template_version=subscription.template_version,
            request={"subscription_id": subscription_id},
            status="error" if getattr(result, "is_error", False) else "ok",
            duration_ms=duration_ms,
            quality="partial" if getattr(result, "is_error", False) else "complete",
            error_type="tool_error" if getattr(result, "is_error", False) else "",
        )
        self._store.complete_delivery(
            idempotency_key,
            status="error" if getattr(result, "is_error", False) else "ok",
        )
        return result

    async def execute(
        self,
        action: str,
        period: str = "week",
        send_time: str = "10:00",
        daily_mode: str = "workdays",
        weekday: int = 1,
        month_day: int = 1,
        report_params: dict[str, Any] | None = None,
        subscription_id: str = "",
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
                )
            )
        if action == "examples":
            return self._result(examples_document(self._authorized_for_magik(channel, user_id)))
        if action == "recent":
            return self._result(
                recent_document(self._store.recent_runs(channel, user_id))
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
                report_params=report_params or {},
                channel=channel,
                user_id=user_id,
            )
        if action == "subscribe":
            return await self._subscribe(
                period=period,
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
