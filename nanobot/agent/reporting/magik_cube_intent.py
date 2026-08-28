"""Intent routing and reviewed phrase refinement for Magik Cube reports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

import json_repair
from filelock import FileLock
from loguru import logger

from nanobot.config.paths import get_runtime_subdir
from nanobot.providers.base import parse_tool_arguments
from nanobot.utils.helpers import _write_text_atomic

if TYPE_CHECKING:
    from nanobot.utils.llm_runtime import LLMRuntime


ReportKind = Literal["day", "week", "month", "recent7", "range"]
ModelScope = Literal["summary", "all", "selected"]
ReportTemplate = Literal["matrix", "full"]

_DEEP_ANALYSIS_RE = re.compile(
    r"(?:深度分析|原因分析|原因解释|分析原因|业务建议|优化建议)"
)
_REPORT_SIGNAL_RE = re.compile(
    r"(?:用量|使用量|使用情况|消耗|趋势|报表|日报|周报|月报|token|tpm)",
    re.IGNORECASE,
)
_PERIOD_SIGNAL_RE = re.compile(
    r"(?:日报|周报|月报|昨天|昨日|今天|前天|上周|本周|上月|上个月|本月|"
    r"近\s*7\s*天|最近\s*7\s*天|一周|\d{4}-\d{2}-\d{2})"
)
_CLASSIFIER_TOOL_NAME = "emit_magik_report_intent"
_VALID_KINDS = frozenset({"day", "week", "month", "recent7", "range"})
_VALID_SCOPES = frozenset({"summary", "all", "selected"})
_VALID_TEMPLATES = frozenset({"matrix", "full"})


@dataclass(frozen=True, slots=True)
class ReportTemplateSpec:
    """Stable calculation and presentation contract for one report family."""

    template_id: str
    report_kind: ReportKind
    granularity: Literal["day", "week"]
    comparison: Literal["previous_period", "previous_month"]
    include_summary_tpm: bool = True
    include_model_tpm: bool = False


REPORT_TEMPLATE_REGISTRY: dict[ReportKind, ReportTemplateSpec] = {
    "day": ReportTemplateSpec(
        "usage_daily_matrix", "day", "day", "previous_period"
    ),
    "week": ReportTemplateSpec(
        "usage_weekly_matrix", "week", "day", "previous_period"
    ),
    "recent7": ReportTemplateSpec(
        "usage_recent7_matrix", "recent7", "day", "previous_period"
    ),
    "month": ReportTemplateSpec(
        "usage_monthly_matrix", "month", "week", "previous_month"
    ),
    "range": ReportTemplateSpec(
        "usage_range_matrix", "range", "day", "previous_period"
    ),
}


def is_deep_analysis_request(text: str) -> bool:
    return bool(_DEEP_ANALYSIS_RE.search(text))


def is_report_intent_candidate(text: str) -> bool:
    """Limit the LLM fallback to self-contained report-like requests."""

    return (
        not is_deep_analysis_request(text)
        and bool(_REPORT_SIGNAL_RE.search(text))
        and bool(_PERIOD_SIGNAL_RE.search(text))
    )


def normalize_phrase(text: str) -> str:
    """Normalize only presentation noise; retain words that carry intent."""

    return re.sub(r"[\s,，。.!！?？:：;；、]+", "", text.strip()).casefold()


@dataclass(frozen=True, slots=True)
class ReportIntent:
    """Validated report semantics; dates remain server-planned."""

    report_kind: ReportKind
    tenant_text: str = ""
    model_scope: ModelScope | None = None
    models: tuple[str, ...] = ()
    template: ReportTemplate = "matrix"
    explicit_start: str = ""
    explicit_end: str = ""

    @classmethod
    def from_payload(cls, payload: Any) -> ReportIntent | None:
        if not isinstance(payload, dict):
            return None
        kind = str(payload.get("report_kind") or "").strip().lower()
        if kind not in _VALID_KINDS:
            return None
        raw_scope = str(payload.get("model_scope") or "").strip().lower()
        scope: ModelScope | None = raw_scope if raw_scope in _VALID_SCOPES else None  # type: ignore[assignment]
        template = str(payload.get("template") or "matrix").strip().lower()
        if template not in _VALID_TEMPLATES:
            return None
        models = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in payload.get("models") or []
                if str(item).strip()
            )
        )
        if scope == "selected" and not models:
            scope = None
        explicit_start = str(payload.get("explicit_start") or "").strip()
        explicit_end = str(payload.get("explicit_end") or "").strip()
        if kind == "range":
            try:
                start = date.fromisoformat(explicit_start)
                end = date.fromisoformat(explicit_end)
            except ValueError:
                return None
            if end < start:
                return None
        return cls(
            report_kind=kind,  # type: ignore[arg-type]
            tenant_text=str(payload.get("tenant_text") or "").strip(),
            model_scope=scope,
            models=models,
            template=template,  # type: ignore[arg-type]
            explicit_start=explicit_start,
            explicit_end=explicit_end,
        )

    def to_tool_params(self, *, today: date) -> dict[str, Any]:
        """Compile semantics to the existing stable Tool interface."""

        params: dict[str, Any] = {"save_snapshot": False}
        spec = REPORT_TEMPLATE_REGISTRY[self.report_kind]
        yesterday = today - timedelta(days=1)
        if self.report_kind == "day":
            start = yesterday
            params.update(
                start_date=start.isoformat(),
                end_date=start.isoformat(),
                comparison=spec.comparison,
            )
        elif self.report_kind == "week":
            start = today - timedelta(days=today.weekday() + 7)
            params.update(
                start_date=start.isoformat(),
                end_date=(start + timedelta(days=6)).isoformat(),
                comparison=spec.comparison,
            )
        elif self.report_kind == "recent7":
            params.update(
                start_date=(yesterday - timedelta(days=6)).isoformat(),
                end_date=yesterday.isoformat(),
                comparison=spec.comparison,
            )
        elif self.report_kind == "month":
            end = today.replace(day=1) - timedelta(days=1)
            params.update(
                start_date=end.replace(day=1).isoformat(),
                end_date=end.isoformat(),
                comparison=spec.comparison,
            )
        else:
            params.update(
                start_date=self.explicit_start,
                end_date=self.explicit_end,
                comparison=spec.comparison,
            )

        if self.tenant_text:
            params["tenant_query"] = self.tenant_text
        if self.template == "full":
            params["report_template"] = "full"
            return params

        params.update(
            report_template="matrix_card",
            granularity=spec.granularity,
            include_tpm=spec.include_summary_tpm,
        )
        if self.tenant_text and self.model_scope:
            params["breakdown"] = "model" if self.model_scope != "summary" else "summary"
            params["report_selections"] = [
                {
                    "tenant_query": self.tenant_text,
                    "model_scope": self.model_scope,
                    "models": list(self.models),
                }
            ]
        else:
            params["interactive"] = True
        return params


def minimal_interactive_intent(text: str) -> ReportIntent | None:
    """Provide a deterministic card if the classifier times out or is invalid."""

    if not is_report_intent_candidate(text):
        return None
    explicit_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if len(explicit_dates) >= 2:
        return ReportIntent(
            report_kind="range",
            explicit_start=explicit_dates[0],
            explicit_end=explicit_dates[1],
        )
    if re.search(r"(?:月报|上月|上个月|本月)", text):
        kind: ReportKind = "month"
    elif re.search(r"(?:周报|上周|本周)", text):
        kind = "week"
    elif re.search(r"(?:近\s*7\s*天|最近\s*7\s*天|一周)", text):
        kind = "recent7"
    else:
        kind = "day"
    return ReportIntent(report_kind=kind)


def _classifier_schema() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": _CLASSIFIER_TOOL_NAME,
                "description": "Return only the structured Magik Cube report intent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "report_kind": {
                            "type": "string",
                            "enum": ["day", "week", "month", "recent7", "range"],
                        },
                        "tenant_text": {"type": "string"},
                        "model_scope": {
                            "type": "string",
                            "enum": ["summary", "all", "selected", "missing"],
                        },
                        "models": {"type": "array", "items": {"type": "string"}},
                        "template": {"type": "string", "enum": ["matrix", "full"]},
                        "explicit_start": {"type": "string"},
                        "explicit_end": {"type": "string"},
                    },
                    "required": [
                        "report_kind",
                        "tenant_text",
                        "model_scope",
                        "models",
                        "template",
                        "explicit_start",
                        "explicit_end",
                    ],
                    "additionalProperties": False,
                },
            },
        }
    ]


async def classify_report_intent(
    text: str,
    runtime: LLMRuntime,
    *,
    timeout_seconds: float,
) -> ReportIntent | None:
    """Use one small provider call; all returned fields are validated locally."""

    messages = [
        {
            "role": "system",
            "content": (
                "Extract a Magik Cube usage report intent. Do not calculate dates or metrics. "
                "Use day for 日报/昨天, week for complete previous-week reports, month for "
                "complete previous-month reports, recent7 for rolling seven complete days, and "
                "range only when two explicit YYYY-MM-DD dates are present. all means all models; "
                "summary means totals only; selected requires explicit model names. Use missing "
                "when model scope is absent. Use full only for 完整/详细/明细 reports."
            ),
        },
        {"role": "user", "content": text},
    ]
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await runtime.provider.chat(
                messages=messages,
                tools=_classifier_schema(),
                model=runtime.model,
                max_tokens=256,
                temperature=0,
                reasoning_effort=None,
                tool_choice={
                    "type": "function",
                    "function": {"name": _CLASSIFIER_TOOL_NAME},
                },
            )
    except TimeoutError:
        logger.warning("Magik report intent classifier timed out")
        return None
    except Exception as exc:
        logger.warning(
            "Magik report intent classifier failed: error_type={}", type(exc).__name__
        )
        return None

    payload: Any = None
    if response.tool_calls and not response.should_execute_tools:
        return None
    for call in response.tool_calls:
        if call.name == _CLASSIFIER_TOOL_NAME:
            payload = parse_tool_arguments(call.arguments)
            break
    if payload is None and response.content:
        try:
            payload = json_repair.loads(response.content)
        except Exception:
            return None
    return ReportIntent.from_payload(payload)


def _intent_pattern(raw_text: str, intent: ReportIntent) -> str:
    pattern = normalize_phrase(raw_text)
    replacements = [(intent.tenant_text, "{tenant}")]
    replacements.extend((model, "{model}") for model in intent.models)
    for value, placeholder in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if value:
            pattern = pattern.replace(normalize_phrase(value), placeholder)
    return pattern


def _candidate_id(pattern: str) -> str:
    return hashlib.sha256(pattern.encode("utf-8")).hexdigest()[:16]


class IntentCandidateStore:
    """Bounded local-only reviewed-learning store."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        retention_days: int = 30,
        max_entries: int = 10_000,
    ) -> None:
        self.path = path or get_runtime_subdir("magik_cube") / "intent_candidates.jsonl"
        self.retention_days = retention_days
        self.max_entries = max_entries

    def _load_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _prune(self, rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
        cutoff = now - timedelta(days=self.retention_days)
        kept: list[dict[str, Any]] = []
        for row in rows:
            try:
                seen = datetime.fromisoformat(str(row.get("last_seen") or ""))
            except ValueError:
                continue
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=now.tzinfo)
            if seen >= cutoff:
                kept.append(row)
        kept.sort(key=lambda item: str(item.get("last_seen") or ""), reverse=True)
        return kept[: self.max_entries]

    def record(self, raw_text: str, intent: ReportIntent, outcome: str) -> str:
        now = datetime.now().astimezone()
        pattern = _intent_pattern(raw_text, intent)
        candidate_id = _candidate_id(pattern)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + ".lock"):
            rows = self._prune(self._load_unlocked(), now)
            existing = next((row for row in rows if row.get("id") == candidate_id), None)
            if existing is None:
                rows.append(
                    {
                        "id": candidate_id,
                        "raw_text": raw_text,
                        "pattern": pattern,
                        "intent": asdict(intent),
                        "outcome": outcome,
                        "count": 1,
                        "first_seen": now.isoformat(),
                        "last_seen": now.isoformat(),
                        "status": "pending",
                    }
                )
            else:
                existing["count"] = int(existing.get("count") or 0) + 1
                existing["last_seen"] = now.isoformat()
                existing["outcome"] = outcome
                existing["intent"] = asdict(intent)
            rows = self._prune(rows, now)
            payload = "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in rows
            )
            _write_text_atomic(self.path, payload)
        return candidate_id

    def list(self, *, include_resolved: bool = False) -> list[dict[str, Any]]:
        now = datetime.now().astimezone()
        with FileLock(str(self.path) + ".lock"):
            rows = self._prune(self._load_unlocked(), now)
        if include_resolved:
            return rows
        return [row for row in rows if row.get("status") == "pending"]

    def set_status(self, candidate_id: str, status: str) -> dict[str, Any]:
        if status not in {"promoted", "rejected"}:
            raise ValueError("unsupported candidate status")
        now = datetime.now().astimezone()
        with FileLock(str(self.path) + ".lock"):
            rows = self._prune(self._load_unlocked(), now)
            row = next((item for item in rows if item.get("id") == candidate_id), None)
            if row is None:
                raise KeyError(candidate_id)
            row["status"] = status
            row["resolved_at"] = now.isoformat()
            payload = "".join(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                for item in rows
            )
            _write_text_atomic(self.path, payload)
        return row


def default_rule_path() -> Path:
    return Path(__file__).with_name("magik_intent_rules.json")


def load_promoted_rules(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or default_rule_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rules = payload.get("rules") if isinstance(payload, dict) else None
    return [item for item in rules or [] if isinstance(item, dict)]


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    parts = re.split(r"(\{tenant\}|\{model\})", pattern)
    expression = ""
    for part in parts:
        if part == "{tenant}":
            expression += r"(?P<tenant>.+?)"
        elif part == "{model}":
            expression += r"(?P<model>.+?)"
        else:
            expression += re.escape(part)
    return re.compile(f"^{expression}$", re.IGNORECASE)


def match_promoted_rule(text: str, *, path: Path | None = None) -> ReportIntent | None:
    normalized = normalize_phrase(text)
    for rule in load_promoted_rules(path):
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            continue
        match = _pattern_regex(pattern).fullmatch(normalized)
        if not match:
            continue
        payload = dict(rule.get("intent") or {})
        groups = match.groupdict()
        if groups.get("tenant"):
            payload["tenant_text"] = groups["tenant"]
        if groups.get("model"):
            payload["models"] = [groups["model"]]
            payload["model_scope"] = "selected"
        return ReportIntent.from_payload(payload)
    return None


def promote_candidate(
    candidate_id: str,
    *,
    apply: bool,
    store: IntentCandidateStore | None = None,
    rule_path: Path | None = None,
) -> dict[str, Any]:
    candidate_store = store or IntentCandidateStore()
    row = next(
        (item for item in candidate_store.list(include_resolved=True) if item.get("id") == candidate_id),
        None,
    )
    if row is None:
        raise KeyError(candidate_id)
    intent = ReportIntent.from_payload(row.get("intent"))
    if intent is None:
        raise ValueError("candidate intent is invalid")
    rule = {
        "id": candidate_id,
        "pattern": str(row.get("pattern") or ""),
        "intent": asdict(intent),
        "example": str(row.get("raw_text") or ""),
    }
    if not apply:
        return rule
    target = rule_path or default_rule_path()
    with FileLock(str(target) + ".lock"):
        existing = load_promoted_rules(target)
        by_id = {str(item.get("id") or ""): item for item in existing}
        by_id[candidate_id] = rule
        payload = {"version": 1, "rules": sorted(by_id.values(), key=lambda item: item["id"])}
        _write_text_atomic(target, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    candidate_store.set_status(candidate_id, "promoted")
    return rule


def today_in_shanghai() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()
