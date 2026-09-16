"""Build and describe the supported deterministic report schedules.

This module is also the single source for the subscription recurrence
choices, the period -> template id maps, and the subscription report-type
table; the NLU Literal surfaces, the report_center tool schema, the guided
service validation, and the channel preview whitelists all derive from the
constants defined here (contract tests pin them equal).
"""

from __future__ import annotations

import re

# Single source for the subscription recurrence choices. The NLU module's
# Literal surface, the report_center tool schema enum, and the guided
# service's validation set all derive from this tuple; it was previously
# repeated as five hand-maintained sets that could drift silently.
SUBSCRIPTION_RECURRENCES: tuple[str, ...] = (
    "every_day",
    "workdays",
    "weekly",
    "monthly",
    "hourly",
)
# The two daily modes a UI may offer for the "day" period.
DAILY_MODES: tuple[str, ...] = ("workdays", "every_day")

# recurrence -> subscription period consumed by build_subscription_schedule.
# The channel preview and the guided service both map through this table;
# it was previously duplicated in both modules.
RECURRENCE_SCHEDULE_PERIODS: dict[str, str] = {
    "every_day": "day",
    "workdays": "day",
    "weekly": "week",
    "monthly": "month",
    "hourly": "recent1h",
}

# Subscription period -> matrix template id (custom windows share the custom
# matrix template). Moved here from report_center so routing, subscription
# compilation, and the guided service read one table instead of copies.
PERIOD_TEMPLATES: dict[str, str] = {
    "day": "usage_daily_matrix",
    "week": "usage_weekly_matrix",
    "month": "usage_monthly_matrix",
    "recent7": "usage_custom_matrix",
    "range": "usage_custom_matrix",
}
BRIEF_PERIOD_TEMPLATES: dict[str, str] = {
    "day": "usage_daily_brief",
    "week": "usage_weekly_brief",
    "month": "usage_monthly_brief",
    "recent7": "usage_custom_brief",
    "range": "usage_custom_brief",
}

# Canonical subscription report types (these ids are template ids) with the
# data period and compile variant each maps to. The preview's report-type
# whitelist, the quoted-reference template whitelist, and the tool schema
# enum all derive from this table; they were three hand-maintained copies.
SUBSCRIPTION_REPORT_TYPE_TABLE: dict[str, tuple[str, str]] = {
    "usage_daily_brief": ("day", "usage_brief"),
    "usage_weekly_brief": ("week", "usage_brief"),
    "usage_monthly_brief": ("month", "usage_brief"),
    "usage_customer_model_daily_brief": ("day", "customer_model_daily_brief"),
    "usage_customer_model_weekly_brief": ("week", "customer_model_weekly_brief"),
    "usage_customer_model_hourly_tpm": ("recent1h", "customer_model_hourly_tpm"),
}
# The tool-parameter surface accepts every concrete type above plus the
# special "inherit" marker used by quoted-report subscriptions.
SUBSCRIPTION_REPORT_TYPE_ENUM: tuple[str, ...] = (
    *SUBSCRIPTION_REPORT_TYPE_TABLE,
    "inherit",
)

REPORT_TEMPLATE_LABELS = {
    "usage_daily_brief": "日报简报",
    "usage_weekly_brief": "周报简报",
    "usage_monthly_brief": "月报简报",
    "usage_daily_matrix": "日报",
    "usage_weekly_matrix": "周报",
    "usage_monthly_matrix": "月报",
    "health_sre": "Cube 健康报告",
    "cost_account": "Cube 成本与账户报表",
    "usage_customer_model_daily_brief": "多客户多模型日报简报",
    "usage_customer_model_weekly_brief": "多客户多模型周报简报",
    "usage_customer_model_hourly_tpm": "多客户多模型小时 TPM 报告",
    "machine_tpm_peak": "单机折算 TPM 峰值",
}

REPORT_DATA_PERIODS = {
    "usage_daily_brief": "前一自然日，对比前一日和上周同期",
    "usage_weekly_brief": "上一完整周，对比前一完整周",
    "usage_monthly_brief": "上一自然月，对比前一自然月",
    "usage_daily_matrix": "前一自然日，对比前两日",
    "usage_weekly_matrix": "上周，对比上上周",
    "usage_monthly_matrix": "上月，对比前一自然月",
    "health_sre": "发送时生成平台级健康日/周趋势",
    "cost_account": "上月账单归属月，对比前一自然月；余额为发送时快照",
    "usage_customer_model_daily_brief": "前一自然日，对比前一日和上周同期",
    "usage_customer_model_weekly_brief": "上一完整自然周，对比此前一完整自然周",
    "machine_tpm_peak": "发送时按订阅周期计算单机折算 TPM 峰值",
    "usage_customer_model_hourly_tpm": "每小时（整点后 5 分钟）发送刚结束的上一完整小时 TPM",
}

_WEEKDAY_LABELS = {
    1: "周一",
    2: "周二",
    3: "周三",
    4: "周四",
    5: "周五",
    6: "周六",
    7: "周日",
}
_TIME_RE = re.compile(r"^(\d{2}):(\d{2})$")


def build_subscription_schedule(
    period: str,
    *,
    send_time: str,
    daily_mode: str = "workdays",
    weekday: int = 1,
    month_day: int = 1,
) -> str:
    """Build a five-field cron expression from the supported UI choices."""

    match = _TIME_RE.fullmatch(send_time)
    if match is None:
        raise ValueError("send_time must use HH:MM")
    hour, minute = (int(value) for value in match.groups())
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("send_time is outside the valid clock range")
    if period == "recent1h":
        # Five minutes after the hour: the upstream hourly TPM aggregate was
        # confirmed to lag slightly behind the hour boundary, so the buffer
        # keeps the freshest hour from being reported as zeros.
        return "5 * * * *"
    if period == "day":
        if daily_mode not in {"workdays", "every_day"}:
            raise ValueError("daily_mode must be workdays or every_day")
        day_of_week = "1-5" if daily_mode == "workdays" else "*"
        return f"{minute} {hour} * * {day_of_week}"
    if period == "week":
        if weekday not in _WEEKDAY_LABELS:
            raise ValueError("weekday must be between 1 and 7")
        return f"{minute} {hour} * * {weekday}"
    if period == "month":
        if not 1 <= month_day <= 28:
            raise ValueError("month_day must be between 1 and 28")
        return f"{minute} {hour} {month_day} * *"
    raise ValueError("period must be recent1h, day, week, or month")


def describe_subscription_schedule(schedule: str) -> str:
    """Render schedules created by this module as concise Chinese text."""

    fields = schedule.split()
    if len(fields) != 5:
        return "自定义定时"
    minute, hour, month_day, month, weekday = fields
    if minute == "5" and hour == "*" and month_day == "*" and month == "*" and weekday == "*":
        return "每小时（整点后 5 分钟）"
    if minute == "0" and hour == "*" and month_day == "*" and month == "*" and weekday == "*":
        return "每小时整点"
    if not minute.isdigit() or not hour.isdigit() or month != "*":
        return "自定义定时"
    if not 0 <= int(hour) <= 23 or not 0 <= int(minute) <= 59:
        return "自定义定时"
    clock = f"{int(hour):02d}:{int(minute):02d}"
    if month_day == "*" and weekday == "1-5":
        return f"每个工作日 {clock}"
    if month_day == "*" and weekday == "*":
        return f"每天 {clock}"
    if month_day == "*" and weekday.isdigit():
        label = _WEEKDAY_LABELS.get(int(weekday))
        if label:
            return f"每{label} {clock}"
    if month_day.isdigit() and weekday == "*" and 1 <= int(month_day) <= 31:
        return f"每月 {int(month_day)} 日 {clock}"
    return "自定义定时"


def report_template_label(template_id: str) -> str:
    return REPORT_TEMPLATE_LABELS.get(template_id, "固定报表")


def report_data_period(template_id: str) -> str:
    return REPORT_DATA_PERIODS.get(template_id, "发送时动态计算")
