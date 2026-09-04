import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CalendarClock,
  Check,
  Database,
  Download,
  FileBarChart,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";

import { ToggleButton } from "@/components/settings/ToggleButton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  fetchReportingSettings,
  fetchReportingSubscriptionOptions,
  runReportingSettingsAction,
} from "@/lib/api";
import type {
  ReportingSettingsPayload,
  ReportingSubscription,
  ReportingSubscriptionForm,
  ReportingSubscriptionOptions,
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
  | "subscription_preview"
  | "subscription_create_guided"
  | "subscription_update"
  | "subscription_enable"
  | "subscription_disable"
  | "subscription_schedule"
  | "subscription_delete";

type GuidedFormState = {
  template_id: string;
  channel: string;
  chat_id: string;
  user_id: string;
  tenant_scope: "all" | "selected";
  tenants: string;
  model_scope: "all" | "selected" | "summary";
  models: string;
  period: string;
  recurrence: ReportingSubscriptionForm["recurrence"];
  send_time: string;
  weekday: number;
  month_day: number;
  timezone: string;
  project: string;
  endpoint: string;
  provider: string;
  cluster: string;
};

const EMPTY_GRANT = {
  channel: "feishu",
  user_id: "",
  resource_type: "connector",
  resource_id: "magik_cube",
};

const EMPTY_SUBSCRIPTION: GuidedFormState = {
  template_id: "",
  channel: "feishu",
  chat_id: "",
  user_id: "",
  tenant_scope: "selected",
  tenants: "",
  model_scope: "all",
  models: "",
  period: "day",
  recurrence: "workdays",
  send_time: "09:00",
  weekday: 1,
  month_day: 1,
  timezone: "Asia/Shanghai",
  project: "",
  endpoint: "",
  provider: "",
  cluster: "",
};

const SELECT_CLASS =
  "h-9 w-full border border-input bg-background px-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const FIELD_LABEL_CLASS = "grid gap-1 text-xs font-medium text-muted-foreground";

function splitList(value: string): string[] {
  return [...new Set(value.split(/[,，、;；\n]+/).map((item) => item.trim()).filter(Boolean))];
}

function joinList(values: string[] | undefined): string {
  return (values ?? []).join("、");
}

function toFormValues(form: GuidedFormState): Record<string, unknown> {
  return {
    template_id: form.template_id,
    channel: form.channel.trim(),
    chat_id: form.chat_id.trim(),
    user_id: form.user_id.trim(),
    tenant_scope: form.tenant_scope,
    tenants: splitList(form.tenants),
    model_scope: form.model_scope,
    models: splitList(form.models),
    period: form.period,
    recurrence: form.recurrence,
    send_time: form.send_time,
    weekday: form.weekday,
    month_day: form.month_day,
    timezone: form.timezone.trim(),
    project: form.project.trim(),
    endpoint: form.endpoint.trim(),
    provider: form.provider.trim(),
    cluster: form.cluster.trim(),
  };
}

function fromSubscription(item: ReportingSubscription): GuidedFormState {
  // Older rows predate the normalized guided-form snapshot.  Reconstruct only
  // the bounded fields needed by the editor; never surface the legacy JSON as
  // an editable blob or infer a broader customer/model scope.
  const legacyTenants = Array.isArray(item.report_params.tenants)
    ? item.report_params.tenants.filter((value): value is string => typeof value === "string")
    : typeof item.report_params.tenant_query === "string" && item.report_params.tenant_query
      ? [item.report_params.tenant_query]
      : [];
  const legacyModels = Array.isArray(item.report_params.models)
    ? item.report_params.models.filter((value): value is string => typeof value === "string")
    : typeof item.report_params.model === "string" && item.report_params.model
      ? [item.report_params.model]
      : [];
  const form: ReportingSubscriptionForm = item.form ?? {
    template_id: item.template_id,
    channel: item.channel,
    chat_id: item.chat_id,
    user_id: item.user_id,
    tenant_scope: item.report_params.all_tenants ? "all" : "selected",
    tenants: legacyTenants,
    model_scope: item.report_params.model_scope === "selected"
      ? "selected"
      : item.report_params.model_scope === "summary"
        ? "summary"
        : "all",
    models: legacyModels,
    period: String(item.report_params.subscription_period ?? "day"),
    recurrence: "workdays",
    send_time: "09:00",
    weekday: 1,
    month_day: 1,
    timezone: item.timezone,
  };
  return {
    template_id: form.template_id || item.template_id,
    channel: form.channel || item.channel,
    chat_id: form.chat_id || item.chat_id,
    user_id: form.user_id || item.user_id,
    tenant_scope: form.tenant_scope,
    tenants: joinList(form.tenants),
    model_scope: form.model_scope,
    models: joinList(form.models),
    period: form.period,
    recurrence: form.recurrence,
    send_time: form.send_time,
    weekday: form.weekday,
    month_day: form.month_day,
    timezone: form.timezone || item.timezone,
    project: form.project ?? "",
    endpoint: form.endpoint ?? "",
    provider: form.provider ?? "",
    cluster: form.cluster ?? "",
  };
}

function templateLabel(
  templateId: string,
  policies: ReportingTemplatePolicy[],
): string {
  return policies.find((item) => item.id === templateId)?.name ?? templateId;
}

function periodLabel(period: string): string {
  return ({ day: "日报", week: "周报", month: "月报", range: "区间" } as Record<string, string>)[period] ?? period;
}

function TemplatePolicyRow({
  item,
  busy,
  buttonPolicyEnabled,
  onSave,
}: {
  item: ReportingTemplatePolicy;
  busy: boolean;
  buttonPolicyEnabled: boolean;
  onSave: (values: Record<string, unknown>) => void;
}) {
  const [enabled, setEnabled] = useState(item.enabled);
  const [mode, setMode] = useState(item.subscription_mode);
  const [showButton, setShowButton] = useState(item.show_subscription_button !== false);

  useEffect(() => {
    setEnabled(item.enabled);
    setMode(item.subscription_mode);
    setShowButton(item.show_subscription_button !== false);
  }, [item]);

  const changed =
    enabled !== item.enabled
    || mode !== item.subscription_mode
    || showButton !== (item.show_subscription_button !== false);

  return (
    <div className="grid gap-4 border-b border-border/50 py-4 last:border-b-0 xl:grid-cols-[minmax(0,1fr)_auto_auto_auto] xl:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-sm font-medium text-foreground">{item.name}</span>
          <code className="text-xs text-muted-foreground">{item.id} v{item.version}</code>
        </div>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{item.description}</p>
        <p className="mt-1 text-xs text-muted-foreground">
          周期：{item.periods.join(" / ")} · 计算代码和接口路径只读 · revision {item.revision}
        </p>
      </div>
      <label className="grid gap-1 text-xs font-medium text-muted-foreground">
        订阅受众
        <select
          className={SELECT_CLASS}
          value={mode}
          onChange={(event) => setMode(event.target.value as ReportingTemplatePolicy["subscription_mode"])}
          aria-label={`${item.name} 订阅受众`}
          disabled={busy}
        >
          <option value="all_authorized">已授权用户</option>
          <option value="allowlist">订阅白名单</option>
          <option value="disabled">禁止新订阅</option>
        </select>
      </label>
      <div className="grid gap-2 text-xs text-muted-foreground">
        <div className="flex items-center justify-between gap-3">
          <span>报表启用</span>
          <ToggleButton checked={enabled} disabled={busy} label={`${item.name} 启用状态`} onChange={setEnabled} />
        </div>
        <div className="flex items-center justify-between gap-3">
          <span>显示订阅按钮</span>
          <ToggleButton
            checked={showButton}
            disabled={busy || !buttonPolicyEnabled}
            label={`${item.name} 结果卡片订阅按钮`}
            onChange={setShowButton}
          />
        </div>
        {!buttonPolicyEnabled ? <span className="text-[11px] text-amber-600 dark:text-amber-300">按钮策略开关未启用</span> : null}
      </div>
      <Button
        size="icon"
        variant="outline"
        title="保存报表策略"
        aria-label={`保存 ${item.name} 报表策略`}
        disabled={busy || !changed}
        onClick={() => onSave({
          template_id: item.id,
          enabled: String(enabled),
          subscription_mode: mode,
          ...(buttonPolicyEnabled
            ? { show_subscription_button: String(showButton) }
            : {}),
          revision: String(item.revision),
        })}
      >
        <Save className="h-4 w-4" />
      </Button>
    </div>
  );
}

function SubscriptionRow({
  item,
  busy,
  policies,
  onEdit,
  onAction,
}: {
  item: ReportingSubscription;
  busy: boolean;
  policies: ReportingTemplatePolicy[];
  onEdit: (item: ReportingSubscription) => void;
  onAction: (action: ReportAction, values: Record<string, unknown>) => void;
}) {
  return (
    <div className="grid gap-4 border-b border-border/50 py-4 last:border-b-0 lg:grid-cols-[minmax(0,1fr)_minmax(12rem,18rem)_auto] lg:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="break-words text-sm font-medium text-foreground">
            {templateLabel(item.template_id, policies)}
          </span>
          <span className={cn(
            "rounded-full border px-2 py-0.5 text-xs",
            item.enabled
              ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
              : "border-border text-muted-foreground",
          )}>{item.enabled ? "启用" : "停用"}</span>
        </div>
        <p className="mt-2 break-words text-sm text-foreground/85">{item.scope_summary || "未指定范围"}</p>
        <p className="mt-1 break-all text-xs text-muted-foreground">
          接收：{item.channel} · {item.user_id || "未指定用户"} · 会话 {item.chat_id || "未指定"}
        </p>
        <p className="mt-1 text-[11px] text-muted-foreground">ID {item.subscription_id} · revision {item.revision}</p>
      </div>
      <div className="min-w-0 text-sm">
        <div className="font-medium text-foreground">{item.schedule_label || "已配置发送计划"}</div>
        <div className="mt-1 text-xs text-muted-foreground">时区：{item.timezone}</div>
        <div className="mt-1 text-xs text-muted-foreground">更新：{item.updated_at || "暂无"}</div>
      </div>
      <div className="flex flex-wrap items-center justify-start gap-2 lg:justify-end" aria-label={`${item.subscription_id} 操作`}>
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => onEdit(item)}
        >
          <Pencil className="h-3.5 w-3.5" />编辑
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => onAction(item.enabled ? "subscription_disable" : "subscription_enable", {
            subscription_id: item.subscription_id,
            revision: item.revision,
          })}
        >
          {item.enabled ? "停用" : "启用"}
        </Button>
        <Button
          size="icon"
          variant="ghost"
          title="删除订阅"
          aria-label={`删除 ${item.scope_summary || item.subscription_id}`}
          disabled={busy}
          onClick={() => {
            if (window.confirm(`确认删除订阅“${item.scope_summary || item.subscription_id}”？历史运行记录会保留。`)) {
              onAction("subscription_delete", {
                subscription_id: item.subscription_id,
                revision: item.revision,
              });
            }
          }}
        >
          <Trash2 className="h-4 w-4 text-destructive" />
        </Button>
      </div>
    </div>
  );
}

function SubscriptionEditor({
  form,
  editing,
  busy,
  policies,
  options,
  optionsLoading,
  onChange,
  onCancel,
  onPreview,
  onSubmit,
}: {
  form: GuidedFormState;
  editing: boolean;
  busy: boolean;
  policies: ReportingTemplatePolicy[];
  options: ReportingSubscriptionOptions | null;
  optionsLoading: boolean;
  onChange: (patch: Partial<GuidedFormState>) => void;
  onCancel: () => void;
  onPreview: () => void;
  onSubmit: () => void;
}) {
  const selectedTenants = useMemo(() => splitList(form.tenants), [form.tenants]);
  const optionTemplates = useMemo(
    () => new Set(
      (options?.templates ?? [])
        .filter((item) => item.subscribable)
        .map((item) => item.id),
    ),
    [options?.templates],
  );
  const availableTemplates = policies.filter((item) => {
    const selectable = options
      ? optionTemplates.has(item.id)
      : item.enabled && item.subscription_mode !== "disabled";
    return selectable || item.id === form.template_id;
  });
  const selectedTemplate = policies.find((item) => item.id === form.template_id);
  const periods = selectedTemplate?.periods ?? ["day", "week", "month"];
  const tenantOptions = options?.tenants ?? [];

  const toggleTenant = (tenantId: string) => {
    const next = selectedTenants.includes(tenantId)
      ? selectedTenants.filter((item) => item !== tenantId)
      : [...selectedTenants, tenantId];
    onChange({ tenants: joinList(next) });
  };

  return (
    <div className="space-y-5 border-y border-border/60 bg-muted/10 px-1 py-5 sm:px-2">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h4 className="text-sm font-semibold text-foreground">{editing ? "编辑订阅" : "新建订阅"}</h4>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            使用结构化字段配置范围和计划。Cron 与内部参数由服务端编译并重新校验。
          </p>
        </div>
        <Button size="icon" variant="ghost" title="关闭表单" aria-label="关闭订阅表单" onClick={onCancel} disabled={busy}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <label className={FIELD_LABEL_CLASS}>
          报表类型
          <select
            className={SELECT_CLASS}
            value={form.template_id}
            onChange={(event) => {
              const nextTemplate = policies.find((item) => item.id === event.target.value);
              onChange({
                template_id: event.target.value,
                period: nextTemplate?.periods.includes(form.period)
                  ? form.period
                  : nextTemplate?.periods[0] ?? "day",
              });
            }}
            disabled={busy}
          >
            <option value="">请选择报表类型</option>
            {availableTemplates.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <label className={FIELD_LABEL_CLASS}>
          报表周期
          <select className={SELECT_CLASS} value={form.period} onChange={(event) => onChange({ period: event.target.value })} disabled={busy}>
            {periods.map((period) => <option key={period} value={period}>{periodLabel(period)}</option>)}
          </select>
        </label>
        <label className={FIELD_LABEL_CLASS}>
          发送频率
          <select className={SELECT_CLASS} value={form.recurrence} onChange={(event) => onChange({ recurrence: event.target.value as GuidedFormState["recurrence"] })} disabled={busy}>
            <option value="every_day">每天</option>
            <option value="workdays">工作日</option>
            <option value="weekly">每周指定日期</option>
            <option value="monthly">每月指定日期</option>
          </select>
        </label>
        <label className={FIELD_LABEL_CLASS}>
          发送时间
          <Input type="time" value={form.send_time} onChange={(event) => onChange({ send_time: event.target.value })} disabled={busy} />
        </label>
        <label className={FIELD_LABEL_CLASS}>
          时区
          <select className={SELECT_CLASS} value={form.timezone} onChange={(event) => onChange({ timezone: event.target.value })} disabled={busy}>
            {(options?.timezones ?? ["Asia/Shanghai", "UTC"]).map((timezone) => <option key={timezone} value={timezone}>{timezone}</option>)}
          </select>
        </label>
        {form.recurrence === "weekly" ? (
          <label className={FIELD_LABEL_CLASS}>
            每周日期
            <select className={SELECT_CLASS} value={form.weekday} onChange={(event) => onChange({ weekday: Number(event.target.value) })} disabled={busy}>
              <option value={1}>周一</option><option value={2}>周二</option><option value={3}>周三</option>
              <option value={4}>周四</option><option value={5}>周五</option><option value={6}>周六</option><option value={7}>周日</option>
            </select>
          </label>
        ) : null}
        {form.recurrence === "monthly" ? (
          <label className={FIELD_LABEL_CLASS}>
            每月日期
            <Input type="number" min={1} max={28} value={form.month_day} onChange={(event) => onChange({ month_day: Number(event.target.value) })} disabled={busy} />
          </label>
        ) : null}
      </div>

      <fieldset className="space-y-3">
        <legend className="text-xs font-semibold text-foreground">客户范围</legend>
        <div className="grid gap-3 sm:grid-cols-[12rem_minmax(0,1fr)] sm:items-start">
          <label className={FIELD_LABEL_CLASS}>
            选择方式
            <select className={SELECT_CLASS} value={form.tenant_scope} onChange={(event) => onChange({ tenant_scope: event.target.value as GuidedFormState["tenant_scope"] })} disabled={busy}>
              <option value="selected">指定客户</option>
              <option value="all">全部客户</option>
            </select>
          </label>
          {form.tenant_scope === "selected" ? (
            <div className="space-y-2">
              {optionsLoading ? <p className="text-xs text-muted-foreground">正在加载 Cube 实时客户目录…</p> : null}
              {tenantOptions.length ? (
                <div className="grid max-h-40 gap-1 overflow-y-auto border border-input bg-background p-2 sm:grid-cols-2" aria-label="客户选项">
                  {tenantOptions.map((tenant) => (
                    <label key={tenant.tenant_id} className="flex min-w-0 items-center gap-2 px-2 py-1.5 text-xs text-foreground hover:bg-muted/50">
                      <input type="checkbox" checked={selectedTenants.includes(tenant.tenant_id)} onChange={() => toggleTenant(tenant.tenant_id)} disabled={busy} />
                      <span className="min-w-0 truncate" title={`${tenant.display_name} (${tenant.tenant_id})`}>{tenant.display_name}</span>
                    </label>
                  ))}
                </div>
              ) : (
                <Input value={form.tenants} onChange={(event) => onChange({ tenants: event.target.value })} placeholder="客户 ID 或已验证名称，用逗号分隔" disabled={busy} aria-label="指定客户" />
              )}
              <p className="text-[11px] text-muted-foreground">选项来自 Cube 实时目录；服务端会再次校验真实客户身份。</p>
            </div>
          ) : (
            <p className="text-xs leading-5 text-muted-foreground">执行时动态读取当前有权限的客户。不会把客户列表固化在订阅参数中。</p>
          )}
        </div>
      </fieldset>

      <fieldset className="space-y-3">
        <legend className="text-xs font-semibold text-foreground">模型范围</legend>
        <div className="grid gap-3 sm:grid-cols-[12rem_minmax(0,1fr)] sm:items-start">
          <label className={FIELD_LABEL_CLASS}>
            选择方式
            <select className={SELECT_CLASS} value={form.model_scope} onChange={(event) => onChange({ model_scope: event.target.value as GuidedFormState["model_scope"] })} disabled={busy}>
              <option value="all">全部模型</option>
              <option value="selected">指定模型</option>
              <option value="summary">仅客户汇总</option>
            </select>
          </label>
          {form.model_scope === "selected" ? (
            <div>
              <Input value={form.models} onChange={(event) => onChange({ models: event.target.value })} placeholder="模型名称，用逗号分隔" disabled={busy} aria-label="指定模型" />
              <p className="mt-1 text-[11px] text-muted-foreground">服务端会针对选中的客户校验模型目录；不存在的模型不会静默忽略。</p>
            </div>
          ) : <p className="text-xs leading-5 text-muted-foreground">{form.model_scope === "all" ? "执行时动态纳入客户当前可用模型。" : "只生成客户级汇总，不展示模型明细。"}</p>}
        </div>
      </fieldset>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <label className={FIELD_LABEL_CLASS}>推送渠道<select className={SELECT_CLASS} value={form.channel} onChange={(event) => onChange({ channel: event.target.value })} disabled={busy}><option value="feishu">Feishu（当前可用）</option><option value="wecom" disabled>企业微信（能力保留，暂不可投递）</option><option value="dingtalk" disabled>钉钉（能力保留，暂不可投递）</option></select></label>
        <label className={FIELD_LABEL_CLASS}>接收人<Input value={form.user_id} onChange={(event) => onChange({ user_id: event.target.value })} placeholder="用户标识" disabled={busy} /></label>
        <label className={FIELD_LABEL_CLASS}>会话<Input value={form.chat_id} onChange={(event) => onChange({ chat_id: event.target.value })} placeholder="会话或群标识" disabled={busy} /></label>
        <label className={FIELD_LABEL_CLASS}>项目过滤（可选）<Input value={form.project} onChange={(event) => onChange({ project: event.target.value })} disabled={busy} /></label>
        <label className={FIELD_LABEL_CLASS}>Endpoint 过滤（可选）<Input value={form.endpoint} onChange={(event) => onChange({ endpoint: event.target.value })} disabled={busy} /></label>
        <label className={FIELD_LABEL_CLASS}>Provider 过滤（可选）<Input value={form.provider} onChange={(event) => onChange({ provider: event.target.value })} disabled={busy} /></label>
        <label className={FIELD_LABEL_CLASS}>Cluster 过滤（可选）<Input value={form.cluster} onChange={(event) => onChange({ cluster: event.target.value })} disabled={busy} /></label>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t border-border/50 pt-4">
        <Button size="sm" variant="outline" disabled={busy || !form.template_id} onClick={onPreview}><Check className="h-4 w-4" />预览配置</Button>
        <Button size="sm" disabled={busy || !form.template_id || !form.user_id || !form.chat_id} onClick={onSubmit}>{editing ? "保存修改" : "创建订阅"}</Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={onCancel}>取消</Button>
      </div>
    </div>
  );
}

export function ReportsSettings({ token }: { token: string }) {
  const [payload, setPayload] = useState<ReportingSettingsPayload | null>(null);
  const [options, setOptions] = useState<ReportingSubscriptionOptions | null>(null);
  const [tab, setTab] = useState<"templates" | "subscriptions" | "permissions">("templates");
  const [grant, setGrant] = useState(EMPTY_GRANT);
  const [subscription, setSubscription] = useState<GuidedFormState>(EMPTY_SUBSCRIPTION);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingRevision, setEditingRevision] = useState<number | null>(null);
  const [showEditor, setShowEditor] = useState(false);
  const [optionsLoading, setOptionsLoading] = useState(false);
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

  const loadOptions = useCallback(async () => {
    setOptionsLoading(true);
    try {
      const result = await fetchReportingSubscriptionOptions(token);
      const subscriptionOptions = result.subscription_options;
      setOptions(subscriptionOptions ?? null);
      if (subscriptionOptions?.timezones.length) {
        setSubscription((current) => ({
          ...current,
          timezone: subscriptionOptions.timezones.includes(current.timezone)
            ? current.timezone
            : subscriptionOptions.timezones[0],
        }));
      }
    } catch (reason) {
      // The form remains usable for stable IDs when a live catalog is unavailable;
      // the service will reject unverified aliases instead of silently widening scope.
      setOptions(null);
      setError(reason instanceof Error ? reason.message : "加载订阅选项失败");
    } finally {
      setOptionsLoading(false);
    }
  }, [token]);

  const openCreate = () => {
    if (!managementEnabled || !guidedUiEnabled) {
      setError("引导式订阅管理尚未启用，请先开启 report_subscription_guided_ui。");
      return;
    }
    setEditingId(null);
    setEditingRevision(null);
    setSubscription({
      ...EMPTY_SUBSCRIPTION,
      template_id: payload?.template_policies[0]?.id ?? "",
    });
    setShowEditor(true);
    void loadOptions();
  };

  const openEdit = (item: ReportingSubscription) => {
    if (!managementEnabled || !guidedUiEnabled) {
      setError("引导式订阅管理尚未启用，请先开启 report_subscription_guided_ui。");
      return;
    }
    setEditingId(item.subscription_id);
    setEditingRevision(item.revision);
    setSubscription(fromSubscription(item));
    setShowEditor(true);
    void loadOptions();
  };

  const closeEditor = () => {
    if (action !== null) return;
    setShowEditor(false);
    setEditingId(null);
    setEditingRevision(null);
  };

  const run = async (nextAction: ReportAction, values: Record<string, unknown>) => {
    setAction(nextAction);
    setMessage(null);
    try {
      const result = await runReportingSettingsAction(token, nextAction, values);
      setPayload(result);
      setError(null);
      setMessage(nextAction === "export" ? `Catalog exported to ${result.last_action?.path ?? "report declarations"}` : "操作已生效。");
      if (["subscription_create_guided", "subscription_update", "subscription_create"].includes(nextAction)) {
        setShowEditor(false);
        setEditingId(null);
        setEditingRevision(null);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "报表管理操作失败");
    } finally {
      setAction(null);
    }
  };

  const submitSubscription = () => {
    const values = toFormValues(subscription);
    if (editingId) {
      void run("subscription_update", {
        ...values,
        subscription_id: editingId,
        revision: editingRevision ?? 0,
      });
    } else {
      void run("subscription_create_guided", values);
    }
  };

  const previewSubscription = () => {
    void run("subscription_preview", toFormValues(subscription));
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
  const guidedUiEnabled = Boolean(payload?.policy.guided_ui_enabled);
  const buttonPolicyEnabled = Boolean(payload?.policy.button_policy_enabled);
  const policies = payload?.template_policies ?? [];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Report platform</h2>
          <p className="mt-1 text-sm text-muted-foreground">统一管理确定性报表、订阅计划和访问范围。</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="icon" onClick={() => void load()} disabled={loading} title="刷新" aria-label="刷新报表平台"><RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} /></Button>
          <Button variant="outline" size="icon" onClick={() => void run("export", {})} disabled={action !== null} title="导出声明" aria-label="导出报表声明"><Download className="h-4 w-4" /></Button>
        </div>
      </div>
      {error ? <div className="border-l-2 border-destructive px-3 py-2 text-sm text-destructive" role="alert">{error}</div> : null}
      {message ? <div className="border-l-2 border-emerald-500 px-3 py-2 text-sm text-muted-foreground" role="status">{message}</div> : null}
      {!managementEnabled ? <div className="border-l-2 border-amber-500 px-3 py-2 text-sm text-muted-foreground">报表管理 feature flag 尚未启用；当前页面保持只读，原 reporting 设置接口继续可用。</div> : null}
      {managementEnabled && !guidedUiEnabled ? <div className="border-l-2 border-amber-500 px-3 py-2 text-sm text-muted-foreground">引导式订阅界面尚未启用；订阅管理暂不可编辑，旧兼容接口仍可用。</div> : null}

      <div className="flex gap-1 overflow-x-auto border-b" role="tablist" aria-label="报表管理视图">
        {tabs.map((item) => <button key={item.id} type="button" role="tab" aria-selected={tab === item.id} onClick={() => setTab(item.id)} className={cn("inline-flex h-10 shrink-0 items-center gap-2 border-b-2 px-3 text-sm", tab === item.id ? "border-primary font-medium text-foreground" : "border-transparent text-muted-foreground hover:text-foreground")}><item.icon className="h-4 w-4" />{item.label}</button>)}
      </div>

      {tab === "templates" ? <section className="space-y-3">
        <div><h3 className="text-sm font-semibold">报表类型</h3><p className="mt-1 text-xs text-muted-foreground">仅管理启用状态、订阅受众和结果卡片按钮；模板代码、公式和接口路径保持只读。</p></div>
        <div className="divide-y border-y">
          {policies.map((item) => <TemplatePolicyRow key={item.id} item={item} buttonPolicyEnabled={buttonPolicyEnabled} busy={!managementEnabled || action !== null} onSave={(values) => void run("template_policy", values)} />)}
        </div>
        <div className="space-y-2 pt-2"><h3 className="text-sm font-semibold">Connector 状态</h3>{(payload?.catalog.connectors ?? []).map((item) => <div key={item.id} className="flex items-start gap-3 border-t py-3"><Database className="mt-0.5 h-4 w-4 text-muted-foreground" /><div className="min-w-0"><div className="text-sm font-medium">{item.name}</div><code className="text-xs text-muted-foreground">{item.id} v{item.version} · {item.read_only ? "只读" : "可写"}</code></div></div>)}</div>
      </section> : null}

      {tab === "subscriptions" ? <section className="space-y-4">
        <div className="flex items-start justify-between gap-3"><div><h3 className="text-sm font-semibold">订阅管理</h3><p className="mt-1 text-xs text-muted-foreground">每一行只操作本行订阅；编辑会同步更新 Cron 和数据库，停用、删除不会修改历史运行记录。</p></div><Button size="sm" disabled={!managementEnabled || !guidedUiEnabled || action !== null} onClick={openCreate}><Plus className="h-4 w-4" />新建订阅</Button></div>
        {showEditor ? <SubscriptionEditor form={subscription} editing={editingId !== null} busy={action !== null} policies={policies} options={options} optionsLoading={optionsLoading} onChange={(patch) => setSubscription((current) => ({ ...current, ...patch }))} onCancel={closeEditor} onPreview={previewSubscription} onSubmit={submitSubscription} /> : null}
        <div className="divide-y border-y">{(payload?.subscriptions ?? []).map((item) => <SubscriptionRow key={item.subscription_id} item={item} policies={policies} busy={!managementEnabled || !guidedUiEnabled || action !== null} onEdit={openEdit} onAction={(next, values) => void run(next, values)} />)}{!payload?.subscriptions.length ? <div className="py-8 text-center text-sm text-muted-foreground">暂无订阅</div> : null}</div>
      </section> : null}

      {tab === "permissions" ? <section className="space-y-5">
        <div className="flex items-center justify-between border-y py-3"><div className="flex items-start gap-3"><ShieldCheck className="mt-0.5 h-4 w-4 text-muted-foreground" /><div><div className="text-sm font-medium">Report RBAC</div><div className="text-xs text-muted-foreground">启用后校验 Connector、Template、客户、模型和订阅模板授权。</div></div></div><ToggleButton checked={Boolean(payload?.policy.rbac_enabled)} disabled={action !== null} label="Report RBAC" onChange={(enabled) => void run("rbac", { enabled: String(enabled) })} /></div>
        <div className="space-y-3"><h3 className="text-sm font-semibold">Grant editor</h3><div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4"><Input value={grant.channel} onChange={(event) => setGrant({ ...grant, channel: event.target.value })} placeholder="Channel" /><Input value={grant.user_id} onChange={(event) => setGrant({ ...grant, user_id: event.target.value })} placeholder="User open_id" /><select className={SELECT_CLASS} value={grant.resource_type} onChange={(event) => setGrant({ ...grant, resource_type: event.target.value })}>{(payload?.policy.resource_types ?? []).map((item) => <option key={item} value={item}>{item}</option>)}</select><Input value={grant.resource_id} onChange={(event) => setGrant({ ...grant, resource_id: event.target.value })} placeholder="Resource ID or *" /></div><div className="flex flex-wrap gap-2"><Button size="sm" disabled={!grant.user_id || !grant.resource_id || action !== null} onClick={() => void run("grant", grant)}>Grant</Button><Button variant="outline" size="sm" disabled={!grant.user_id || !grant.resource_id || action !== null} onClick={() => void run("revoke", grant)}>Revoke</Button><Button variant="ghost" size="sm" disabled={!grant.user_id} onClick={() => void load({ channel: grant.channel, user_id: grant.user_id })}>Inspect user</Button></div>{payload?.grants.length ? <div className="divide-y border-y text-sm">{payload.grants.map((item) => <div key={`${item.resource_type}:${item.resource_id}`} className="flex items-center justify-between py-2"><span>{item.resource_type}</span><code className="text-xs text-muted-foreground">{item.resource_id}</code></div>)}</div> : null}</div>
      </section> : null}

      <div className="flex items-center gap-2 text-xs text-muted-foreground"><Database className="h-3.5 w-3.5" />{payload?.storage.backend} · {payload?.storage.retention_days}-day run retention · onboarding v{payload?.onboarding_version}</div>
    </div>
  );
}
