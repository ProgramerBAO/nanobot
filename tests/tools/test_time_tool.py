from __future__ import annotations

import json
from datetime import UTC, datetime

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.time import TimeTool


def _clock() -> datetime:
    return datetime(2026, 8, 28, 3, 4, 5, 678000, tzinfo=UTC)


async def test_now_uses_agent_timezone_and_returns_machine_formats() -> None:
    tool = TimeTool("Asia/Shanghai", clock=_clock)

    payload = json.loads(await tool.execute(action="now"))

    assert payload["datetime"] == "2026-08-28T11:04:05.678000+08:00"
    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["utc"] == "2026-08-28T03:04:05.678000Z"
    assert payload["utc_offset"] == "+08:00"
    assert payload["weekday"] == "Friday"
    assert payload["unix_milliseconds"] == 1_787_886_245_678


async def test_convert_handles_naive_source_timezone_and_unix_milliseconds() -> None:
    tool = TimeTool("UTC", clock=_clock)

    converted = json.loads(
        await tool.execute(
            action="convert",
            time="2026-03-08T01:30:00",
            source_timezone="America/New_York",
            timezone="Asia/Shanghai",
        )
    )
    from_timestamp = json.loads(
        await tool.execute(
            action="convert",
            time=str(converted["unix_milliseconds"]),
            timezone="America/New_York",
        )
    )

    assert converted["datetime"] == "2026-03-08T14:30:00+08:00"
    assert from_timestamp["datetime"] == "2026-03-08T01:30:00-05:00"


async def test_add_clamps_calendar_month_and_uses_elapsed_hours_across_dst() -> None:
    tool = TimeTool("America/New_York", clock=_clock)

    clamped = json.loads(
        await tool.execute(action="add", time="2024-01-31T09:00:00", months=1)
    )
    dst = json.loads(
        await tool.execute(action="add", time="2026-03-08T01:30:00", hours=1)
    )

    assert clamped["result"]["datetime"] == "2024-02-29T09:00:00-05:00"
    assert dst["result"]["datetime"] == "2026-03-08T03:30:00-04:00"


async def test_difference_is_elapsed_time_across_dst() -> None:
    tool = TimeTool("America/New_York", clock=_clock)

    payload = json.loads(
        await tool.execute(
            action="difference",
            time="2026-03-08T00:00:00",
            end_time="2026-03-09T00:00:00",
        )
    )

    assert payload["elapsed_hours"] == 23
    assert payload["elapsed_days"] == 23 / 24


async def test_range_resolves_week_and_quarter_boundaries() -> None:
    tool = TimeTool("Asia/Shanghai", clock=_clock)

    week = json.loads(
        await tool.execute(action="range", time="2026-08-28T12:00:00", unit="week")
    )
    quarter = json.loads(
        await tool.execute(action="range", time="2026-08-28", unit="quarter")
    )

    assert week["start_date"] == "2026-08-24"
    assert week["end_date_inclusive"] == "2026-08-30"
    assert quarter["start_date"] == "2026-07-01"
    assert quarter["end_date_inclusive"] == "2026-09-30"


async def test_relative_dates_and_invalid_inputs_return_stable_results() -> None:
    tool = TimeTool("Asia/Shanghai", clock=_clock)

    yesterday = json.loads(
        await tool.execute(action="convert", time="昨天", timezone="Asia/Shanghai")
    )
    bad_zone = await tool.execute(action="now", timezone="Mars/Olympus")
    missing = await tool.execute(action="difference", time="2026-01-01")

    assert yesterday["datetime"] == "2026-08-27T00:00:00+08:00"
    assert bad_zone.is_error
    assert str(bad_zone) == "Error: unknown timezone 'Mars/Olympus'"
    assert missing.is_error
    assert str(missing) == "Error: action=difference requires time and end_time"


async def test_naive_dst_gap_and_overlap_require_explicit_offset() -> None:
    tool = TimeTool("America/New_York", clock=_clock)

    gap = await tool.execute(action="convert", time="2026-03-08T02:30:00")
    overlap = await tool.execute(action="convert", time="2026-11-01T01:30:00")
    explicit = json.loads(
        await tool.execute(action="convert", time="2026-11-01T01:30:00-05:00")
    )

    assert gap.is_error
    assert "nonexistent local time" in str(gap)
    assert overlap.is_error
    assert "ambiguous local time" in str(overlap)
    assert explicit["utc"] == "2026-11-01T06:30:00Z"


def test_create_inherits_tool_context_timezone() -> None:
    tool = TimeTool.create(ToolContext(config=None, workspace="/tmp", timezone="Asia/Shanghai"))

    assert isinstance(tool, TimeTool)
    assert tool._default_timezone == "Asia/Shanghai"
    assert tool.read_only
    assert tool._scopes == {"core", "subagent"}
