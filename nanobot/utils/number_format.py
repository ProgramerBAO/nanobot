"""Shared K/M/B compact quantity formatting for report values (2026-09-17).

Single source for both the agent-layer magik tool and the reporting
package: ``utils`` is a dependency-free leaf, so magik_cube (which the
reporting package imports during connector construction) can use it without
an import cycle, and the reporting templates import it directly.

Ladder (user-confirmed 2026-09-17): ``abs >= 1e9`` -> ``B`` (10 亿),
``>= 1e6`` -> ``M``, ``>= 1e3`` -> ``K``, each with at most two decimals and
trailing zeros trimmed; smaller magnitudes render as plain integers.
Examples: 1000 -> ``1K``, 1500 -> ``1.5K``, 6683 万 -> ``66.83M``,
1 亿 -> ``100M``, 110.92 亿 -> ``11.09B``. Count-style values (machines,
samples), percentages, latencies, and money intentionally stay on their own
formatters — this helper is for token/request/TPM-style quantities only.
"""

from __future__ import annotations

_TIERS: tuple[tuple[float, str], ...] = (
    (1_000_000_000.0, "B"),
    (1_000_000.0, "M"),
    (1_000.0, "K"),
)


def format_quantity_compact(value: int | float) -> str:
    """Render one quantity with the K/M/B ladder and trimmed precision.

    ``None`` is deliberately not handled here: every call site keeps its own
    missing-data wording (暂无数据 / 暂不可用), so the caller checks for
    ``None`` first. Signs are preserved (``-1500`` -> ``-1.5K``).
    """

    for size, suffix in _TIERS:
        if abs(value) >= size:
            text = f"{value / size:.2f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{value:,.0f}"
