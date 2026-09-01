"""Deterministic current-time, timezone, and calendar calculations."""

from __future__ import annotations

import calendar
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.schema import (
    IntegerSchema,
    NumberSchema,
    StringSchema,
    tool_parameters_schema,
)

_TIME_VALUE_DESCRIPTION = (
    "ISO 8601 datetime, date, Unix seconds/milliseconds as a numeric string, "
    "or now/today/yesterday/tomorrow. Naive values use source_timezone."
)

_TIME_PARAMETERS = tool_parameters_schema(
    action=StringSchema(
        "Operation to perform. Use now whenever an answer depends on the current clock.",
        enum=["now", "convert", "add", "difference", "range"],
    ),
    timezone=StringSchema(
        "Target IANA timezone, for example Asia/Shanghai or America/New_York. "
        "Defaults to the agent timezone."
    ),
    source_timezone=StringSchema(
        "IANA timezone applied only to input values that do not contain an offset. "
        "Defaults to the agent timezone."
    ),
    time=StringSchema(_TIME_VALUE_DESCRIPTION),
    end_time=StringSchema(
        "Second ISO/Unix/relative time. Required for action=difference."
    ),
    years=IntegerSchema(description="Calendar years to add; action=add only."),
    months=IntegerSchema(description="Calendar months to add; action=add only."),
    weeks=IntegerSchema(description="Calendar weeks to add; action=add only."),
    days=IntegerSchema(description="Calendar days to add; action=add only."),
    hours=NumberSchema(description="Elapsed hours to add; action=add only."),
    minutes=NumberSchema(description="Elapsed minutes to add; action=add only."),
    seconds=NumberSchema(description="Elapsed seconds to add; action=add only."),
    unit=StringSchema(
        "Calendar boundary for action=range.",
        enum=["day", "week", "month", "quarter", "year"],
    ),
    week_start=StringSchema(
        "First weekday for action=range when unit=week.",
        enum=["monday", "sunday"],
    ),
    required=["action"],
    description=(
        "Action requirements: convert/add require time; difference requires time and end_time; "
        "range accepts an optional time and defaults to the current day."
    ),
)

_NUMERIC_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_RELATIVE_DAYS = {
    "today": 0,
    "今天": 0,
    "yesterday": -1,
    "昨天": -1,
    "tomorrow": 1,
    "明天": 1,
}
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@tool_parameters(_TIME_PARAMETERS)
class TimeTool(Tool):
    """Provide precise time data without relying on the model's internal clock."""

    _scopes = {"core", "subagent"}

    def __init__(
        self,
        default_timezone: str = "UTC",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._zone(default_timezone)
        self._default_timezone = default_timezone
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(default_timezone=ctx.timezone)

    @property
    def name(self) -> str:
        return "time"

    @property
    def description(self) -> str:
        return (
            "Get the exact current time, convert timezones or Unix timestamps, add calendar/time "
            "durations, calculate exact elapsed differences, and resolve day/week/month/quarter/year "
            "boundaries. Always call this tool when an answer depends on the current date or time; "
            "never infer the live clock from model knowledge."
        )

    @property
    def read_only(self) -> bool:
        return True

    @staticmethod
    def _zone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone '{name}'") from None

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("time tool clock returned a naive datetime")
        return value

    def _parse(self, value: str | None, source_timezone: str) -> datetime:
        zone = self._zone(source_timezone)
        if value is None or not str(value).strip():
            return self._now()
        text = str(value).strip()
        normalized = text.casefold()
        if normalized in {"now", "现在"}:
            return self._now()
        if normalized in _RELATIVE_DAYS:
            current = self._now().astimezone(zone)
            day = current.date() + timedelta(days=_RELATIVE_DAYS[normalized])
            return datetime(day.year, day.month, day.day, tzinfo=zone)
        if _NUMERIC_RE.fullmatch(text):
            timestamp = float(text)
            if abs(timestamp) >= 100_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=UTC)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"invalid time value '{text}'") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = self._attach_timezone(parsed, zone, source_timezone)
        return parsed

    @staticmethod
    def _attach_timezone(value: datetime, zone: ZoneInfo, zone_name: str) -> datetime:
        first = value.replace(tzinfo=zone, fold=0)
        round_trip = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if round_trip != value:
            raise ValueError(
                f"nonexistent local time '{value.isoformat()}' in timezone '{zone_name}'; "
                "provide an explicit UTC offset"
            )
        second = value.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError(
                f"ambiguous local time '{value.isoformat()}' in timezone '{zone_name}'; "
                "provide an explicit UTC offset"
            )
        return first

    @staticmethod
    def _format_offset(value: datetime) -> str:
        offset = value.utcoffset() or timedelta(0)
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        hours, minutes = divmod(abs(total_minutes), 60)
        return f"{sign}{hours:02d}:{minutes:02d}"

    @classmethod
    def _describe(cls, value: datetime, timezone: str) -> dict[str, Any]:
        local = value.astimezone(cls._zone(timezone))
        utc = local.astimezone(UTC).isoformat().replace("+00:00", "Z")
        epoch_delta = local.astimezone(UTC) - _UNIX_EPOCH
        unix_microseconds = (
            (epoch_delta.days * 86400 + epoch_delta.seconds) * 1_000_000
            + epoch_delta.microseconds
        )
        return {
            "datetime": local.isoformat(),
            "date": local.date().isoformat(),
            "time": local.timetz().isoformat(),
            "timezone": timezone,
            "timezone_abbreviation": local.tzname() or "",
            "utc_offset": cls._format_offset(local),
            "weekday": local.strftime("%A"),
            "weekday_iso": local.isoweekday(),
            "unix_seconds": unix_microseconds // 1_000_000,
            "unix_milliseconds": unix_microseconds // 1000,
            "utc": utc,
        }

    @staticmethod
    def _add_months(value: datetime, months: int) -> datetime:
        month_index = value.year * 12 + value.month - 1 + months
        year, zero_based_month = divmod(month_index, 12)
        month = zero_based_month + 1
        day = min(value.day, calendar.monthrange(year, month)[1])
        return value.replace(year=year, month=month, day=day)

    def _add(
        self,
        value: datetime,
        *,
        years: int,
        months: int,
        weeks: int,
        days: int,
        hours: float,
        minutes: float,
        seconds: float,
    ) -> datetime:
        result = self._add_months(value, years * 12 + months)
        result += timedelta(weeks=weeks, days=days)
        elapsed = timedelta(hours=hours, minutes=minutes, seconds=seconds)
        if elapsed:
            result = (result.astimezone(UTC) + elapsed).astimezone(result.tzinfo)
        return result

    def _range(self, value: datetime, unit: str, week_start: str) -> tuple[datetime, datetime]:
        local = value.replace(hour=0, minute=0, second=0, microsecond=0)
        if unit == "day":
            start = local
            end = local + timedelta(days=1)
        elif unit == "week":
            weekday = local.weekday()
            offset = weekday if week_start == "monday" else (weekday + 1) % 7
            start = local - timedelta(days=offset)
            end = start + timedelta(days=7)
        elif unit == "month":
            start = local.replace(day=1)
            end = self._add_months(start, 1)
        elif unit == "quarter":
            quarter_month = ((local.month - 1) // 3) * 3 + 1
            start = local.replace(month=quarter_month, day=1)
            end = self._add_months(start, 3)
        else:
            start = local.replace(month=1, day=1)
            end = start.replace(year=start.year + 1)
        return start, end

    async def execute(
        self,
        action: str,
        timezone: str | None = None,
        source_timezone: str | None = None,
        time: str | None = None,
        end_time: str | None = None,
        years: int = 0,
        months: int = 0,
        weeks: int = 0,
        days: int = 0,
        hours: float = 0,
        minutes: float = 0,
        seconds: float = 0,
        unit: str = "day",
        week_start: str = "monday",
        **_kwargs: Any,
    ) -> str:
        target_name = timezone or self._default_timezone
        source_name = source_timezone or self._default_timezone
        try:
            self._zone(target_name)
            if action == "now":
                payload = self._describe(self._now(), target_name)
            elif action == "convert":
                if not time:
                    return ToolResult.error("Error: action=convert requires time")
                payload = self._describe(self._parse(time, source_name), target_name)
            elif action == "add":
                if not time:
                    return ToolResult.error("Error: action=add requires time")
                original = self._parse(time, source_name).astimezone(self._zone(target_name))
                result = self._add(
                    original,
                    years=years,
                    months=months,
                    weeks=weeks,
                    days=days,
                    hours=hours,
                    minutes=minutes,
                    seconds=seconds,
                )
                payload = {
                    "original": self._describe(original, target_name),
                    "result": self._describe(result, target_name),
                }
            elif action == "difference":
                if not time or not end_time:
                    return ToolResult.error(
                        "Error: action=difference requires time and end_time"
                    )
                start = self._parse(time, source_name)
                end = self._parse(end_time, source_name)
                elapsed = end.astimezone(UTC) - start.astimezone(UTC)
                elapsed_microseconds = (
                    (elapsed.days * 86400 + elapsed.seconds) * 1_000_000
                    + elapsed.microseconds
                )
                elapsed_seconds = elapsed_microseconds / 1_000_000
                payload = {
                    "start": self._describe(start, target_name),
                    "end": self._describe(end, target_name),
                    "elapsed_microseconds": elapsed_microseconds,
                    "elapsed_milliseconds": elapsed_microseconds / 1000,
                    "elapsed_seconds": elapsed_seconds,
                    "elapsed_minutes": elapsed_seconds / 60,
                    "elapsed_hours": elapsed_seconds / 3600,
                    "elapsed_days": elapsed_seconds / 86400,
                }
            elif action == "range":
                if unit not in {"day", "week", "month", "quarter", "year"}:
                    return ToolResult.error(f"Error: unsupported range unit '{unit}'")
                if week_start not in {"monday", "sunday"}:
                    return ToolResult.error(f"Error: unsupported week_start '{week_start}'")
                anchor = self._parse(time, source_name).astimezone(self._zone(target_name))
                start, end = self._range(anchor, unit, week_start)
                payload = {
                    "unit": unit,
                    "week_start": week_start if unit == "week" else None,
                    "start": self._describe(start, target_name),
                    "end_exclusive": self._describe(end, target_name),
                    "start_date": start.date().isoformat(),
                    "end_date_inclusive": (end.date() - timedelta(days=1)).isoformat(),
                }
            else:
                return ToolResult.error(f"Error: unsupported time action '{action}'")
        except (OSError, OverflowError, ValueError) as exc:
            return ToolResult.error(f"Error: {exc}")
        return json.dumps(
            {"action": action, **payload},
            ensure_ascii=False,
            separators=(",", ":"),
        )
