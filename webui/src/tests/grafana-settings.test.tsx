import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GrafanaSettings } from "@/components/settings/GrafanaSettings";
import { fetchGrafanaSettings, runGrafanaSettingsAction } from "@/lib/api";
import type { GrafanaSettingsPayload } from "@/lib/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchGrafanaSettings: vi.fn(),
    runGrafanaSettingsAction: vi.fn(),
  };
});

function connectionRow(overrides: Partial<GrafanaSettingsPayload["connections"][number]> = {}) {
  return {
    slug: "prod",
    server_name: "grafana-prod",
    base_url: "https://grafana.example.com",
    org_id: "",
    enabled: true,
    managed: true,
    mode: "read",
    write_tools: [],
    write_enabled: false,
    package: "mcp-grafana@1.6.2",
    token_hint: "glsa_ab••••3f2d",
    token_source: "value",
    token_env_available: null,
    token_configured: true,
    tools: ["user_info", "search_dashboards"],
    tool_count: 11,
    ...overrides,
  };
}

function payloadWith(rows: GrafanaSettingsPayload["connections"] = []): GrafanaSettingsPayload {
  return {
    connections: rows,
    read_only_tools: Array.from({ length: 30 }, (_, index) => `read_tool_${index}`),
    write_tools_catalog: ["update_dashboard", "alerting_manage_silences"],
    package: "mcp-grafana@1.6.2",
    read_only: true,
    uvx_available: true,
  };
}

describe("GrafanaSettings", () => {
  beforeEach(() => {
    vi.mocked(fetchGrafanaSettings).mockReset();
    vi.mocked(runGrafanaSettingsAction).mockReset();
  });

  it("lists connections with read-only and enabled pills", async () => {
    vi.mocked(fetchGrafanaSettings).mockResolvedValue(
      payloadWith([connectionRow()]),
    );

    render(<GrafanaSettings token="token" />);

    expect(await screen.findByText("prod")).toBeInTheDocument();
    expect(screen.getByText("Read-only")).toBeInTheDocument();
    expect(screen.getByText("Enabled")).toBeInTheDocument();
    // Redacted hint renders; the payload has no raw token to leak anyway.
    expect(screen.getByText(/glsa_ab••••3f2d/)).toBeInTheDocument();
    expect(screen.getByText(/token glsa_ab••••3f2d · 11 tools/)).toBeInTheDocument();
  });

  it("flags unmanaged entries and keeps their actions disabled", async () => {
    vi.mocked(fetchGrafanaSettings).mockResolvedValue(
      payloadWith([connectionRow({ slug: "legacy", managed: false, enabled: false })]),
    );

    render(<GrafanaSettings token="token" />);

    expect(await screen.findByText("Hand-edited")).toBeInTheDocument();
    const test = await screen.findByRole("button", { name: /^Test/ });
    expect(test).toBeDisabled();
    expect(screen.getByRole("button", { name: "Edit" })).toBeDisabled();
    // Delete stays available — a hand-edited entry can still be removed.
    expect(screen.getByRole("button", { name: "Delete legacy" })).toBeEnabled();
  });

  it("warns when uvx is missing on the gateway host", async () => {
    const payload = payloadWith();
    payload.uvx_available = false;
    vi.mocked(fetchGrafanaSettings).mockResolvedValue(payload);

    render(<GrafanaSettings token="token" />);

    expect(
      await screen.findByText(/uvx is not on the nanobot PATH/),
    ).toBeInTheDocument();
  });

  it("creates a connection through the editor with a pre-save test", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchGrafanaSettings).mockResolvedValue(payloadWith());
    vi.mocked(runGrafanaSettingsAction)
      .mockResolvedValueOnce({
        ...payloadWith(),
        last_action: { ok: true, message: "'prod' connected as svc-bot (org 1)." },
      })
      .mockResolvedValueOnce({
        ...payloadWith([connectionRow()]),
        last_action: { ok: true, action: "create", slug: "prod", message: "Added Grafana connection 'prod'." },
      });

    render(<GrafanaSettings token="token" />);
    await user.click(await screen.findByRole("button", { name: /Add connection/ }));

    await user.type(screen.getByLabelText(/Connection ID/), "Prod");
    await user.type(screen.getByLabelText(/Grafana URL/), "https://grafana.example.com");
    await user.type(
      screen.getByLabelText(/Service account token/),
      "glsa-typed-token",
    );
    await user.type(screen.getByLabelText(/Organization ID/), "1");

    // Pre-save test runs the test action with the typed values, then save
    // creates; the slug is normalized to lowercase on submit.
    await user.click(screen.getByRole("button", { name: /Test before saving/ }));
    await waitFor(() =>
      expect(runGrafanaSettingsAction).toHaveBeenCalledWith("token", "test", {
        slug: "prod",
        base_url: "https://grafana.example.com",
        token: "glsa-typed-token",
        org_id: "1",
      }),
    );
    expect(await screen.findByText(/connected as svc-bot/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Save connection/ }));
    await waitFor(() =>
      expect(runGrafanaSettingsAction).toHaveBeenCalledWith("token", "create", {
        slug: "prod",
        base_url: "https://grafana.example.com",
        token: "glsa-typed-token",
        org_id: "1",
        write_tools: [],
      }),
    );
    // Editor closed after a successful save; the row now renders.
    expect(await screen.findByText("Added Grafana connection 'prod'.")).toBeInTheDocument();
    expect(screen.queryByLabelText(/Connection ID/)).not.toBeInTheDocument();
  });

  it("toggles a connection without opening the editor", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchGrafanaSettings).mockResolvedValue(
      payloadWith([connectionRow()]),
    );
    vi.mocked(runGrafanaSettingsAction).mockResolvedValue(
      payloadWith([connectionRow({ enabled: false })]),
    );

    render(<GrafanaSettings token="token" />);

    await user.click(await screen.findByRole("switch", { name: "prod enabled state" }));

    await waitFor(() =>
      expect(runGrafanaSettingsAction).toHaveBeenCalledWith("token", "update", {
        slug: "prod",
        enabled: "false",
      }),
    );
    expect(await screen.findByText("Disabled")).toBeInTheDocument();
  });

  it("deletes a connection after confirmation", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchGrafanaSettings).mockResolvedValue(
      payloadWith([connectionRow()]),
    );
    vi.mocked(runGrafanaSettingsAction).mockResolvedValue(payloadWith());
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<GrafanaSettings token="token" />);

    await user.click(await screen.findByRole("button", { name: "Delete prod" }));

    await waitFor(() =>
      expect(runGrafanaSettingsAction).toHaveBeenCalledWith("token", "delete", {
        slug: "prod",
      }),
    );
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("prod"));
    expect(await screen.findByText(/No Grafana connections yet/)).toBeInTheDocument();
    confirmSpy.mockRestore();
  });

  it("edits without resubmitting the stored token", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchGrafanaSettings).mockResolvedValue(
      payloadWith([connectionRow()]),
    );
    vi.mocked(runGrafanaSettingsAction).mockResolvedValue(
      payloadWith([connectionRow({ org_id: "7" })]),
    );

    render(<GrafanaSettings token="token" />);
    await user.click(await screen.findByRole("button", { name: "Edit" }));

    const tokenField = screen.getByLabelText(/Service account token/);
    // Empty token field means "keep the stored value" (sentinel semantics).
    expect(tokenField).toHaveValue("");
    await user.clear(screen.getByLabelText(/Organization ID/));
    await user.click(screen.getByRole("button", { name: /Save connection/ }));

    await waitFor(() =>
      expect(runGrafanaSettingsAction).toHaveBeenCalledWith("token", "update", {
        slug: "prod",
        base_url: "https://grafana.example.com",
        token: "",
        org_id: "",
        write_tools: [],
      }),
    );
  });

  it("badges write-mode rows and keeps read-only rows clean", async () => {
    vi.mocked(fetchGrafanaSettings).mockResolvedValue(
      payloadWith([
        connectionRow(),
        connectionRow({
          slug: "ops",
          mode: "write",
          write_tools: ["update_dashboard", "alerting_manage_silences"],
          write_enabled: true,
        }),
      ]),
    );

    render(<GrafanaSettings token="token" />);

    expect(await screen.findByText("Write × 2")).toBeInTheDocument();
    expect(screen.getByTitle(/Editor-or-higher token/)).toBeInTheDocument();
    // The read-only row shows no write badge.
    const prod = screen.getByText("prod").closest("div");
    expect(prod?.textContent ?? "").not.toContain("Write ×");
  });

  it("enables write mode from the editor with a warning", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchGrafanaSettings).mockResolvedValue(payloadWith());
    vi.mocked(runGrafanaSettingsAction).mockResolvedValue(
      payloadWith([connectionRow({
        mode: "write",
        write_tools: ["update_dashboard"],
        write_enabled: true,
      })]),
    );

    render(<GrafanaSettings token="token" />);
    await user.click(await screen.findByRole("button", { name: /Add connection/ }));
    await user.type(screen.getByLabelText(/Connection ID/), "prod");
    await user.type(screen.getByLabelText(/Grafana URL/), "https://grafana.example.com");
    await user.type(
      screen.getByLabelText(/Service account token/),
      "glsa-typed-token",
    );

    // No warning before any write tool is selected.
    expect(screen.queryByText(/Write mode enabled/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /update_dashboard/ }));
    expect(await screen.findByText(/Write mode enabled/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Save connection/ }));
    await waitFor(() =>
      expect(runGrafanaSettingsAction).toHaveBeenCalledWith("token", "create", {
        slug: "prod",
        base_url: "https://grafana.example.com",
        token: "glsa-typed-token",
        org_id: "",
        write_tools: ["update_dashboard"],
      }),
    );
  });

  it("unchecking every write tool returns the connection to read-only", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchGrafanaSettings).mockResolvedValue(
      payloadWith([connectionRow({
        mode: "write",
        write_tools: ["update_dashboard"],
        write_enabled: true,
      })]),
    );
    vi.mocked(runGrafanaSettingsAction).mockResolvedValue(
      payloadWith([connectionRow()]),
    );

    render(<GrafanaSettings token="token" />);
    await user.click(await screen.findByRole("button", { name: "Edit" }));

    const checkbox = screen.getByRole("checkbox", { name: /update_dashboard/ });
    expect(checkbox).toBeChecked();
    await user.click(checkbox);
    await user.click(screen.getByRole("button", { name: /Save connection/ }));

    await waitFor(() =>
      expect(runGrafanaSettingsAction).toHaveBeenCalledWith("token", "update", {
        slug: "prod",
        base_url: "https://grafana.example.com",
        token: "",
        org_id: "",
        write_tools: [],
      }),
    );
  });
});
