"""Routing regression for scheduled hourly TPM subscriptions.

The 2026-09-16 live incident: an all-model hourly subscription (created by
quoting an hourly TPM card) was misrouted at run time to the legacy magik
fallback, which has no hourly concept and delivered a daily brief of all
models instead of the hourly TPM report. Root cause: the compileability
probe in ``_run_subscription`` calls the compiler without the per-run
tenant_models discovery result, so all-model hourly subscriptions looked
non-compilable.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import nanobot.agent.tools.report_center as report_center_module
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.magik_cube import MagikCubeToolConfig
from nanobot.agent.tools.report_center import ReportCenterTool, ReportCenterToolConfig
from nanobot.cron.session_turns import CRON_TRIGGER_META
from nanobot.reporting.store import ReportStateStore, ReportSubscription


class _FakeCron:
    def __init__(self) -> None:
        self.jobs: list[Any] = []

    def add_job(self, **kwargs: Any):
        job = SimpleNamespace(id=f"job-{len(self.jobs) + 1}", **kwargs)
        self.jobs.append(job)
        return job

    def remove_job(self, job_id: str):
        return "removed"

    def enable_job(self, job_id: str, enabled: bool):
        return SimpleNamespace(id=job_id, enabled=enabled)


def _hourly_all_model_subscription() -> ReportSubscription:
    now = "2026-09-16T12:00:00+00:00"
    return ReportSubscription(
        subscription_id="sub-hourly-all",
        channel="feishu",
        chat_id="chat-a",
        user_id="ou_a",
        connector_id="magik_cube",
        template_id="usage_customer_model_hourly_tpm",
        template_version="2.0",
        schedule="5 * * * *",
        timezone="Asia/Shanghai",
        report_params={
            "report_family": "usage",
            "report_template": "brief",
            "report_variant": "customer_model_hourly_tpm",
            "report_template_id": "usage_customer_model_hourly_tpm",
            "subscription_period": "recent1h",
            "tenant_scope": "selected",
            "tenants": ["tenant-a", "tenant-b"],
            "model_scope": "all",
            "models": [],
            "report_selections": [
                {"tenant_query": "tenant-a", "model_scope": "all", "models": []},
                {"tenant_query": "tenant-b", "model_scope": "all", "models": []},
            ],
        },
        cron_job_id="job-hourly-all",
        enabled=True,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_all_model_hourly_subscription_routes_to_cube_runner(
    monkeypatch, tmp_path
) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    monkeypatch.setattr(report_center_module, "get_report_state_store", lambda **_kwargs: store)
    magik = AsyncMock()
    # The legacy fallback renders a daily brief of all models — exactly the
    # wrong artifact the live incident delivered.
    magik.execute.return_value = ToolResult("legacy-daily-brief")
    tool = ReportCenterTool(
        ReportCenterToolConfig(),
        _FakeCron(),
        magik,
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example.internal",
            access_token="",
        ),
    )
    assert store.add_subscription(_hourly_all_model_subscription(), "fp-hourly-all")

    cube_result = ToolResult("hourly-tpm-document")
    cube_run = AsyncMock(return_value=cube_result)
    monkeypatch.setattr(tool, "_run_cube_subscription", cube_run)

    cron_context = RequestContext(
        channel="feishu",
        chat_id="chat-a",
        sender_id="cron",
        session_key="feishu:chat-a",
        metadata={
            CRON_TRIGGER_META: {
                "run_id": "run-hourly-all",
                "scheduled_at_ms": 1_787_680_800_000,
            }
        },
    )
    with request_context(cron_context):
        result = await tool.execute(
            action="run_subscription", subscription_id="sub-hourly-all"
        )

    assert result is cube_result
    cube_run.assert_awaited_once()
    # The legacy magik path (daily brief) must not have run.
    magik.execute.assert_not_awaited()
