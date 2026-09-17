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
    // Component chrome renders through i18n; the test environment runs the
    // default English locale, while the flag labels below come from the
    // server payload and stay as provided.
    await user.click(await screen.findByRole("tab", { name: /Feature flags/ }));

    // The label renders once as the row text and once as the switch's
    // accessible name.
    expect((await screen.findAllByText("小时 TPM 报告")).length).toBeGreaterThan(0);
    expect(screen.getByText("cube_customer_model_hourly_tpm · default")).toBeInTheDocument();
    // Override-backed rows expose a reset affordance; default rows do not.
    expect(screen.getByTitle("Reset to default")).toBeInTheDocument();

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
    await user.click(await screen.findByRole("tab", { name: /Feature flags/ }));
    await user.click(await screen.findByTitle("Reset to default"));

    await waitFor(() =>
      expect(runReportingSettingsAction).toHaveBeenCalledWith(
        "token",
        "feature_flag_reset",
        { flag: "cube_multi_scope_brief" },
      ),
    );
  });
});

describe("ReportsSettings delivery groups", () => {
  beforeEach(() => {
    vi.mocked(fetchReportingSettings).mockReset();
    vi.mocked(runReportingSettingsAction).mockReset();
  });

  it("badges group members and edits targets as one list", async () => {
    const user = userEvent.setup();
    const payload = payloadWithFlags();
    payload.subscriptions = [
      {
        subscription_id: "sub-0",
        channel: "feishu",
        chat_id: "chat-a",
        user_id: "ou-a",
        connector_id: "magik_cube",
        template_id: "usage_customer_model_hourly_tpm",
        template_version: "2.1",
        schedule: "5 9,10 * * *",
        timezone: "Asia/Shanghai",
        enabled: true,
        revision: 0,
        report_params: {},
        scope_summary: "佛跳墙 · 全部模型",
        schedule_label: "每天 9、10 点（整点后 5 分钟）",
        delivery_targets: [
          { subscription_id: "sub-0", chat_id: "chat-a", enabled: true, revision: 0 },
          { subscription_id: "sub-1", chat_id: "chat-b", enabled: true, revision: 0 },
        ],
        updated_at: "2026-09-17T00:00:00Z",
      },
    ];
    vi.mocked(fetchReportingSettings).mockResolvedValue(payload);

    render(<ReportsSettings token="token" />);
    await user.click(await screen.findByRole("tab", { name: /Subscriptions/ }));

    // Group members carry the shared target-count badge (2026-09-16).
    expect(await screen.findByText("Delivers to 2 chats")).toBeInTheDocument();

    // Editing loads the derived delivery target list into one field.
    await user.click(await screen.findByRole("button", { name: /Edit/ }));
    const targetsInput = await screen.findByLabelText(/Delivery chats/);
    expect((targetsInput as HTMLInputElement).value).toBe("chat-a、chat-b");
  });

  it("edits tenant name aliases and saves the whole table", async () => {
    const user = userEvent.setup();
    const payload = payloadWithFlags();
    payload.tenant_mappings = {
      values: { "阳春面": "tenant-a" },
      source: "default",
      default_values: { "阳春面": "tenant-a" },
    };
    vi.mocked(fetchReportingSettings).mockResolvedValue(payload);
    vi.mocked(runReportingSettingsAction).mockResolvedValue(payload);

    render(<ReportsSettings token="token" />);
    await user.click(await screen.findByRole("tab", { name: /Subscriptions/ }));

    // The configured default renders with its source badge and one row.
    expect(await screen.findByText("Customer name aliases")).toBeInTheDocument();
    expect(screen.getByText("Default (config.json)")).toBeInTheDocument();
    const aliasInput = screen.getByLabelText("Customer name");
    expect((aliasInput as HTMLInputElement).value).toBe("阳春面");

    // Saving posts the whole mapping through the update action.
    await user.click(screen.getByRole("button", { name: /Save aliases/ }));
    await waitFor(() =>
      expect(runReportingSettingsAction).toHaveBeenCalledWith(
        "token",
        "tenant_mappings_update",
        { tenant_mappings: { "阳春面": "tenant-a" } },
      ),
    );
  });
});
