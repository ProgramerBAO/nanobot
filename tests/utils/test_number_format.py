"""Boundary pins for the shared K/M/B quantity ladder (2026-09-17)."""

from __future__ import annotations

import pytest

from nanobot.utils.number_format import format_quantity_compact


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (500, "500"),
        (999, "999"),
        (1000, "1K"),
        (1500, "1.5K"),
        (1234, "1.23K"),
        (999_500, "999.5K"),
        (1_000_000, "1M"),
        # 6683 万 = 66.83M — the previous 万/亿 wording mapped onto the ladder.
        (66_830_000, "66.83M"),
        # 1 亿 = 100M (stays M — the B tier starts at 10 亿).
        (100_000_000, "100M"),
        # 110.92 亿 = 11.092B -> two decimals.
        (11_092_000_000, "11.09B"),
        # 713.01 亿 = 71.301B -> trailing zero trimmed.
        (71_301_000_000, "71.3B"),
        # 万亿 stays B-scaled (no T tier by design).
        (1_000_000_000_000, "1000B"),
        # Signs are preserved.
        (-1500, "-1.5K"),
        (-66_830_000, "-66.83M"),
        # Sub-thousand floats keep the historical integer rounding.
        (900.0, "900"),
    ],
)
def test_format_quantity_compact(value: float, expected: str) -> None:
    assert format_quantity_compact(value) == expected
