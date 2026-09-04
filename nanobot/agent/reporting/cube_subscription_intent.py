"""Bounded natural-language intent parsing for Cube report subscriptions.

The LLM extracts user-facing names and schedule semantics only. Tenant IDs,
authorization, report parameters, Cron expressions, and persistence remain
server-owned so a model error cannot broaden report access or create a job.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import json_repair
from loguru import logger

from nanobot.providers.base import parse_tool_arguments

if TYPE_CHECKING:
    from nanobot.utils.llm_runtime import LLMRuntime


SubscriptionReportType = Literal[
    "usage_daily_brief",
    "usage_weekly_brief",
    "usage_monthly_brief",
    "usage_customer_model_daily_brief",
    "inherit",
]
SubscriptionRecurrence = Literal["every_day", "workdays", "weekly", "monthly"]
SubscriptionModelScope = Literal["all", "selected", "summary", "inherit"]

_TOOL_NAME = "emit_cube_subscription_intent"
_SUBSCRIPTION_SIGNAL_RE = re.compile(
    r"(?:订阅|定时|每天|工作日|每周|每月|发送给我|推送给我).{0,96}"
    r"(?:发|发送|推送|报表|简报)|"
    r"(?:订阅|定时|每天|工作日|每周|每月).{0,128}"
    r"(?:日报|周报|月报|简报|这份报表|该报表)",
    re.IGNORECASE,
)
_VALID_REPORT_TYPES = frozenset(
    {
        "usage_daily_brief",
        "usage_weekly_brief",
        "usage_monthly_brief",
        "usage_customer_model_daily_brief",
        "inherit",
    }
)
_VALID_RECURRENCES = frozenset({"every_day", "workdays", "weekly", "monthly"})
_VALID_MODEL_SCOPES = frozenset({"all", "selected", "summary", "inherit"})
_PAYLOAD_FIELDS = frozenset(
    {
        "report_type",
        "tenant_scope",
        "tenant_aliases",
        "model_scope",
        "models",
        "recurrence",
        "send_time",
        "weekday",
        "month_day",
        "inherit_report_scope",
    }
)


def _parse_chinese_number(value: str) -> int | None:
    """Parse the small bounded Chinese number forms used in schedule text.

    This parser intentionally supports only clock/day values.  It is not a
    general natural-language number parser, which keeps deterministic routing
    predictable and prevents unrelated text from becoming a schedule.
    """

    if value.isdigit():
        return int(value)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if value.startswith("十"):
        tail = value[1:]
        return 10 + digits.get(tail, 0) if tail else 10
    if value.endswith("十"):
        head = value[:-1]
        return digits.get(head, 0) * 10 if head else 10
    if len(value) == 2 and value[0] in digits and value[1] in digits:
        return digits[value[0]] * 10 + digits[value[1]]
    return digits.get(value)


_CLOCK_RE = re.compile(
    r"(?P<meridiem>上午|早上|中午|下午|晚上)?\s*"
    r"(?P<hour>\d{1,2}|[零一二三四五六七八九十两]{1,3})"
    r"(?:点|时)(?:(?P<minute>[0-5]?\d)分?)?"
    r"|(?P<clock_hour>\d{1,2}):(?P<clock_minute>[0-5]\d)"
)


def _deterministic_clock(text: str) -> str | None:
    """Extract a bounded 24-hour clock value from common Chinese phrasing."""

    match = _CLOCK_RE.search(text)
    if not match:
        return None
    raw_hour = match.group("hour") or match.group("clock_hour") or ""
    hour = _parse_chinese_number(raw_hour)
    if hour is None or not 0 <= hour <= 23:
        return None
    raw_minute = match.group("minute") or match.group("clock_minute") or "0"
    minute = int(raw_minute)
    meridiem = match.group("meridiem")
    if meridiem in {"下午", "晚上"} and hour < 12:
        hour += 12
    elif meridiem == "中午" and hour < 11:
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def parse_deterministic_subscription_intent(
    text: str,
    *,
    referenced_report: bool = False,
) -> CubeSubscriptionIntent | None:
    """Parse unambiguous Cube subscription phrases without an LLM.

    The function extracts only cadence, time and coarse report/model scope.
    Customer identities are deliberately left to the caller's live Cube
    catalog resolver.  For a quoted report, scope is inherited only when the
    message contains no explicit scope override; the stored server-side
    reference remains the sole source of tenant/model identity.
    """

    raw = text.strip()
    if not is_subscription_intent_candidate(raw):
        return None
    send_time = _deterministic_clock(raw)
    if send_time is None:
        return None

    if re.search(r"工作日", raw):
        recurrence: SubscriptionRecurrence = "workdays"
    elif re.search(r"每周", raw):
        recurrence = "weekly"
    elif re.search(r"每月", raw):
        recurrence = "monthly"
    elif re.search(r"每天|每日", raw):
        recurrence = "every_day"
    else:
        return None

    weekday = 1
    weekday_match = re.search(r"每周\s*([一二三四五六日天1-7])", raw)
    if weekday_match:
        weekday = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                   "六": 6, "日": 7, "天": 7}.get(weekday_match.group(1),
                                                    int(weekday_match.group(1))
                                                    if weekday_match.group(1).isdigit()
                                                    else 1)
    month_day = 1
    month_match = re.search(r"每月\s*(\d{1,2}|[一二三四五六七八九十两]{1,3})\s*[日号]", raw)
    if month_match:
        month_day = _parse_chinese_number(month_match.group(1)) or 0
        if not 1 <= month_day <= 28:
            return None

    if referenced_report:
        # Explicit customer/model/template wording means the user is changing
        # scope; leave that case to the validated classifier and catalog path.
        if re.search(r"(?:客户|租户|用户|模型|endpoint|项目|供应商)", raw, re.IGNORECASE):
            return None
        return CubeSubscriptionIntent(
            report_type="inherit",
            tenant_scope="inherit",
            tenant_aliases=(),
            model_scope="inherit",
            models=(),
            recurrence=recurrence,
            send_time=send_time,
            weekday=weekday,
            month_day=month_day,
            inherit_report_scope=True,
        )

    all_models = bool(re.search(r"(?:全部|所有|全量|各个|每个|全体)\s*模型", raw))
    has_daily = bool(re.search(r"日报", raw))
    has_multi_scope = bool(re.search(r"多客户|多模型", raw))
    if has_daily and (has_multi_scope or all_models):
        report_type: SubscriptionReportType = "usage_customer_model_daily_brief"
    elif "周报" in raw:
        report_type = "usage_weekly_brief"
    elif "月报" in raw:
        report_type = "usage_monthly_brief"
    elif has_daily:
        report_type = "usage_daily_brief"
    else:
        return None
    tenant_scope: Literal["selected", "all", "inherit"] = (
        "all" if re.search(r"(?:全部|所有|全量|各个|每个|全体)\s*(?:客户|租户|用户)", raw)
        else "selected"
    )
    model_scope: SubscriptionModelScope = "all" if all_models else "summary"
    return CubeSubscriptionIntent(
        report_type=report_type,
        tenant_scope=tenant_scope,
        tenant_aliases=(),
        model_scope=model_scope,
        models=(),
        recurrence=recurrence,
        send_time=send_time,
        weekday=weekday,
        month_day=month_day,
        inherit_report_scope=False,
    )


def _split_entity_values(value: Any) -> tuple[str, ...] | None:
    """Normalize bounded human entity lists without changing names containing spaces.

    The classifier schema requires arrays, but providers sometimes serialize a
    Chinese list as one array item such as ``"阳春面、豆汁、佛跳墙"``.  Only
    explicit list separators are accepted here; whitespace is intentionally
    preserved because it can be part of a model or tenant display name.
    """

    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    values: list[str] = []
    for item in value:
        values.extend(
            part.strip()
            for part in re.split(r"[,，、;；\n]+", item)
            if part.strip()
        )
    return tuple(dict.fromkeys(values))


def is_subscription_intent_candidate(text: str) -> bool:
    """Return whether one bounded subscription-classification call is appropriate.

    Natural-language schedules often put the recipient and a long list of
    tenants between the cadence and the report name.  The matcher therefore
    uses a bounded window rather than a short adjacency expression.  It still
    requires a delivery/schedule signal, so an ordinary ``每天查看日报`` query
    is not silently turned into a subscription.
    """

    raw = text.strip()
    if not raw:
        return False
    if not _SUBSCRIPTION_SIGNAL_RE.search(raw):
        return False
    has_schedule = bool(re.search(r"订阅|定时|每天|工作日|每周|每月", raw, re.IGNORECASE))
    has_delivery = bool(re.search(r"发|发送|推送|报表|简报", raw, re.IGNORECASE))
    return has_schedule and has_delivery


@dataclass(frozen=True, slots=True)
class CubeSubscriptionIntent:
    """Validated subscription semantics before catalog and RBAC resolution."""

    report_type: SubscriptionReportType
    tenant_scope: Literal["selected", "all", "inherit"]
    tenant_aliases: tuple[str, ...]
    model_scope: SubscriptionModelScope
    models: tuple[str, ...]
    recurrence: SubscriptionRecurrence
    send_time: str
    weekday: int = 1
    month_day: int = 1
    inherit_report_scope: bool = False

    @classmethod
    def from_payload(cls, payload: Any) -> CubeSubscriptionIntent | None:
        """Validate model output without silently repairing unsafe scope fields."""

        if not isinstance(payload, dict) or set(payload) - _PAYLOAD_FIELDS:
            return None
        report_type = str(payload.get("report_type") or "").strip()
        recurrence = str(payload.get("recurrence") or "").strip()
        model_scope = str(payload.get("model_scope") or "").strip()
        tenant_scope = str(payload.get("tenant_scope") or "").strip()
        if report_type not in _VALID_REPORT_TYPES or recurrence not in _VALID_RECURRENCES:
            return None
        if model_scope not in _VALID_MODEL_SCOPES or tenant_scope not in {
            "selected",
            "all",
            "inherit",
        }:
            return None
        send_time = str(payload.get("send_time") or "").strip()
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", send_time):
            return None
        # Providers occasionally omit an empty optional list even though the
        # schema requests it. Empty lists are safe defaults; selected scopes
        # are still verified by the live catalog boundary.
        aliases = _split_entity_values(payload.get("tenant_aliases", []))
        models = _split_entity_values(payload.get("models", []))
        if aliases is None or models is None:
            return None
        if len(aliases) > 20 or len(models) > 20:
            return None
        inherit_value = payload.get("inherit_report_scope")
        if not isinstance(inherit_value, bool):
            return None
        inherit = inherit_value
        if inherit and report_type != "inherit":
            return None
        # A provider may fail to extract names from a long Chinese sentence even
        # though the server can recover them from the original text and the live
        # Cube catalog.  Keep an empty selected list valid at this boundary; the
        # caller must still resolve at least one live tenant before preview or
        # creation, so this is recovery rather than a scope broadening.
        if model_scope == "selected" and not models:
            return None
        weekday = payload.get("weekday")
        month_day = payload.get("month_day")
        if (
            not isinstance(weekday, int)
            or isinstance(weekday, bool)
            or not isinstance(month_day, int)
            or isinstance(month_day, bool)
        ):
            return None
        if not 1 <= weekday <= 7 or not 1 <= month_day <= 28:
            return None
        return cls(
            report_type=report_type,  # type: ignore[arg-type]
            tenant_scope=tenant_scope,  # type: ignore[arg-type]
            tenant_aliases=aliases,
            model_scope=model_scope,  # type: ignore[arg-type]
            models=models,
            recurrence=recurrence,  # type: ignore[arg-type]
            send_time=send_time,
            weekday=weekday,
            month_day=month_day,
            inherit_report_scope=inherit,
        )


def _classifier_schema() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": _TOOL_NAME,
                "description": "Return only a structured Cube report subscription intent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "report_type": {
                            "type": "string",
                            "enum": sorted(_VALID_REPORT_TYPES),
                        },
                        "tenant_scope": {
                            "type": "string",
                            "enum": ["selected", "all", "inherit"],
                        },
                        "tenant_aliases": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 128},
                            "maxItems": 20,
                        },
                        "model_scope": {
                            "type": "string",
                            "enum": sorted(_VALID_MODEL_SCOPES),
                        },
                        "models": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 128},
                            "maxItems": 20,
                        },
                        "recurrence": {
                            "type": "string",
                            "enum": sorted(_VALID_RECURRENCES),
                        },
                        "send_time": {
                            "type": "string",
                            "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
                        },
                        "weekday": {"type": "integer", "minimum": 1, "maximum": 7},
                        "month_day": {"type": "integer", "minimum": 1, "maximum": 28},
                        "inherit_report_scope": {"type": "boolean"},
                    },
                    "required": [
                        "report_type",
                        "tenant_scope",
                        "tenant_aliases",
                        "model_scope",
                        "models",
                        "recurrence",
                        "send_time",
                        "weekday",
                        "month_day",
                        "inherit_report_scope",
                    ],
                    "additionalProperties": False,
                },
            },
        }
    ]


async def classify_subscription_intent(
    text: str,
    runtime: LLMRuntime,
    *,
    timeout_seconds: float,
    referenced_report: dict[str, str] | None = None,
) -> CubeSubscriptionIntent | None:
    """Extract one subscription intent with a forced tool call and hard timeout."""

    reference_hint = ""
    if referenced_report:
        reference_hint = (
            " A verified referenced report exists. Use report_type=inherit, "
            "tenant_scope=inherit, model_scope=inherit and inherit_report_scope=true unless "
            "the user explicitly changes that field. Referenced template="
            f"{referenced_report.get('template_id', '')}, period="
            f"{referenced_report.get('period', '')}."
        )
    messages = [
        {
            "role": "system",
            "content": (
                "Extract a Cube report subscription request. Parse names exactly as written; "
                "never invent tenant IDs, Cron expressions, URLs, API paths, SQL, tokens, or "
                "credentials. Chinese 上午十点 means 10:00. 工作日 means workdays; 每天 means "
                "every_day. 多客户多模型日报简报 means "
                "usage_customer_model_daily_brief. Any daily brief that names more than one "
                "customer must also use usage_customer_model_daily_brief. 全部模型 means "
                "model_scope=all. "
                "Use weekday 1 for Monday and month_day 1 when they are irrelevant."
                + reference_hint
            ),
        },
        {"role": "user", "content": text[:2_000]},
    ]
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await runtime.provider.chat(
                messages=messages,
                tools=_classifier_schema(),
                model=runtime.model,
                max_tokens=384,
                temperature=0,
                reasoning_effort=None,
                tool_choice={"type": "function", "function": {"name": _TOOL_NAME}},
            )
    except TimeoutError:
        logger.warning("Cube subscription intent classifier timed out")
        return None
    except Exception as exc:
        logger.warning(
            "Cube subscription intent classifier failed: error_type={}", type(exc).__name__
        )
        return None

    payload: Any = None
    if response.tool_calls and not response.should_execute_tools:
        logger.warning(
            "Cube subscription intent rejected: stage=classifier reason=unsafe_finish"
        )
        return None
    for call in response.tool_calls:
        if call.name == _TOOL_NAME:
            payload = parse_tool_arguments(call.arguments)
            break
    if payload is None and response.content:
        try:
            payload = json_repair.loads(response.content)
        except Exception:
            logger.warning(
                "Cube subscription intent rejected: stage=classifier reason=invalid_json"
            )
            return None
    intent = CubeSubscriptionIntent.from_payload(payload)
    if intent is None:
        logger.warning(
            "Cube subscription intent rejected: stage=classifier reason=invalid_schema"
        )
    return intent
