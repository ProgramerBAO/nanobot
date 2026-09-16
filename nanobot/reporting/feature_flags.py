"""Runtime feature-flag resolution for report capabilities.

Feature switches are store-backed overrides (``report_feature_flags``) that
fall back to the process configuration default. Template registration is no
longer flag-gated: every Cube template registers whenever its connector
exists, and per-template on/off lives in the always-enforced
``report_template_policies`` table since the 2026-09-16 consolidation
(phase 2d retired the per-template family flags; the
``reports policy migrate-flags`` CLI converts existing overrides).
This registry now covers behavior/routing switches only: the subscription
mechanism, NLU routing, the brief-default routing, help visibility, the
management surface, and the extension-connector families (cost).
Construction-level semantics (version switches, thresholds,
``include_details``) stay config-level and require a restart; they are
intentionally absent from this registry.

Only the keys listed here may be written through the management surface;
unknown keys are rejected server-side (fail closed).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Callable

RUNTIME_FEATURE_FLAGS: "OrderedDict[str, dict[str, str]]" = OrderedDict(
    [
        # group: 用量报表
        (
            "cube_usage_brief_default",
            {"label": "简报默认路由", "group": "用量报表"},
        ),
        (
            "cube_admin_skill_help",
            {"label": "Cube 灵活查询帮助", "group": "用量报表"},
        ),
        # group: 小时 TPM
        # (The hourly report/subscription family flags were retired to the
        # usage_customer_model_hourly_tpm template policy in phase 2d.)
        # group: 订阅
        (
            "cube_subscription",
            {"label": "Cube 报表订阅", "group": "订阅"},
        ),
        (
            "cube_subscription_nlu_v2",
            {"label": "自然语言订阅", "group": "订阅"},
        ),
        (
            "cube_report_reference_subscription",
            {"label": "引用报表创建订阅", "group": "订阅"},
        ),
        # group: 健康报告
        # (Retired to the health_sre template policy in phase 2d.)
        # group: 供应商质量
        # (Retired to the provider_quality template policy in phase 2d.)
        # group: 成本报表
        # Promoted from config-level gating in the 2026-09-16 consolidation
        # (phase 2c): report execution and subscription creation are runtime
        # switches now, while the TokenAPI connector/template registration
        # stays config-level because the credential must exist at
        # construction time. Configured defaults remain opt-in (False).
        (
            "cube_cost_report",
            {"label": "成本报表", "group": "成本报表"},
        ),
        (
            "cube_cost_subscription",
            {"label": "成本报表订阅", "group": "成本报表"},
        ),
        # group: 管理界面
        # report_management_v1 gates only the WebUI management surface and
        # guided forms since phase 2a; the template policy itself is always
        # enforced.
        (
            "report_management_v1",
            {"label": "Report platform 管理页", "group": "管理界面"},
        ),
        (
            "report_subscription_guided_ui",
            {"label": "引导式订阅编辑", "group": "管理界面"},
        ),
        (
            "report_subscription_button_policy",
            {"label": "订阅按钮策略", "group": "管理界面"},
        ),
    ]
)

RUNTIME_FEATURE_FLAG_KEYS = frozenset(RUNTIME_FEATURE_FLAGS)


def effective_feature_flags(
    store: Any,
    default_of: Callable[[str], bool],
) -> dict[str, bool]:
    """Resolve the effective value of every runtime feature flag.

    ``store`` is a :class:`~nanobot.reporting.store.ReportStateStore`-like
    object; ``default_of(key)`` returns the configured (or source) default.
    A store override always wins; a missing key falls back to the default and
    must never be read as ``False``.
    """

    overrides = store.get_feature_flags()
    return {
        key: bool(overrides.get(key, default_of(key)))
        for key in RUNTIME_FEATURE_FLAG_KEYS
    }


def feature_flag_details(
    store: Any,
    default_of: Callable[[str], bool],
) -> list[dict[str, Any]]:
    """Build the management-payload view of every runtime feature flag."""

    overrides = store.get_feature_flags()
    details: list[dict[str, Any]] = []
    for key, meta in RUNTIME_FEATURE_FLAGS.items():
        has_override = key in overrides
        details.append(
            {
                "key": key,
                "label": meta["label"],
                "group": meta["group"],
                "enabled": bool(overrides.get(key, default_of(key))),
                "source": "override" if has_override else "default",
            }
        )
    return details
