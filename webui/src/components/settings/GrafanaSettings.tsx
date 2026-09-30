import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Loader2,
  Pencil,
  PlugZap,
  Plus,
  Trash2,
  X,
} from "lucide-react";

import { ToggleButton } from "@/components/settings/ToggleButton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetchGrafanaSettings, runGrafanaSettingsAction } from "@/lib/api";
import type { GrafanaConnection, GrafanaSettingsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

const FIELD_LABEL_CLASS = "grid gap-1 text-xs font-medium text-muted-foreground";

type EditorState = {
  mode: "create" | "edit";
  slug: string;
  baseUrl: string;
  token: string;
  orgId: string;
};

function emptyEditor(): EditorState {
  return { mode: "create", slug: "", baseUrl: "", token: "", orgId: "" };
}

function editorFromConnection(item: GrafanaConnection): EditorState {
  return {
    mode: "edit",
    slug: item.slug,
    baseUrl: item.base_url,
    // Never prefill the stored token; the placeholder shows its redacted hint.
    token: "",
    orgId: item.org_id,
  };
}

function ConnectionRow({
  item,
  busy,
  testing,
  onTest,
  onEdit,
  onToggle,
  onDelete,
}: {
  item: GrafanaConnection;
  busy: boolean;
  testing: boolean;
  onTest: (item: GrafanaConnection) => void;
  onEdit: (item: GrafanaConnection) => void;
  onToggle: (item: GrafanaConnection) => void;
  onDelete: (item: GrafanaConnection) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="grid gap-4 border-b border-border/50 py-4 last:border-b-0 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="break-words text-sm font-medium text-foreground">{item.slug}</span>
          <span className={cn(
            "rounded-full border px-2 py-0.5 text-xs",
            item.enabled
              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
              : "border-border text-muted-foreground",
          )}>
            {item.enabled
              ? t("settings.grafana.row.enabled", { defaultValue: "Enabled" })
              : t("settings.grafana.row.disabled", { defaultValue: "Disabled" })}
          </span>
          <span className="rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-xs text-sky-700 dark:text-sky-300">
            {t("settings.grafana.row.readOnly", { defaultValue: "Read-only" })}
          </span>
          {!item.managed ? (
            <span
              className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-xs text-amber-700 dark:text-amber-300"
              title={t("settings.grafana.row.unmanagedTitle", { defaultValue: "This grafana-* config.json entry was hand-edited outside this page; delete and re-create it here to manage it." })}
            >
              {t("settings.grafana.row.unmanaged", { defaultValue: "Hand-edited" })}
            </span>
          ) : null}
        </div>
        <p className="mt-1 break-all text-xs text-muted-foreground">{item.base_url}</p>
        <p className="mt-1 text-xs text-muted-foreground">
          {t("settings.grafana.row.meta", {
            defaultValue: `token ${item.token_hint ?? "—"} · ${item.tool_count} tools · ${item.package}`,
            hint: item.token_hint ?? "—",
            count: item.tool_count,
            package: item.package,
          })}
          {item.token_source === "env" && item.token_env_available === false
            ? ` · ${t("settings.grafana.row.envMissing", { defaultValue: "referenced env var is not set" })}`
            : ""}
        </p>
      </div>
      <div className="flex flex-wrap items-center justify-start gap-2 lg:justify-end">
        <Button
          size="sm"
          variant="outline"
          disabled={busy || !item.managed}
          title={!item.managed
            ? t("settings.grafana.row.unmanagedTitle", { defaultValue: "This grafana-* config.json entry was hand-edited outside this page; delete and re-create it here to manage it." })
            : t("settings.grafana.row.testTitle", { defaultValue: "Spawn the connection and verify the token end-to-end", slug: item.slug })}
          onClick={() => onTest(item)}
        >
          {testing ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <PlugZap className="mr-1.5 h-3.5 w-3.5" />}
          {t("settings.grafana.row.test", { defaultValue: "Test" })}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={busy || !item.managed}
          onClick={() => onEdit(item)}
        >
          <Pencil className="mr-1.5 h-3.5 w-3.5" />
          {t("settings.grafana.row.edit", { defaultValue: "Edit" })}
        </Button>
        <ToggleButton
          checked={item.enabled}
          disabled={busy || !item.managed}
          label={t("settings.grafana.row.toggleAria", { defaultValue: "{{slug}} enabled state", slug: item.slug })}
          onChange={() => onToggle(item)}
        />
        <Button
          size="icon"
          variant="outline"
          disabled={busy}
          className="text-destructive hover:bg-destructive/10 hover:text-destructive"
          title={t("settings.grafana.row.deleteTitle", { defaultValue: "Delete {{slug}}", slug: item.slug })}
          aria-label={t("settings.grafana.row.deleteTitle", { defaultValue: "Delete {{slug}}", slug: item.slug })}
          onClick={() => onDelete(item)}
        >
          <Trash2 className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

export function GrafanaSettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const [payload, setPayload] = useState<GrafanaSettingsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [testingSlug, setTestingSlug] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [messageOk, setMessageOk] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [editorBusy, setEditorBusy] = useState(false);
  const [editorTest, setEditorTest] = useState<{ ok: boolean; message: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await fetchGrafanaSettings(token);
      setPayload(result);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("settings.grafana.loadFailed", { defaultValue: "Failed to load Grafana connections" }));
    } finally {
      setLoading(false);
    }
  }, [token, t]);

  useEffect(() => { void load(); }, [load]);

  // Every action response carries the full connections payload, so a
  // completed action doubles as the refresh.
  const applyResult = (result: GrafanaSettingsPayload) => {
    setPayload(result);
    const last = result.last_action;
    if (last && typeof last.message === "string" && last.message) {
      setMessage(last.message);
      setMessageOk(Boolean(last.ok));
    } else {
      setMessage(t("settings.grafana.actionApplied", { defaultValue: "Change applied without restarting nanobot." }));
      setMessageOk(true);
    }
  };

  const run = async (
    action: "create" | "update" | "delete" | "test",
    values: Record<string, unknown>,
    opts: { asTest?: boolean } = {},
  ) => {
    setBusy(true);
    setError(null);
    if (opts.asTest) setTestingSlug(String(values.slug ?? ""));
    try {
      const result = await runGrafanaSettingsAction(token, action, values);
      applyResult(result);
      return result;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("settings.grafana.actionFailed", { defaultValue: "Grafana connection action failed" }));
      return null;
    } finally {
      setBusy(false);
      if (opts.asTest) setTestingSlug(null);
    }
  };

  const runEditorTest = async () => {
    if (!editor) return;
    setEditorBusy(true);
    setEditorTest(null);
    try {
      const result = await runGrafanaSettingsAction(token, "test", {
        slug: editor.slug.trim().toLowerCase(),
        base_url: editor.baseUrl.trim(),
        token: editor.token.trim(),
        org_id: editor.orgId.trim(),
      });
      const last = result.last_action;
      setEditorTest({
        ok: Boolean(last?.ok),
        message: last?.message ?? t("settings.grafana.test.noResult", { defaultValue: "Test finished without a result message." }),
      });
    } catch (reason) {
      setEditorTest({
        ok: false,
        message: reason instanceof Error ? reason.message : t("settings.grafana.actionFailed", { defaultValue: "Grafana connection action failed" }),
      });
    } finally {
      setEditorBusy(false);
    }
  };

  const submitEditor = () => {
    if (!editor) return;
    const values = {
      slug: editor.slug.trim().toLowerCase(),
      base_url: editor.baseUrl.trim(),
      token: editor.token.trim(),
      org_id: editor.orgId.trim(),
    };
    const finish = () => {
      setEditor(null);
      setEditorTest(null);
    };
    if (editor.mode === "create") {
      void run("create", values).then((result) => { if (result) finish(); });
    } else {
      void run("update", values).then((result) => { if (result) finish(); });
    }
  };

  const deleteConnection = (item: GrafanaConnection) => {
    if (!window.confirm(t("settings.grafana.deleteConfirm", {
      defaultValue: "Delete Grafana connection \"{{slug}}\"? The chat assistant immediately loses its tools.",
      slug: item.slug,
    }))) {
      return;
    }
    void run("delete", { slug: item.slug });
  };

  if (loading && !payload) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        {t("settings.grafana.loading", { defaultValue: "Loading Grafana connections" })}
      </div>
    );
  }

  const connections = payload?.connections ?? [];
  const readOnlyTools = payload?.read_only_tools ?? [];
  const editingItem = editor?.mode === "edit"
    ? connections.find((item) => item.slug === editor.slug)
    : undefined;
  const canSave = Boolean(
    editor
    && editor.slug.trim()
    && editor.baseUrl.trim()
    && (editor.mode === "edit" || editor.token.trim()),
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Grafana</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {t("settings.grafana.subtitle", { defaultValue: "Manage read-only Grafana platform connections for the chat assistant." })}
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            {t("settings.grafana.readOnlyNote", {
              defaultValue: `Phase 1 is read-only: every connection runs {{package}} with --disable-write and a curated tool list ({{count}} tools).`,
              package: payload?.package ?? "",
              count: readOnlyTools.length,
            })}
          </p>
        </div>
        <Button
          size="sm"
          onClick={() => { setEditor(emptyEditor()); setEditorTest(null); }}
          disabled={editor !== null || busy}
        >
          <Plus className="mr-1.5 h-4 w-4" />
          {t("settings.grafana.add", { defaultValue: "Add connection" })}
        </Button>
      </div>

      {payload && !payload.uvx_available ? (
        <div className="rounded-[14px] border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-[13px] text-amber-700 dark:text-amber-300">
          {t("settings.grafana.uvxMissing", { defaultValue: "uvx is not on the nanobot PATH. Install uv (https://docs.astral.sh/uv/) on the gateway host before testing connections." })}
        </div>
      ) : null}

      {error ? (
        <div className="rounded-[14px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}
      {message && !error ? (
        <div className={cn(
          "rounded-[14px] border px-4 py-3 text-[13px]",
          messageOk
            ? "border-emerald-500/25 bg-emerald-500/5 text-emerald-700 dark:text-emerald-300"
            : "border-destructive/20 bg-destructive/5 text-destructive",
        )}>
          {message}
        </div>
      ) : null}

      {editor ? (
        <div className="rounded-[18px] border border-border bg-settings-surface p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold text-foreground">
              {editor.mode === "create"
                ? t("settings.grafana.editor.createTitle", { defaultValue: "Add Grafana connection" })
                : t("settings.grafana.editor.editTitle", { defaultValue: "Edit {{slug}}", slug: editor.slug })}
            </h3>
            <Button
              size="icon"
              variant="ghost"
              onClick={() => { setEditor(null); setEditorTest(null); }}
              disabled={editorBusy}
              aria-label={t("settings.grafana.editor.cancel", { defaultValue: "Cancel" })}
            >
              <X className="h-4 w-4" />
            </Button>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <label className={FIELD_LABEL_CLASS}>
              {t("settings.grafana.editor.slug", { defaultValue: "Connection ID" })}
              <Input
                value={editor.slug}
                onChange={(event) => setEditor({ ...editor, slug: event.target.value })}
                placeholder="prod"
                disabled={editor.mode === "edit" || editorBusy}
                autoComplete="off"
              />
              <span className="text-[11px] font-normal text-muted-foreground">
                {t("settings.grafana.editor.slugHelp", { defaultValue: "Lowercase letters, digits, '-' and '_'. Chat tools appear as mcp_grafana-<id>_… ." })}
              </span>
            </label>
            <label className={FIELD_LABEL_CLASS}>
              {t("settings.grafana.editor.baseUrl", { defaultValue: "Grafana URL" })}
              <Input
                value={editor.baseUrl}
                onChange={(event) => setEditor({ ...editor, baseUrl: event.target.value })}
                placeholder="https://grafana.example.com"
                disabled={editorBusy}
                autoComplete="off"
              />
            </label>
            <label className={FIELD_LABEL_CLASS}>
              {t("settings.grafana.editor.token", { defaultValue: "Service account token" })}
              <Input
                type="password"
                value={editor.token}
                onChange={(event) => setEditor({ ...editor, token: event.target.value })}
                placeholder={editingItem?.token_hint ?? "glsa_…"}
                disabled={editorBusy}
                autoComplete="new-password"
              />
              <span className="text-[11px] font-normal text-muted-foreground">
                {editor.mode === "edit"
                  ? t("settings.grafana.editor.tokenKeep", {
                    defaultValue: `Leave empty to keep the current token (${editingItem?.token_hint ?? "—"}).`,
                    hint: editingItem?.token_hint ?? "—",
                  })
                  : t("settings.grafana.editor.tokenHelp", { defaultValue: "Viewer-role service account token; stored locally in config.json and shown only as a hint afterwards." })}
              </span>
            </label>
            <label className={FIELD_LABEL_CLASS}>
              {t("settings.grafana.editor.orgId", { defaultValue: "Organization ID (optional)" })}
              <Input
                value={editor.orgId}
                onChange={(event) => setEditor({ ...editor, orgId: event.target.value })}
                placeholder="1"
                disabled={editorBusy}
                autoComplete="off"
              />
            </label>
          </div>
          {editorTest ? (
            <div className={cn(
              "mt-3 rounded-[14px] border px-3 py-2 text-xs",
              editorTest.ok
                ? "border-emerald-500/25 bg-emerald-500/5 text-emerald-700 dark:text-emerald-300"
                : "border-destructive/20 bg-destructive/5 text-destructive",
            )}>
              {editorTest.message}
            </div>
          ) : null}
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={editorBusy || !editor.slug.trim() || !editor.baseUrl.trim()}
              onClick={() => void runEditorTest()}
            >
              {editorBusy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <PlugZap className="mr-1.5 h-3.5 w-3.5" />}
              {t("settings.grafana.editor.test", { defaultValue: "Test before saving" })}
            </Button>
            <Button size="sm" disabled={editorBusy || !canSave} onClick={submitEditor}>
              {t("settings.grafana.editor.save", { defaultValue: "Save connection" })}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={editorBusy}
              onClick={() => { setEditor(null); setEditorTest(null); }}
            >
              {t("settings.grafana.editor.cancel", { defaultValue: "Cancel" })}
            </Button>
          </div>
        </div>
      ) : null}

      <div>
        <h3 className="mb-1 text-sm font-semibold text-foreground">
          {t("settings.grafana.connectionsTitle", { defaultValue: "Connections" })}
        </h3>
        {connections.length === 0 ? (
          <p className="py-6 text-sm text-muted-foreground">
            {t("settings.grafana.empty", { defaultValue: "No Grafana connections yet. Add one to give the chat assistant read-only Grafana tools." })}
          </p>
        ) : (
          <div>
            {connections.map((item) => (
              <ConnectionRow
                key={item.slug}
                item={item}
                busy={busy}
                testing={testingSlug === item.slug}
                onTest={(connection) => void run("test", { slug: connection.slug }, { asTest: true })}
                onEdit={(connection) => { setEditor(editorFromConnection(connection)); setEditorTest(null); }}
                onToggle={(connection) => void run("update", {
                  slug: connection.slug,
                  enabled: String(!connection.enabled),
                })}
                onDelete={deleteConnection}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
