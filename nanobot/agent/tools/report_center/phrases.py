"""Shared private constants for the report center.

The routing phrase regexes previously duplicated here drifted from the live
copies in ``report_center/__init__.py`` (the 2026-10-08 current-hour
vocabulary missed this file) and were removed in the 2026-10-09 Phase 0
cleanup. ``__init__.py`` is the single source for routing regexes; this
module keeps only the symbols that other report-center modules import —
the report-parameter allowlist, the safe-ID pattern, and the tenant-mention
resolution dataclass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SAFE_CUBE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_ALLOWED_REPORT_PARAM_KEYS = frozenset(
    {
        "tenant_query",
        "tenants",
        "tenant_labels",
        "model",
        "models",
        "model_scope",
        "project",
        "endpoint",
        "provider",
        "all_tenants",
        "breakdown",
        "report_template",
        "granularity",
        "include_tpm",
        "report_selections",
        "comparison",
        "report_family",
        "subscription_period",
        "provider_id",
        "providers",
        "cluster",
        "report_variant",
        "tenant_scope",
        # Hourly TPM subscriptions carry their template id so the cron-run
        # compiler can select the hourly intent branch without guessing from
        # the period alone.
        "report_template_id",
    }
)


@dataclass(frozen=True, slots=True)
class _TenantMentionResolution:
    """Result of reconciling natural-language tenant names with live Cube IDs.

    ``values`` contains only labels that can be resolved back to one live
    tenant.  ``error`` is intentionally a small machine-readable category so
    the caller can choose a recovery UI without exposing catalog responses or
    silently narrowing a requested multi-tenant scope.
    """

    values: tuple[str, ...]
    error: str | None = None
    unresolved: tuple[str, ...] = ()


# Per-template report switches retired by the 2026-09-16 consolidation
# (phase 2d): their semantics moved into the always-enforced
# report_template_policies store table. The config fields below stay
# accepted for one migration window so existing config.json files keep
# loading (a warning is logged when set non-default); run
# `nanobot reports policy migrate-flags` and remove them from config.
