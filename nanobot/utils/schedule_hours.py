"""Shared validation for explicit hourly-broadcast hour lists (2026-09-16).

This module is deliberately dependency-free (stdlib ``re`` only) so both
``nanobot.reporting.schedules`` and ``nanobot.agent.reporting`` can import
it without re-entering the reporting package — importing the reporting
package from the agent layer creates an import cycle through the connector
construction chain.
"""

from __future__ import annotations

import re

# An explicit hour-list hour field: two or more comma-separated 0-23 values.
# A single listed hour is intentionally excluded: "5 9 * * *" is shape-wise
# indistinguishable from a daily 09:05 schedule and only callers with
# template context may resolve it as an hourly broadcast hour.
HOUR_LIST_RE = re.compile(r"^(?:[0-9]|1\d|2[0-3])(?:,(?:[0-9]|1\d|2[0-3]))+$")


def normalize_subscription_hours(hours: object) -> tuple[int, ...] | None:
    """Validate and normalize an explicit hourly-broadcast hour list.

    ``None`` (or an empty list) means "every hour" — the pre-existing
    default. Explicit lists accept 0-23 integers (or a delimited string
    from chat/UI forms), are deduplicated and sorted, and are capped at 24
    entries. Raises ``ValueError`` for non-integer or out-of-range input so
    an invalid form fails at the boundary instead of compiling a wrong
    cron expression.
    """

    if hours is None:
        return None
    if isinstance(hours, str):
        parts = [part for part in re.split(r"[,\s，、]+", hours) if part]
        try:
            hours = [int(part, 10) for part in parts]
        except ValueError:
            raise ValueError("hours must be integers between 0 and 23") from None
    if not isinstance(hours, (list, tuple, set)):
        raise ValueError("hours must be a list of clock hours between 0 and 23")
    values: list[int] = []
    for item in hours:
        # bool is an int subclass; a stray true/false must not become 1/0.
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError("hours must be integers between 0 and 23")
        if not 0 <= item <= 23:
            raise ValueError("hours must be integers between 0 and 23")
        values.append(item)
    if not values:
        return None
    normalized = tuple(sorted(set(values)))
    if len(normalized) > 24:
        raise ValueError("hours must list at most 24 clock hours")
    return normalized


def parse_subscription_hours(schedule: str) -> tuple[int, ...] | None:
    """Extract an explicit hour list from a compiled cron expression.

    Returns the sorted hour tuple when the hour field is a comma list of
    at least two valid clock hours, and ``None`` for every-hour (``*``),
    single-hour, or non-list shapes. The minute field is not inspected:
    the hourly path always compiles minute 5 and callers gate on their own
    cadence.
    """

    fields = schedule.split()
    if len(fields) != 5:
        return None
    hour_field = fields[1]
    if not HOUR_LIST_RE.fullmatch(hour_field):
        return None
    return tuple(int(value) for value in hour_field.split(","))
