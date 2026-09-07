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

    reporting_api.reporting_settings_action(
        "subscription_disable", _query(subscription_id="sub-a")
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
