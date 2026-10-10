"""Bounded read-only report classification; execution and identity stay server-owned.

This module deliberately does not import the reporting package: tool discovery
imports it before connector construction has finished. The classifier emits
names copied from the current message, never credentials, IDs or executable plans.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nanobot.providers.base import parse_tool_arguments

ROUTER_SETTING = "report_intent_router"
ROUTER_MODES = ("off", "fallback", "primary")
_TOOL = "emit_report_intent"
_SIGNAL = re.compile(
    r"报表|报告|日报|周报|月报|简报|订阅|用量|消耗|吞吐|token|tpm|"
    r"健康|错误率|延迟|账单|余额|花了|供应商|每台机器|单机|客户|模型|请求量",
    re.I,
)
_REALTIME = re.compile(r"当前|现在|目前|实时", re.I)
_TPM = re.compile(r"tpm|每分钟.*token|吞吐", re.I)
_HOUR = re.compile(r"(?:上一|上个|过去|最近|刚才).{0,8}(?:小时|钟头)", re.I)
_UNSAFE = re.compile(r"https?://|Bearer\s|\bSELECT\b|/api/|password|api[_-]?key", re.I)


def effective_router_mode(store: Any, default: str = "off") -> str:
    """Read the instant page override; corrupt persisted values fail closed."""
    mode = store.setting(ROUTER_SETTING, default)
    return mode if mode in ROUTER_MODES else "off"


def is_report_candidate(text: str) -> bool:
    """Bound classifier cost and leave realtime TPM to the Grafana agent turn."""
    if not text.strip() or len(text) > 2000 or _UNSAFE.search(text):
        return False
    if is_realtime_tpm(text):
        return False
    return bool(_SIGNAL.search(text))


def is_realtime_tpm(text: str) -> bool:
    """Protect live token-throughput phrasing in every deterministic tier."""
    return bool(_REALTIME.search(text) and _TPM.search(text) and not _HOUR.search(text))


class ReportIntentDraft(BaseModel):
    """Untrusted semantic slots, validated before compiling a read-only action.

    Mutation actions are absent. Subscription creation remains in the separate
    confirmation classifier; fuzzy entity matching is intentionally Phase 3.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal[
        "home",
        "examples",
        "recent",
        "subscriptions",
        "cube_report",
        "multi_scope_brief",
        "customer_model_hourly_tpm",
        "machine_tpm_report",
        "health_report",
        "cost_report",
        "provider_quality_report",
        "clarify",
        "not_report",
        "realtime",
    ]
    confidence: float = Field(ge=0, le=1)
    period: Literal["day", "week", "month", "recent7", "recent15m", "recent1h", "range"] | None = (
        None
    )
    tenants: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Customer names copied from user text, never model names or dates",
    )
    models: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Model names/aliases copied from user text, e.g. K3 or vLLM, not customer names",
    )
    all_tenants: bool = False
    all_models: bool = False
    provider: str = ""
    endpoint: str = ""
    cluster: str = ""
    report_template: Literal["brief", "matrix_card", "full"] = "brief"
    time_expression: Literal[
        "default",
        "yesterday",
        "day_before_yesterday",
        "last_week",
        "last_month",
        "recent7",
        "recent1h",
        "recent15m",
        "explicit",
        "unsupported",
    ] = "default"
    start_date: str = ""
    end_date: str = ""

    def compile(self, text: str, *, today: date) -> dict[str, Any] | None:
        """Compile grounded slots only; uncertain scope opens existing selectors.

        The output is ordinary tool input and receives the same live catalog,
        template policy and RBAC validation as a manually invoked report.
        Explicit dates are authoritative and cannot be replaced by defaults.
        """
        if self.action in {"not_report", "realtime"}:
            return None
        if (self.start_date or self.end_date) and self.time_expression != "explicit":
            # A live model placed a customer name and invented dates here.
            # Do not silently ignore malformed slots and claim a valid scope.
            return {"action": "report_parse_failed"}
        if (
            self.confidence < 0.85
            or self.action == "clarify"
            or self.time_expression == "unsupported"
        ):
            return {"action": "report_parse_failed"}
        names = [*self.tenants, *self.models, self.provider, self.endpoint, self.cluster]
        if any(not name.strip() for name in [*self.tenants, *self.models]):
            return {"action": "report_parse_failed"}
        if any(
            value
            and (
                len(value) > 128 or value.casefold() not in text.casefold() or _UNSAFE.search(value)
            )
            for value in names
        ):
            return {"action": "report_parse_failed"}
        explicit_all_tenants = bool(
            re.search(r"(?:全部|所有|各个|每个|全体|各)(?:的)?(?:客户|用户|租户)|大家", text)
        )
        all_tenants = self.all_tenants or explicit_all_tenants
        if self.all_tenants and not explicit_all_tenants:
            return {"action": "report_parse_failed"}
        if self.all_models and not re.search(r"全部模型|所有模型|各模型|各个模型|每个模型", text):
            return {"action": "report_parse_failed"}
        if self.action in {"home", "examples", "recent", "subscriptions"}:
            return {"action": self.action}
        # A runner cannot silently discard a field it does not implement.
        # Reject such scopes rather than widening a request to platform data.
        if self.action in {"multi_scope_brief", "customer_model_hourly_tpm"} and any(
            (self.provider, self.endpoint, self.cluster)
        ):
            return {"action": "report_parse_failed"}
        if self.action in {"machine_tpm_report", "provider_quality_report"} and (
            self.tenants or all_tenants
        ):
            return {"action": "report_parse_failed"}
        unsupported_filters = {
            "cube_report": (self.cluster,),
            "machine_tpm_report": (self.provider, self.endpoint),
            "provider_quality_report": (self.cluster,),
            "cost_report": (self.provider, self.cluster),
        }
        if any(unsupported_filters.get(self.action, ())):
            return {"action": "report_parse_failed"}
        allowed = {
            "cube_report": {"day", "week", "month", "recent7", "range"},
            "multi_scope_brief": {"day", "week"},
            "customer_model_hourly_tpm": {"recent1h"},
            "machine_tpm_report": {"day", "week", "range"},
            "health_report": {"recent15m", "day", "week"},
            "cost_report": {"month"},
            "provider_quality_report": {"recent15m", "day", "week", "range"},
        }
        # Omitted slots use the chosen action's documented default, not the
        # daily default for every report. Explicit incompatible periods reject.
        relative_period = {
            "last_week": "week",
            "last_month": "month",
            "recent7": "recent7",
            "recent1h": "recent1h",
            "recent15m": "recent15m",
        }
        period = (
            self.period
            or relative_period.get(self.time_expression)
            or {
                "customer_model_hourly_tpm": "recent1h",
                "health_report": "recent15m",
                "provider_quality_report": "recent15m",
                "cost_report": "month",
            }.get(self.action, "day")
        )
        if period not in allowed[self.action]:
            return {"action": "report_parse_failed"}
        if self.action == "customer_model_hourly_tpm" and re.search(r"请求|rpm", text, re.I):
            return {"action": "report_parse_failed"}
        if self.time_expression in {"last_week", "last_month", "recent7", "recent1h", "recent15m"}:
            required = {
                "last_week": "week",
                "last_month": "month",
                "recent7": "recent7",
                "recent1h": "recent1h",
                "recent15m": "recent15m",
            }
            if period != required[self.time_expression]:
                return {"action": "report_parse_failed"}
        # Never reinterpret an explicitly named live/current window as a
        # historical complete window, even when the model misclassifies it.
        if re.search(r"本周|这周|这个礼拜|本月|这个月", text):
            return {"action": "report_parse_failed"}
        params: dict[str, Any] = {"action": self.action, "period": period}
        explicit = re.findall(r"\d{4}-\d{2}-\d{2}", text)
        if explicit and self.action in {"cost_report", "customer_model_hourly_tpm"}:
            # These runners derive their window from the execution clock and
            # do not accept dates; forwarding a date would silently discard it.
            return {"action": "report_parse_failed"}
        if re.search(r"前天", text) and self.time_expression != "day_before_yesterday":
            return {"action": "report_parse_failed"}
        if explicit and self.time_expression != "explicit":
            return {"action": "report_parse_failed"}
        if self.time_expression == "explicit":
            try:
                start = date.fromisoformat(self.start_date)
                end = date.fromisoformat(self.end_date or self.start_date)
            except ValueError:
                return {"action": "report_parse_failed"}
            if start.isoformat() not in explicit or end.isoformat() not in explicit or end < start:
                return {"action": "report_parse_failed"}
            params.update(start_date=start.isoformat(), end_date=end.isoformat())
        elif self.time_expression in {"yesterday", "day_before_yesterday"}:
            if period != "day":
                return {"action": "report_parse_failed"}
            target = today - timedelta(
                days=2 if self.time_expression == "day_before_yesterday" else 1
            )
            params.update(start_date=target.isoformat(), end_date=target.isoformat())
        if period == "range" and not (params.get("start_date") and params.get("end_date")):
            return {"action": "report_parse_failed"}
        if (
            self.action in {"machine_tpm_report", "provider_quality_report", "cost_report"}
            and len(self.models) > 1
        ):
            # The existing runners consume a single model, so a list cannot
            # silently become an unfiltered report.
            return {"action": "report_parse_failed"}
        if self.action == "health_report":
            if any(names) or explicit:
                return {"action": "report_parse_failed"}
            return params
        if self.action in {"multi_scope_brief", "customer_model_hourly_tpm"}:
            params.update(
                tenants=list(dict.fromkeys(self.tenants)),
                models=list(dict.fromkeys(self.models)),
                all_tenants=all_tenants,
                model_scope="selected" if self.models else "all",
                interactive=not self.tenants and not all_tenants,
            )
        else:
            if len(self.tenants) > 1:
                return {"action": "report_parse_failed"}
            params.update(
                tenant_query=self.tenants[0] if self.tenants else "",
                model=self.models[0] if len(self.models) == 1 else "",
                models=self.models,
                all_tenants=all_tenants,
                interactive=not self.tenants and not all_tenants,
            )
            if self.action == "cube_report":
                params.update(
                    report_template=self.report_template,
                    breakdown="model" if self.models or self.all_models else "summary",
                )
            for key in ("provider", "endpoint", "cluster"):
                if getattr(self, key):
                    params[key] = getattr(self, key)
        return params


async def classify_report_intent(
    text: str, runtime: Any, *, timeout_seconds: float = 3, max_tokens: int = 384
) -> ReportIntentDraft | None:
    """Make exactly one forced tool call; reject prose, extra calls and unsafe finish.

    No history or catalog/permission data is sent. Logs contain outcomes only,
    never user text, names, responses or numeric business data.
    Explicit evaluation budgets may accommodate reasoning models without
    changing the Gateway's existing defaults or weakening slot validation.
    """
    if not is_report_candidate(text):
        return None
    parameters = ReportIntentDraft.model_json_schema()
    if not re.search(r"\d{4}-\d{2}-\d{2}", text):
        # Relative windows are computed server-side. Removing date slots from
        # the offered schema prevents the model filling them with invented
        # calendar values; compilation still rejects unsolicited date slots.
        for field in ("start_date", "end_date"):
            parameters["properties"].pop(field, None)
    prompt = (
        "Extract a read-only Cube report intent, not an answer. Copy entity names exactly "
        "from the user. Never invent IDs, numeric report values, queries, credentials or "
        "tool arguments. Use not_report for explanatory/non-report questions, realtime for "
        "live TPM, clarify for ambiguous or unsupported windows. Missing customers remain "
        "empty for a selector. 多客户/各客户 use multi_scope_brief; prior complete hour TPM "
        "uses customer_model_hourly_tpm/recent1h; per-machine TPM uses machine_tpm_report. "
        "TPM measures tokens, not requests per minute: RPM requests require clarify. "
        "Health defaults recent15m; provider comparison defaults recent15m; cost only supports "
        "previous complete month. Current week/month requires unsupported, never last week/month. "
        "Daily is yesterday, weekly last complete natural week, monthly last complete month. "
        "Explicit YYYY-MM-DD dates must be copied; relative dates use time_expression. "
        "Entity roles matter: K3/Kimi-K3/GLM-5.2/vLLM are model wording, not customer names. "
        "Customer aliases may be food names or other nicknames (佛跳墙, 豆汁, 阳春面). "
        "Copy these to tenants when named; do not drop unfamiliar customers because you "
        "cannot verify them. The server resolves catalog identities later. Example: "
        "过去一小时佛跳墙全部模型的吞吐量怎么样 => tenants [佛跳墙], models [], "
        "action customer_model_hourly_tpm, period recent1h, all_models true. "
        "Example: 上一个钟头K3的TPM什么情况 => action customer_model_hourly_tpm, "
        "period recent1h, models [K3], tenants []. Do not invent a customer for model-only requests. "
        "上一个钟头/上个小时/过去一小时 use time_expression recent1h, never recent7. "
        "Confidence measures clarity of the requested action, not availability of API data; "
        "missing scope is a valid selector, not itself uncertainty. 错误率有没有涨起来 "
        "requests health_report/recent15m. 单机折算TPM看一下vLLM requests "
        "machine_tpm_report/day, models [vLLM]; vLLM is not a cluster. "
        "Explicit all/every customer wording sets all_tenants true; explicit multiple customer "
        "names must all be copied to tenants and use multi_scope_brief for day/week. "
        "For recent7, month or custom ranges, use cube_report even for all customers; "
        "multi_scope_brief supports day and week only. Relative windows never emit calendar dates. "
        "Omit empty/default fields: emit only action, confidence and necessary slots. "
        "Navigation: listing available reports (有哪些报表可以看) uses home; "
        "asking for sample phrasing/how to ask uses examples; previous generated reports "
        "use recent; existing subscriptions use subscriptions. Use the exact action and "
        "time_expression enum strings, never alternate names or camelCase keys. Mutations and subscription "
        "creation use not_report (handled by a separate confirmation flow)."
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await runtime.provider.chat(
                messages=[{"role": "system", "content": prompt}, {"role": "user", "content": text}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": _TOOL,
                            "description": "Extract semantic slots only",
                            "parameters": parameters,
                        },
                    }
                ],
                model=runtime.model,
                max_tokens=max_tokens,
                temperature=0,
                # Request reduced thinking only through the provider's verified
                # mappings. An opaque endpoint/model alias may have no mapping;
                # "none" therefore does not guarantee backend thinking is off.
                # Evaluation may grant more tokens/time without changing chat.
                reasoning_effort="none",
                tool_choice={"type": "function", "function": {"name": _TOOL}},
            )
        if not response.should_execute_tools or len(response.tool_calls) != 1:
            logger.warning(
                "Report intent output rejected: finish_reason={} tool_calls={}",
                response.finish_reason,
                len(response.tool_calls),
            )
            return None
        call = response.tool_calls[0]
        if call.name != _TOOL:
            return None
        return ReportIntentDraft.model_validate(parse_tool_arguments(call.arguments))
    except ValidationError as exc:
        # Never log Pydantic's input values: only bounded known field names and
        # error codes are needed to diagnose a model/schema mismatch.
        errors = [
            {
                "field": item["loc"][0]
                if item["loc"] and item["loc"][0] in ReportIntentDraft.model_fields
                else "unknown",
                "type": item["type"],
            }
            for item in exc.errors()
        ][:8]
        logger.warning("Report intent schema rejected: errors={}", errors)
    except TimeoutError as exc:
        logger.warning("Report intent classification rejected: reason={}", type(exc).__name__)
    except Exception as exc:
        logger.warning("Report intent provider unavailable: error_type={}", type(exc).__name__)
    return None
