"""Read-only Magik Cube daily reporting tool."""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yaml
from pydantic import Field

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema
from nanobot.config.paths import get_runtime_subdir
from nanobot.config_base import Base
from nanobot.utils.helpers import _write_text_atomic


class MagikCubeToolConfig(Base):
    """Connection and report settings for the Magik Cube admin API."""

    enable: bool = False
    base_url: str = ""
    api_prefix: str = "/api/v1"
    access_token: str = Field(default="", repr=False)
    account: str = ""
    password: str = Field(default="", repr=False)
    login_path: str = "/token-api/v1/accounts/login/with-password"
    extra_headers: dict[str, str] = Field(default_factory=dict)
    cluster_names: list[str] = Field(default_factory=list)
    proxy_namespace: str = "envoy-gateway-system"
    proxy_labels: str = "gateway.magikcompute.ai/name:magik-ai-gateway"
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    verify_ssl: bool = True
    pd_window_minutes: int = Field(default=15, ge=1, le=1440)
    max_pages: int = Field(default=10, ge=1, le=100)
    max_report_items: int = Field(default=20, ge=1, le=100)
    tenant_mappings: dict[str, str] = Field(default_factory=dict)


class MagikCubeApiError(RuntimeError):
    """Raised when the Magik Cube API returns an unsuccessful response."""


_PASSWORD_LOGIN_PATH = "token-api/v1/accounts/login/with-password"
_READ_ONLY_ROUTES = frozenset(
    {
        ("GET", "clusters"),
        ("GET", "gateway/proxy-configs"),
        ("GET", "gateway/usages"),
        ("GET", "inference/endpoints"),
        ("GET", "inference/model-configs"),
        ("GET", "tenants"),
        ("POST", "analysis/active-tenant-daily-usage/query"),
        ("POST", "analysis/endpoint-max-tpm/daily/query"),
        ("POST", "analysis/model-machine-usage/query"),
        ("POST", "quota-changes/list"),
    }
)


class MagikCubeClient:
    def __init__(
        self,
        config: MagikCubeToolConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._api_prefix = config.api_prefix.strip("/")
        headers = {"Accept": "application/json", **config.extra_headers}
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            headers=headers,
            timeout=config.timeout_seconds,
            verify=config.verify_ssl,
            transport=transport,
        )

    async def __aenter__(self) -> MagikCubeClient:
        if self._config.account and self._config.password:
            await self._login_with_password()
        elif self._config.access_token:
            self._client.headers["Authorization"] = f"Bearer {self._config.access_token}"
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self._client.aclose()

    async def _login_with_password(self) -> None:
        path = self._config.login_path.lstrip("/")
        if path != _PASSWORD_LOGIN_PATH:
            raise MagikCubeApiError("password login path is not on the strict allowlist")
        response = await self._client.post(
            path,
            json={"account": self._config.account, "password": self._config.password},
            follow_redirects=False,
        )
        data = self._decode_response("POST", self._config.login_path, response)
        token = _pick(data, "accessToken", "access_token", default="")
        if not token:
            raise MagikCubeApiError("password login succeeded but returned no access token")
        self._client.headers["Authorization"] = f"Bearer {token}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_method = method.upper()
        normalized_path = path.strip("/")
        if (normalized_method, normalized_path) not in _READ_ONLY_ROUTES:
            raise MagikCubeApiError(
                f"blocked non-allowlisted Magik Cube API request: "
                f"{normalized_method} /{normalized_path}"
            )
        api_path = "/".join(part for part in (self._api_prefix, normalized_path) if part)
        response = await self._client.request(
            normalized_method,
            api_path,
            params=params,
            json=json_body,
            follow_redirects=False,
        )
        return self._decode_response(method, path, response)

    @staticmethod
    def _decode_response(
        method: str, path: str, response: httpx.Response
    ) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise MagikCubeApiError(
                f"{method} {path} returned non-JSON HTTP {response.status_code}"
            ) from exc
        if response.is_error:
            message = payload.get("message") if isinstance(payload, dict) else None
            raise MagikCubeApiError(
                f"{method} {path} failed with HTTP {response.status_code}: {message or 'unknown error'}"
            )
        if not isinstance(payload, dict):
            raise MagikCubeApiError(f"{method} {path} returned an invalid response")
        if "code" in payload:
            code = payload.get("code")
            if code not in (0, 200, "0", "200", "OK"):
                raise MagikCubeApiError(
                    f"{method} {path} failed: {payload.get('message') or payload.get('reason') or code}"
                )
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        return payload


@dataclass(frozen=True)
class _Tenant:
    tenant_id: str
    name: str
    tags: tuple[str, ...] = ()


@dataclass
class _TenantMetrics:
    tokens: dict[str, int]
    max_tpm: dict[str, int]
    max_tpm_endpoint: dict[str, str]


def _pick(obj: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in obj:
            return obj[name]
    return default


def _as_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _format_number(value: int | float) -> str:
    absolute = abs(value)
    if absolute >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿"
    if absolute >= 10_000:
        return f"{value / 10_000:.2f}万"
    return f"{value:,.0f}"


def _format_change(current: int, baseline: int) -> str:
    if baseline == 0:
        if current == 0:
            return "持平"
        return "新增"
    change = (current - baseline) / baseline * 100
    arrow = "↑" if change > 0 else "↓" if change < 0 else ""
    return f"{arrow}{abs(change):.1f}%" if arrow else "持平"


def _format_quota_field(label: str, change: Any) -> str | None:
    if not isinstance(change, dict):
        return None
    old = _as_int(_pick(change, "oldValue", "old_value"))
    new = _as_int(_pick(change, "newValue", "new_value"))
    if old == new:
        return None
    return f"{label} {_format_number(old)} → {_format_number(new)}"


def _proxy_values(raw: str) -> dict[str, int]:
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError:
        return {}
    if not isinstance(doc, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("maxTPM", "maxRunningRequests", "maxNewSessions"):
        if key in doc:
            result[key] = _as_int(doc[key])
    return result


def _diff_proxy_snapshots(
    old: dict[str, dict[str, int]], new: dict[str, dict[str, int]]
) -> list[str]:
    changes: list[str] = []
    for key in sorted(old.keys() | new.keys()):
        if key not in old:
            changes.append(f"{key}：新增，{_format_proxy_values(new[key])}")
            continue
        if key not in new:
            changes.append(f"{key}：已删除")
            continue
        fields = []
        for field in sorted(old[key].keys() | new[key].keys()):
            before = old[key].get(field)
            after = new[key].get(field)
            if before != after:
                fields.append(f"{field} {before if before is not None else '无'} → {after if after is not None else '无'}")
        if fields:
            changes.append(f"{key}：" + "；".join(fields))
    return changes


def _format_proxy_values(values: dict[str, int]) -> str:
    if not values:
        return "未识别到限流字段"
    return "，".join(f"{key}={value}" for key, value in sorted(values.items()))


def _normalize_tenant_query(value: str) -> str:
    normalized = "".join(value.casefold().split())
    for suffix in ("用户", "客户", "租户"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized


class MagikCubeReporter:
    """Collect and format one daily report from the admin API."""

    def __init__(
        self,
        client: Any,
        config: MagikCubeToolConfig,
        snapshot_path: Path,
        timezone: str,
    ) -> None:
        self._client = client
        self._config = config
        self._snapshot_path = snapshot_path
        self._tz = ZoneInfo(timezone)
        self._warnings: list[str] = []

    async def generate(self, report_date: date, *, save_snapshot: bool = True) -> str:
        tenants = await self._list_key_accounts()
        if not tenants:
            raise MagikCubeApiError("未找到 is_key_account=true 的大客户租户")

        metrics_results = await asyncio.gather(
            *(self._tenant_metrics(tenant, report_date) for tenant in tenants)
        )
        metrics = {tenant.tenant_id: value for tenant, value in zip(tenants, metrics_results)}

        resources_task = asyncio.create_task(self._list_customer_resources(tenants))
        quota_task = asyncio.create_task(self._list_quota_changes(report_date))
        proxy_task = asyncio.create_task(self._proxy_changes(report_date, save_snapshot))
        machines_task = asyncio.create_task(self._machine_usage())
        pd_task = asyncio.create_task(self._observed_pd_ratio())
        resources, quotas, proxy_changes, machines, pd_summary = await asyncio.gather(
            resources_task, quota_task, proxy_task, machines_task, pd_task
        )

        quota_lines = self._format_quota_changes(quotas, resources)
        return self._render(
            report_date,
            tenants,
            metrics,
            quota_lines,
            proxy_changes,
            machines,
            pd_summary,
        )

    async def generate_usage_query(
        self,
        report_date: date,
        *,
        tenant_query: str = "",
        model: str = "",
    ) -> str:
        tenants = (
            await self._list_matching_tenants(tenant_query)
            if tenant_query
            else await self._list_key_accounts()
        )
        if not tenants:
            raise MagikCubeApiError(f"未找到匹配客户：{tenant_query}")
        metrics_results = await asyncio.gather(
            *(self._tenant_metrics(tenant, report_date, model=model) for tenant in tenants)
        )
        return self._render_usage_query(
            report_date,
            tenant_query,
            model,
            tenants,
            metrics_results,
        )

    async def _list_key_accounts(self) -> list[_Tenant]:
        tenants: list[_Tenant] = []
        for page in range(1, self._config.max_pages + 1):
            data = await self._client.request(
                "GET",
                "tenants",
                params={"page_num": page, "page_size": 500, "isKeyAccount": "true"},
            )
            items = data.get("list") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                is_ka = bool(_pick(item, "isKeyAccount", "is_key_account", default=False))
                if not is_ka:
                    continue
                tenant_id = str(_pick(item, "tenantId", "tenant_id", default=""))
                if tenant_id:
                    tenants.append(
                        _Tenant(
                            tenant_id=tenant_id,
                            name=str(_pick(item, "tenantName", "tenant_name", default=tenant_id)),
                            tags=tuple(
                                str(tag)
                                for tag in _pick(
                                    item, "tenantTags", "tenant_tags", default=[]
                                )
                            ),
                        )
                    )
            total = _as_int(data.get("total"))
            if page * 500 >= total or not items:
                break
        return tenants

    async def _list_matching_tenants(self, query: str) -> list[_Tenant]:
        normalized = _normalize_tenant_query(query)
        for alias, tenant_id in self._config.tenant_mappings.items():
            if _normalize_tenant_query(alias) == normalized:
                return [_Tenant(tenant_id=tenant_id, name=alias)]
        exact: list[_Tenant] = []
        partial: list[_Tenant] = []
        for page in range(1, self._config.max_pages + 1):
            data = await self._client.request(
                "GET", "tenants", params={"page_num": page, "page_size": 500}
            )
            items = data.get("list") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                tenant_id = str(_pick(item, "tenantId", "tenant_id", default=""))
                if not tenant_id:
                    continue
                name = str(_pick(item, "tenantName", "tenant_name", default=tenant_id))
                tags = tuple(
                    str(tag)
                    for tag in _pick(item, "tenantTags", "tenant_tags", default=[])
                )
                values = [_normalize_tenant_query(name)]
                tenant = _Tenant(tenant_id=tenant_id, name=name, tags=tags)
                if normalized in values:
                    exact.append(tenant)
                elif any(normalized in value for value in values):
                    partial.append(tenant)
            total = _as_int(data.get("total"))
            if page * 500 >= total or not items:
                break
        return (exact or partial)[: self._config.max_report_items]

    def _day_bounds(self, day: date) -> tuple[str, str]:
        start = datetime.combine(day, time.min, tzinfo=self._tz)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=self._tz)
        return start.isoformat(), end.isoformat()

    async def _tenant_metrics(
        self, tenant: _Tenant, report_date: date, *, model: str = ""
    ) -> _TenantMetrics:
        start_day = report_date - timedelta(days=7)
        start_time, _ = self._day_bounds(start_day)
        _, end_time = self._day_bounds(report_date)
        tokens: dict[str, int] = {}
        max_tpm: dict[str, int] = {}
        endpoints: dict[str, str] = {}
        try:
            body = {
                "startTime": start_time,
                "endTime": end_time,
                "tenantId": tenant.tenant_id,
                "topN": 0,
                "timeLevel": "TIME_LEVEL_DAY",
            }
            if model:
                body["model"] = model
            data = await self._client.request(
                "POST",
                "analysis/active-tenant-daily-usage/query",
                json_body=body,
            )
            for item in data.get("items") or []:
                for point in item.get("points") or []:
                    day = str(point.get("date") or "")[:10]
                    tokens[day] = tokens.get(day, 0) + _as_int(
                        _pick(point, "totalTokens", "total_tokens")
                    )
        except Exception as exc:
            self._warnings.append(f"{tenant.name} Token 用量获取失败：{exc}")

        try:
            body = {
                "startDate": start_day.isoformat(),
                "endDate": report_date.isoformat(),
                "tenantId": tenant.tenant_id,
            }
            if model:
                body["model"] = model
            data = await self._client.request(
                "POST",
                "analysis/endpoint-max-tpm/daily/query",
                json_body=body,
            )
            for item in data.get("items") or []:
                endpoint = str(item.get("endpoint") or "")
                for point in item.get("points") or []:
                    day = str(point.get("date") or "")[:10]
                    value = _as_int(_pick(point, "maxTpm", "max_tpm"))
                    if value >= max_tpm.get(day, -1):
                        max_tpm[day] = value
                        endpoints[day] = endpoint
        except Exception as exc:
            self._warnings.append(f"{tenant.name} 峰值 TPM 获取失败：{exc}")
        return _TenantMetrics(tokens=tokens, max_tpm=max_tpm, max_tpm_endpoint=endpoints)

    def _render_usage_query(
        self,
        report_date: date,
        tenant_query: str,
        model: str,
        tenants: list[_Tenant],
        metrics: list[_TenantMetrics],
    ) -> str:
        previous = report_date - timedelta(days=1)
        week_ago = report_date - timedelta(days=7)
        day = report_date.isoformat()
        total_tokens = sum(item.tokens.get(day, 0) for item in metrics)
        peak_tpm = max((item.max_tpm.get(day, 0) for item in metrics), default=0)
        filters = []
        if tenant_query:
            filters.append(f"客户={tenant_query}")
        if model:
            filters.append(f"模型={model}")
        lines = [
            f"📈 指定用量查询 · {day}",
            f"筛选：{'，'.join(filters) if filters else '全部大客户'}",
            f"匹配 {len(tenants)} 个租户｜Token 合计 {_format_number(total_tokens)}｜最高峰值 TPM {_format_number(peak_tpm)}",
            "",
        ]
        for tenant, item in zip(tenants, metrics):
            current_tokens = item.tokens.get(day, 0)
            current_tpm = item.max_tpm.get(day, 0)
            endpoint = item.max_tpm_endpoint.get(day)
            endpoint_suffix = f"（{endpoint}）" if endpoint else ""
            lines.extend(
                [
                    f"• {tenant.name}",
                    f"  Token {_format_number(current_tokens)}｜较前日 {_format_change(current_tokens, item.tokens.get(previous.isoformat(), 0))}｜较7日前 {_format_change(current_tokens, item.tokens.get(week_ago.isoformat(), 0))}",
                    f"  峰值TPM {_format_number(current_tpm)}{endpoint_suffix}｜较前日 {_format_change(current_tpm, item.max_tpm.get(previous.isoformat(), 0))}｜较7日前 {_format_change(current_tpm, item.max_tpm.get(week_ago.isoformat(), 0))}",
                ]
            )
        if len(tenants) > 1:
            lines.extend(
                [
                    "",
                    "提示：客户关键词匹配到多个租户，以上合计包含全部明细；如需单租户，请直接使用租户名查询。",
                ]
            )
        if self._warnings:
            lines.extend(["", "数据提示"])
            lines.extend(f"• {warning}" for warning in self._warnings[:10])
        return "\n".join(lines)

    async def _list_customer_resources(self, tenants: list[_Tenant]) -> dict[str, _Tenant]:
        mapping: dict[str, _Tenant] = {}

        async def load(tenant: _Tenant, path: str, id_names: tuple[str, str]) -> None:
            try:
                for page in range(1, self._config.max_pages + 1):
                    data = await self._client.request(
                        "GET",
                        path,
                        params={
                            "tenantId": tenant.tenant_id,
                            "page_num": page,
                            "page_size": 500,
                        },
                    )
                    items = data.get("list") or []
                    for item in items:
                        if isinstance(item, dict):
                            entity_id = str(_pick(item, *id_names, default=""))
                            if entity_id:
                                mapping[entity_id] = tenant
                    if page * 500 >= _as_int(data.get("total")) or not items:
                        break
            except Exception as exc:
                self._warnings.append(f"{tenant.name} 配额资源映射失败：{exc}")

        await asyncio.gather(
            *(
                load(tenant, path, ids)
                for tenant in tenants
                for path, ids in (
                    ("inference/endpoints", ("endpointId", "endpoint_id")),
                    ("inference/model-configs", ("modelConfigId", "model_config_id")),
                )
            )
        )
        return mapping

    async def _list_quota_changes(self, report_date: date) -> list[dict[str, Any]]:
        start_time, end_time = self._day_bounds(report_date)
        changes: list[dict[str, Any]] = []
        try:
            for page in range(1, self._config.max_pages + 1):
                data = await self._client.request(
                    "POST",
                    "quota-changes/list",
                    json_body={
                        "pageNum": page,
                        "pageSize": 100,
                        "startTime": start_time,
                        "endTime": end_time,
                        "status": "executed",
                        "sortBy": "updatedAt",
                        "sortOrder": "desc",
                    },
                )
                items = data.get("list") or []
                changes.extend(item for item in items if isinstance(item, dict))
                if page * 100 >= _as_int(data.get("total")) or not items:
                    break
        except Exception as exc:
            self._warnings.append(f"配额变更获取失败：{exc}")
        return changes

    def _format_quota_changes(
        self, changes: list[dict[str, Any]], resources: dict[str, _Tenant]
    ) -> list[str]:
        lines: list[str] = []
        for change in changes:
            entity_id = str(_pick(change, "entityId", "entity_id", default=""))
            tenant = resources.get(entity_id)
            if tenant is None:
                continue
            fields = [
                _format_quota_field("TPM", _pick(change, "tpmChange", "tpm_change")),
                _format_quota_field("RPM", _pick(change, "rpmChange", "rpm_change")),
                _format_quota_field(
                    "并发", _pick(change, "concurrencyChange", "concurrency_change")
                ),
            ]
            changed = [field for field in fields if field]
            if not changed:
                continue
            entity = str(_pick(change, "entityName", "entity_name", default=entity_id))
            operator = str(_pick(change, "requesterName", "requester_name", default="未知"))
            lines.append(f"{tenant.name} / {entity}：{'；'.join(changed)}（{operator}）")
        return lines[: self._config.max_report_items]

    async def _cluster_names(self) -> list[str]:
        if self._config.cluster_names:
            return self._config.cluster_names
        try:
            data = await self._client.request(
                "GET", "clusters", params={"page_num": 1, "page_size": 500}
            )
            return [str(item.get("name")) for item in data.get("list") or [] if item.get("name")]
        except Exception as exc:
            self._warnings.append(f"集群列表获取失败：{exc}")
            return []

    async def _proxy_snapshot(self) -> tuple[dict[str, dict[str, int]], bool]:
        clusters = await self._cluster_names()
        snapshot: dict[str, dict[str, int]] = {}
        complete = True
        for cluster in clusters:
            try:
                for page in range(1, self._config.max_pages + 1):
                    data = await self._client.request(
                        "GET",
                        "gateway/proxy-configs",
                        params={
                            "clusterName": cluster,
                            "namespace": self._config.proxy_namespace,
                            "labels": self._config.proxy_labels,
                            "page_num": page,
                            "page_size": 500,
                        },
                    )
                    items = data.get("list") or []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("name") or "unknown")
                        config_data = item.get("data") or {}
                        if not isinstance(config_data, dict):
                            continue
                        raw = config_data.get("proxy.yaml")
                        if isinstance(raw, str):
                            snapshot[f"{cluster}/{name}"] = _proxy_values(raw)
                    if page * 500 >= _as_int(data.get("total")) or not items:
                        break
            except Exception as exc:
                complete = False
                self._warnings.append(f"{cluster} Proxy 配置获取失败：{exc}")
        return snapshot, complete

    async def _proxy_changes(self, report_date: date, save_snapshot: bool) -> list[str]:
        current, complete = await self._proxy_snapshot()
        previous: dict[str, dict[str, int]] | None = None
        if self._snapshot_path.exists():
            try:
                raw = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
                value = raw.get("proxies") if isinstance(raw, dict) else None
                if isinstance(value, dict):
                    previous = value
            except (OSError, ValueError):
                self._warnings.append("历史 Proxy 快照损坏，本次重新建立基线")
        changes = [] if previous is None else _diff_proxy_snapshots(previous, current)
        if save_snapshot and complete:
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            _write_text_atomic(
                self._snapshot_path,
                json.dumps(
                    {"capturedAt": datetime.now(self._tz).isoformat(), "proxies": current},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        if previous is None:
            return ["配置基线已建立；从下一次运行开始展示净变化"]
        return changes

    async def _machine_usage(self) -> list[dict[str, Any]]:
        try:
            data = await self._client.request(
                "POST",
                "analysis/model-machine-usage/query",
                json_body={"clusterName": "", "model": ""},
            )
            items = [item for item in data.get("list") or [] if isinstance(item, dict)]
            return sorted(
                items,
                key=lambda item: (
                    -float(_pick(item, "machineCount", "machine_count", default=0) or 0),
                    str(item.get("model") or ""),
                ),
            )
        except Exception as exc:
            self._warnings.append(f"模型机器数获取失败：{exc}")
            return []

    async def _observed_pd_ratio(self) -> str:
        now = datetime.now(self._tz)
        start = now - timedelta(minutes=self._config.pd_window_minutes)
        prefill: set[str] = set()
        decode: set[str] = set()
        clusters = self._config.cluster_names or [""]
        try:
            for cluster in clusters:
                for page in range(1, self._config.max_pages + 1):
                    params: dict[str, Any] = {
                        "startTime": start.isoformat(),
                        "endTime": now.isoformat(),
                        "page_num": page,
                        "page_size": 500,
                        "sortBy": "reqTimestamp",
                        "sortOrder": "desc",
                    }
                    if cluster:
                        params["clusterName"] = cluster
                    data = await self._client.request("GET", "gateway/usages", params=params)
                    items = data.get("list") or []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        p_name = str(_pick(item, "prefillPodName", "prefill_pod_name", default=""))
                        d_name = str(_pick(item, "podName", "pod_name", default=""))
                        if p_name:
                            prefill.add(p_name)
                        if d_name:
                            decode.add(d_name)
                    if page * 500 >= _as_int(data.get("total")) or not items:
                        break
        except Exception as exc:
            self._warnings.append(f"P/D 观测数据获取失败：{exc}")
            return "暂无可用 P/D 观测数据"
        if not prefill and not decode:
            return f"最近 {self._config.pd_window_minutes} 分钟无可识别的 P/D 调用"
        divisor = math.gcd(len(prefill), len(decode)) or 1
        ratio = f"{len(prefill) // divisor}:{len(decode) // divisor}"
        return (
            f"最近 {self._config.pd_window_minutes} 分钟活跃 Pod："
            f"P={len(prefill)}、D={len(decode)}，观测比例 {ratio}"
        )

    def _render(
        self,
        report_date: date,
        tenants: list[_Tenant],
        metrics: dict[str, _TenantMetrics],
        quota_lines: list[str],
        proxy_changes: list[str],
        machines: list[dict[str, Any]],
        pd_summary: str,
    ) -> str:
        previous = report_date - timedelta(days=1)
        week_ago = report_date - timedelta(days=7)
        lines = [f"📊 大客户运营日报 · {report_date.isoformat()}", "", "一、用量与峰值"]
        for tenant in tenants[: self._config.max_report_items]:
            item = metrics[tenant.tenant_id]
            current_tokens = item.tokens.get(report_date.isoformat(), 0)
            previous_tokens = item.tokens.get(previous.isoformat(), 0)
            week_tokens = item.tokens.get(week_ago.isoformat(), 0)
            current_tpm = item.max_tpm.get(report_date.isoformat(), 0)
            previous_tpm = item.max_tpm.get(previous.isoformat(), 0)
            week_tpm = item.max_tpm.get(week_ago.isoformat(), 0)
            endpoint = item.max_tpm_endpoint.get(report_date.isoformat())
            suffix = f"（{endpoint}）" if endpoint else ""
            lines.extend(
                [
                    f"• {tenant.name}",
                    f"  Token {_format_number(current_tokens)}｜较前日 {_format_change(current_tokens, previous_tokens)}｜较7日前 {_format_change(current_tokens, week_tokens)}",
                    f"  峰值TPM {_format_number(current_tpm)}{suffix}｜较前日 {_format_change(current_tpm, previous_tpm)}｜较7日前 {_format_change(current_tpm, week_tpm)}",
                ]
            )

        lines.extend(["", "二、配置变更"])
        if quota_lines:
            lines.extend(f"• {line}" for line in quota_lines)
        else:
            lines.append("• 大客户 TPM/RPM/并发：昨日无已记录变更")
        if proxy_changes:
            lines.extend(f"• Proxy {line}" for line in proxy_changes[: self._config.max_report_items])
        else:
            lines.append("• Proxy：相对上一份快照无净变化")

        lines.extend(["", "三、昨日告警", "• 暂未接入告警事件数据源；HTTP 错误指标不计作正式告警"])
        lines.extend(["", "四、当前机器使用情况"])
        if machines:
            for item in machines[: self._config.max_report_items]:
                cluster = str(_pick(item, "clusterName", "cluster_name", default="未知集群"))
                model = str(item.get("model") or "未知模型")
                machine_count = float(
                    _pick(item, "machineCount", "machine_count", default=0) or 0
                )
                gpu_count = _as_int(_pick(item, "gpuCount", "gpu_count"))
                gpu_product = str(_pick(item, "gpuProduct", "gpu_product", default="unknown"))
                lines.append(
                    f"• {model} / {cluster}：{machine_count:g} 台（8卡等效），{gpu_count} × {gpu_product} GPU"
                )
        else:
            lines.append("• 暂无机器统计数据")
        lines.append(f"• {pd_summary}")

        if self._warnings:
            lines.extend(["", "数据提示"])
            lines.extend(f"• {warning}" for warning in self._warnings[:10])
        return "\n".join(lines)


_MAGIK_CUBE_PARAMETERS = tool_parameters_schema(
    report_date=StringSchema(
        "Report date in YYYY-MM-DD. Defaults to yesterday in the agent timezone."
    ),
    save_snapshot=BooleanSchema(
        default=True,
        description=(
            "Persist the current Proxy baseline for next-run change detection. "
            "Keep true for scheduled reports; set false for previews/tests."
        ),
    ),
    tenant_query=StringSchema(
        "Optional tenant name or tenant tag, such as 佛跳墙 or tencent_token_hub."
    ),
    model=StringSchema("Optional exact model filter, such as GLM-5.2."),
    required=[],
    description=(
        "Generate a Magik Cube key-account daily report, or query usage for a specified "
        "tenant name/tag and model."
    ),
)


@tool_parameters(_MAGIK_CUBE_PARAMETERS)
class MagikCubeDailyReportTool(Tool):
    """Generate the daily key-account operations report."""

    config_key = "magik_cube"

    @classmethod
    def config_cls(cls):
        return MagikCubeToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.magik_cube.enable

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(
            config=ctx.config.magik_cube,
            timezone=ctx.timezone,
            snapshot_path=get_runtime_subdir("magik_cube") / "proxy_snapshot.json",
        )

    def __init__(
        self,
        config: MagikCubeToolConfig | None = None,
        timezone: str = "Asia/Shanghai",
        snapshot_path: Path | None = None,
    ) -> None:
        self._config = config or MagikCubeToolConfig()
        self._timezone = timezone
        self._snapshot_path = snapshot_path or (
            get_runtime_subdir("magik_cube") / "proxy_snapshot.json"
        )

    @property
    def name(self) -> str:
        return "magik_cube_daily_report"

    @property
    def description(self) -> str:
        return (
            "Generate the Magik Cube key-account daily report: Token usage and daily peak TPM "
            "comparisons, quota/Proxy changes, machine usage, and observed P/D ratio. "
            "It can also answer ad-hoc Feishu questions for a configured tenant alias or "
            "exact tenant name and model, "
            "for example 佛跳墙 + GLM-5.2."
        )

    @property
    def max_calls_per_turn(self) -> int | None:
        # One focused query already includes the target day, previous day,
        # and seven-days-prior comparisons.  Three calls leave room for a
        # small follow-up while preventing unbounded day-by-day scans.
        return 3

    def match_direct_request(self, text: str) -> dict[str, Any] | None:
        raw = text.strip()
        if not re.search(
            r"(?:用量|使用量|用了多少量|多少量|token|峰值\s*tpm|tpm)",
            raw,
            re.IGNORECASE,
        ):
            return None

        model_match = re.search(
            r"\b(?:GLM|KIMI|MINIMAX|DEEPSEEK|QWEN|HY)[A-Z0-9._-]*\b",
            raw,
            re.IGNORECASE,
        )
        model = model_match.group(0) if model_match else ""

        cleaned = raw
        for phrase in (
            "帮我",
            "麻烦",
            "请",
            "看看",
            "看下",
            "查一下",
            "查询",
            "昨天",
            "昨日",
            "今天",
            "前天",
            "的",
        ):
            cleaned = cleaned.replace(phrase, " ")
        tenant_query = next(
            (
                alias
                for alias in sorted(self._config.tenant_mappings, key=len, reverse=True)
                if alias.casefold() in raw.casefold()
            ),
            "",
        )
        if not tenant_query:
            tenant_match = re.search(
                r"([A-Za-z0-9_\-\u4e00-\u9fff]+)\s*(?:用户|客户|租户)", cleaned
            )
            tenant_query = tenant_match.group(1) if tenant_match else ""

        is_daily_report = "大客户" in raw and "日报" in raw
        if not tenant_query and not model and not is_daily_report:
            return None

        params: dict[str, Any] = {"save_snapshot": False}
        date_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", raw)
        if date_match:
            params["report_date"] = date_match.group(0)
        if tenant_query:
            params["tenant_query"] = tenant_query
        if model:
            params["model"] = model
        return params

    async def execute(
        self,
        report_date: str | None = None,
        save_snapshot: bool = True,
        tenant_query: str | None = None,
        model: str | None = None,
        **_kwargs: Any,
    ) -> str:
        has_auth = bool(
            self._config.access_token
            or (self._config.account and self._config.password)
        )
        if not self._config.base_url or not has_auth:
            return ToolResult.error(
                "Error: configure tools.magikCube.baseUrl plus either account/password "
                "or accessToken"
            )
        tz = ZoneInfo(self._timezone)
        if report_date:
            try:
                target_date = date.fromisoformat(report_date)
            except ValueError:
                return ToolResult.error("Error: report_date must use YYYY-MM-DD")
        else:
            target_date = datetime.now(tz).date() - timedelta(days=1)
        try:
            async with MagikCubeClient(self._config) as client:
                reporter = MagikCubeReporter(
                    client,
                    self._config,
                    self._snapshot_path,
                    self._timezone,
                )
                if tenant_query or model:
                    return await reporter.generate_usage_query(
                        target_date,
                        tenant_query=(tenant_query or "").strip(),
                        model=(model or "").strip(),
                    )
                return await reporter.generate(target_date, save_snapshot=save_snapshot)
        except (MagikCubeApiError, httpx.HTTPError) as exc:
            return ToolResult.error(f"Error: failed to generate Magik Cube report: {exc}")
