import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
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
  RotateCcw,
  Save,
  ShieldCheck,
  SlidersHorizontal,
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
  type ReportingSettingsAction,
} from "@/lib/api";
import type {
  ReportingFeatureFlag,
  ReportingSettingsPayload,
  ReportingSubscription,
  ReportingSubscriptionForm,
  ReportingSubscriptionOptions,
  ReportingTemplatePolicy,
} from "@/lib/types";
import { cn } from "@/lib/utils";

// Derived from the shared api-side action union: this component never
// dispatches subscription_options itself (it uses the dedicated fetch).
type ReportAction = Exclude<ReportingSettingsAction, "subscription_options">;

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
  // Comma/顿号 separated clock hours for the hourly cadence; empty means
  // every hour. Kept as text so partial input stays editable.
  hours: string;
  // Comma/顿号 separated delivery chat targets; empty falls back to the
  // single chat_id (solo subscription).
  chat_ids: string;
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
  hours: "",
  chat_ids: "",
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

// Parse the hourly-broadcast hour text into the number list the guided form
// submits; anything unparsable is dropped so the server remains the single
// validator.
function parseHourList(value: string): number[] {
  return [...new Set(
    splitList(value)
      .map((item) => Number(item))
      .filter((item) => Number.isInteger(item) && item >= 0 && item <= 23),
  )].sort((a, b) => a - b);
}

function toFormValues(form: GuidedFormState): Record<string, unknown> {
  const chatTargets = splitList(form.chat_ids);
  return {
    template_id: form.template_id,
    channel: form.channel.trim(),
    // The single chat target stays for the solo-subscription path; a
    // non-empty target list fans the broadcast out server-side.
    chat_id: chatTargets[0] ?? form.chat_id.trim(),
    chat_ids: chatTargets,
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
    hours: form.recurrence === "hourly" ? parseHourList(form.hours) : [],
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
    hours: [],
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
    hours: (form.hours ?? []).join("、"),
    // Delivery targets come from the derived group; older payloads without
    // the field fall back to the single chat target.
    chat_ids: (item.delivery_targets ?? []).map((target) => target.chat_id).join("、")
      || form.chat_id,
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

type Translate = (key: string, options?: { defaultValue?: string }) => string;

function periodLabel(period: string, t: Translate): string {
  return ({
    day: t("settings.reports.period.day", { defaultValue: "日报" }),
    week: t("settings.reports.period.week", { defaultValue: "周报" }),
    month: t("settings.reports.period.month", { defaultValue: "月报" }),
    range: t("settings.reports.period.range", { defaultValue: "区间" }),
  } as Record<string, string>)[period] ?? period;
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
  const { t } = useTranslation();
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
          {t("settings.reports.policy.periods", { defaultValue: `周期：${item.periods.join(" / ")} · 计算代码和接口路径只读 · revision ${item.revision}`, periods: item.periods.join(" / "), revision: item.revision })}
        </p>
      </div>
      <label className="grid gap-1 text-xs font-medium text-muted-foreground">
        {t("settings.reports.policy.audience", { defaultValue: "订阅受众" })}
        <select
          className={SELECT_CLASS}
          value={mode}
          onChange={(event) => setMode(event.target.value as ReportingTemplatePolicy["subscription_mode"])}
          aria-label={t("settings.reports.policy.audienceAria", { defaultValue: `${item.name} 订阅受众`, name: item.name })}
          disabled={busy}
        >
          <option value="all_authorized">{t("settings.reports.policy.audienceAll", { defaultValue: "已授权用户" })}</option>
          <option value="allowlist">{t("settings.reports.policy.audienceAllowlist", { defaultValue: "订阅白名单" })}</option>
          <option value="disabled">{t("settings.reports.policy.audienceDisabled", { defaultValue: "禁止新订阅" })}</option>
        </select>
      </label>
      <div className="grid gap-2 text-xs text-muted-foreground">
        <div className="flex items-center justify-between gap-3">
          <span>{t("settings.reports.policy.reportEnabled", { defaultValue: "报表启用" })}</span>
          <ToggleButton checked={enabled} disabled={busy} label={t("settings.reports.policy.enabledAria", { defaultValue: `${item.name} 启用状态`, name: item.name })} onChange={setEnabled} />
        </div>
        <div className="flex items-center justify-between gap-3">
          <span>{t("settings.reports.policy.showButton", { defaultValue: "显示订阅按钮" })}</span>
          <ToggleButton
            checked={showButton}
            disabled={busy || !buttonPolicyEnabled}
            label={t("settings.reports.policy.buttonAria", { defaultValue: `${item.name} 结果卡片订阅按钮`, name: item.name })}
            onChange={setShowButton}
          />
        </div>
        {!buttonPolicyEnabled ? <span className="text-[11px] text-amber-600 dark:text-amber-300">{t("settings.reports.policy.buttonPolicyOff", { defaultValue: "按钮策略开关未启用" })}</span> : null}
      </div>
      <Button
        size="icon"
        variant="outline"
        title={t("settings.reports.policy.save", { defaultValue: "保存报表策略" })}
        aria-label={t("settings.reports.policy.saveAria", { defaultValue: `保存 ${item.name} 报表策略`, name: item.name })}
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
  const { t } = useTranslation();
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
          )}>{item.enabled ? t("settings.reports.sub.enabled", { defaultValue: "启用" }) : t("settings.reports.sub.disabled", { defaultValue: "停用" })}</span>
          {(item.delivery_targets?.length ?? 0) > 1 ? (
            <span
              className="rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-xs text-sky-700 dark:text-sky-300"
              title={t("settings.reports.sub.deliveryTargetsTitle", { defaultValue: `投递组会话：${item.delivery_targets?.map((target) => target.chat_id).join("、") ?? ""}`, targets: item.delivery_targets?.map((target) => target.chat_id).join("、") ?? "" })}
            >
              {t("settings.reports.sub.deliveryCount", { defaultValue: `投递 ${item.delivery_targets?.length ?? 0} 会话`, count: item.delivery_targets?.length ?? 0 })}
            </span>
          ) : null}
        </div>
        <p className="mt-2 break-words text-sm text-foreground/85">{item.scope_summary || t("settings.reports.sub.noScope", { defaultValue: "未指定范围" })}</p>
        <p className="mt-1 break-all text-xs text-muted-foreground">
          {t("settings.reports.sub.receive", { defaultValue: `接收：${item.channel} · ${item.user_id || t("settings.reports.sub.unspecifiedUser", { defaultValue: "未指定用户" })} · 会话 ${item.chat_id || t("settings.reports.sub.unspecified", { defaultValue: "未指定" })}`, channel: item.channel, user: item.user_id || t("settings.reports.sub.unspecifiedUser", { defaultValue: "未指定用户" }), chat: item.chat_id || t("settings.reports.sub.unspecified", { defaultValue: "未指定" }) })}
        </p>
        <p className="mt-1 text-[11px] text-muted-foreground">ID {item.subscription_id} · revision {item.revision}</p>
      </div>
      <div className="min-w-0 text-sm">
        <div className="font-medium text-foreground">{item.schedule_label || t("settings.reports.sub.scheduleConfigured", { defaultValue: "已配置发送计划" })}</div>
        <div className="mt-1 text-xs text-muted-foreground">{t("settings.reports.sub.timezone", { defaultValue: `时区：${item.timezone}`, timezone: item.timezone })}</div>
        <div className="mt-1 text-xs text-muted-foreground">{t("settings.reports.sub.updated", { defaultValue: `更新：${item.updated_at || t("settings.reports.sub.noUpdate", { defaultValue: "暂无" })}`, updated: item.updated_at || t("settings.reports.sub.noUpdate", { defaultValue: "暂无" }) })}</div>
      </div>
      <div className="flex flex-wrap items-center justify-start gap-2 lg:justify-end" aria-label={t("settings.reports.sub.actionsAria", { defaultValue: `${item.subscription_id} 操作`, id: item.subscription_id })}>
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => onEdit(item)}
        >
          <Pencil className="h-3.5 w-3.5" />{t("settings.reports.sub.edit", { defaultValue: "编辑" })}
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
          {item.enabled ? t("settings.reports.sub.disableAction", { defaultValue: "停用" }) : t("settings.reports.sub.enableAction", { defaultValue: "启用" })}
        </Button>
        <Button
          size="icon"
          variant="ghost"
          title={t("settings.reports.sub.delete", { defaultValue: "删除订阅" })}
          aria-label={t("settings.reports.sub.deleteAria", { defaultValue: `删除 ${item.scope_summary || item.subscription_id}`, target: item.scope_summary || item.subscription_id })}
          disabled={busy}
          onClick={() => {
            if (window.confirm(t("settings.reports.sub.deleteConfirm", { defaultValue: `确认删除订阅“${item.scope_summary || item.subscription_id}”？历史运行记录会保留。`, target: item.scope_summary || item.subscription_id }))) {
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
  const { t } = useTranslation();
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
          <h4 className="text-sm font-semibold text-foreground">{editing ? t("settings.reports.editor.editTitle", { defaultValue: "编辑订阅" }) : t("settings.reports.editor.createTitle", { defaultValue: "新建订阅" })}</h4>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            {t("settings.reports.editor.description", { defaultValue: "使用结构化字段配置范围和计划。Cron 与内部参数由服务端编译并重新校验。" })}
          </p>
        </div>
        <Button size="icon" variant="ghost" title={t("settings.reports.editor.close", { defaultValue: "关闭表单" })} aria-label={t("settings.reports.editor.closeAria", { defaultValue: "关闭订阅表单" })} onClick={onCancel} disabled={busy}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        <label className={FIELD_LABEL_CLASS}>
          {t("settings.reports.editor.reportType", { defaultValue: "报表类型" })}
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
            <option value="">{t("settings.reports.editor.chooseTemplate", { defaultValue: "请选择报表类型" })}</option>
            {availableTemplates.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <label className={FIELD_LABEL_CLASS}>
          {t("settings.reports.editor.reportPeriod", { defaultValue: "报表周期" })}
          <select className={SELECT_CLASS} value={form.period} onChange={(event) => onChange({ period: event.target.value })} disabled={busy}>
            {periods.map((period) => <option key={period} value={period}>{periodLabel(period, t)}</option>)}
          </select>
        </label>
        <label className={FIELD_LABEL_CLASS}>
          {t("settings.reports.editor.frequency", { defaultValue: "发送频率" })}
          <select className={SELECT_CLASS} value={form.recurrence} onChange={(event) => onChange({ recurrence: event.target.value as GuidedFormState["recurrence"] })} disabled={busy}>
            <option value="every_day">{t("settings.reports.editor.everyDay", { defaultValue: "每天" })}</option>
            <option value="workdays">{t("settings.reports.editor.workdays", { defaultValue: "工作日" })}</option>
            <option value="weekly">{t("settings.reports.editor.weeklyOn", { defaultValue: "每周指定日期" })}</option>
            <option value="monthly">{t("settings.reports.editor.monthlyOn", { defaultValue: "每月指定日期" })}</option>
            {/* Hourly cadence only exists for the hourly TPM template (its
                period list is recent1h only); other templates cannot deliver
                an hourly report. */}
            <option value="hourly" disabled={!periods.includes("recent1h")}>{t("settings.reports.editor.hourly", { defaultValue: "每小时（整点后 5 分钟）" })}</option>
          </select>
        </label>
        {form.recurrence === "hourly" ? (
          <label className={FIELD_LABEL_CLASS}>
            {t("settings.reports.editor.broadcastHours", { defaultValue: "播报小时" })}
            <Input value={form.hours} onChange={(event) => onChange({ hours: event.target.value })} placeholder={t("settings.reports.editor.broadcastHoursPlaceholder", { defaultValue: "留空 = 全部小时；如 9、10、15" })} disabled={busy} />
            <span className="text-[11px] text-muted-foreground">{t("settings.reports.editor.broadcastHoursHint", { defaultValue: "指定时点的整点后 5 分钟，投递刚结束的上一完整小时数据。" })}</span>
          </label>
        ) : (
          <label className={FIELD_LABEL_CLASS}>
            {t("settings.reports.editor.sendTime", { defaultValue: "发送时间" })}
            <Input type="time" value={form.send_time} onChange={(event) => onChange({ send_time: event.target.value })} disabled={busy} />
          </label>
        )}
        <label className={FIELD_LABEL_CLASS}>
          {t("settings.reports.editor.timezone", { defaultValue: "时区" })}
          <select className={SELECT_CLASS} value={form.timezone} onChange={(event) => onChange({ timezone: event.target.value })} disabled={busy}>
            {(options?.timezones ?? ["Asia/Shanghai", "UTC"]).map((timezone) => <option key={timezone} value={timezone}>{timezone}</option>)}
          </select>
        </label>
        {form.recurrence === "weekly" ? (
          <label className={FIELD_LABEL_CLASS}>
            {t("settings.reports.editor.weekdayLabel", { defaultValue: "每周日期" })}
            <select className={SELECT_CLASS} value={form.weekday} onChange={(event) => onChange({ weekday: Number(event.target.value) })} disabled={busy}>
              <option value={1}>{t("settings.reports.editor.mon", { defaultValue: "周一" })}</option><option value={2}>{t("settings.reports.editor.tue", { defaultValue: "周二" })}</option><option value={3}>{t("settings.reports.editor.wed", { defaultValue: "周三" })}</option>
              <option value={4}>{t("settings.reports.editor.thu", { defaultValue: "周四" })}</option><option value={5}>{t("settings.reports.editor.fri", { defaultValue: "周五" })}</option><option value={6}>{t("settings.reports.editor.sat", { defaultValue: "周六" })}</option><option value={7}>{t("settings.reports.editor.sun", { defaultValue: "周日" })}</option>
            </select>
          </label>
        ) : null}
        {form.recurrence === "monthly" ? (
          <label className={FIELD_LABEL_CLASS}>
            {t("settings.reports.editor.monthDayLabel", { defaultValue: "每月日期" })}
            <Input type="number" min={1} max={28} value={form.month_day} onChange={(event) => onChange({ month_day: Number(event.target.value) })} disabled={busy} />
          </label>
        ) : null}
      </div>

      <fieldset className="space-y-3">
        <legend className="text-xs font-semibold text-foreground">{t("settings.reports.editor.tenantScope", { defaultValue: "客户范围" })}</legend>
        <div className="grid gap-3 sm:grid-cols-[12rem_minmax(0,1fr)] sm:items-start">
          <label className={FIELD_LABEL_CLASS}>
            {t("settings.reports.editor.selectMode", { defaultValue: "选择方式" })}
            <select className={SELECT_CLASS} value={form.tenant_scope} onChange={(event) => onChange({ tenant_scope: event.target.value as GuidedFormState["tenant_scope"] })} disabled={busy}>
              <option value="selected">{t("settings.reports.editor.selectedTenants", { defaultValue: "指定客户" })}</option>
              <option value="all">{t("settings.reports.editor.allTenants", { defaultValue: "全部客户" })}</option>
            </select>
          </label>
          {form.tenant_scope === "selected" ? (
            <div className="space-y-2">
              {optionsLoading ? <p className="text-xs text-muted-foreground">{t("settings.reports.editor.loadingCatalog", { defaultValue: "正在加载 Cube 实时客户目录…" })}</p> : null}
              {tenantOptions.length ? (
                <div className="grid max-h-40 gap-1 overflow-y-auto border border-input bg-background p-2 sm:grid-cols-2" aria-label={t("settings.reports.editor.tenantOptions", { defaultValue: "客户选项" })}>
                  {tenantOptions.map((tenant) => (
                    <label key={tenant.tenant_id} className="flex min-w-0 items-center gap-2 px-2 py-1.5 text-xs text-foreground hover:bg-muted/50">
                      <input type="checkbox" checked={selectedTenants.includes(tenant.tenant_id)} onChange={() => toggleTenant(tenant.tenant_id)} disabled={busy} />
                      <span className="min-w-0 truncate" title={`${tenant.display_name} (${tenant.tenant_id})`}>{tenant.display_name}</span>
                    </label>
                  ))}
                </div>
              ) : (
                <Input value={form.tenants} onChange={(event) => onChange({ tenants: event.target.value })} placeholder={t("settings.reports.editor.tenantPlaceholder", { defaultValue: "客户 ID 或已验证名称，用逗号分隔" })} disabled={busy} aria-label={t("settings.reports.editor.selectedTenants", { defaultValue: "指定客户" })} />
              )}
              <p className="text-[11px] text-muted-foreground">{t("settings.reports.editor.tenantHint", { defaultValue: "选项来自 Cube 实时目录；服务端会再次校验真实客户身份。" })}</p>
            </div>
          ) : (
            <p className="text-xs leading-5 text-muted-foreground">{t("settings.reports.editor.dynamicTenants", { defaultValue: "执行时动态读取当前有权限的客户。不会把客户列表固化在订阅参数中。" })}</p>
          )}
        </div>
      </fieldset>

      <fieldset className="space-y-3">
        <legend className="text-xs font-semibold text-foreground">{t("settings.reports.editor.modelScope", { defaultValue: "模型范围" })}</legend>
        <div className="grid gap-3 sm:grid-cols-[12rem_minmax(0,1fr)] sm:items-start">
          <label className={FIELD_LABEL_CLASS}>
            {t("settings.reports.editor.selectMode", { defaultValue: "选择方式" })}
            <select className={SELECT_CLASS} value={form.model_scope} onChange={(event) => onChange({ model_scope: event.target.value as GuidedFormState["model_scope"] })} disabled={busy}>
              <option value="all">{t("settings.reports.editor.allModels", { defaultValue: "全部模型" })}</option>
              <option value="selected">{t("settings.reports.editor.selectedModels", { defaultValue: "指定模型" })}</option>
              <option value="summary">{t("settings.reports.editor.summaryOnly", { defaultValue: "仅客户汇总" })}</option>
            </select>
          </label>
          {form.model_scope === "selected" ? (
            <div>
              <Input value={form.models} onChange={(event) => onChange({ models: event.target.value })} placeholder={t("settings.reports.editor.modelPlaceholder", { defaultValue: "模型名称，用逗号分隔" })} disabled={busy} aria-label={t("settings.reports.editor.selectedModels", { defaultValue: "指定模型" })} />
              <p className="mt-1 text-[11px] text-muted-foreground">{t("settings.reports.editor.modelHint", { defaultValue: "服务端会针对选中的客户校验模型目录；不存在的模型不会静默忽略。" })}</p>
            </div>
          ) : <p className="text-xs leading-5 text-muted-foreground">{form.model_scope === "all" ? t("settings.reports.editor.dynamicModels", { defaultValue: "执行时动态纳入客户当前可用模型。" }) : t("settings.reports.editor.summaryHint", { defaultValue: "只生成客户级汇总，不展示模型明细。" })}</p>}
        </div>
      </fieldset>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <label className={FIELD_LABEL_CLASS}>{t("settings.reports.editor.channel", { defaultValue: "推送渠道" })}<select className={SELECT_CLASS} value={form.channel} onChange={(event) => onChange({ channel: event.target.value })} disabled={busy}><option value="feishu">{t("settings.reports.editor.channelFeishu", { defaultValue: "Feishu（当前可用）" })}</option><option value="wecom" disabled>{t("settings.reports.editor.channelWecom", { defaultValue: "企业微信（能力保留，暂不可投递）" })}</option><option value="dingtalk" disabled>{t("settings.reports.editor.channelDingtalk", { defaultValue: "钉钉（能力保留，暂不可投递）" })}</option></select></label>
        <label className={FIELD_LABEL_CLASS}>{t("settings.reports.editor.recipient", { defaultValue: "接收人" })}<Input value={form.user_id} onChange={(event) => onChange({ user_id: event.target.value })} placeholder={t("settings.reports.editor.recipientPlaceholder", { defaultValue: "用户标识" })} disabled={busy} /></label>
        <label className={FIELD_LABEL_CLASS}>
          {t("settings.reports.editor.deliveryTargets", { defaultValue: "投递会话" })}
          <Input value={form.chat_ids} onChange={(event) => onChange({ chat_ids: event.target.value })} placeholder={t("settings.reports.editor.deliveryTargetsPlaceholder", { defaultValue: "会话/群标识，用顿号分隔（首个为主投递）" })} disabled={busy} />
          <span className="text-[11px] text-muted-foreground">{t("settings.reports.editor.deliveryTargetsHint", { defaultValue: "同一内容和发送计划会同时投递到所有列出的会话。" })}</span>
        </label>
        <label className={FIELD_LABEL_CLASS}>{t("settings.reports.editor.projectFilter", { defaultValue: "项目过滤（可选）" })}<Input value={form.project} onChange={(event) => onChange({ project: event.target.value })} disabled={busy} /></label>
        <label className={FIELD_LABEL_CLASS}>{t("settings.reports.editor.endpointFilter", { defaultValue: "Endpoint 过滤（可选）" })}<Input value={form.endpoint} onChange={(event) => onChange({ endpoint: event.target.value })} disabled={busy} /></label>
        <label className={FIELD_LABEL_CLASS}>{t("settings.reports.editor.providerFilter", { defaultValue: "Provider 过滤（可选）" })}<Input value={form.provider} onChange={(event) => onChange({ provider: event.target.value })} disabled={busy} /></label>
        <label className={FIELD_LABEL_CLASS}>{t("settings.reports.editor.clusterFilter", { defaultValue: "Cluster 过滤（可选）" })}<Input value={form.cluster} onChange={(event) => onChange({ cluster: event.target.value })} disabled={busy} /></label>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t border-border/50 pt-4">
        <Button size="sm" variant="outline" disabled={busy || !form.template_id} onClick={onPreview}><Check className="h-4 w-4" />{t("settings.reports.editor.preview", { defaultValue: "预览配置" })}</Button>
        <Button size="sm" disabled={busy || !form.template_id || !form.user_id || !(splitList(form.chat_ids)[0] ?? form.chat_id.trim())} onClick={onSubmit}>{editing ? t("settings.reports.editor.save", { defaultValue: "保存修改" }) : t("settings.reports.editor.create", { defaultValue: "创建订阅" })}</Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={onCancel}>{t("settings.reports.editor.cancel", { defaultValue: "取消" })}</Button>
      </div>
    </div>
  );
}

export function ReportsSettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const [payload, setPayload] = useState<ReportingSettingsPayload | null>(null);
  const [options, setOptions] = useState<ReportingSubscriptionOptions | null>(null);
  // Alias -> tenant ID draft rows for the tenant-mapping editor; re-synced
  // from the payload after every action so a save/reset round-trips the
  // server's effective table.
  const [tenantAliasDraft, setTenantAliasDraft] = useState<Array<{ alias: string; tenantId: string }>>([]);
  const [tab, setTab] = useState<"templates" | "subscriptions" | "permissions" | "flags">("templates");
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
      setError(reason instanceof Error ? reason.message : t("settings.reports.loadFailed", { defaultValue: "加载报表平台失败" }));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { void load(); }, [load]);

  // Draft rows follow the payload's effective mapping; every action result
  // refreshes the payload, so a completed save/reset re-syncs the editor.
  const tenantMappingsView = payload?.tenant_mappings;
  useEffect(() => {
    setTenantAliasDraft(
      Object.entries(tenantMappingsView?.values ?? {}).map(([alias, tenantId]) => ({
        alias,
        tenantId,
      })),
    );
  }, [tenantMappingsView]);

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
      setError(reason instanceof Error ? reason.message : t("settings.reports.optionsFailed", { defaultValue: "加载订阅选项失败" }));
    } finally {
      setOptionsLoading(false);
    }
  }, [token]);

  const openCreate = () => {
    if (!managementEnabled || !guidedUiEnabled) {
      setError(t("settings.reports.guidedDisabledHint", { defaultValue: "引导式订阅管理尚未启用，请先开启 report_subscription_guided_ui。" }));
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
      setError(t("settings.reports.guidedDisabledHint", { defaultValue: "引导式订阅管理尚未启用，请先开启 report_subscription_guided_ui。" }));
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
      setMessage(nextAction === "export" ? `Catalog exported to ${result.last_action?.path ?? "report declarations"}` : t("settings.reports.actionApplied", { defaultValue: "操作已生效。" }));
      if (["subscription_create_guided", "subscription_update", "subscription_create"].includes(nextAction)) {
        setShowEditor(false);
        setEditingId(null);
        setEditingRevision(null);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("settings.reports.actionFailed", { defaultValue: "报表管理操作失败" }));
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
    return <div className="flex h-40 items-center justify-center text-sm text-muted-foreground"><Loader2 className="mr-2 h-4 w-4 animate-spin" />{t("settings.reports.loading", { defaultValue: "加载报表平台" })}</div>;
  }

  const tabs = [
    { id: "templates" as const, label: t("settings.reports.tabs.templates", { defaultValue: "报表类型" }), icon: FileBarChart },
    { id: "subscriptions" as const, label: t("settings.reports.tabs.subscriptions", { defaultValue: "订阅管理" }), icon: CalendarClock },
    { id: "permissions" as const, label: t("settings.reports.tabs.permissions", { defaultValue: "权限管理" }), icon: ShieldCheck },
    { id: "flags" as const, label: t("settings.reports.tabs.flags", { defaultValue: "功能开关" }), icon: SlidersHorizontal },
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
          <p className="mt-1 text-sm text-muted-foreground">{t("settings.reports.subtitle", { defaultValue: "统一管理确定性报表、订阅计划和访问范围。" })}</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="icon" onClick={() => void load()} disabled={loading} title={t("settings.reports.refresh", { defaultValue: "刷新" })} aria-label={t("settings.reports.refreshAria", { defaultValue: "刷新报表平台" })}><RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} /></Button>
          <Button variant="outline" size="icon" onClick={() => void run("export", {})} disabled={action !== null} title={t("settings.reports.export", { defaultValue: "导出声明" })} aria-label={t("settings.reports.exportAria", { defaultValue: "导出报表声明" })}><Download className="h-4 w-4" /></Button>
        </div>
      </div>
      {error ? <div className="border-l-2 border-destructive px-3 py-2 text-sm text-destructive" role="alert">{error}</div> : null}
      {message ? <div className="border-l-2 border-emerald-500 px-3 py-2 text-sm text-muted-foreground" role="status">{message}</div> : null}
      {!managementEnabled ? <div className="border-l-2 border-amber-500 px-3 py-2 text-sm text-muted-foreground">{t("settings.reports.bannerManagementOff", { defaultValue: "报表管理功能当前关闭；报表类型与订阅管理保持只读，功能开关页仍可操作（可在其中重新开启）。" })}</div> : null}
      {managementEnabled && !guidedUiEnabled ? <div className="border-l-2 border-amber-500 px-3 py-2 text-sm text-muted-foreground">{t("settings.reports.bannerGuidedOff", { defaultValue: "引导式订阅界面尚未启用；订阅管理暂不可编辑，旧兼容接口仍可用。" })}</div> : null}

      <div className="flex gap-1 overflow-x-auto border-b" role="tablist" aria-label={t("settings.reports.tablistAria", { defaultValue: "报表管理视图" })}>
        {tabs.map((item) => <button key={item.id} type="button" role="tab" aria-selected={tab === item.id} onClick={() => { setTab(item.id); if (item.id === "subscriptions") void loadOptions(); }} className={cn("inline-flex h-10 shrink-0 items-center gap-2 border-b-2 px-3 text-sm", tab === item.id ? "border-primary font-medium text-foreground" : "border-transparent text-muted-foreground hover:text-foreground")}><item.icon className="h-4 w-4" />{item.label}</button>)}
      </div>

      {tab === "templates" ? <section className="space-y-3">
        <div><h3 className="text-sm font-semibold">{t("settings.reports.templates.title", { defaultValue: "报表类型" })}</h3><p className="mt-1 text-xs text-muted-foreground">{t("settings.reports.templates.description", { defaultValue: "仅管理启用状态、订阅受众和结果卡片按钮；模板代码、公式和接口路径保持只读。" })}</p></div>
        <div className="divide-y border-y">
          {policies.map((item) => <TemplatePolicyRow key={item.id} item={item} buttonPolicyEnabled={buttonPolicyEnabled} busy={!managementEnabled || action !== null} onSave={(values) => void run("template_policy", values)} />)}
        </div>
        <div className="space-y-2 pt-2"><h3 className="text-sm font-semibold">{t("settings.reports.templates.connectors", { defaultValue: "Connector 状态" })}</h3>{(payload?.catalog.connectors ?? []).map((item) => <div key={item.id} className="flex items-start gap-3 border-t py-3"><Database className="mt-0.5 h-4 w-4 text-muted-foreground" /><div className="min-w-0"><div className="text-sm font-medium">{item.name}</div><code className="text-xs text-muted-foreground">{item.id} v{item.version} · {item.read_only ? t("settings.reports.templates.readOnly", { defaultValue: "只读" }) : t("settings.reports.templates.writable", { defaultValue: "可写" })}</code></div></div>)}</div>
      </section> : null}

      {tab === "subscriptions" ? <section className="space-y-4">
        <div className="flex items-start justify-between gap-3"><div><h3 className="text-sm font-semibold">{t("settings.reports.subscriptions.title", { defaultValue: "订阅管理" })}</h3><p className="mt-1 text-xs text-muted-foreground">{t("settings.reports.subscriptions.description", { defaultValue: "每一行只操作本行订阅；编辑会同步更新 Cron 和数据库，停用、删除不会修改历史运行记录。" })}</p></div><Button size="sm" disabled={!managementEnabled || !guidedUiEnabled || action !== null} onClick={openCreate}><Plus className="h-4 w-4" />{t("settings.reports.subscriptions.new", { defaultValue: "新建订阅" })}</Button></div>
        {showEditor ? <SubscriptionEditor form={subscription} editing={editingId !== null} busy={action !== null} policies={policies} options={options} optionsLoading={optionsLoading} onChange={(patch) => setSubscription((current) => ({ ...current, ...patch }))} onCancel={closeEditor} onPreview={previewSubscription} onSubmit={submitSubscription} /> : null}
        <div className="divide-y border-y">{(payload?.subscriptions ?? []).map((item) => <SubscriptionRow key={item.subscription_id} item={item} policies={policies} busy={!managementEnabled || !guidedUiEnabled || action !== null} onEdit={openEdit} onAction={(next, values) => void run(next, values)} />)}{!payload?.subscriptions.length ? <div className="py-8 text-center text-sm text-muted-foreground">{t("settings.reports.subscriptions.empty", { defaultValue: "暂无订阅" })}</div> : null}</div>

        {tenantMappingsView ? (
          <div className="space-y-3 border-t pt-4">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <h3 className="text-sm font-semibold">{t("settings.reports.tenantAliases.title", { defaultValue: "客户名称映射" })}</h3>
                <p className="mt-1 max-w-2xl text-xs leading-5 text-muted-foreground">{t("settings.reports.tenantAliases.description", { defaultValue: "别名用于在聊天中按名称匹配客户与报表显示中文名。整表覆盖 config.json 的 tenantMappings，保存立即生效；目标客户必须存在于 Cube 实时目录。" })}</p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <span className={cn("rounded-full border px-2 py-0.5 text-xs", tenantMappingsView.source === "override" ? "border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300" : "border-border text-muted-foreground")}>{tenantMappingsView.source === "override" ? t("settings.reports.tenantAliases.sourceOverride", { defaultValue: "页面覆盖" }) : t("settings.reports.tenantAliases.sourceDefault", { defaultValue: "默认（config.json）" })}</span>
                <Button size="sm" variant="outline" disabled={action !== null} onClick={() => setTenantAliasDraft((current) => [...current, { alias: "", tenantId: "" }])}><Plus className="h-4 w-4" />{t("settings.reports.tenantAliases.add", { defaultValue: "添加映射" })}</Button>
              </div>
            </div>
            <div className="space-y-2">
              {tenantAliasDraft.map((row, index) => (
                <div key={index} className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)_auto] sm:items-center">
                  <Input
                    value={row.alias}
                    onChange={(event) => setTenantAliasDraft((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, alias: event.target.value } : item))}
                    placeholder={t("settings.reports.tenantAliases.aliasPlaceholder", { defaultValue: "客户名称，如：阳春面" })}
                    disabled={action !== null}
                    aria-label={t("settings.reports.tenantAliases.aliasLabel", { defaultValue: "客户名称" })}
                  />
                  {options?.tenants.length ? (
                    <select
                      className={SELECT_CLASS}
                      value={row.tenantId}
                      onChange={(event) => setTenantAliasDraft((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, tenantId: event.target.value } : item))}
                      disabled={action !== null}
                      aria-label={t("settings.reports.tenantAliases.tenantLabel", { defaultValue: "目标客户" })}
                    >
                      <option value="">{t("settings.reports.tenantAliases.tenantPlaceholder", { defaultValue: "请选择目标客户" })}</option>
                      {options.tenants.map((tenant) => <option key={tenant.tenant_id} value={tenant.tenant_id}>{tenant.display_name}（{tenant.tenant_id}）</option>)}
                    </select>
                  ) : (
                    <Input
                      value={row.tenantId}
                      onChange={(event) => setTenantAliasDraft((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, tenantId: event.target.value } : item))}
                      placeholder={t("settings.reports.tenantAliases.tenantIdPlaceholder", { defaultValue: "客户 tenant ID" })}
                      disabled={action !== null}
                      aria-label={t("settings.reports.tenantAliases.tenantLabel", { defaultValue: "目标客户" })}
                    />
                  )}
                  <Button size="icon" variant="ghost" disabled={action !== null} title={t("settings.reports.tenantAliases.remove", { defaultValue: "删除该映射" })} aria-label={t("settings.reports.tenantAliases.remove", { defaultValue: "删除该映射" })} onClick={() => setTenantAliasDraft((current) => current.filter((_item, itemIndex) => itemIndex !== index))}><Trash2 className="h-4 w-4 text-destructive" /></Button>
                </div>
              ))}
              {!tenantAliasDraft.length ? <p className="text-xs text-muted-foreground">{t("settings.reports.tenantAliases.empty", { defaultValue: "暂无别名。添加后，聊天中可直接用名称匹配客户。" })}</p> : null}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                disabled={action !== null || tenantAliasDraft.some((row) => !row.alias.trim() || !row.tenantId.trim())}
                onClick={() => {
                  const mapping: Record<string, string> = {};
                  for (const row of tenantAliasDraft) {
                    mapping[row.alias.trim()] = row.tenantId.trim();
                  }
                  void run("tenant_mappings_update", { tenant_mappings: mapping });
                }}
              >
                {t("settings.reports.tenantAliases.save", { defaultValue: "保存映射" })}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={action !== null || tenantMappingsView.source !== "override"}
                title={t("settings.reports.tenantAliases.resetTitle", { defaultValue: `恢复为 config.json 默认（${Object.keys(tenantMappingsView.default_values).length} 条）`, count: Object.keys(tenantMappingsView.default_values).length })}
                onClick={() => void run("tenant_mappings_reset", {})}
              >
                <RotateCcw className="h-4 w-4" />{t("settings.reports.tenantAliases.reset", { defaultValue: "恢复默认" })}
              </Button>
            </div>
          </div>
        ) : null}
      </section> : null}

      {tab === "permissions" ? <section className="space-y-5">
        <div className="flex items-center justify-between border-y py-3"><div className="flex items-start gap-3"><ShieldCheck className="mt-0.5 h-4 w-4 text-muted-foreground" /><div><div className="text-sm font-medium">Report RBAC</div><div className="text-xs text-muted-foreground">{t("settings.reports.permissions.rbacDescription", { defaultValue: "启用后校验 Connector、Template、客户、模型和订阅模板授权。" })}</div></div></div><ToggleButton checked={Boolean(payload?.policy.rbac_enabled)} disabled={action !== null} label="Report RBAC" onChange={(enabled) => void run("rbac", { enabled: String(enabled) })} /></div>
        <div className="space-y-3"><h3 className="text-sm font-semibold">Grant editor</h3><div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4"><Input value={grant.channel} onChange={(event) => setGrant({ ...grant, channel: event.target.value })} placeholder="Channel" /><Input value={grant.user_id} onChange={(event) => setGrant({ ...grant, user_id: event.target.value })} placeholder="User open_id" /><select className={SELECT_CLASS} value={grant.resource_type} onChange={(event) => setGrant({ ...grant, resource_type: event.target.value })}>{(payload?.policy.resource_types ?? []).map((item) => <option key={item} value={item}>{item}</option>)}</select><Input value={grant.resource_id} onChange={(event) => setGrant({ ...grant, resource_id: event.target.value })} placeholder="Resource ID or *" /></div><div className="flex flex-wrap gap-2"><Button size="sm" disabled={!grant.user_id || !grant.resource_id || action !== null} onClick={() => void run("grant", grant)}>Grant</Button><Button variant="outline" size="sm" disabled={!grant.user_id || !grant.resource_id || action !== null} onClick={() => void run("revoke", grant)}>Revoke</Button><Button variant="ghost" size="sm" disabled={!grant.user_id} onClick={() => void load({ channel: grant.channel, user_id: grant.user_id })}>Inspect user</Button></div>{payload?.grants.length ? <div className="divide-y border-y text-sm">{payload.grants.map((item) => <div key={`${item.resource_type}:${item.resource_id}`} className="flex items-center justify-between py-2"><span>{item.resource_type}</span><code className="text-xs text-muted-foreground">{item.resource_id}</code></div>)}</div> : null}</div>
      </section> : null}

      {tab === "flags" ? (() => {
        const featureFlags = payload?.feature_flags ?? [];
        const groups = featureFlags.reduce<Record<string, ReportingFeatureFlag[]>>((acc, item) => {
          const key = item.group || t("settings.reports.flags.otherGroup", { defaultValue: "其他" });
          acc[key] = [...(acc[key] ?? []), item];
          return acc;
        }, {});
        return <section className="space-y-4">
          <div><h3 className="text-sm font-semibold">{t("settings.reports.flags.title", { defaultValue: "功能开关" })}</h3><p className="mt-1 text-xs text-muted-foreground">{t("settings.reports.flags.description", { defaultValue: "切换立即生效，无需重启网关；覆盖值保存在报表状态库并记录管理审计。计算口径、阈值与扩展连接（Grafana/企业微信/钉钉/成本 TokenAPI）仍属部署配置，需修改配置文件并重启。" })}</p></div>
          {Object.entries(groups).map(([group, items]) => (
            <div key={group} className="space-y-1">
              <h4 className="pt-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{group}</h4>
              <div className="divide-y border-y">
                {items.map((item) => (
                  <div key={item.key} className="flex items-center justify-between gap-3 py-3">
                    <div className="min-w-0">
                      <div className="text-sm font-medium">{item.label}</div>
                      <code className="text-xs text-muted-foreground">{item.key} · {item.source === "override" ? t("settings.reports.flags.override", { defaultValue: "页面覆盖" }) : t("settings.reports.flags.defaultValue", { defaultValue: "默认值" })}</code>
                    </div>
                    <div className="flex items-center gap-2">
                      {item.source === "override" ? (
                        <Button variant="ghost" size="sm" disabled={action !== null} title={t("settings.reports.flags.reset", { defaultValue: "恢复默认值" })} aria-label={t("settings.reports.flags.resetAria", { defaultValue: `恢复 ${item.label} 默认值`, label: item.label })} onClick={() => void run("feature_flag_reset", { flag: item.key })}><RotateCcw className="h-4 w-4" /></Button>
                      ) : null}
                      <ToggleButton checked={item.enabled} disabled={action !== null} label={item.label} onChange={(enabled) => void run("feature_flag", { flag: item.key, enabled: String(enabled) })} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </section>;
      })() : null}

      <div className="flex items-center gap-2 text-xs text-muted-foreground"><Database className="h-3.5 w-3.5" />{payload?.storage.backend} · {payload?.storage.retention_days}-day run retention · onboarding v{payload?.onboarding_version}</div>
      {payload?.construction ? (
        <div className="text-xs text-muted-foreground">
          {t("settings.reports.construction.label", { defaultValue: "计算口径（只读，重启生效）：" })}
          {t("settings.reports.construction.healthSemantics", { defaultValue: "健康语义" })} {payload.construction.cube_health_semantics_v2 ? "v2" : "v1"}
          {" · "}{t("settings.reports.construction.healthCard", { defaultValue: "健康卡片" })} {payload.construction.cube_health_card_v2 ? "v2" : "v1"}
          {" · "}{t("settings.reports.construction.ttftDetail", { defaultValue: "TTFT 明细" })} {payload.construction.cube_ttft_detail ? t("settings.reports.construction.on", { defaultValue: "开" }) : t("settings.reports.construction.off", { defaultValue: "关" })}
          {" · "}{t("settings.reports.construction.usageSemantics", { defaultValue: "用量语义" })} {payload.construction.cube_usage_semantics_v2 ? "v2" : "v1"}
          {" · "}{t("settings.reports.construction.providerDetail", { defaultValue: "供应商明细" })} {payload.construction.cube_provider_quality_detail ? t("settings.reports.construction.on", { defaultValue: "开" }) : t("settings.reports.construction.off", { defaultValue: "关" })}
          {" · "}{t("settings.reports.construction.wecomRenderer", { defaultValue: "企微渲染" })} {payload.construction.wecom_renderer ? t("settings.reports.construction.on", { defaultValue: "开" }) : t("settings.reports.construction.off", { defaultValue: "关" })}
          {" · "}{t("settings.reports.construction.dingtalkRenderer", { defaultValue: "钉钉渲染" })} {payload.construction.dingtalk_renderer ? t("settings.reports.construction.on", { defaultValue: "开" }) : t("settings.reports.construction.off", { defaultValue: "关" })}
          {" · "}Grafana {payload.construction.grafana_connector ? t("settings.reports.construction.on", { defaultValue: "开" }) : t("settings.reports.construction.off", { defaultValue: "关" })}
          {" · "}{t("settings.reports.construction.costConnector", { defaultValue: "成本连接" })} {payload.construction.cost_connector ? t("settings.reports.construction.on", { defaultValue: "开" }) : t("settings.reports.construction.off", { defaultValue: "关" })}
        </div>
      ) : null}
    </div>
  );
}
