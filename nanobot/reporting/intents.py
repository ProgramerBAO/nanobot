"""Fixed report Intent router with no arbitrary query execution path."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Pattern
from zoneinfo import ZoneInfo

from nanobot.reporting.contracts import ReportIntent, ReportPeriod


@dataclass(frozen=True, slots=True)
class IntentRule:
    rule_id: str
    pattern: Pattern[str]
    connector_id: str
    template_id: str
    period: ReportPeriod


class IntentRouter:
    """Match reviewed phrases to fixed connector/template pairs."""

    def __init__(self, rules: tuple[IntentRule, ...] = ()) -> None:
        self._rules = rules

    def route(self, text: str, *, today: date | None = None) -> ReportIntent | None:
        value = text.strip()
        for rule in self._rules:
            if not rule.pattern.search(value):
                continue
            start, end = _period_dates(rule.period, today or date.today())
            if rule.period == "recent15m":
                end_time = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
                    second=0, microsecond=0
                )
                start_time = end_time - timedelta(minutes=15)
                return ReportIntent(
                    connector_id=rule.connector_id,
                    template_id=rule.template_id,
                    period=rule.period,
                    start_date=start_time.date(),
                    end_date=end_time.date(),
                    start_time=start_time,
                    end_time=end_time,
                    comparison_start_time=start_time - timedelta(minutes=15),
                    comparison_end_time=end_time - timedelta(minutes=15),
                )
            return ReportIntent(
                connector_id=rule.connector_id,
                template_id=rule.template_id,
                period=rule.period,
                start_date=start,
                end_date=end,
            )
        return None


def build_default_intent_router() -> IntentRouter:
    return IntentRouter(
        (
            IntentRule(
                "health_report",
                re.compile(r"健康|错误率|5xx|TTFT|接口延迟", re.I),
                "magik_cube",
                "health_sre",
                "recent15m",
            ),
            IntentRule(
                "cost_report",
                re.compile(r"成本|费用|账单|余额", re.I),
                "magik_cube",
                "cost_account",
                "month",
            ),
        )
    )


def _period_dates(period: ReportPeriod, today: date) -> tuple[date, date]:
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
    if period == "recent15m":
        return today, today
    raise ValueError("range requires explicit dates")
