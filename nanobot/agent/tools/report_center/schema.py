"""Report center tool configuration, parameter schema, and config views."""

from __future__ import annotations

from typing import Any, Callable, Literal

from loguru import logger
from pydantic import Field, field_validator, model_validator

from nanobot.config_base import Base
from nanobot.reporting.capabilities import (
    ONBOARDING_VERSION,
)
from nanobot.reporting.cube import normalize_health_thresholds
from nanobot.reporting.feature_flags import RUNTIME_FEATURE_FLAG_KEYS
from nanobot.reporting.schedules import (
    DAILY_MODES,
    SUBSCRIPTION_RECURRENCES,
    SUBSCRIPTION_REPORT_TYPE_ENUM,
)

_RETIRED_TEMPLATE_FLAG_FIELDS = frozenset(
    {
        "cube_subscription_nlu_v3",
        "cube_health_report",
        "cube_health_subscription",
        "cube_usage_brief_template",
        "cube_multi_scope_brief",
        "cube_multi_scope_weekly_brief",
        "cube_machine_tpm_report",
        "cube_customer_model_hourly_tpm",
        "cube_customer_model_hourly_tpm_subscription",
        "cube_provider_quality_report",
        "cube_provider_quality_subscription",
    }
)




class ReportCenterToolConfig(Base):
    enable: bool = True
    # Cube is the only production report path in this phase; the Magik tool
    # remains a separate compatibility entry point when its own flag is on.
    cube_connector: bool = True
    cube_template: bool = True
    cube_report_runner: bool = True
    cube_subscription: bool = True
    # Natural-language parsing is bounded to one schema-forced LLM call. Job
    # creation remains behind a server-validated confirmation action.
    cube_subscription_nlu_v2: bool = True
    # V3 adds route preemption and live-catalog scope reconciliation. V2 remains
    # the compatibility gate so existing deployments receive the safety fix
    # without requiring a configuration migration.
    cube_subscription_nlu_v3: bool = True
    cube_report_reference_subscription: bool = True
    cube_subscription_nlu_timeout_seconds: float = Field(default=3.0, ge=0.5, le=10.0)
    cube_report_reference_retention_days: int = Field(default=30, ge=1, le=90)
    # Usage-family features default on (2026-09-15 product decision): they are
    # read-only Cube routes and fail closed without credentials. Runtime
    # on/off control lives in the report_feature_flags store overrides, so
    # deployments no longer edit config.json per environment. Connector and
    # template availability is registry-derived at construction time, so there
    # are no separate health connector/template switches. The semantics
    # and TTFT switches below select calculation/presentation versions and
    # stay config-level (restart to change).
    cube_health_report: bool = True
    cube_health_subscription: bool = True
    cube_health_semantics_v2: bool = True
    cube_health_card_v2: bool = True
    cube_ttft_detail: bool = True
    cube_usage_semantics_v2: bool = False
    # Brief templates share the v2 metric semantics. The default route can be
    # disabled independently to restore existing matrix cards immediately.
    cube_usage_brief_template: bool = True
    cube_usage_brief_default: bool = True
    # Wider-scope capabilities also default on; page-level switches (store
    # overrides) provide instant enable/disable without a restart.
    cube_multi_scope_brief: bool = True
    cube_multi_scope_weekly_brief: bool = True
    cube_machine_tpm_report: bool = True
    cube_customer_model_hourly_tpm: bool = True
    cube_customer_model_hourly_tpm_subscription: bool = True
    report_management_v1: bool = True
    # Guided WebUI subscription editing and result-card policy follow the
    # same default-on decision; the legacy settings endpoint remains
    # available regardless.
    report_subscription_guided_ui: bool = True
    report_subscription_button_policy: bool = True
    cube_admin_skill_help: bool = True
    # Cost/account reports need both an enabled template and an independently
    # configured TokenAPI credential. The Admin JWT is never used as fallback.
    # They stay opt-in because the credential is a deployment prerequisite.
    cube_cost_connector: bool = False
    cube_cost_template: bool = False
    cube_cost_report: bool = False
    cube_cost_subscription: bool = False
    # Provider quality is a read-only Cube route and defaults on together
    # with the usage family; include_details only enriches the report.
    # Connector availability is registry-derived (the connector registers
    # whenever the Cube config exists), so there are no separate provider
    # connector/template switches. The
    # subscription flag stays opt-in per the approved rollout scope.
    cube_provider_quality_report: bool = True
    cube_provider_quality_detail: bool = True
    cube_provider_quality_subscription: bool = False
    cube_provider_quality_selector: bool = True
    cube_provider_quality_empty_collapse: bool = True
    cube_scope_selector_v2: bool = False
    cube_transient_run_retry: bool = False
    cube_transient_retry_delay_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    # Cross-customer model reports can fan out across the full Cube catalog.
    # They stay disabled until an operator explicitly enables this feature.
    cube_model_all_tenant_report: bool = False
    # Shadow mode compares legacy/v2 calculations on the same normalized dataset
    # and persists only changed metric IDs, never values or raw Cube responses.
    cube_semantics_shadow: bool = False
    health_thresholds: dict[str, dict[str, float]] = Field(default_factory=dict)
    # Extension declarations are opt-in. They do not configure or contact a
    # real Grafana, WeCom, or DingTalk service by themselves.
    grafana_connector: bool = False
    wecom_renderer: bool = False
    dingtalk_renderer: bool = False
    onboarding_version: int = ONBOARDING_VERSION
    timezone: str = "Asia/Shanghai"
    rbac_enforced: bool = False
    run_retention_days: int = 30
    state_backend: Literal["sqlite", "postgresql"] = "sqlite"
    postgres_dsn_env: str = "NANOBOT_REPORTING_POSTGRES_DSN"
    # Grafana expressions are deployment-owned query definitions. Secrets must
    # be supplied as SecretRef objects, for example {"provider": "env", "key": "..."}.
    grafana: dict[str, Any] = Field(default_factory=dict)

    @field_validator("grafana")
    @classmethod
    def _require_grafana_secret_ref(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key in ("service_account_token", "serviceAccountToken"):
            secret = value.get(key)
            if isinstance(secret, str) and secret.strip():
                raise ValueError("Grafana service account credentials must use SecretRef")
        return value

    @field_validator("health_thresholds")
    @classmethod
    def _validate_health_thresholds(
        cls, value: dict[str, dict[str, float]]
    ) -> dict[str, dict[str, float]]:
        return normalize_health_thresholds(value)

    @model_validator(mode="after")
    def _warn_retired_template_flags(self) -> "ReportCenterToolConfig":
        """Log (never raise) when a retired per-template switch is set non-default.

        The retired fields no longer gate anything (phase 2d); the warning
        points operators at the replacement surface instead of failing a
        previously valid config.json during the migration window.
        """

        changed = [
            name
            for name in _RETIRED_TEMPLATE_FLAG_FIELDS
            if getattr(self, name, None) is not None
            and getattr(self, name) != type(self).model_fields[name].default
        ]
        if changed:
            logger.warning(
                "Retired per-template report flags in tools.reporting are ignored; "
                "manage these templates in Report platform -> 报表类型 instead: {}",
                ", ".join(sorted(changed)),
            )
        return self




_REPORT_CENTER_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "home",
                "cube_report",
                "health_report",
                "cost_report",
                "provider_quality_report",
                "multi_scope_brief",
                "multi_scope_weekly_brief",
                "machine_tpm_report",
                "customer_model_hourly_tpm",
                "examples",
                "recent",
                "subscriptions",
                "subscription_setup",
                "subscription_preview",
                "subscription_reference_missing",
                "subscription_parse_failed",
                "subscription_scope_failed",
                "subscribe",
                "subscription_enable",
                "subscription_disable",
                "subscription_remove",
                "run_subscription",
                "request_access",
            ],
        },
        "period": {"type": "string", "enum": ["day", "week", "month", "recent7", "recent15m", "recent1h", "range"]},
        "report_family": {
            "type": "string",
            "enum": ["usage", "health", "cost", "provider_quality", "capacity"],
        },
        "report_template": {
            "type": "string",
            "enum": ["brief", "matrix_card", "full"],
        },
        "report_type": {
            "type": "string",
            "enum": list(SUBSCRIPTION_REPORT_TYPE_ENUM),
        },
        "tenant_scope": {"type": "string", "enum": ["selected", "all", "inherit"]},
        "tenant_aliases": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
            "maxItems": 20,
        },
        "model_scope": {
            "type": "string",
            "enum": ["summary", "all", "selected", "inherit"],
        },
        "inherit_report_scope": {"type": "boolean"},
        "reference_message_id": {"type": "string", "maxLength": 128},
        "catalog_unavailable": {"type": "boolean"},
        "scope_unresolved": {"type": "boolean"},
        "tenant_ambiguous": {"type": "boolean"},
        "subscription_error": {
            "type": "string",
            "enum": [
                "nlu_unavailable",
                "classifier_disabled",
                "catalog_unavailable",
                "tenant_ambiguous",
                "scope_unresolved",
                "reference_not_found_or_expired",
            ],
        },
        "unresolved_tenants": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
            "maxItems": 20,
        },
        "recurrence": {
            "type": "string",
            "enum": list(SUBSCRIPTION_RECURRENCES),
        },
        "tenant_query": {"type": "string", "maxLength": 128},
        "tenants": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
            "maxItems": 20,
        },
        "project": {"type": "string", "maxLength": 128},
        "model": {"type": "string", "maxLength": 128},
        "models": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
            "maxItems": 20,
        },
        "report_selections": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "tenant_query": {"type": "string", "maxLength": 128},
                    "model_scope": {"type": "string", "enum": ["summary", "all", "selected"]},
                    "models": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 128},
                        "maxItems": 20,
                    },
                },
                "required": ["tenant_query", "model_scope", "models"],
                "additionalProperties": False,
            },
        },
        "breakdown": {"type": "string", "enum": ["summary", "model"]},
        "endpoint": {"type": "string", "maxLength": 128},
        "provider": {"type": "string", "maxLength": 128},
        "provider_id": {"type": "string", "maxLength": 64},
        "cluster": {"type": "string", "maxLength": 128},
        "providers": {
            "type": "array",
            "items": {"type": "string", "maxLength": 128},
            "maxItems": 50,
        },
        "selection_confirmed": {"type": "boolean"},
        "include_empty": {"type": "boolean"},
        "start_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
        "end_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
        "interactive": {"type": "boolean"},
        "all_tenants": {"type": "boolean"},
        "send_time": {"type": "string", "maxLength": 5},
        "daily_mode": {"type": "string", "enum": list(DAILY_MODES)},
        "weekday": {"type": "integer", "minimum": 1, "maximum": 7},
        "month_day": {"type": "integer", "minimum": 1, "maximum": 28},
        "report_params": {"type": "object"},
        "subscription_id": {"type": "string", "maxLength": 64},
        # Modern card actions carry the row revision so a stale enable/disable
        # or delete button cannot overwrite a newer subscription state.  It is
        # optional for the legacy text command during the migration window.
        "revision": {"type": "integer", "minimum": 0},
    },
    "required": ["action"],
    "additionalProperties": False,
}




class _EffectiveFlagConfigView:
    """Config view that resolves runtime feature flags through store overrides.

    ``ReportSubscriptionService`` reads the reporting config through this
    adapter, so management-surface toggles apply to the confirmation path on
    the next request. Non-runtime attributes (construction semantics,
    retention, timezone) delegate to the captured startup config.
    """

    def __init__(self, config: Any, flag_of: Callable[[str], bool]) -> None:
        object.__setattr__(self, "_config", config)
        object.__setattr__(self, "_flag_of", flag_of)

    def __getattr__(self, name: str) -> Any:
        if name in RUNTIME_FEATURE_FLAG_KEYS:
            return self._flag_of(name)
        return getattr(self._config, name)


