import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ReportsSettings } from "@/components/settings/ReportsSettings";
import {
  fetchReportingSettings,
  runReportingSettingsAction,
} from "@/lib/api";
import type { ReportingSettingsPayload } from "@/lib/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchReportingSettings: vi.fn(),
    fetchReportingSubscriptionOptions: vi.fn(),
    runReportingSettingsAction: vi.fn(),
  };
});

function payloadWithFlags(): ReportingSettingsPayload {
  return {
    catalog: {
      connectors: [],
      templates: [],
      load_errors: {},
    },
    policy: {
      rbac_enabled: false,
      management_enabled: true,
      guided_ui_enabled: true,
      button_policy_enabled: false,
      resource_types: [],
    },
    feature_flags: [
      { key: "cube_customer_model_hourly_tpm", label: "小时 TPM 报告", group: "小时 TPM", enabled: true, source: "default" },
      { key: "cube_multi_scope_brief", label: "多客户多模型日报简报", group: "用量报表", enabled: false, source: "override" },
    ],
    storage: { backend: "sqlite", retention_days: 30 },
    onboarding_version: 1,
    grants: [],
    recent_runs: [],
    subscriptions: [],
    template_policies: [],
  };
}

describe("ReportsSettings feature flags", () => {
  beforeEach(() => {
    vi.mocked(fetchReportingSettings).mockReset();
    vi.mocked(runReportingSettingsAction).mockReset();
  });

  it("toggles a flag instantly through the feature_flag action", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchReportingSettings).mockResolvedValue(payloadWithFlags());
    vi.mocked(runReportingSettingsAction).mockResolvedValue(payloadWithFlags());

    render(<ReportsSettings token="token" />);
    await user.click(await screen.findByRole("tab", { name: /功能开关/ }));

    // The label renders once as the row text and once as the switch's
    // accessible name.
    expect((await screen.findAllByText("小时 TPM 报告")).length).toBeGreaterThan(0);
    expect(screen.getByText("cube_customer_model_hourly_tpm · 默认值")).toBeInTheDocument();
    // Override-backed rows expose a reset affordance; default rows do not.
    expect(screen.getByTitle("恢复默认值")).toBeInTheDocument();

    await user.click(screen.getByRole("switch", { name: "小时 TPM 报告" }));
    await waitFor(() =>
      expect(runReportingSettingsAction).toHaveBeenCalledWith(
        "token",
        "feature_flag",
        { flag: "cube_customer_model_hourly_tpm", enabled: "false" },
      ),
    );
  });

  it("resets an override back to the configured default", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchReportingSettings).mockResolvedValue(payloadWithFlags());
    vi.mocked(runReportingSettingsAction).mockResolvedValue(payloadWithFlags());

    render(<ReportsSettings token="token" />);
    await user.click(await screen.findByRole("tab", { name: /功能开关/ }));
    await user.click(await screen.findByTitle("恢复默认值"));

    await waitFor(() =>
      expect(runReportingSettingsAction).toHaveBeenCalledWith(
        "token",
        "feature_flag_reset",
        { flag: "cube_multi_scope_brief" },
      ),
    );
  });
});
