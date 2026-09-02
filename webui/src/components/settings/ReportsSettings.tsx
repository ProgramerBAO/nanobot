import { useCallback, useEffect, useState } from "react";
import {
  CalendarClock,
  Database,
  Download,
  FileBarChart,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
} from "lucide-react";

import { ToggleButton } from "@/components/settings/ToggleButton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetchReportingSettings, runReportingSettingsAction } from "@/lib/api";
import type {
  ReportingSettingsPayload,
  ReportingSubscription,
  ReportingTemplatePolicy,
} from "@/lib/types";
import { cn } from "@/lib/utils";

type ReportAction =
  | "rbac"
  | "grant"
  | "revoke"
  | "export"
  | "template_policy"
  | "subscription_create"
  | "subscription_enable"
  | "subscription_disable"
  | "subscription_schedule"
  | "subscription_delete";

const EMPTY_GRANT = {
  channel: "feishu",
  user_id: "",
  resource_type: "connector",
  resource_id: "magik_cube",
};

const EMPTY_SUBSCRIPTION = {
  template_id: "",
  channel: "feishu",
  chat_id: "",
  user_id: "",
  schedule: "0 9 * * *",
  timezone: "Asia/Shanghai",
  report_params_json: "{}",
};

function TemplatePolicyRow({
  item,
  busy,
  onSave,
}: {
  item: ReportingTemplatePolicy;
  busy: boolean;
  onSave: (values: Record<string, string>) => void;
}) {
  const [enabled, setEnabled] = useState(item.enabled);
  const [mode, setMode] = useState(item.subscription_mode);
  useEffect(() => { setEnabled(item.enabled); setMode(item.subscription_mode); }, [item]);
  return (
    <div className="grid gap-3 py-4 lg:grid-cols-[minmax(0,1fr)_auto_auto] lg:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-sm font-medium">{item.name}</span>
          <code className="text-xs text-muted-foreground">{item.id} v{item.version}</code>
        </div>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{item.description}</p>
        <p className="mt-1 text-xs text-muted-foreground">周期：{item.periods.join(" / ")} · 计算代码只读</p>
      </div>
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        订阅受众
        <select
          className="h-9 border border-input bg-background px-2 text-sm text-foreground"
          value={mode}
          onChange={(event) => setMode(event.target.value as ReportingTemplatePolicy["subscription_mode"])}
          aria-label={`${item.name} 订阅受众`}
        >
          <option value="all_authorized">已授权用户</option>
          <option value="allowlist">订阅白名单</option>
          <option value="disabled">禁止新订阅</option>
        </select>
      </label>
      <div className="flex items-center gap-3">
        <ToggleButton checked={enabled} disabled={busy} label={`${item.name} 启用状态`} onChange={setEnabled} />
        <Button
          size="icon"
          variant="outline"
          title="保存报表策略"
          disabled={busy || (enabled === item.enabled && mode === item.subscription_mode)}
          onClick={() => onSave({
            template_id: item.id,
            enabled: String(enabled),
            subscription_mode: mode,
            revision: String(item.revision),
          })}
        >
          <Save className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

function SubscriptionRow({
  item,
  busy,
  onAction,
}: {
  item: ReportingSubscription;
  busy: boolean;
  onAction: (action: ReportAction, values: Record<string, string>) => void;
}) {
  const [schedule, setSchedule] = useState(item.schedule);
  const [timezone, setTimezone] = useState(item.timezone);
  useEffect(() => { setSchedule(item.schedule); setTimezone(item.timezone); }, [item]);
  return (
    <div className="grid gap-3 py-4 xl:grid-cols-[minmax(0,1fr)_180px_170px_auto] xl:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{item.template_id}</span>
          <span className={cn(
            "border px-2 py-0.5 text-xs",
            item.enabled ? "border-emerald-500/30 text-emerald-700 dark:text-emerald-300" : "border-border text-muted-foreground",
          )}>{item.enabled ? "启用" : "停用"}</span>
        </div>
        <p className="mt-1 break-all text-xs text-muted-foreground">
          {item.channel} · {item.user_id} · {item.chat_id} · {item.subscription_id}
        </p>
      </div>
      <Input value={schedule} onChange={(event) => setSchedule(event.target.value)} aria-label="Cron 表达式" />
      <Input value={timezone} onChange={(event) => setTimezone(event.target.value)} aria-label="订阅时区" />
      <div className="flex gap-2">
        <Button
          size="icon"
          variant="outline"
          title="保存发送计划"
          disabled={busy || (schedule === item.schedule && timezone === item.timezone)}
          onClick={() => onAction("subscription_schedule", { subscription_id: item.subscription_id, schedule, timezone })}
        ><Save className="h-4 w-4" /></Button>
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => onAction(item.enabled ? "subscription_disable" : "subscription_enable", { subscription_id: item.subscription_id })}
        >{item.enabled ? "停用" : "启用"}</Button>
        <Button
          size="icon"
          variant="ghost"
          title="删除订阅"
          disabled={busy}
          onClick={() => {
            if (window.confirm(`确认删除订阅 ${item.subscription_id}？历史运行记录会保留。`)) {
              onAction("subscription_delete", { subscription_id: item.subscription_id });
            }
          }}
        ><Trash2 className="h-4 w-4 text-destructive" /></Button>
      </div>
    </div>
  );
}

export function ReportsSettings({ token }: { token: string }) {
  const [payload, setPayload] = useState<ReportingSettingsPayload | null>(null);
  const [tab, setTab] = useState<"templates" | "subscriptions" | "permissions">("templates");
  const [grant, setGrant] = useState(EMPTY_GRANT);
  const [subscription, setSubscription] = useState(EMPTY_SUBSCRIPTION);
  const [showCreate, setShowCreate] = useState(false);
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (filters: Record<string, string> = {}) => {
    setLoading(true);
    try {
      const result = await fetchReportingSettings(token, filters);
      setPayload(result);
      setError(null);
      setSubscription((current) => ({
        ...current,
        template_id: current.template_id || result.template_policies[0]?.id || "",
      }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "加载报表平台失败");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { void load(); }, [load]);

  const run = async (nextAction: ReportAction, values: Record<string, string>) => {
    setAction(nextAction);
    setMessage(null);
    try {
      const result = await runReportingSettingsAction(token, nextAction, values);
      setPayload(result);
      setError(null);
      setMessage(nextAction === "export" ? `Catalog exported to ${result.last_action?.path ?? "report declarations"}` : "操作已生效。");
      if (nextAction === "subscription_create") setShowCreate(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "报表管理操作失败");
    } finally {
      setAction(null);
    }
  };

  if (loading && !payload) {
    return <div className="flex h-40 items-center justify-center text-sm text-muted-foreground"><Loader2 className="mr-2 h-4 w-4 animate-spin" />加载报表平台</div>;
  }

  const tabs = [
    { id: "templates" as const, label: "报表类型", icon: FileBarChart },
    { id: "subscriptions" as const, label: "订阅管理", icon: CalendarClock },
    { id: "permissions" as const, label: "权限管理", icon: ShieldCheck },
  ];
  const managementEnabled = Boolean(payload?.policy.management_enabled);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h2 className="text-lg font-semibold">Report platform</h2><p className="mt-1 text-sm text-muted-foreground">统一管理确定性报表、订阅计划和访问范围。</p></div>
        <div className="flex gap-2">
          <Button variant="outline" size="icon" onClick={() => void load()} disabled={loading} title="刷新"><RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} /></Button>
          <Button variant="outline" size="icon" onClick={() => void run("export", {})} disabled={action !== null} title="导出声明"><Download className="h-4 w-4" /></Button>
        </div>
      </div>
      {error ? <div className="border-l-2 border-destructive px-3 py-2 text-sm text-destructive">{error}</div> : null}
      {message ? <div className="border-l-2 border-emerald-500 px-3 py-2 text-sm text-muted-foreground">{message}</div> : null}
      {!managementEnabled ? <div className="border-l-2 border-amber-500 px-3 py-2 text-sm text-muted-foreground">报表管理 feature flag 尚未启用；当前页面保持只读，原 reporting 设置接口继续可用。</div> : null}

      <div className="flex gap-1 border-b" role="tablist" aria-label="报表管理视图">
        {tabs.map((item) => <button key={item.id} type="button" role="tab" aria-selected={tab === item.id} onClick={() => setTab(item.id)} className={cn("inline-flex h-10 items-center gap-2 border-b-2 px-3 text-sm", tab === item.id ? "border-primary font-medium text-foreground" : "border-transparent text-muted-foreground hover:text-foreground")}><item.icon className="h-4 w-4" />{item.label}</button>)}
      </div>

      {tab === "templates" ? <section className="space-y-3">
        <div><h3 className="text-sm font-semibold">报表类型</h3><p className="mt-1 text-xs text-muted-foreground">仅管理启用状态与订阅受众；模板代码、公式和接口路径保持只读。</p></div>
        <div className="divide-y border-y">
          {(payload?.template_policies ?? []).map((item) => <TemplatePolicyRow key={item.id} item={item} busy={!managementEnabled || action !== null} onSave={(values) => void run("template_policy", values)} />)}
        </div>
        <div className="space-y-2 pt-2"><h3 className="text-sm font-semibold">Connector 状态</h3>{(payload?.catalog.connectors ?? []).map((item) => <div key={item.id} className="flex items-start gap-3 border-t py-3"><Database className="mt-0.5 h-4 w-4 text-muted-foreground" /><div className="min-w-0"><div className="text-sm font-medium">{item.name}</div><code className="text-xs text-muted-foreground">{item.id} v{item.version} · {item.read_only ? "只读" : "可写"}</code></div></div>)}</div>
      </section> : null}

      {tab === "subscriptions" ? <section className="space-y-4">
        <div className="flex items-start justify-between gap-3"><div><h3 className="text-sm font-semibold">订阅管理</h3><p className="mt-1 text-xs text-muted-foreground">计划变更会同步到 Cron；停用和删除不会修改历史运行记录。</p></div><Button size="sm" disabled={!managementEnabled || action !== null} onClick={() => setShowCreate((value) => !value)}><Plus className="h-4 w-4" />新建订阅</Button></div>
        {showCreate ? <div className="grid gap-3 border-y py-4 sm:grid-cols-2 xl:grid-cols-3">
          <label className="grid gap-1 text-xs text-muted-foreground">报表类型<select className="h-9 border border-input bg-background px-2 text-sm text-foreground" value={subscription.template_id} onChange={(event) => setSubscription({ ...subscription, template_id: event.target.value })}>{(payload?.template_policies ?? []).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          {(["channel", "chat_id", "user_id", "schedule", "timezone"] as const).map((field) => <label key={field} className="grid gap-1 text-xs text-muted-foreground">{field}<Input value={subscription[field]} onChange={(event) => setSubscription({ ...subscription, [field]: event.target.value })} /></label>)}
          <label className="grid gap-1 text-xs text-muted-foreground sm:col-span-2 xl:col-span-3">报表范围 JSON<textarea className="min-h-24 border border-input bg-background p-2 font-mono text-xs text-foreground" value={subscription.report_params_json} onChange={(event) => setSubscription({ ...subscription, report_params_json: event.target.value })} /></label>
          <div className="sm:col-span-2 xl:col-span-3"><Button size="sm" disabled={action !== null || !subscription.template_id || !subscription.chat_id || !subscription.user_id} onClick={() => void run("subscription_create", subscription)}>确认创建</Button></div>
        </div> : null}
        <div className="divide-y border-y">{(payload?.subscriptions ?? []).map((item) => <SubscriptionRow key={item.subscription_id} item={item} busy={!managementEnabled || action !== null} onAction={(next, values) => void run(next, values)} />)}{!payload?.subscriptions.length ? <div className="py-8 text-center text-sm text-muted-foreground">暂无订阅</div> : null}</div>
      </section> : null}

      {tab === "permissions" ? <section className="space-y-5">
        <div className="flex items-center justify-between border-y py-3"><div className="flex items-start gap-3"><ShieldCheck className="mt-0.5 h-4 w-4 text-muted-foreground" /><div><div className="text-sm font-medium">Report RBAC</div><div className="text-xs text-muted-foreground">启用后校验 Connector、Template、客户、模型和订阅模板授权。</div></div></div><ToggleButton checked={Boolean(payload?.policy.rbac_enabled)} disabled={action !== null} label="Report RBAC" onChange={(enabled) => void run("rbac", { enabled: String(enabled) })} /></div>
        <div className="space-y-3"><h3 className="text-sm font-semibold">Grant editor</h3><div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4"><Input value={grant.channel} onChange={(event) => setGrant({ ...grant, channel: event.target.value })} placeholder="Channel" /><Input value={grant.user_id} onChange={(event) => setGrant({ ...grant, user_id: event.target.value })} placeholder="User open_id" /><select className="h-9 border border-input bg-background px-3 text-sm" value={grant.resource_type} onChange={(event) => setGrant({ ...grant, resource_type: event.target.value })}>{(payload?.policy.resource_types ?? []).map((item) => <option key={item} value={item}>{item}</option>)}</select><Input value={grant.resource_id} onChange={(event) => setGrant({ ...grant, resource_id: event.target.value })} placeholder="Resource ID or *" /></div><div className="flex gap-2"><Button size="sm" disabled={!grant.user_id || !grant.resource_id || action !== null} onClick={() => void run("grant", grant)}>Grant</Button><Button variant="outline" size="sm" disabled={!grant.user_id || !grant.resource_id || action !== null} onClick={() => void run("revoke", grant)}>Revoke</Button><Button variant="ghost" size="sm" disabled={!grant.user_id} onClick={() => void load({ channel: grant.channel, user_id: grant.user_id })}>Inspect user</Button></div>{payload?.grants.length ? <div className="divide-y border-y text-sm">{payload.grants.map((item) => <div key={`${item.resource_type}:${item.resource_id}`} className="flex items-center justify-between py-2"><span>{item.resource_type}</span><code className="text-xs text-muted-foreground">{item.resource_id}</code></div>)}</div> : null}</div>
      </section> : null}

      <div className="flex items-center gap-2 text-xs text-muted-foreground"><Database className="h-3.5 w-3.5" />{payload?.storage.backend} · {payload?.storage.retention_days}-day run retention · onboarding v{payload?.onboarding_version}</div>
    </div>
  );
}
