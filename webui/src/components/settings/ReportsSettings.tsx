import { useCallback, useEffect, useState } from "react";
import { Database, Download, Loader2, RefreshCw, ShieldCheck } from "lucide-react";

import { ToggleButton } from "@/components/settings/ToggleButton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetchReportingSettings, runReportingSettingsAction } from "@/lib/api";
import type { ReportingSettingsPayload } from "@/lib/types";

const EMPTY_GRANT = {
  channel: "feishu",
  user_id: "",
  resource_type: "connector",
  resource_id: "magik_cube",
};

export function ReportsSettings({ token }: { token: string }) {
  const [payload, setPayload] = useState<ReportingSettingsPayload | null>(null);
  const [grant, setGrant] = useState(EMPTY_GRANT);
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (filters: Record<string, string> = {}) => {
    setLoading(true);
    try {
      setPayload(await fetchReportingSettings(token, filters));
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to load reporting settings");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { void load(); }, [load]);

  const run = async (
    nextAction: "rbac" | "grant" | "revoke" | "export",
    values: Record<string, string>,
  ) => {
    setAction(nextAction);
    setMessage(null);
    try {
      const result = await runReportingSettingsAction(token, nextAction, values);
      setPayload(result);
      setError(null);
      setMessage(
        nextAction === "export"
          ? `Catalog exported to ${result.last_action?.path ?? "report declarations"}`
          : "Reporting policy updated.",
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Reporting action failed");
    } finally {
      setAction(null);
    }
  };

  if (loading && !payload) {
    return <div className="flex h-40 items-center justify-center text-sm text-muted-foreground"><Loader2 className="mr-2 h-4 w-4 animate-spin" />Loading reports</div>;
  }

  return (
    <div className="space-y-7">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Report platform</h2>
          <p className="mt-1 text-sm text-muted-foreground">Connectors, deterministic templates, access policy, and declarations.</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => void load()} disabled={loading} title="Refresh report catalog">
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
          <Button variant="outline" size="sm" onClick={() => void run("export", {})} disabled={action !== null}>
            <Download className="h-4 w-4" />
            Export
          </Button>
        </div>
      </div>

      {error ? <div className="border-l-2 border-destructive px-3 py-2 text-sm text-destructive">{error}</div> : null}
      {message ? <div className="border-l-2 border-emerald-500 px-3 py-2 text-sm text-muted-foreground">{message}</div> : null}

      <section className="space-y-3">
        <h3 className="text-sm font-semibold">Access policy</h3>
        <div className="flex items-center justify-between border-y py-3">
          <div className="flex items-start gap-3">
            <ShieldCheck className="mt-0.5 h-4 w-4 text-muted-foreground" />
            <div>
              <div className="text-sm font-medium">Report RBAC</div>
              <div className="text-xs text-muted-foreground">When enabled, connector, template, tenant, and selected model grants are checked before querying.</div>
            </div>
          </div>
          <ToggleButton
            checked={Boolean(payload?.policy.rbac_enabled)}
            disabled={action !== null}
            label="Report RBAC"
            onChange={(enabled) => void run("rbac", { enabled: String(enabled) })}
          />
        </div>
      </section>

      <section className="space-y-3">
        <h3 className="text-sm font-semibold">Grant editor</h3>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          <Input value={grant.channel} onChange={(event) => setGrant({ ...grant, channel: event.target.value })} placeholder="Channel" aria-label="Channel" />
          <Input value={grant.user_id} onChange={(event) => setGrant({ ...grant, user_id: event.target.value })} placeholder="User open_id" aria-label="User ID" />
          <select
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
            value={grant.resource_type}
            onChange={(event) => setGrant({ ...grant, resource_type: event.target.value })}
            aria-label="Resource type"
          >
            {(payload?.policy.resource_types ?? []).map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
          <Input value={grant.resource_id} onChange={(event) => setGrant({ ...grant, resource_id: event.target.value })} placeholder="Resource ID or *" aria-label="Resource ID" />
        </div>
        <div className="flex gap-2">
          <Button size="sm" disabled={!grant.user_id || !grant.resource_id || action !== null} onClick={() => void run("grant", grant)}>Grant</Button>
          <Button variant="outline" size="sm" disabled={!grant.user_id || !grant.resource_id || action !== null} onClick={() => void run("revoke", grant)}>Revoke</Button>
          <Button variant="ghost" size="sm" disabled={!grant.user_id} onClick={() => void load({ channel: grant.channel, user_id: grant.user_id })}>Inspect user</Button>
        </div>
        {payload?.grants.length ? (
          <div className="divide-y border-y text-sm">
            {payload.grants.map((item) => (
              <div key={`${item.resource_type}:${item.resource_id}`} className="flex items-center justify-between py-2">
                <span>{item.resource_type}</span><code className="text-xs text-muted-foreground">{item.resource_id}</code>
              </div>
            ))}
          </div>
        ) : null}
      </section>

      <section className="space-y-3">
        <h3 className="text-sm font-semibold">Plugin catalog</h3>
        <div className="divide-y border-y">
          {(payload?.catalog.connectors ?? []).map((item) => (
            <div key={item.id} className="flex items-start gap-3 py-3">
              <Database className="mt-0.5 h-4 w-4 text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline justify-between gap-2"><span className="text-sm font-medium">{item.name}</span><code className="text-xs text-muted-foreground">{item.id} v{item.version}</code></div>
                <p className="mt-1 text-xs text-muted-foreground">{item.metrics.join(" · ")}</p>
              </div>
            </div>
          ))}
          {(payload?.catalog.templates ?? []).map((item) => (
            <div key={item.id} className="py-3">
              <div className="flex flex-wrap items-baseline justify-between gap-2"><span className="text-sm font-medium">{item.name}</span><code className="text-xs text-muted-foreground">{item.id} v{item.version}</code></div>
              <p className="mt-1 text-xs text-muted-foreground">{item.description}</p>
            </div>
          ))}
        </div>
        {Object.keys(payload?.catalog.load_errors ?? {}).length ? (
          <div className="text-xs text-destructive">Some report plugins failed to load. Other plugins remain available.</div>
        ) : null}
      </section>

      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Database className="h-3.5 w-3.5" />
        {payload?.storage.backend} · {payload?.storage.retention_days}-day run retention · onboarding v{payload?.onboarding_version}
      </div>
    </div>
  );
}
