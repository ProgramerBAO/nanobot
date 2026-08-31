"""Build and describe the supported deterministic report schedules."""

from __future__ import annotations

import re

REPORT_TEMPLATE_LABELS = {
    "usage_daily_matrix": "日报",
    "usage_weekly_matrix": "周报",
    "usage_monthly_matrix": "月报",
    "health_sre": "Cube 健康报告",
    "cost_account": "Cube 成本与账户报表",
}

REPORT_DATA_PERIODS = {
    "usage_daily_matrix": "前一自然日，对比前两日",
    "usage_weekly_matrix": "上周，对比上上周",
    "usage_monthly_matrix": "上月，对比前一自然月",
    "health_sre": "发送时生成平台级健康日/周趋势",
    "cost_account": "上月账单归属月，对比前一自然月；余额为发送时快照",
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
    raise ValueError("period must be day, week, or month")


def describe_subscription_schedule(schedule: str) -> str:
    """Render schedules created by this module as concise Chinese text."""

    fields = schedule.split()
    if len(fields) != 5:
        return "自定义定时"
    minute, hour, month_day, month, weekday = fields
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
