from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nanobot.agent.reporting.magik_cube_intent import (
    IntentCandidateStore,
    ReportIntent,
    match_promoted_rule,
    promote_candidate,
)
from nanobot.agent.tools.magik_cube import (
    MagikCubeDailyReportTool,
    MagikCubeReporter,
    MagikCubeToolConfig,
    _plan_comparison_windows,
)
from nanobot.bus.events import OUTBOUND_META_AGENT_UI


def test_attached_slug_and_fixed_daily_monthly_templates_route_without_interaction(
    tmp_path: Path,
) -> None:
    tool = MagikCubeDailyReportTool(snapshot_path=tmp_path / "proxy.json")

    weekly = tool.match_direct_request("tencent_token_hub所有模型的周报")
    assert weekly is not None
    assert weekly["tenant_query"] == "tencent_token_hub"
    assert weekly["report_selections"] == [
        {"tenant_query": "tencent_token_hub", "model_scope": "all", "models": []}
    ]
    assert "interactive" not in weekly

    partial = tool.match_direct_request("tencent_token_hub周报")
    assert partial is not None
    assert partial["tenant_query"] == "tencent_token_hub"
    assert partial["interactive"] is True

    daily = tool.match_direct_request("tencent_token_hub所有模型的日报")
    assert daily is not None
    assert daily["start_date"] == daily["end_date"]
    assert daily["comparison"] == "previous_period"
    assert daily["granularity"] == "day"

    monthly = tool.match_direct_request("tencent_token_hub所有模型的月报")
    assert monthly is not None
    start = date.fromisoformat(monthly["start_date"])
    end = date.fromisoformat(monthly["end_date"])
    assert start.day == 1
    assert end.day >= 28
    assert monthly["comparison"] == "previous_month"
    assert monthly["granularity"] == "week"

    full_daily = tool.match_direct_request("完整日报")
    assert full_daily is not None
    assert full_daily["report_template"] == "full"
    assert "start_date" not in full_daily
    assert full_daily["report_date"]

    assert tool.match_direct_request("深度分析上周和上上周各模型用量") is None


class _ZeroFilteringClient:
    def __init__(self) -> None:
        self.tpm_models: list[str] = []

    async def request(
        self,
        _method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if path == "tenants":
            return {"list": [{"tenantId": "prod", "tenantName": "生产客户"}], "total": 1}
        if path == "inference/model-configs":
            return {
                "list": [
                    {"model": "ACTIVE"},
                    {"model": "STOPPED"},
                    {"model": "ZERO"},
                    {"model": "BROKEN"},
                ],
                "total": 4,
            }
        assert json_body is not None
        model = str(json_body.get("model") or "")
        if path == "analysis/active-tenant-daily-usage/query":
            if model == "BROKEN":
                raise RuntimeError("partial model failure")
            start = date.fromisoformat(str(json_body["startTime"])[:10])
            end = date.fromisoformat(str(json_body["endTime"])[:10])
            points: list[dict[str, Any]] = []
            cursor = start
            while cursor < end:
                current = cursor >= date(2026, 8, 8)
                if model == "ACTIVE":
                    tokens = 100 if current else 50
                elif model == "STOPPED":
                    tokens = 0 if current else 25
                elif model == "ZERO":
                    tokens = 0
                else:
                    tokens = 1_000 if current else 800
                points.append(
                    {
                        "date": cursor.isoformat(),
                        "totalTokens": tokens,
                        "requestCount": 1 if tokens else 0,
                    }
                )
                cursor += timedelta(days=1)
            return {"items": [{"tenantId": "prod", "points": points}]}
        if path == "analysis/endpoint-max-tpm/daily/query":
            self.tpm_models.append(model)
            return {
                "items": [
                    {
                        "endpoint": "summary",
                        "points": [{"date": "2026-08-14", "maxTpm": 123}],
                    }
                ]
            }
        raise AssertionError(path)


async def test_matrix_hides_only_complete_double_zero_and_skips_model_tpm(
    tmp_path: Path,
) -> None:
    client = _ZeroFilteringClient()
    reporter = MagikCubeReporter(
        client,
        MagikCubeToolConfig(
            base_url="https://cube.example",
            tenant_mappings={"测试租户": "prod"},
        ),
        tmp_path / "proxy.json",
        "Asia/Shanghai",
    )
    plan = _plan_comparison_windows(
        date(2026, 8, 8), date(2026, 8, 14), comparison="previous_period"
    )

    result = await reporter.generate_matrix_report(
        plan,
        [{"tenant_query": "测试租户", "model_scope": "all", "models": []}],
        granularity="day",
        include_tpm=True,
    )

    card = result.metadata[OUTBOUND_META_AGENT_UI]["cards"][0]
    names = [row["model"] for row in card["table"]["rows"]]
    assert names[0] == "ACTIVE"
    assert set(names) == {"ACTIVE", "STOPPED", "BROKEN"}
    assert "停用" in next(row for row in card["table"]["rows"] if row["model"] == "STOPPED")[
        "change"
    ]
    assert next(row for row in card["table"]["rows"] if row["model"] == "BROKEN")[
        "change"
    ] == "数据不完整"
    assert "已隐藏 1 个两期均为 0 的模型" in card["quality"]
    assert client.tpm_models == [""]


async def test_single_day_card_uses_daily_title_and_compact_columns(tmp_path: Path) -> None:
    reporter = MagikCubeReporter(
        _ZeroFilteringClient(),
        MagikCubeToolConfig(
            base_url="https://cube.example",
            tenant_mappings={"测试租户": "prod"},
        ),
        tmp_path / "proxy.json",
        "Asia/Shanghai",
    )
    plan = _plan_comparison_windows(
        date(2026, 8, 14), date(2026, 8, 14), comparison="previous_period"
    )

    result = await reporter.generate_matrix_report(
        plan,
        [{"tenant_query": "测试租户", "model_scope": "all", "models": []}],
        granularity="day",
        include_tpm=True,
    )

    card = result.metadata[OUTBOUND_META_AGENT_UI]["cards"][0]
    assert card["title"] == "生产客户 日报"
    assert [item["name"] for item in card["table"]["columns"]] == [
        "model",
        "total",
        "change",
    ]
    assert all(
        set(row) == {"model", "total", "change"} for row in card["table"]["rows"]
    )
    assert any("平均 Token/请求" in item for item in card["overview"])


def test_monthly_segments_use_fixed_day_buckets_across_leap_years(tmp_path: Path) -> None:
    reporter = MagikCubeReporter(
        _ZeroFilteringClient(),
        MagikCubeToolConfig(base_url="https://cube.example"),
        tmp_path / "proxy.json",
        "Asia/Shanghai",
    )
    normal = _plan_comparison_windows(
        date(2025, 3, 1), date(2025, 3, 31), comparison="previous_month"
    )
    leap = _plan_comparison_windows(
        date(2024, 3, 1), date(2024, 3, 31), comparison="previous_month"
    )

    normal_pairs = reporter._segment_pairs(normal, "week")
    leap_pairs = reporter._segment_pairs(leap, "week")
    assert [label for label, _, _ in normal_pairs] == ["W1", "W2", "W3", "W4", "W5"]
    assert normal_pairs[-1][1].start == date(2025, 3, 29)
    assert normal_pairs[-1][2] is None
    assert leap_pairs[-1][2] is not None
    assert leap_pairs[-1][2].start == date(2024, 2, 29)
    assert leap_pairs[-1][2].end == date(2024, 2, 29)


def test_candidate_store_deduplicates_promotes_and_expires(tmp_path: Path) -> None:
    candidate_path = tmp_path / "candidates.jsonl"
    rule_path = tmp_path / "rules.json"
    rule_path.write_text('{"version":1,"rules":[]}\n', encoding="utf-8")
    store = IntentCandidateStore(candidate_path, retention_days=30, max_entries=100)
    intent = ReportIntent(
        report_kind="week", tenant_text="tenant_a", model_scope="all"
    )

    candidate_id = store.record("tenant_a所有模型周报", intent, "direct")
    assert store.record("tenant_a所有模型周报", intent, "direct") == candidate_id
    assert store.list()[0]["count"] == 2

    old = {
        "id": "expired",
        "raw_text": "old",
        "pattern": "old",
        "intent": {"report_kind": "day"},
        "count": 1,
        "last_seen": (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(),
        "status": "pending",
    }
    with candidate_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(old) + "\n")
    assert all(row["id"] != "expired" for row in store.list())

    preview = promote_candidate(
        candidate_id, apply=False, store=store, rule_path=rule_path
    )
    assert preview["pattern"] == "{tenant}所有模型周报"
    assert json.loads(rule_path.read_text(encoding="utf-8"))["rules"] == []

    promote_candidate(candidate_id, apply=True, store=store, rule_path=rule_path)
    promoted = match_promoted_rule("tenant_b所有模型周报", path=rule_path)
    assert promoted is not None
    assert promoted.tenant_text == "tenant_b"
    assert promoted.model_scope == "all"
    assert store.list() == []
