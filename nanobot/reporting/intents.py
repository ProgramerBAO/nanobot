"""Fixed report Intent router with no arbitrary query execution path."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Pattern

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
            IntentRule("health_report", re.compile(r"健康|错误率|5xx|TTFT|接口延迟", re.I), "grafana", "health_sre", "recent7"),
            IntentRule("cost_report", re.compile(r"成本|费用|账单|余额|Token消耗", re.I), "grafana", "cost_summary", "month"),
            IntentRule("capacity_report", re.compile(r"容量|GPU|TPM配额", re.I), "grafana", "capacity_summary", "week"),
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
    raise ValueError("range requires explicit dates")
