from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.reporting.magik_cube_intent import IntentCandidateStore
from nanobot.agent.tools.magik_cube import MagikCubeDailyReportTool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest


def _make_loop(tmp_path: Path) -> tuple[AgentLoop, MagicMock]:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    provider.generation.max_tokens = 4096
    provider.generation.temperature = 0.7
    provider.generation.reasoning_effort = None
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="normal agent", tool_calls=[])
    )
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=128_000,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop, provider


@pytest.mark.asyncio
async def test_attached_slug_fixed_route_is_one_tool_and_zero_llm(tmp_path: Path) -> None:
    loop, provider = _make_loop(tmp_path)
    tool = MagikCubeDailyReportTool(snapshot_path=tmp_path / "proxy.json")
    tool.execute = AsyncMock(return_value="weekly matrix")  # type: ignore[method-assign]
    loop.tools.register(tool)

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="user",
            chat_id="chat",
            content="tencent_token_hub所有模型的周报",
        )
    )

    assert response is not None and response.content == "weekly matrix"
    tool.execute.assert_awaited_once()  # type: ignore[attr-defined]
    params = tool.execute.await_args.kwargs  # type: ignore[attr-defined]
    assert params["tenant_query"] == "tencent_token_hub"
    assert params["report_selections"][0]["model_scope"] == "all"
    assert "interactive" not in params
    provider.chat.assert_not_called()
    provider.chat_with_retry.assert_not_awaited()
    await loop.close_mcp()

@pytest.mark.asyncio
async def test_unseen_phrase_uses_one_classifier_then_direct_tool(tmp_path: Path) -> None:
    loop, provider = _make_loop(tmp_path)
    provider.chat = AsyncMock(
        return_value=LLMResponse(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[
                ToolCallRequest(
                    id="intent-1",
                    name="emit_magik_report_intent",
                    arguments={
                        "report_kind": "range",
                        "tenant_text": "tencent_token_hub",
                        "model_scope": "all",
                        "models": [],
                        "template": "matrix",
                        "explicit_start": "2026-08-01",
                        "explicit_end": "2026-08-07",
                    },
                )
            ],
        )
    )
    tool = MagikCubeDailyReportTool(snapshot_path=tmp_path / "proxy.json")
    tool._intent_candidates = IntentCandidateStore(tmp_path / "candidates.jsonl")
    tool.execute = AsyncMock(return_value="range matrix")  # type: ignore[method-assign]
    loop.tools.register(tool)

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="user",
            chat_id="chat",
            content="请做 tencent_token_hub 2026-08-01 到 2026-08-07 的模型趋势",
        )
    )

    assert response is not None and response.content == "range matrix"
    provider.chat.assert_awaited_once()
    provider.chat_with_retry.assert_not_awaited()
    tool.execute.assert_awaited_once()  # type: ignore[attr-defined]
    params = tool.execute.await_args.kwargs  # type: ignore[attr-defined]
    assert params["start_date"] == "2026-08-01"
    assert params["end_date"] == "2026-08-07"
    assert params["report_selections"][0]["model_scope"] == "all"
    candidates = tool._intent_candidates.list()
    assert len(candidates) == 1
    assert candidates[0]["raw_text"].startswith("请做 tencent_token_hub")
    assert "sender" not in candidates[0] and "chat" not in candidates[0]
    await loop.close_mcp()


@pytest.mark.asyncio
async def test_invalid_classifier_output_degrades_without_second_llm(tmp_path: Path) -> None:
    loop, provider = _make_loop(tmp_path)
    provider.chat = AsyncMock(return_value=LLMResponse(content="not-json"))
    tool = MagikCubeDailyReportTool(snapshot_path=tmp_path / "proxy.json")
    tool.execute = AsyncMock(return_value="selection card")  # type: ignore[method-assign]
    loop.tools.register(tool)

    response = await loop._process_message(
        InboundMessage(
            channel="feishu",
            sender_id="user",
            chat_id="chat",
            content="请做 2026-08-01 到 2026-08-07 的模型趋势",
        )
    )

    assert response is not None and response.content == "selection card"
    provider.chat.assert_awaited_once()
    provider.chat_with_retry.assert_not_awaited()
    params = tool.execute.await_args.kwargs  # type: ignore[attr-defined]
    assert params["interactive"] is True
    assert params["start_date"] == "2026-08-01"
    assert params["end_date"] == "2026-08-07"
    await loop.close_mcp()
