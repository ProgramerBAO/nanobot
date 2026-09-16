"""Tests for the retired-flag -> template-policy migration (Phase 2b).

The migration must be idempotent, expand-only, and must never overwrite an
administrator-written policy row.
"""

from __future__ import annotations

from nanobot.reporting.flag_migration import (
    migrate_flag_overrides_to_template_policies,
)
from nanobot.reporting.store import ReportStateStore


def test_off_family_override_creates_disabled_policy_rows(tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    store.set_feature_flag("cube_health_report", False, updated_by="webui_admin")
    store.set_feature_flag("cube_machine_tpm_report", False, updated_by="webui_admin")

    report = migrate_flag_overrides_to_template_policies(store)

    health = store.template_policy("health_sre")
    assert health is not None and health["enabled"] is False
    assert health["subscription_mode"] == "all_authorized"
    # machine_tpm_peak keeps its unsubscribable-by-default mode.
    machine = store.template_policy("machine_tpm_peak")
    assert machine is not None and machine["enabled"] is False
    assert machine["subscription_mode"] == "disabled"
    assert report["created"] == ["health_sre", "machine_tpm_peak"]


def test_on_or_absent_overrides_create_no_rows(tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    store.set_feature_flag("cube_health_report", True, updated_by="webui_admin")

    report = migrate_flag_overrides_to_template_policies(store)

    assert store.template_policy("health_sre") is None
    assert report["created"] == []


def test_migration_is_idempotent(tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    store.set_feature_flag("cube_health_report", False, updated_by="webui_admin")

    migrate_flag_overrides_to_template_policies(store)
    second = migrate_flag_overrides_to_template_policies(store)

    assert second["created"] == []
    assert second["updated"] == []
    assert "health_sre" in second["skipped_existing"]
    # The first run's row is unchanged by the second run.
    assert store.template_policy("health_sre")["revision"] == 1


def test_admin_policy_row_wins_over_flag_override(tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    store.set_template_policy(
        "health_sre",
        enabled=True,
        subscription_mode="allowlist",
        updated_by="admin",
        expected_revision=0,
    )
    store.set_feature_flag("cube_health_report", False, updated_by="webui_admin")

    report = migrate_flag_overrides_to_template_policies(store)

    policy = store.template_policy("health_sre")
    assert policy["enabled"] is True
    assert policy["subscription_mode"] == "allowlist"
    assert "health_sre" in report["skipped_existing"]


def test_off_subscription_override_disables_new_subscriptions_only(tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    store.set_feature_flag("cube_health_subscription", False, updated_by="webui_admin")

    report = migrate_flag_overrides_to_template_policies(store)

    policy = store.template_policy("health_sre")
    assert policy is not None
    assert policy["enabled"] is True
    assert policy["subscription_mode"] == "disabled"
    assert report["created"] == ["health_sre"]


def test_family_and_subscription_overrides_merge_into_one_row(tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    store.set_feature_flag("cube_health_report", False, updated_by="webui_admin")
    store.set_feature_flag("cube_health_subscription", False, updated_by="webui_admin")

    report = migrate_flag_overrides_to_template_policies(store)

    policy = store.template_policy("health_sre")
    assert policy is not None
    assert policy["enabled"] is False
    assert policy["subscription_mode"] == "disabled"
    # One row, two writes in the same run: the family phase creates it and
    # the subscription phase refines the mode; the report is honest about
    # both.
    assert report["created"] == ["health_sre"]
    assert report["updated"] == ["health_sre"]
    assert policy["revision"] == 2


def test_dry_run_reports_without_writing(tmp_path) -> None:
    store = ReportStateStore(tmp_path / "state.db")
    store.set_feature_flag("cube_health_report", False, updated_by="webui_admin")

    report = migrate_flag_overrides_to_template_policies(store, dry_run=True)

    assert store.template_policy("health_sre") is None
    assert report["planned"] == [
        {
            "template_id": "health_sre",
            "enabled": False,
            "subscription_mode": "all_authorized",
        }
    ]
    assert report["created"] == []
