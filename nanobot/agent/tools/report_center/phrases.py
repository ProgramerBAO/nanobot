"""Routing phrase regexes and shared private constants for the report center."""

from __future__ import annotations

import re
from dataclasses import dataclass

_HOME_RE = re.compile(
    r"^(?:请)?(?:打开|显示|查看|进入)?(?:报表中心|报表菜单|功能菜单|菜单|帮助|你能做什么|你会什么|有哪些功能)[？?。！!]*$"
)
_RECENT_RE = re.compile(r"^(?:查看|打开|显示)?(?:我的)?最近报表[？?。！!]*$")
_SUBSCRIPTIONS_RE = re.compile(r"^(?:查看|打开|显示|管理)?(?:我的)?(?:报表)?订阅[？?。！!]*$")
_SUBSCRIPTION_CONTROL_RE = re.compile(
    r"^(?P<operation>启用|停用)订阅：(?P<subscription_id>[0-9a-f]{1,64})$"
)
_BRIEF_SUBSCRIPTION_RE = re.compile(
    r"^订阅(?P<period>日报|周报|月报)简报：客户 (?P<tenant>[^，（]{1,128})"
    r"(?:（ID (?P<tenant_id>[^）]{1,128})）)?，模型 (?P<models>[^，]{1,512})$"
)
# A quoted report owns its verified scope.  Only these explicit entity words
# indicate that the user is asking to override that scope; schedule/recipient
# wording such as “工作日上午十点发送给我” must never make the classifier pick
# a new default tenant or template.  Keeping this boundary server-side avoids
# turning an LLM omission into a silently narrowed subscription.
_REFERENCE_SCOPE_OVERRIDE_RE = re.compile(
    r"(?:全部|所有|全量|指定|仅|只|改为|换成|换为)?\s*"
    r"(?:客户|租户|用户|模型|endpoint|项目|供应商)",
    re.IGNORECASE,
)
_HEALTH_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成|打开|显示)?(?:过去\s*15\s*分钟|近\s*15\s*分钟)?(?:平台)?健康(?:报告|情况)?[？?。！!]*$"
)
_COST_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成|打开|显示)?(?:成本|费用|账单|余额|账户)(?:报告|情况|概览)?[？?。！!]*$"
)
_PROVIDER_QUALITY_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成|打开|显示)?\s*"
    r"(?:(?:过去\s*15\s*分钟|近\s*15\s*分钟|昨天)\s*)?"
    r"(?:各\s*)?"
    r"(?:供应商质量|供应商性能|供应商详细情况|平台供应商质量)"
    r"(?:报告|情况|排行|对比)?[？?。！!]*$",
    re.IGNORECASE,
)
_NAMED_PROVIDER_QUALITY_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成|打开|显示)?\s*"
    r"供应商\s*(?P<provider>[A-Za-z0-9._-]+)\s*(?:的)?\s*"
    r"(?:质量|性能|详细情况)(?:报告|情况|排行|对比)?[？?。！!]*$",
    re.IGNORECASE,
)
_MODEL_PROVIDER_QUALITY_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成|打开|显示)?\s*"
    r"(?P<model>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:模型)?\s*"
    r"各供应商(?:质量|性能)(?:报告|情况|排行|对比)?[？?。！!]*$",
    re.IGNORECASE,
)
_PROVIDER_QUALITY_EMPTY_RE = re.compile(
    r"^查看(?:本次)?(?P<period>近\s*15\s*分钟|昨日|上一完整周|"
    r"自定义区间\s+(?P<start_date>\d{4}-\d{2}-\d{2})\s+至\s+(?P<end_date>\d{4}-\d{2}-\d{2}))"
    r"供应商无用量[？?。！!]*$"
)
_CUBE_PERIOD_RE = re.compile(
    r"^(?:请)?(?:我要|生成|查看|打开|显示)?\s*"
    r"(?P<template>简报|详细|完整)?(?P<period>日报|周报|月报)[？?。！!]*$"
)
_MODEL_CUBE_PERIOD_RE = re.compile(
    r"^(?:请)?(?:我要|我需要|给我|生成|查看|打开|显示)?\s*"
    r"(?P<model>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:模型)?\s*(?:的)?\s*"
    r"(?P<template>简报|详细|完整)?(?P<period>日报|周报|月报)\s*"
    r"(?:全部|所有|全体)?\s*(?:客户|用户|租户)?[？?。！!]*$",
    re.IGNORECASE,
)
_MULTI_SCOPE_BRIEF_RE = re.compile(
    r"^(?:请)?(?:打开|查看|生成)?(?:Cube\s*)?多客户多模型(?P<period>日报|周报)?简报[？?。！!]*$",
    re.IGNORECASE,
)
_MULTI_SCOPE_WEEKLY_RE = re.compile(
    r"^(?:请)?(?:打开|查看|生成)?(?:Cube\s*)?各客户模型周报(?:简报)?[？?。！!]*$",
    re.IGNORECASE,
)
_MULTI_SCOPE_NAMED_RE = re.compile(
    r"^(?P<tenants>.+?)(?:的)?(?:全部|所有|全量)模型(?:的)?多客户(?:多模型)?"
    r"(?P<period>日报|周报)简报[？?。！!]*$",
    re.IGNORECASE,
)
_CUSTOMER_MODEL_HOURLY_TPM_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成)?\s*(?P<tenants>.+?)\s*(?:的)?\s*"
    r"(?:全部|所有|全量)\s*模型\s*(?:的)?\s*"
    r"(?:上一|最近)\s*(?:完整)?\s*(?:一)?\s*小时\s*(?:TPM|tpm)\s*"
    r"(?:峰值和均值|均值和峰值|报告|报表)?[？?。！!]*$",
    re.IGNORECASE,
)
_CUSTOMER_MODEL_HOURLY_TPM_SELECTED_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成)?\s*(?P<tenants>.+?)\s+"
    r"(?P<models>[A-Za-z0-9][A-Za-z0-9._-]*(?:[、，,]\s*[A-Za-z0-9][A-Za-z0-9._-]*)*)\s*"
    r"(?:模型)?\s*(?:的)?\s*(?:上一|最近)\s*(?:完整)?\s*(?:一)?\s*小时\s*"
    r"(?:TPM|tpm)\s*(?:峰值和均值|均值和峰值|报告|报表)?[？?。！!]*$",
    re.IGNORECASE,
)
# This guard is intentionally broader than the deterministic parsers above.
# Hourly TPM requests that are incomplete must fail closed in ReportCenter
# instead of falling through to the legacy daily-usage matcher.
_HOURLY_TPM_SIGNAL_RE = re.compile(
    r"(?:上一|最近)\s*(?:完整)?\s*(?:一)?\s*小时.*(?:TPM|tpm)",
    re.IGNORECASE,
)
_MACHINE_TPM_RE = re.compile(
    r"^(?:请)?(?:查看|查询|生成)?\s*(?P<model>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"\s*(?:模型)?(?:的)?(?:单机|每台机器)\s*(?:折算)?\s*TPM"
    r"\s*(?:峰值)?\s*(?:报表)?[？?。！!]*$",
    re.IGNORECASE,
)
_FURTHER_ANALYSIS_RE = re.compile(
    r"^进一步分析（(?P<period>日报|周报|月报|区间报表)）："
    r"客户 (?P<tenant>[^，]{1,128})，模型 (?P<models>[^，]{1,512})，"
    r"日期 (?P<start>\d{4}-\d{2}-\d{2}) 至 (?P<end>\d{4}-\d{2}-\d{2})$"
)
_SAFE_CUBE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_ALLOWED_REPORT_PARAM_KEYS = frozenset(
    {
        "tenant_query",
        "tenants",
        "tenant_labels",
        "model",
        "models",
        "model_scope",
        "project",
        "endpoint",
        "provider",
        "all_tenants",
        "breakdown",
        "report_template",
        "granularity",
        "include_tpm",
        "report_selections",
        "comparison",
        "report_family",
        "subscription_period",
        "provider_id",
        "providers",
        "cluster",
        "report_variant",
        "tenant_scope",
        # Hourly TPM subscriptions carry their template id so the cron-run
        # compiler can select the hourly intent branch without guessing from
        # the period alone.
        "report_template_id",
    }
)


@dataclass(frozen=True, slots=True)
class _TenantMentionResolution:
    """Result of reconciling natural-language tenant names with live Cube IDs.

    ``values`` contains only labels that can be resolved back to one live
    tenant.  ``error`` is intentionally a small machine-readable category so
    the caller can choose a recovery UI without exposing catalog responses or
    silently narrowing a requested multi-tenant scope.
    """

    values: tuple[str, ...]
    error: str | None = None
    unresolved: tuple[str, ...] = ()


# Per-template report switches retired by the 2026-09-16 consolidation
# (phase 2d): their semantics moved into the always-enforced
# report_template_policies store table. The config fields below stay
# accepted for one migration window so existing config.json files keep
# loading (a warning is logged when set non-default); run
# `nanobot reports policy migrate-flags` and remove them from config.
