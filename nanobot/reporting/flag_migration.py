"""One-time migration of per-template feature-flag overrides into policies.

Phase 2b/2d of the 2026-09-16 switch consolidation retire the per-template
family flags and the per-family subscription-creation flags from the
``report_feature_flags`` runtime registry; their semantics move into
``report_template_policies`` (``enabled`` blocks execution/visibility/new
subscriptions, ``subscription_mode`` blocks new subscriptions only).

This module converts existing store overrides into policy rows so an
operator who turned a report family off through the WebUI keeps that state
after upgrading. The migration is expand-only and idempotent:

- Only overrides that exist in the store are considered; absent overrides
  keep falling back to the (default-on) policy semantics.
- A template that already has a policy row written by an administrator is
  never touched — the admin row wins.
- A row created earlier in the same run (family flag) may be refined by a
  subscription flag in the same run (mode=disabled) without a re-run.
- ``dry_run=True`` reports the planned rows without writing.

The old ``report_feature_flags`` override rows are deliberately NOT deleted:
they become inert when the flag keys leave ``RUNTIME_FEATURE_FLAGS`` and can
be cleaned up after an observation window.
"""

from __future__ import annotations

from typing import Any

from nanobot.reporting.subscriptions import DEFAULT_UNSUBSCRIBABLE_TEMPLATES

# Retired per-template family flags -> template ids whose exposure policy
# (enabled=False) replaces an "off" override.
FAMILY_FLAG_TEMPLATES: dict[str, tuple[str, ...]] = {
    "cube_health_report": ("health_sre",),
    "cube_provider_quality_report": ("provider_quality",),
    "cube_machine_tpm_report": ("machine_tpm_peak",),
    "cube_customer_model_hourly_tpm": ("usage_customer_model_hourly_tpm",),
    "cube_multi_scope_brief": ("usage_customer_model_daily_brief",),
    "cube_multi_scope_weekly_brief": ("usage_customer_model_weekly_brief",),
    "cube_usage_brief_template": (
        "usage_daily_brief",
        "usage_weekly_brief",
        "usage_monthly_brief",
        "usage_custom_brief",
    ),
}

# Retired subscription-creation flags -> template id whose subscription_mode
# replaces an "off" override. "On" overrides need no row: every target
# template is subscribable by default.
SUBSCRIPTION_FLAG_TEMPLATES: dict[str, str] = {
    "cube_health_subscription": "health_sre",
    "cube_provider_quality_subscription": "provider_quality",
    "cube_customer_model_hourly_tpm_subscription": "usage_customer_model_hourly_tpm",
}


def _default_mode(template_id: str) -> str:
    # Mirrors the management-page default so a later manual enable of a
    # machine_tpm_peak row cannot accidentally open subscriptions.
    return "disabled" if template_id in DEFAULT_UNSUBSCRIBABLE_TEMPLATES else "all_authorized"


def migrate_flag_overrides_to_template_policies(
    store: Any,
    *,
    updated_by: str = "flag-migration",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Convert retired per-template flag overrides into policy rows.

    Returns a report dict with ``created``/``updated``/``skipped_existing``
    lists (or ``planned`` in dry-run mode). Safe to re-run: existing rows
    make every step a no-op.
    """

    overrides = store.get_feature_flags()
    report: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "planned": [],
        "created": [],
        "updated": [],
        "skipped_existing": [],
    }
    # Template ids this run created; a subscription flag may still refine
    # their mode below without violating the admin-row-wins rule.
    created_here: set[str] = set()

    for flag, template_ids in FAMILY_FLAG_TEMPLATES.items():
        if flag not in overrides or bool(overrides[flag]):
            # Absent or "on" overrides keep the default (enabled) semantics.
            continue
        for template_id in template_ids:
            if store.template_policy(template_id) is not None:
                report["skipped_existing"].append(template_id)
                continue
            mode = _default_mode(template_id)
            if dry_run:
                report["planned"].append(
                    {"template_id": template_id, "enabled": False, "subscription_mode": mode}
                )
                continue
            store.set_template_policy(
                template_id,
                enabled=False,
                subscription_mode=mode,
                updated_by=updated_by,
            )
            created_here.add(template_id)
            report["created"].append(template_id)

    for flag, template_id in SUBSCRIPTION_FLAG_TEMPLATES.items():
        if flag not in overrides or bool(overrides[flag]):
            continue
        existing = store.template_policy(template_id)
        if existing is not None and template_id not in created_here:
            # An administrator already owns this row; do not overwrite it.
            report["skipped_existing"].append(template_id)
            continue
        if existing is not None and str(existing["subscription_mode"]) == "disabled":
            continue
        if dry_run:
            report["planned"].append(
                {
                    "template_id": template_id,
                    "enabled": bool(existing["enabled"]) if existing else True,
                    "subscription_mode": "disabled",
                }
            )
            continue
        store.set_template_policy(
            template_id,
            enabled=bool(existing["enabled"]) if existing else True,
            subscription_mode="disabled",
            updated_by=updated_by,
            expected_revision=int(existing["revision"]) if existing else None,
        )
        if existing is None:
            report["created"].append(template_id)
        else:
            report["updated"].append(template_id)

    return report
