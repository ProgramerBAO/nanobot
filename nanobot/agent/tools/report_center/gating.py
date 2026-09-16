"""Feature-flag, template-policy, and authorization gates for report routing."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from nanobot.reporting import (
    CubeConnector,
    CubeProviderQualityConnector,
)
from nanobot.reporting.capabilities import (
    template_enabled,
)
from nanobot.reporting.subscriptions import (
    POLICY_DENIED_ALLOWLIST,
    POLICY_DENIED_TEMPLATE_DISABLED,
    evaluate_subscription_policy,
)


class _ReportGatingMixin:

    def _requested_usage_template(self, requested: str | None) -> str:
        """Map user-facing depth words to stable compatibility template names."""

        if requested == "完整":
            return "full"
        if requested == "详细":
            return "matrix_card"
        if requested == "简报" and template_enabled(self._store, "usage_daily_brief"):
            return "brief"
        if self._usage_brief_default_enabled:
            return "brief"
        return "matrix_card"


    @property
    def _usage_brief_default_enabled(self) -> bool:
        """Require the brief template policy and routing flag to enable the default safely."""

        return (
            template_enabled(self._store, "usage_daily_brief")
            and self._flag("cube_usage_brief_default")
        )


    def _flag(self, key: str) -> bool:
        """Resolve a runtime feature flag: store override wins, config default otherwise.

        Overrides come from the ``report_feature_flags`` store table written
        by the management page, so a toggle applies on the next request
        without a restart. Keys outside the runtime set (construction-level
        semantics) must keep reading ``self._config`` directly.
        """

        overrides = self._store.get_feature_flags()
        if key in overrides:
            return bool(overrides[key])
        return bool(getattr(self._config, key))


    @property
    def health_connector_enabled(self) -> bool:
        # Registration is no longer flag-gated; availability is simply whether
        # the Cube connector and its health template are registered.
        return bool(
            isinstance(self._registry.connector("magik_cube"), CubeConnector)
            and self._registry.template("health_sre") is not None
        )


    @property
    def health_reports_enabled(self) -> bool:
        # Per-template on/off lives in the template policy since the
        # 2026-09-16 consolidation; the runtime flag was retired.
        return self.health_connector_enabled and template_enabled(
            self._store, "health_sre"
        )


    @property
    def cost_connector_enabled(self) -> bool:
        connector = self._registry.connector("magik_cube")
        return bool(
            self._config.cube_cost_connector
            and self._config.cube_cost_template
            and isinstance(connector, CubeConnector)
            and connector.account_configured
            and self._registry.template("cost_account") is not None
        )


    @property
    def cost_reports_enabled(self) -> bool:
        # Runtime flag (store override over the configured default) since the
        # 2026-09-16 consolidation; connector/template registration stays
        # config-level because the TokenAPI credential must exist at
        # construction time.
        return self.cost_connector_enabled and self._flag("cube_cost_report")


    @property
    def cost_subscriptions_enabled(self) -> bool:
        return self.cost_connector_enabled and self._flag("cube_cost_subscription")


    @property
    def provider_quality_connector_enabled(self) -> bool:
        # Registration is no longer flag-gated; the provider connector exists
        # whenever a Cube config is present.
        return bool(
            isinstance(
                self._registry.connector("cube_provider_quality"),
                CubeProviderQualityConnector,
            )
            and self._registry.template("provider_quality") is not None
        )


    @property
    def provider_quality_reports_enabled(self) -> bool:
        # Per-template on/off lives in the template policy since the
        # 2026-09-16 consolidation; the runtime flag was retired.
        return self.provider_quality_connector_enabled and template_enabled(
            self._store, "provider_quality"
        )


    def _authorized_for_magik(self, channel: str, user_id: str) -> bool:
        return self._magik_tool is not None and self._store.allowed(
            channel, user_id, "connector", "magik_cube"
        )


    def _subscription_policy_denial(
        self,
        *,
        channel: str,
        user_id: str,
        template_id: str,
    ) -> str | None:
        """Return a safe denial for a template before subscription side effects.

        The template policy table is the single source of truth for lifecycle,
        audience, and opt-in policy and is always enforced (2026-09-16
        consolidation): the report_management_v1 runtime flag now gates only
        the WebUI management surface, never enforcement. Unsubscribable-by-
        default templates still require an explicit policy row.
        """

        template = self._registry.template(template_id)
        if template is None:
            return "Error: report subscription template is unavailable"
        if template.manifest.lifecycle_state not in {"publish", "canary"}:
            return "Error: this report template is not available"

        if self._store.rbac_enabled():
            required_grants = [
                ("capability", "subscriptions"),
                ("template", template_id),
            ]
            required_grants.extend(
                ("connector", connector_id)
                for connector_id in template.manifest.connector_ids
            )
            if any(
                not self._store.allowed(channel, user_id, resource_type, resource_id)
                for resource_type, resource_id in required_grants
            ):
                return "Error: no permission to subscribe to this report template"

        policy = self._store.template_policy(template_id)
        reason = evaluate_subscription_policy(template_id, policy)
        if reason == POLICY_DENIED_ALLOWLIST:
            if not self._store.allowed(
                channel, user_id, "subscription_template", template_id
            ):
                return "Error: no permission to subscribe to this report template"
            return None
        if reason == POLICY_DENIED_TEMPLATE_DISABLED:
            return "Error: this report template is disabled"
        if reason is not None:
            return "Error: this report template does not allow subscriptions"
        return None


    def _filter_usage_subscription_actions(
        self,
        document: Any,
        *,
        channel: str,
        user_id: str,
        template_id: str,
    ) -> Any:
        """Hide brief subscription actions unless the full server capability is usable.

        The action remains independently authorized during setup and creation. This
        presentation gate prevents offering a control that the current Gateway,
        template lifecycle, or user grants cannot execute.
        """

        template = self._registry.template(template_id)
        policy = (
            self._store.template_policy(template_id)
            if self._flag("report_subscription_button_policy")
            else None
        )
        policy_denial = self._subscription_policy_denial(
            channel=channel,
            user_id=user_id,
            template_id=template_id,
        )
        allowed = bool(
            self._cron is not None
            and self._flag("cube_subscription")
            and user_id
            and self._authorized_for_magik(channel, user_id)
            and self._store.allowed(channel, user_id, "capability", "subscriptions")
            and template is not None
            and template.manifest.lifecycle_state in {"publish", "canary"}
            and self._store.allowed(channel, user_id, "template", template_id)
            and policy_denial is None
            and (policy is None or bool(policy.get("show_subscription_button", True)))
        )
        if allowed:
            return document

        blocks = []
        for block in document.blocks:
            if block.kind != "actions":
                blocks.append(block)
                continue
            actions = block.data.get("actions")
            if not isinstance(actions, list):
                blocks.append(block)
                continue
            visible_actions = [
                action
                for action in actions
                if not (isinstance(action, dict) and self._is_subscription_action(action))
            ]
            if visible_actions:
                blocks.append(replace(block, data={**block.data, "actions": visible_actions}))
        return replace(document, blocks=tuple(blocks))


    @staticmethod
    def _is_subscription_action(action: dict[str, Any]) -> bool:
        """Recognize legacy and current subscription controls for policy hiding."""

        action_id = str(action.get("action_id") or "").casefold()
        if action_id.startswith(("usage_subscription_setup:", "subscribe:", "subscription:")):
            return True
        params = action.get("params")
        return isinstance(params, dict) and str(params.get("action") or "").casefold() in {
            "subscription_setup",
            "subscription_preview",
            "subscribe",
        }
