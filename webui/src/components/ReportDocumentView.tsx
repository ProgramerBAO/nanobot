import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ChevronDown,
  CircleHelp,
  Clock3,
  Database,
  Filter,
  Play,
  Search,
  XCircle,
} from "lucide-react";
import { useMemo, useState } from "react";

import type { AgentUIBlob, ReportActionRequest } from "@/lib/types";
import { cn } from "@/lib/utils";

interface ReportDocumentViewProps {
  document: AgentUIBlob;
  onAction?: (request: ReportActionRequest) => void;
  onCommand?: (command: string) => void;
}

type RecordValue = Record<string, unknown>;

function recordValue(value: unknown): RecordValue {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as RecordValue
    : {};
}

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : value == null ? fallback : String(value);
}

function blockData(block: unknown): RecordValue {
  return recordValue(recordValue(block).data);
}

function qualityLabel(value: unknown): string {
  const quality = stringValue(value).toLowerCase();
  if (quality === "complete") return "数据完整";
  if (quality === "partial") return "部分数据";
  if (quality === "missing") return "数据缺失";
  return value ? String(value) : "未标记";
}

function qualityTone(value: unknown): string {
  const quality = stringValue(value).toLowerCase();
  if (quality === "complete") return "border-emerald-500/25 bg-emerald-500/8 text-emerald-700 dark:text-emerald-300";
  if (quality === "partial") return "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300";
  return "border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300";
}

function statusIcon(status: string) {
  if (status.includes("异常")) return <XCircle className="h-4 w-4" aria-hidden />;
  if (status.includes("关注")) return <AlertTriangle className="h-4 w-4" aria-hidden />;
  if (status.includes("数据")) return <Database className="h-4 w-4" aria-hidden />;
  return <CheckCircle2 className="h-4 w-4" aria-hidden />;
}

function reportStatus(document: AgentUIBlob): { value: string; reason: string } {
  for (const block of document.blocks ?? []) {
    if (block.kind !== "metrics") continue;
    for (const item of (blockData(block).items as unknown[] ?? [])) {
      const value = recordValue(item);
      if (stringValue(value.label) === "总体状态") {
        return { value: stringValue(value.value, "数据不足"), reason: stringValue(value.change) };
      }
    }
  }
  return { value: "报表结果", reason: "" };
}

function contextLines(document: AgentUIBlob): string[] {
  const context = recordValue(document.context);
  const current = recordValue(context.current_window);
  const baseline = recordValue(context.baseline_window);
  const comparisons = (context.comparison_windows as unknown[] ?? []).map(recordValue);
  const sources = (context.sources as unknown[] ?? [])
    .map((source) => {
      const item = recordValue(source);
      const system = stringValue(item.system);
      const route = stringValue(item.route);
      return system && route ? `${system} / ${route}` : system || route;
    })
    .filter(Boolean);
  const aggregations = (context.metric_definitions as unknown[] ?? [])
    .map((item) => stringValue(recordValue(item).aggregation))
    .filter(Boolean);
  const comparisonLines = comparisons.length
    ? comparisons.map((comparison) => {
        const window = recordValue(comparison.window);
        return `对比（${stringValue(comparison.label, "对比周期")}）：${stringValue(window.start, "暂无")} - ${stringValue(window.end, "暂无")}`;
      })
    : [baseline.start || baseline.end ? `对比基准：${baseline.start || "暂无"} - ${baseline.end || "暂无"}` : "对比基准：暂无可比基准"];
  return [
    current.start || current.end ? `统计窗口：${current.start || "暂无"} - ${current.end || "暂无"}` : "",
    ...comparisonLines,
    context.timezone ? `时区：${context.timezone}` : "",
    sources.length ? `来源：${[...new Set(sources)].join("；")}` : "",
    aggregations.length ? `口径：${[...new Set(aggregations)].join("；")}` : "",
  ].filter(Boolean);
}

function valueText(value: unknown): string {
  if (value == null || value === "") return "暂无数据";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function ReportMetrics({ data }: { data: RecordValue }) {
  const items = (data.items as unknown[] ?? []).map(recordValue);
  return (
    <section className="border-b border-border/70 px-4 py-4" aria-label="核心指标">
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        <BarChart3 className="h-4 w-4" aria-hidden />
        核心指标
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {items.map((item, index) => {
          const label = stringValue(item.label, "指标");
          const value = stringValue(item.value, "暂无数据");
          const change = stringValue(item.change);
          const comparisons = (item.comparisons as unknown[] ?? []).map(recordValue);
          return (
            <div key={`${label}-${index}`} className="min-w-0 border-l-2 border-primary/30 bg-muted/25 px-3 py-2.5">
              <div className="truncate text-xs text-muted-foreground">{label}</div>
              <div className="mt-1 break-words text-lg font-semibold tracking-normal text-foreground">{value}</div>
              {comparisons.length
                ? comparisons.map((comparison, comparisonIndex) => (
                    <div key={`${stringValue(comparison.key)}-${comparisonIndex}`} className="mt-1 break-words text-xs text-muted-foreground">
                      {stringValue(comparison.label, "对比")}：{stringValue(comparison.change, "暂无可比基准")}
                    </div>
                  ))
                : null}
              {!comparisons.length && change ? <div className="mt-1 break-words text-xs text-muted-foreground">较基准 {change}</div> : null}
              {item.valid_sample_count != null ? (
                <div className="mt-1 text-xs text-muted-foreground">有效样本 {valueText(item.valid_sample_count)}</div>
              ) : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function ReportTable({ data }: { data: RecordValue }) {
  const headers = (data.headers as unknown[] ?? []).map(String);
  const columns = (data.columns as unknown[] ?? []).map(recordValue);
  const keys = columns.length ? columns.map((column) => stringValue(column.name)) : headers;
  const labels = columns.length ? columns.map((column) => stringValue(column.display_name, stringValue(column.name))) : headers;
  const rows = (data.rows as unknown[] ?? []).map(recordValue);
  const title = stringValue(data.title, "明细");
  const table = (
    <>
      {rows.length === 0 ? (
        <div className="border border-dashed border-border px-3 py-4 text-sm text-muted-foreground">暂无可展示数据</div>
      ) : (
        <div className="overflow-x-auto border border-border/70">
          <table className="w-full min-w-[680px] border-collapse text-left text-xs">
            <thead className="bg-muted/55 text-muted-foreground">
              <tr>{labels.map((label, index) => <th key={`${label}-${index}`} className="whitespace-nowrap px-3 py-2 font-medium">{label}</th>)}</tr>
            </thead>
            <tbody className="divide-y divide-border/60">
              {rows.map((row, rowIndex) => (
                <tr key={rowIndex} className="align-top hover:bg-muted/25">
                  {keys.map((key, columnIndex) => <td key={`${key}-${columnIndex}`} className="max-w-[240px] break-words px-3 py-2 text-foreground/85">{valueText(row[key])}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
  if (data.collapsed === true) {
    return (
      <section className="border-b border-border/70 px-4 py-4" aria-label={title}>
        <details className="group">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-sm font-semibold text-foreground">
            <span className="min-w-0 break-words">{stringValue(data.collapsed_label, title)}</span>
            <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-180" aria-hidden />
          </summary>
          <div className="mt-3">{table}</div>
        </details>
      </section>
    );
  }
  return (
    <section className="border-b border-border/70 px-4 py-4" aria-label={title}>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="min-w-0 break-words text-sm font-semibold text-foreground">{title}</h3>
        {rows.length ? <span className="shrink-0 text-xs text-muted-foreground">{rows.length} 条</span> : null}
      </div>
      {table}
    </section>
  );
}

function ProviderSelector({ data, onAction }: { data: RecordValue; onAction?: (request: ReportActionRequest) => void }) {
  const options = (data.options as unknown[] ?? []).map(recordValue);
  const allOption = recordValue(data.all_option);
  const defaultOptions = (data.default_options as unknown[] ?? []).map(String);
  const [selected, setSelected] = useState<string[]>(defaultOptions.length ? defaultOptions : [stringValue(allOption.token)]);
  const [period, setPeriod] = useState(stringValue(data.default_period, "recent15m"));
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [search, setSearch] = useState("");
  const allToken = stringValue(allOption.token);
  const visibleOptions = useMemo(() => options.filter((option) => {
    const text = `${option.label} ${option.description}`.toLowerCase();
    return text.includes(search.trim().toLowerCase());
  }), [options, search]);
  const allSelected = selected.includes(allToken);

  const chooseAll = () => setSelected([allToken]);
  const toggleOption = (token: string) => {
    setSelected((current) => {
      const withoutAll = current.filter((item) => item !== allToken);
      if (withoutAll.includes(token)) {
        const next = withoutAll.filter((item) => item !== token);
        return next.length ? next : [allToken];
      }
      return [...withoutAll, token];
    });
  };
  const submit = () => {
    if (!onAction) return;
    onAction({
      interactionId: stringValue(data.interaction_id),
      submitToken: stringValue(data.submit_token),
      selectedOptions: selected,
      period,
      startDate: period === "range" ? startDate : undefined,
      endDate: period === "range" ? endDate : undefined,
    });
  };
  return (
    <section className="border-b border-border/70 px-4 py-4" aria-label="选择供应商">
      <div className="mb-3 flex items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center bg-primary/10 text-primary"><Filter className="h-4 w-4" aria-hidden /></div>
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">选择供应商</h3>
          <p className="mt-1 text-xs text-muted-foreground">可选择一个或多个供应商进行对比。全部供应商模式会折叠无用量对象。</p>
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_180px]">
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" aria-hidden />
          <input aria-label="搜索供应商" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索供应商" className="h-9 w-full border border-input bg-background pl-9 pr-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" />
        </div>
        <label className="flex items-center gap-2 text-sm text-muted-foreground">
          <Clock3 className="h-4 w-4" aria-hidden />
          <span className="sr-only">统计周期</span>
          <select aria-label="统计周期" value={period} onChange={(event) => setPeriod(event.target.value)} className="h-9 min-w-0 flex-1 border border-input bg-background px-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring">
            {(data.periods as unknown[] ?? []).map((item) => { const option = recordValue(item); return <option key={stringValue(option.value)} value={stringValue(option.value)}>{stringValue(option.label, stringValue(option.value))}</option>; })}
          </select>
        </label>
      </div>
      {period === "range" ? (
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <label className="grid gap-1 text-xs text-muted-foreground">开始日期<input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} className="h-9 border border-input bg-background px-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring" /></label>
          <label className="grid gap-1 text-xs text-muted-foreground">结束日期<input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} className="h-9 border border-input bg-background px-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring" /></label>
        </div>
      ) : null}
      <div className="mt-3 max-h-64 overflow-y-auto border border-border/70 p-2">
        <label className={cn("flex cursor-pointer items-start gap-3 px-2 py-2", allSelected ? "bg-primary/8" : "hover:bg-muted/35")}>
          <input type="checkbox" checked={allSelected} onChange={chooseAll} className="mt-0.5 h-4 w-4 accent-primary" />
          <span className="min-w-0"><span className="block text-sm font-medium">{stringValue(allOption.label, "全部供应商")}</span><span className="block text-xs text-muted-foreground">{stringValue(allOption.description)}</span></span>
        </label>
        {visibleOptions.map((option) => {
          const token = stringValue(option.token);
          const checked = !allSelected && selected.includes(token);
          return <label key={token} className="flex cursor-pointer items-start gap-3 px-2 py-2 hover:bg-muted/35"><input type="checkbox" checked={checked} onChange={() => toggleOption(token)} className="mt-0.5 h-4 w-4 accent-primary" /><span className="min-w-0"><span className="block break-words text-sm font-medium">{stringValue(option.label, "未命名供应商")}</span><span className="block break-words text-xs text-muted-foreground">{stringValue(option.description)}{option.enabled === false ? " · 未启用" : ""}</span></span></label>;
        })}
        {!visibleOptions.length ? <div className="px-2 py-4 text-sm text-muted-foreground">没有匹配的供应商</div> : null}
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
        <span className="text-xs text-muted-foreground">已选 {allSelected ? "全部" : selected.length} 个</span>
        <button type="button" onClick={submit} disabled={!data.interaction_id || !data.submit_token} className="inline-flex h-9 items-center gap-2 bg-primary px-3 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"><Play className="h-4 w-4" aria-hidden />生成报告</button>
      </div>
    </section>
  );
}

export function ReportDocumentView({ document, onAction, onCommand }: ReportDocumentViewProps) {
  const status = reportStatus(document);
  const context = contextLines(document);
  const notes: string[] = [];
  for (const block of document.blocks ?? []) {
    if (block.kind === "note") {
      const content = stringValue(blockData(block).content);
      if (content) notes.push(content);
    }
  }
  const actions = (document.blocks ?? []).flatMap((block) => block.kind === "actions" ? (blockData(block).actions as unknown[] ?? []).map(recordValue) : []);
  const isSelector = document.document_id === "provider_quality_selector";
  return (
    <article className="w-full max-w-[min(100%,64rem)] overflow-hidden border border-border/80 bg-card shadow-sm" data-testid="report-document">
      <header className="border-b border-border/70 px-4 py-4 sm:px-5">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0"><h2 className="break-words text-base font-semibold tracking-normal text-foreground">{stringValue(document.title, "报表")}</h2>{document.subtitle ? <p className="mt-1 break-words text-sm text-muted-foreground">{document.subtitle}</p> : null}</div>
          {!isSelector ? <div className={cn("inline-flex shrink-0 items-center gap-1.5 border px-2 py-1 text-xs font-medium", qualityTone(document.quality))}>{statusIcon(status.value)}{status.value} · {qualityLabel(document.quality)}</div> : null}
        </div>
        {!isSelector && status.reason ? <p className="mt-3 break-words text-xs text-muted-foreground">{status.reason}</p> : null}
        {context.length ? <div className="mt-3 grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">{context.map((line) => <div key={line} className="min-w-0 break-words">{line}</div>)}</div> : null}
      </header>
      {document.blocks?.map((block, index) => {
        const data = blockData(block);
        if (block.kind === "selector") return <ProviderSelector key={index} data={data} onAction={onAction} />;
        if (block.kind === "metrics") return <ReportMetrics key={index} data={data} />;
        if (block.kind === "table") return <ReportTable key={index} data={data} />;
        if (block.kind === "markdown") return <section key={index} className="border-b border-border/70 px-4 py-4 text-sm leading-6 text-foreground/85">{stringValue(data.content)}</section>;
        if (block.kind === "note") return <section key={index} className={cn("border-b border-border/70 px-4 py-3 text-xs leading-5 whitespace-pre-wrap", stringValue(data.severity) === "warning" ? "bg-amber-500/8 text-amber-800 dark:text-amber-200" : "text-muted-foreground")}><div className="flex gap-2"><CircleHelp className="mt-0.5 h-4 w-4 shrink-0" aria-hidden /><div className="min-w-0 break-words">{stringValue(data.content)}</div></div></section>;
        if (block.kind === "actions") return null;
        return null;
      })}
      {actions.length ? <div className="flex flex-wrap gap-2 border-b border-border/70 px-4 py-3">{actions.map((action, index) => { const id = stringValue(action.action_id); return <button key={`${id}-${index}`} type="button" onClick={() => { if (id === "provider_quality_report") onCommand?.("供应商质量报告"); else if (id === "provider_quality_show_empty") onCommand?.(stringValue(action.command, "查看近15分钟供应商无用量")); }} className="inline-flex items-center gap-2 border border-border px-3 py-2 text-sm transition-colors hover:bg-muted/50"><ChevronDown className="h-4 w-4 rotate-[-90deg]" aria-hidden />{stringValue(action.label, "打开")}</button>; })}</div> : null}
      {document.warnings?.length ? <div className="border-b border-border/70 bg-amber-500/8 px-4 py-3 text-xs leading-5 text-amber-800 dark:text-amber-200"><div className="flex gap-2"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden /><span className="break-words">{document.warnings.join("；")}</span></div></div> : null}
      {!isSelector ? <details className="group px-4 py-3"><summary className="flex cursor-pointer list-none items-center gap-2 text-xs font-medium text-muted-foreground"><ChevronDown className="h-4 w-4 transition-transform group-open:rotate-180" aria-hidden />报表说明与读法</summary><div className="mt-3 space-y-1 whitespace-pre-wrap text-xs leading-5 text-muted-foreground">{notes.length ? notes.join("\n") : "错误率、延迟和 TTFT 越低越好；RPM/TPM 表示流量，不单独代表故障。"}</div></details> : null}
    </article>
  );
}
