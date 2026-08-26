"""Report authorization checks shared by messages, cards, and schedules."""

from __future__ import annotations

from datetime import date
from typing import Any

from nanobot.reporting.store import ReportStateStore


def template_id_for_magik_params(params: dict[str, Any]) -> str:
    if str(params.get("report_template") or "full") != "matrix_card":
        return "usage_full"
    if str(params.get("granularity") or "day") == "week":
        return "usage_monthly_matrix"
    try:
        start = date.fromisoformat(str(params.get("start_date") or ""))
        end = date.fromisoformat(str(params.get("end_date") or ""))
        days = (end - start).days + 1
    except ValueError:
        days = 1
    return "usage_daily_matrix" if days == 1 else "usage_weekly_matrix"


def authorize_magik_params(
    store: ReportStateStore,
    *,
    channel: str,
    user_id: str,
    params: dict[str, Any],
) -> str | None:
    """Return a stable denial reason without exposing unauthorized catalog data."""

    if not store.rbac_enabled():
        return None
    checks: list[tuple[str, str]] = [
        ("connector", "magik_cube"),
        ("template", template_id_for_magik_params(params)),
    ]
    tenant = str(params.get("tenant_query") or "").strip()
    if tenant:
        checks.append(("tenant", tenant))
    selections = params.get("report_selections")
    if isinstance(selections, list):
        for selection in selections:
            if not isinstance(selection, dict):
                continue
            tenant = str(selection.get("tenant_query") or "").strip()
            if tenant:
                checks.append(("tenant", tenant))
            if selection.get("model_scope") == "selected":
                for model in selection.get("models") or []:
                    model_name = str(model).strip()
                    if model_name:
                        checks.append(("model", model_name))
    for resource_type, resource_id in checks:
        if not store.allowed(channel, user_id, resource_type, resource_id):
            return "当前账号没有执行该报表的权限，请联系管理员授权。"
    return None
