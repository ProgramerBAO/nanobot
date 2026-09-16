from __future__ import annotations

import pytest

from nanobot.reporting.capabilities import subscriptions_document
from nanobot.reporting.schedules import (
    build_subscription_schedule,
    describe_subscription_schedule,
    normalize_subscription_hours,
    parse_subscription_hours,
)
from nanobot.reporting.store import ReportSubscription


@pytest.mark.parametrize(
    ("period", "kwargs", "expected_cron", "expected_text"),
    [
        (
            "day",
            {"send_time": "10:00", "daily_mode": "workdays"},
            "0 10 * * 1-5",
            "每个工作日 10:00",
        ),
        (
            "day",
            {"send_time": "09:30", "daily_mode": "every_day"},
            "30 9 * * *",
            "每天 09:30",
        ),
        (
            "week",
            {"send_time": "10:00", "weekday": 1},
            "0 10 * * 1",
            "每周一 10:00",
        ),
        (
            "month",
            {"send_time": "18:30", "month_day": 15},
            "30 18 15 * *",
            "每月 15 日 18:30",
        ),
        # Hourly TPM runs five minutes after the hour so the upstream hourly
        # aggregate of the just-completed hour is already ingested.
        (
            "recent1h",
            {"send_time": "00:00"},
            "5 * * * *",
            "每小时（整点后 5 分钟）",
        ),
        # Explicit broadcast hours (user-confirmed 2026-09-16) narrow the
        # hourly cadence; lists are deduplicated and sorted on compile.
        (
            "recent1h",
            {"send_time": "00:00", "hours": [10, 9, 10]},
            "5 9,10 * * *",
            "每天 9、10 点（整点后 5 分钟）",
        ),
        # Human-friendly separated strings from chat/UI forms are accepted.
        (
            "recent1h",
            {"send_time": "00:00", "hours": "15、9"},
            "5 9,15 * * *",
            "每天 9、15 点（整点后 5 分钟）",
        ),
    ],
)
def test_build_and_describe_subscription_schedule(
    period: str, kwargs: dict, expected_cron: str, expected_text: str
) -> None:
    schedule = build_subscription_schedule(period, **kwargs)
    assert schedule == expected_cron
    assert describe_subscription_schedule(schedule) == expected_text


@pytest.mark.parametrize(
    ("period", "kwargs"),
    [
        ("day", {"send_time": "24:00"}),
        ("week", {"send_time": "10:00", "weekday": 8}),
        ("month", {"send_time": "10:00", "month_day": 29}),
        # Broadcast hours must be 0-23 integers; bool is rejected even
        # though it subclasses int.
        ("recent1h", {"send_time": "00:00", "hours": [24]}),
        ("recent1h", {"send_time": "00:00", "hours": [-1]}),
        ("recent1h", {"send_time": "00:00", "hours": [True]}),
        ("recent1h", {"send_time": "00:00", "hours": ["9"]}),
        ("recent1h", {"send_time": "00:00", "hours": "not-hours"}),
    ],
)
def test_invalid_subscription_schedule_is_rejected(period: str, kwargs: dict) -> None:
    with pytest.raises(ValueError):
        build_subscription_schedule(period, **kwargs)


def test_empty_broadcast_hours_mean_every_hour() -> None:
    assert build_subscription_schedule("recent1h", send_time="00:00", hours=[]) == "5 * * * *"
    assert normalize_subscription_hours([]) is None
    assert normalize_subscription_hours(None) is None


def test_parse_subscription_hours_round_trip() -> None:
    assert parse_subscription_hours("5 9,10,15 * * *") == (9, 10, 15)
    # The every-hour wildcard and single-hour shapes are not hour lists.
    assert parse_subscription_hours("5 * * * *") is None
    assert parse_subscription_hours("5 9 * * *") is None
    assert parse_subscription_hours("garbage") is None
    # Round trip with the builder.
    built = build_subscription_schedule("recent1h", send_time="00:00", hours=[15, 9])
    assert parse_subscription_hours(built) == (9, 15)


def test_describe_hour_list_requires_minute_five() -> None:
    # Only the hourly path produces hour lists and it always compiles minute
    # 5; a hand-crafted cron with another minute stays opaque instead of
    # pretending to be the hourly cadence.
    assert describe_subscription_schedule("0 9,10 * * *") == "自定义定时"


def test_unknown_cron_is_not_exposed_to_users() -> None:
    assert describe_subscription_schedule("*/5 * * * *") == "自定义定时"


def test_subscriptions_document_uses_readable_schedule() -> None:
    row = ReportSubscription(
        subscription_id="sub-a",
        channel="feishu",
        chat_id="chat-a",
        user_id="ou-a",
        connector_id="magik_cube",
        template_id="usage_daily_matrix",
        template_version="1.0",
        schedule="0 10 * * 1-5",
        timezone="Asia/Shanghai",
        report_params={},
        cron_job_id="job-a",
        enabled=True,
        created_at="2026-08-26T10:00:00+08:00",
        updated_at="2026-08-26T10:00:00+08:00",
    )

    document = subscriptions_document([row])

    assert "日报" in document.fallback_text
    assert "每个工作日 10:00" in document.fallback_text
    assert "前一自然日" in document.fallback_text
    assert "0 10 * * 1-5" not in document.fallback_text


def test_subscriptions_document_pairs_each_summary_with_its_numbered_action() -> None:
    rows = [
        ReportSubscription(
            subscription_id="aaaaaaaaaaaaaaaa",
            channel="feishu",
            chat_id="chat-a",
            user_id="ou-a",
            connector_id="magik_cube",
            template_id="usage_daily_brief",
            template_version="2.0",
            schedule="0 10 * * 1-5",
            timezone="Asia/Shanghai",
            report_params={
                "tenant_query": "tenant-a",
                "model": "Kimi-K3",
            },
            cron_job_id="job-a",
            enabled=True,
            created_at="2026-09-02T10:00:00+08:00",
            updated_at="2026-09-02T10:00:00+08:00",
        ),
        ReportSubscription(
            subscription_id="bbbbbbbbbbbbbbbb",
            channel="feishu",
            chat_id="chat-a",
            user_id="ou-a",
            connector_id="magik_cube",
            template_id="usage_weekly_brief",
            template_version="2.0",
            schedule="0 9 * * 1",
            timezone="Asia/Shanghai",
            report_params={"all_tenants": True, "model_scope": "summary"},
            cron_job_id="job-b",
            enabled=False,
            created_at="2026-09-02T10:00:00+08:00",
            updated_at="2026-09-02T10:00:00+08:00",
        ),
    ]

    document = subscriptions_document(rows)

    assert [block.kind for block in document.blocks] == [
        "note",
        "markdown",
        "actions",
        "markdown",
        "actions",
    ]
    assert document.blocks[1].data["title"] == "日报简报"
    assert document.blocks[2].data["actions"][0]["label"] == "停用订阅 1"
    assert document.blocks[3].data["title"] == "周报简报"
    assert document.blocks[4].data["actions"][0]["label"] == "启用订阅 2"
    assert "tenant-a｜Kimi-K3" in document.fallback_text
    assert "全部客户｜汇总" in document.fallback_text
