"""Read-only live-model evaluation: classification only, no report/job execution.

Uses the Gateway provider configuration without printing credentials. This
is an explicit external-model smoke check, never part of deterministic CI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import date
from pathlib import Path

from nanobot.agent.reporting.intent_router import classify_report_intent, is_report_candidate
from nanobot.config.loader import load_config, resolve_config_env_vars
from nanobot.providers.factory import build_provider_snapshot
from nanobot.utils.llm_runtime import runtime_from_provider_snapshot


async def evaluate(limit: int, today: date, timeout_seconds: float, max_tokens: int) -> int:
    """Evaluate at most 30 paraphrases with one bounded classification each."""
    config = resolve_config_env_vars(load_config())
    snapshot = build_provider_snapshot(config)
    runtime = runtime_from_provider_snapshot(snapshot)
    print(
        f"model={runtime.model} provider={type(runtime.provider).__name__} timeout={timeout_seconds}s max_tokens={max_tokens}",
        flush=True,
    )
    corpus = json.loads(
        (Path(__file__).resolve().parents[1] / "tests/fixtures/routing_eval.json").read_text(
            encoding="utf-8"
        )
    )
    cases = [case for case in corpus["cases"] if case["tier"] == "paraphrase"][:limit]
    matched = 0
    for case in cases:
        started = time.perf_counter()
        draft = await classify_report_intent(
            case["phrase"], runtime, timeout_seconds=timeout_seconds, max_tokens=max_tokens
        )
        params = draft.compile(case["phrase"], today=today) if draft else None
        expected = case["expect"]
        ok = params is not None and all(params.get(key) == value for key, value in expected.items())
        matched += int(ok)
        print(
            json.dumps(
                {
                    "phrase": case["phrase"],
                    "candidate": is_report_candidate(case["phrase"]),
                    "match": ok,
                    "action": params.get("action") if params else None,
                    "expected": expected,
                    "compiled": params,
                    "draft": draft.model_dump(exclude_defaults=True) if draft else None,
                    "seconds": round(time.perf_counter() - started, 2),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    print(f"live_model_matches={matched}/{len(cases)}")
    return 0 if matched == len(cases) and cases else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=21, choices=range(1, 31))
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=3,
        choices=range(1, 181),
        help="Evaluation-only finite per-case deadline (does not change Gateway settings)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=384,
        choices=range(128, 8193),
        help="Evaluation-only generation budget, including model reasoning tokens",
    )
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        required=True,
        help="Explicit client date YYYY-MM-DD for reproducible window compilation",
    )
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(evaluate(args.limit, args.today, args.timeout_seconds, args.max_tokens))
    )
