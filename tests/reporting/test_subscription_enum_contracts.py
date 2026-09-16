"""Contract tests pinning the subscription enum single sources together.

The recurrence/report-type values are defined once in
``nanobot.reporting.schedules`` and derived everywhere else (the NLU Literal
surfaces, the report_center tool schema, the guided service validation, the
preview whitelists). These tests fail when a new value lands on one surface
but not the others — the exact drift mode behind the 2026-09-15
``recurrence=hourly`` schema-rejection incident.
"""

from __future__ import annotations

from typing import get_args

from nanobot.agent.reporting import cube_subscription_intent as intent
from nanobot.agent.tools import report_center as report_center_module
from nanobot.reporting.schedules import (
    BRIEF_PERIOD_TEMPLATES,
    DAILY_MODES,
    PERIOD_TEMPLATES,
    RECURRENCE_SCHEDULE_PERIODS,
    SUBSCRIPTION_RECURRENCES,
    SUBSCRIPTION_REPORT_TYPE_ENUM,
    SUBSCRIPTION_REPORT_TYPE_TABLE,
)


def _schema_enum(name: str) -> list[str]:
    properties = report_center_module._REPORT_CENTER_PARAMETERS["properties"]
    return list(properties[name]["enum"])


def test_recurrence_single_sources_are_pinned_equal() -> None:
    assert get_args(intent.SubscriptionRecurrence) == SUBSCRIPTION_RECURRENCES
    assert intent._VALID_RECURRENCES == frozenset(SUBSCRIPTION_RECURRENCES)
    assert _schema_enum("recurrence") == list(SUBSCRIPTION_RECURRENCES)
    # Every recurrence must map to a schedule period, and the daily modes
    # are a subset of the recurrence surface.
    assert set(RECURRENCE_SCHEDULE_PERIODS) == set(SUBSCRIPTION_RECURRENCES)
    assert set(DAILY_MODES) <= set(SUBSCRIPTION_RECURRENCES)


def test_report_type_single_sources_are_pinned_equal() -> None:
    assert _schema_enum("report_type") == list(SUBSCRIPTION_REPORT_TYPE_ENUM)
    # The tool surface is exactly the concrete subscribable table plus the
    # quoted-report "inherit" marker.
    assert SUBSCRIPTION_REPORT_TYPE_ENUM == (
        *SUBSCRIPTION_REPORT_TYPE_TABLE,
        "inherit",
    )
    # The classifier surface is a strict subset: it intentionally excludes
    # the weekly multi-scope brief, which the deterministic compiler derives
    # from period + scope instead of trusting the model to name it.
    assert intent._VALID_REPORT_TYPES < frozenset(SUBSCRIPTION_REPORT_TYPE_ENUM)
    assert "usage_customer_model_weekly_brief" not in intent._VALID_REPORT_TYPES


def test_period_template_maps_cover_the_same_periods() -> None:
    assert set(PERIOD_TEMPLATES) == set(BRIEF_PERIOD_TEMPLATES)
    # Every concrete subscription report type carries the data period its
    # template family maps back to.
    for report_type, (data_period, _variant) in SUBSCRIPTION_REPORT_TYPE_TABLE.items():
        assert data_period in PERIOD_TEMPLATES or data_period == "recent1h"
