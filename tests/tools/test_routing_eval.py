"""Routing evaluation corpus suite (report-chain LLM overhaul Phase 0, 2026-10-09).

Loads ``tests/fixtures/routing_eval.json`` and drives it through the
production deterministic chain in order — ``ReportCenterTool.match_direct_request``
first, then the legacy ``MagikCubeDailyReportTool`` matcher (the registry tier
both tools participate in) — so the corpus doubles as:

- a **zero-regression gate** for canonical phrases and negatives (hard
  assertions, must pass now and forever);
- a **pre-Phase-2 baseline** for paraphrases (``xfail(strict=False)``):
  natural rewordings mostly miss every regex today, and each case flips to
  XPASS→PASS when the unified intent router (Phase 2) lands;
- a measurable **error-rate baseline**: ``test_routing_eval_baseline_report``
  prints, per group, how many paraphrases at least get claimed by some
  deterministic tier versus falling through to the unstructured LLM turn
  (the fabrication-risk tier documented in WORK_CONTEXT 2026-09-15).

Subscription-creation phrasings are intentionally out of scope: they route
through the subscription classifier chain with their own corpus in
``test_cube_subscription_intent.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import nanobot.agent.tools.report_center as report_center_module
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.magik_cube import MagikCubeDailyReportTool
from nanobot.agent.tools.report_center import ReportCenterTool, ReportCenterToolConfig
from nanobot.reporting.store import ReportStateStore

_CORPUS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "routing_eval.json"
_CORPUS: dict[str, Any] = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
_CASES: list[dict[str, Any]] = _CORPUS["cases"]


class _RoutingChain:
    """The two deterministic matchers in production order, isolated per test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock

        store = ReportStateStore(tmp_path / "state.db")
        monkeypatch.setattr(
            report_center_module, "get_report_state_store", lambda **_kwargs: store
        )
        magik = MagicMock()
        magik.execute = AsyncMock(return_value=ToolResult("ok"))
        self.report_center = ReportCenterTool(
            ReportCenterToolConfig(), MagicMock(), magik
        )
        self.legacy = MagikCubeDailyReportTool(snapshot_path=tmp_path / "proxy.json")

    def route(self, phrase: str) -> dict[str, Any] | None:
        """Return the winning deterministic params, production order."""
        result = self.report_center.match_direct_request(phrase)
        if result is not None:
            return result
        return self.legacy.match_direct_request(phrase)

    def routed_at_all(self, phrase: str) -> bool:
        """Whether any deterministic tier claims the phrase (vs agent turn)."""
        return (
            self.report_center.match_direct_request(phrase) is not None
            or self.legacy.match_direct_request(phrase) is not None
        )


def _routing_cases(tier: str) -> list[dict[str, Any]]:
    return [case for case in _CASES if case["tier"] == tier]


def _case_id(case: dict[str, Any]) -> str:
    return f"{case['group']}-{case['phrase'][:24]}"


def _assert_expected(chain: _RoutingChain, case: dict[str, Any]) -> None:
    expect = case["expect"]
    if expect is None:
        assert not chain.routed_at_all(case["phrase"]), (
            f"{case['phrase']!r} must not be claimed by any deterministic matcher"
        )
        return
    if case.get("route") == "legacy":
        result = chain.legacy.match_direct_request(case["phrase"])
        tier = "legacy"
    else:
        result = chain.route(case["phrase"])
        tier = "report_center->legacy"
    assert result is not None, f"{case['phrase']!r} did not route ({tier})"
    for key, value in expect.items():
        assert result.get(key) == value, (
            f"{case['phrase']!r}: expected {key}={value!r}, got {result.get(key)!r} "
            f"(tier {tier}, full result: {result})"
        )


@pytest.mark.routing_eval
@pytest.mark.parametrize(
    "case",
    _routing_cases("canonical"),
    ids=_case_id,
)
def test_canonical_phrases_route_deterministically(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chain = _RoutingChain(monkeypatch, tmp_path)
    _assert_expected(chain, case)


@pytest.mark.routing_eval
@pytest.mark.parametrize(
    "case",
    _routing_cases("negative"),
    ids=_case_id,
)
def test_negative_phrases_are_not_claimed(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chain = _RoutingChain(monkeypatch, tmp_path)
    _assert_expected(chain, case)


@pytest.mark.routing_eval
@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            case,
            marks=pytest.mark.xfail(
                strict=False,
                reason="paraphrase baseline: routes after the Phase 2 unified intent router",
            ),
        )
        for case in _routing_cases("paraphrase")
    ],
    ids=_case_id,
)
def test_paraphrase_phrases_route(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    chain = _RoutingChain(monkeypatch, tmp_path)
    _assert_expected(chain, case)


@pytest.mark.routing_eval
def test_routing_eval_baseline_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Measure the deterministic chain against the paraphrase corpus.

    Prints two numbers per group: ``routed`` (some deterministic tier claims
    the phrase — no fabrication risk, possibly a wrong window) and ``freestyle``
    (both matchers return None — the message reaches the unstructured LLM
    turn). The only hard assertion is corpus well-formedness so the report
    cannot silently rot; the numbers themselves are the Phase 0 baseline.
    """

    chain = _RoutingChain(monkeypatch, tmp_path)
    groups: dict[str, dict[str, int]] = {}
    for case in _routing_cases("paraphrase"):
        stats = groups.setdefault(case["group"], {"routed": 0, "freestyle": 0})
        key = "routed" if chain.routed_at_all(case["phrase"]) else "freestyle"
        stats[key] += 1

    lines = ["routing eval baseline (deterministic chain vs paraphrase corpus):"]
    total_routed = total_freestyle = 0
    for group in sorted(groups):
        stats = groups[group]
        total_routed += stats["routed"]
        total_freestyle += stats["freestyle"]
        size = stats["routed"] + stats["freestyle"]
        lines.append(f"  {group}: routed {stats['routed']}/{size}")
    total = total_routed + total_freestyle
    rate = (100.0 * total_routed / total) if total else 0.0
    lines.append(
        f"  TOTAL: routed {total_routed}/{total} ({rate:.1f}%), "
        f"freestyle-fallthrough {total_freestyle}"
    )
    print("\n".join(lines))

    canonical_groups = {case["group"] for case in _routing_cases("canonical")}
    assert canonical_groups <= set(groups), (
        "every action group with paraphrases must also pin its canonical form"
    )


def test_corpus_shape_is_well_formed() -> None:
    assert _CORPUS["version"] == 1
    assert _CASES, "corpus must not be empty"
    tiers = {case["tier"] for case in _CASES}
    assert tiers == {"canonical", "paraphrase", "negative"}
    routes = {case.get("route", "report_center") for case in _CASES}
    assert routes <= {"report_center", "legacy"}
    for case in _CASES:
        assert case["phrase"].strip(), "corpus phrases must be non-empty"
        assert "group" in case and "tier" in case
        if case["tier"] != "negative":
            assert isinstance(case["expect"], dict) and case["expect"], (
                f"non-negative cases must carry expectations: {case['phrase']!r}"
            )


@pytest.mark.asyncio
async def test_report_center_runtime_context_carries_fabrication_guardrails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Phase 1: report-ish LLM agent turns must see numeric-integrity rules.

    The 2026-09-15 fabrication incident class came from report questions that
    fell through every regex and reached the unstructured agent turn, whose
    only guards were two generic tool-contract lines. The runtime context
    block is the report-specific guard for exactly that tier, and it is
    signal-gated: chit-chat turns skip it (no prompt cost, no history bloat).
    """

    from nanobot.agent.tools.context import RequestContext
    from nanobot.runtime_context import resolve_runtime_context

    chain = _RoutingChain(monkeypatch, tmp_path)
    provider = chain.report_center.runtime_context_provider()
    assert provider is not None

    reportish = RequestContext(
        channel="feishu",
        chat_id="chat-1",
        original_user_text="过去一小时佛跳墙全部模型的吞吐量怎么样",
    )
    blocks = await resolve_runtime_context([provider], reportish)

    assert len(blocks) == 1
    block = blocks[0]
    assert block.source == "report_center"
    # The guardrails that close the fabrication class, pinned so they cannot
    # silently rot: numbers only via tools, no estimation, pass-through names.
    assert "never estimate, calculate, or invent" in block.content
    assert "report_center" in block.content
    assert "re-resolves them against the live catalog" in block.content

    chitchat = RequestContext(
        channel="feishu",
        chat_id="chat-1",
        original_user_text="你好，今天心情不错",
    )
    assert await resolve_runtime_context([provider], chitchat) == []


def test_report_center_tool_description_pins_numeric_integrity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Phase 1b: the tool schema carries the same anti-fabrication contract."""

    chain = _RoutingChain(monkeypatch, tmp_path)
    description = chain.report_center.description
    assert "without recalculating or estimating any numeric value" in description
    assert "never answer such questions from memory" in description
