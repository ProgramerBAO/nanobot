"""Contract tests for the authenticated reporting management control plane."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from nanobot.agent.tools.magik_cube import MagikCubeToolConfig
from nanobot.agent.tools.report_center import ReportCenterToolConfig
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule
from nanobot.reporting.store import ReportStateStore, ReportSubscription
from nanobot.webui import reporting_api


def _config(tmp_path):
    reporting = ReportCenterToolConfig(
        report_management_v1=True,
        cube_multi_scope_brief=True,
        cube_machine_tpm_report=True,
    )
    return SimpleNamespace(
        workspace_path=tmp_path,
        tools=SimpleNamespace(
            reporting=reporting,
            magik_cube=MagikCubeToolConfig(enable=True),
        ),
        agents=SimpleNamespace(defaults=SimpleNamespace(unified_session=False)),
    )


def _query(**values: object) -> dict[str, list[str]]:
    return {key: [str(value)] for key, value in values.items()}


def test_reporting_settings_fall_back_on_wrong_shape_startup_handle(
    monkeypatch, tmp_path
) -> None:
    """A channel-section startup handle must never crash the settings surface.

    Regression for the 2026-09-16 phase-4 wiring bug: the WebSocket channel
    section (WebSocketConfig, no ``tools`` attribute) reached the reporting
    API as the startup config and every settings action failed with the
    generic 500 "reporting settings action failed".
    """

    class _ChannelSection:
        max_message_bytes = 1

    monkeypatch.setattr(reporting_api, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(reporting_api, "resolve_config_env_vars", lambda value: value)

    # The wrong-shape handle falls back to a fresh resolved load instead of
    # raising AttributeError on .tools.reporting.
    payload = reporting_api.reporting_settings_payload(
        startup_config=_ChannelSection()
    )
    assert payload["storage"]["backend"] == "sqlite"

    # A correctly shaped handle is used as-is (the running process's view).
    payload = reporting_api.reporting_settings_payload(
        startup_config=_config(tmp_path)
    )
    assert payload["storage"]["backend"] == "sqlite"


def test_template_policy_action_enforces_revision(monkeypatch, tmp_path) -> None:
    store = ReportStateStore(tmp_path / "reporting.db")
    monkeypatch.setattr(reporting_api, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(reporting_api, "get_report_state_store", lambda *_args, **_kwargs: store)

    payload = reporting_api.reporting_settings_action(
        "template_policy",
        _query(
            template_id="machine_tpm_peak",
            enabled="true",
            subscription_mode="allowlist",
            revision=0,
        ),
    )

    policy = next(item for item in payload["template_policies"] if item["id"] == "machine_tpm_peak")
    assert policy["enabled"] is True
    assert policy["subscription_mode"] == "allowlist"
    assert policy["revision"] == 1
    with pytest.raises(reporting_api.ReportingSettingsError) as exc_info:
        reporting_api.reporting_settings_action(
            "template_policy",
            _query(
                template_id="machine_tpm_peak",
                enabled="false",
                subscription_mode="disabled",
                revision=0,
            ),
        )
    assert exc_info.value.status == 409


def test_feature_flag_action_toggles_instantly_and_audits(monkeypatch, tmp_path) -> None:
    """Page toggles write store overrides; unknown keys fail closed."""

    store = ReportStateStore(tmp_path / "reporting.db")
    monkeypatch.setattr(reporting_api, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(reporting_api, "get_report_state_store", lambda *_args, **_kwargs: store)

    payload = reporting_api.reporting_settings_action(
        "feature_flag",
        _query(flag="cube_admin_skill_help", enabled="false"),
    )
    hourly = next(
        item for item in payload["feature_flags"] if item["key"] == "cube_admin_skill_help"
    )
    # The override wins over the (default-on) configuration value.
    assert hourly["enabled"] is False
    assert hourly["source"] == "override"

    # Resetting the override restores the configured default.
    payload = reporting_api.reporting_settings_action(
        "feature_flag_reset",
        _query(flag="cube_admin_skill_help"),
    )
    hourly = next(
        item for item in payload["feature_flags"] if item["key"] == "cube_admin_skill_help"
    )
    assert hourly["enabled"] is True
    assert hourly["source"] == "default"

    # Unknown keys and invalid values are rejected server-side.
    with pytest.raises(reporting_api.ReportingSettingsError) as unknown_key:
        reporting_api.reporting_settings_action(
            "feature_flag", _query(flag="not_a_report_flag", enabled="true")
        )
    assert unknown_key.value.status == 400
    with pytest.raises(reporting_api.ReportingSettingsError) as bad_value:
        reporting_api.reporting_settings_action(
            "feature_flag", _query(flag="cube_admin_skill_help", enabled="maybe")
        )
    assert bad_value.value.status == 400
    with pytest.raises(reporting_api.ReportingSettingsError) as missing_override:
        reporting_api.reporting_settings_action(
            "feature_flag_reset", _query(flag="cube_usage_brief_default")
        )
    assert missing_override.value.status == 404

    # Every mutation is audited in the control-plane audit log.
    import sqlite3

    with sqlite3.connect(tmp_path / "reporting.db") as db:
        audit_actions = [
            row[0]
            for row in db.execute(
                "SELECT action FROM report_admin_audit ORDER BY created_at"
            ).fetchall()
        ]
    assert audit_actions.count("feature_flag_update") == 1
    assert audit_actions.count("feature_flag_reset") == 1


def test_feature_flags_payload_reflects_effective_values(monkeypatch, tmp_path) -> None:
    """The payload merges store overrides with configured defaults."""

    store = ReportStateStore(tmp_path / "reporting.db")
    store.set_feature_flag("report_management_v1", False, updated_by="webui_admin")
    monkeypatch.setattr(reporting_api, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(reporting_api, "get_report_state_store", lambda *_args, **_kwargs: store)

    payload = reporting_api.reporting_settings_payload()

    flags = {item["key"]: item for item in payload["feature_flags"]}
    # The store override wins even though the config enables management.
    assert payload["policy"]["management_enabled"] is False
    assert flags["report_management_v1"]["enabled"] is False
    assert flags["report_management_v1"]["source"] == "override"
    # Untouched flags fall back to configured defaults.
    assert flags["cube_admin_skill_help"]["enabled"] is True
    assert flags["cube_admin_skill_help"]["source"] == "default"


def test_default_subscription_policy_allows_daily_brief_but_not_machine_peak(
    monkeypatch, tmp_path
) -> None:
    """Daily multi-scope briefs remain subscribable until an admin disables them."""

    store = ReportStateStore(tmp_path / "reporting.db")
    monkeypatch.setattr(reporting_api, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(reporting_api, "get_report_state_store", lambda *_args, **_kwargs: store)

    payload = reporting_api.reporting_settings_payload()
    policies = {item["id"]: item for item in payload["template_policies"]}

    assert policies["usage_customer_model_daily_brief"]["subscription_mode"] == (
        "all_authorized"
    )
    assert policies["machine_tpm_peak"]["subscription_mode"] == "disabled"


def test_reporting_settings_resolves_environment_references_before_building_payload(
    monkeypatch, tmp_path
) -> None:
    """The WebUI control plane must use the Gateway's resolved config boundary."""

    config = _config(tmp_path)
    calls = []
    monkeypatch.setattr(reporting_api, "load_config", lambda: config)
    monkeypatch.setattr(reporting_api, "get_report_state_store", lambda *_args, **_kwargs: ReportStateStore(tmp_path / "reporting.db"))

    def resolve(value):
        calls.append(value)
        return value

    monkeypatch.setattr(reporting_api, "resolve_config_env_vars", resolve)
    reporting_api.reporting_settings_payload()

    assert calls == [config]


def test_guided_form_accepts_legacy_zero_revision() -> None:
    """Revision zero is valid for subscriptions created before CAS migration."""

    assert reporting_api._form_integer({"revision": 0}, "revision") == 0
    assert reporting_api._form_integer({"revision": "0"}, "revision") == 0
    with pytest.raises(reporting_api.ReportingSettingsError, match="missing revision"):
        reporting_api._form_integer({}, "revision")


def test_subscription_disable_updates_cron_and_database(monkeypatch, tmp_path) -> None:
    store = ReportStateStore(tmp_path / "reporting.db")
    config = _config(tmp_path)
    monkeypatch.setattr(reporting_api, "load_config", lambda: config)
    monkeypatch.setattr(reporting_api, "get_report_state_store", lambda *_args, **_kwargs: store)
    cron = CronService(tmp_path / "cron" / "jobs.json")
    job = cron.add_job(
        name="Daily report",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="Asia/Shanghai"),
        message="执行固定报表订阅",
        session_key="feishu:chat-a",
        origin_channel="feishu",
        origin_chat_id="chat-a",
    )
    now = datetime.now(UTC).isoformat()
    subscription = ReportSubscription(
        subscription_id="sub-a",
        channel="feishu",
        chat_id="chat-a",
        user_id="ou-a",
        connector_id="magik_cube",
        template_id="usage_daily_brief",
        template_version="2.0",
        schedule="0 9 * * *",
        timezone="Asia/Shanghai",
        report_params={"report_template": "brief", "subscription_period": "day"},
        cron_job_id=job.id,
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    assert store.add_subscription(subscription, "fingerprint-a")

    # Since the phase-3 consolidation the row revision is mandatory: a
    # missing revision is rejected instead of falling back to the legacy
    # inline path.
    with pytest.raises(
        reporting_api.ReportingSettingsError, match="revision is required"
    ) as missing_revision:
        reporting_api.reporting_settings_action(
            "subscription_disable", _query(subscription_id="sub-a")
        )
    assert missing_revision.value.status == 400

    reporting_api.reporting_settings_action(
        "subscription_disable", _query(subscription_id="sub-a", revision="0")
    )

    assert store.subscription("sub-a").enabled is False
    persisted_job = next(
        item
        for item in CronService(tmp_path / "cron" / "jobs.json").list_jobs(
            include_disabled=True
        )
        if item.id == job.id
    )
    assert persisted_job.enabled is False
