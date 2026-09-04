"""Read-only Magik Cube daily reporting tool."""

from __future__ import annotations

import asyncio
import calendar
import json
import math
import re
import statistics
import time as time_module
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar
from zoneinfo import ZoneInfo

import httpx
import yaml
from loguru import logger
from pydantic import Field

from nanobot.agent.reporting.cube_subscription_intent import (
    is_subscription_intent_candidate,
)
from nanobot.agent.reporting.magik_cube_intent import (
    IntentCandidateStore,
    classify_report_intent,
    is_deep_analysis_request,
    is_report_intent_candidate,
    match_promoted_rule,
    minimal_interactive_intent,
)
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.bus.events import OUTBOUND_META_AGENT_UI
from nanobot.config.paths import get_runtime_subdir
from nanobot.config_base import Base
from nanobot.utils.helpers import _write_text_atomic
from nanobot.utils.report_failures import (
    ReportFailureCode,
    ReportFailureError,
    classify_report_failure,
    report_failure_message,
)


class MagikCubeTokenApiConfig(Base):
    """Independent read-only configuration for the Cube user TokenAPI."""

    # Account data never reuses the internal Admin JWT. Enable only after a
    # Casbin-scoped TokenAPI credential has been provisioned for this deployment.
    enable: bool = False
    base_url: str = ""
    api_prefix: str = "/api/v1"
    access_token: str = Field(default="", repr=False)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    verify_ssl: bool = True
    max_pages: int = Field(default=10, ge=1, le=100)
    max_concurrency: int = Field(default=4, ge=1, le=16)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=0.25, ge=0, le=10)
    # These fields preserve the small shared-client interface. TokenAPI uses
    # only its supplied bearer credential and never performs password login.
    account: str = ""
    password: str = Field(default="", repr=False)
    login_path: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.enable and self.base_url and self.access_token)


class MagikCubeToolConfig(Base):
    """Connection and report settings for the Magik Cube admin API."""

    # Tool 默认关闭，避免未配置 Magik Cube 时向 LLM 暴露一个不可用的工具。
    enable: bool = False
    # 仅用于显式区分部署环境。阶段 0 的契约验证器只接受 staging，防止误探测生产管理面。
    deployment_environment: Literal["", "development", "staging", "production"] = ""
    # 默认为关闭；即使配置被标记为 staging，也需要显式打开才能运行契约探测。
    contract_validation_enabled: bool = False
    # 管理面 API 的 origin，例如 https://magik-cube.example.com；不要在这里重复 api_prefix。
    base_url: str = ""
    # 业务 API 的公共前缀。请求最终会拼成 {base_url}/{api_prefix}/{route}。
    api_prefix: str = "/api/v1"
    # 已有 Bearer token；repr=False 防止配置对象被日志或异常 repr 时泄露密钥。
    access_token: str = Field(default="", repr=False)
    # 成本、账单、余额必须通过独立 TokenAPI JWT 访问，不能复用管理面 access_token。
    token_api: MagikCubeTokenApiConfig = Field(default_factory=MagikCubeTokenApiConfig)
    # 账号密码登录的账号。只有 account 和 password 同时存在时才会触发密码登录。
    account: str = ""
    # 密码字段同样禁止出现在对象 repr 中。
    password: str = Field(default="", repr=False)
    # 密码登录接口。客户端还会与严格常量比对，防止配置把凭据发送到任意路径。
    login_path: str = "/token-api/v1/accounts/login/with-password"
    # 额外 HTTP headers，例如租户路由或网关要求的自定义 header；Authorization 由客户端管理。
    extra_headers: dict[str, str] = Field(default_factory=dict)
    # 固定集群列表。为空时通过 clusters API 自动发现；配置固定列表可减少一次发现请求。
    cluster_names: list[str] = Field(default_factory=list)
    # 查询 Envoy Gateway Proxy 配置时使用的 namespace。
    proxy_namespace: str = "envoy-gateway-system"
    # 查询 Proxy 配置时使用的 label selector 字符串。
    proxy_labels: str = "gateway.magikcompute.ai/name:magik-ai-gateway"
    # 单次 HTTP 请求超时时间，范围限制避免配置成无超时或过短导致日报全部失败。
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    # TLS 证书校验开关。生产环境应保持 True；关闭只适合明确受控的测试环境。
    verify_ssl: bool = True
    # P/D 观测窗口，从当前时间向前回溯该分钟数。
    pd_window_minutes: int = Field(default=15, ge=1, le=1440)
    # 每个分页接口最多读取的页数；达到上限时可能只得到部分数据，应结合 warning 判断。
    max_pages: int = Field(default=10, ge=1, le=100)
    # 报告中最多展示多少个租户、变更或机器条目，避免单次消息过大。
    max_report_items: int = Field(default=20, ge=1, le=100)
    # 用户可读别名到真实 tenant ID 的映射。仅用于已由 Cube catalog 返回的客户展示；
    # 不能绕过 catalog 直接构造客户，也不能作为客户列表的补充来源。
    tenant_mappings: dict[str, str] = Field(default_factory=dict)
    # 模型简称到管理面真实模型名的映射；同时用于 intent 解析和最终模型清单校验。
    model_aliases: dict[str, str] = Field(default_factory=lambda: {"k3": "Kimi-K3"})
    # 所有只读业务 API 共用的并发上限，避免模型 fan-out 冲击管理面。
    max_concurrency: int = Field(default=8, ge=1, le=32)
    # 对 429/5xx 和传输错误执行有限指数退避；认证、参数和 4xx 错误不重试。
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=0.25, ge=0, le=10)
    # 单个分析接口请求最多覆盖的自然日数；更长范围自动分片。
    max_range_days_per_request: int = Field(default=90, ge=1, le=90)
    # 主周期与对比周期的总查询天数上限。
    max_query_days: int = Field(default=366, ge=1, le=366)
    # 租户与模型清单缓存时间；用量数据始终实时读取，不进入缓存。
    cache_ttl_seconds: int = Field(default=300, ge=0, le=3600)
    # 模型进入趋势异常榜所需的最小 Token 占比。
    trend_min_share: float = Field(default=0.01, ge=0, le=1)
    # 可选的 GPU/机器数量合理性基准。不同集群拓扑可能不同，因此默认关闭；
    # 只有部署方明确配置后才告警，绝不据此改写 Cube 返回值。
    machine_gpu_per_host_expected: float | None = Field(default=None, gt=0, le=64)
    machine_gpu_per_host_tolerance: float = Field(default=0.15, ge=0, le=1)
    # 当日 Token 超过周期中位数的倍数阈值。
    spike_median_multiplier: float = Field(default=1.5, ge=1, le=10)
    # 一次交互最多查询的客户数，避免模型 fan-out 放大管理面压力。
    interactive_max_tenants: int = Field(default=5, ge=1, le=10)
    # 飞书模型矩阵每页行数；较小页长兼顾移动端可读性。
    matrix_page_size: int = Field(default=8, ge=4, le=20)
    # 未固化报表表达最多调用一次轻量 LLM 进行结构化意图解析。
    intent_fallback_enabled: bool = True
    # 意图解析必须快速失败；失败后返回确定性参数卡，不进入第二次 LLM。
    intent_fallback_timeout_seconds: float = Field(default=3.0, ge=0.5, le=10)
    # 候选问法只保存在本机 runtime 目录，并按天自动淘汰。
    intent_candidate_retention_days: int = Field(default=30, ge=1, le=365)
    # 防止候选日志无界增长。
    intent_candidate_max_entries: int = Field(default=10_000, ge=100, le=100_000)
    # 通用 Admin API 工具单次返回给模型的最大 JSON 字符数，防止日志/明细撑满上下文。
    admin_max_response_chars: int = Field(default=50_000, ge=1_000, le=200_000)

class MagikCubeApiError(ReportFailureError):
    """Raised when the Magik Cube API returns an unsuccessful response."""

    def __init__(
        self,
        message: str,
        *,
        failure_code: ReportFailureCode = "upstream_failed",
    ) -> None:
        super().__init__(message, failure_code=failure_code)


class MagikCubeTenantResolutionError(MagikCubeApiError):
    """Raised when a user selection cannot resolve to exactly one catalog tenant."""


# 密码登录唯一允许的路径。登录是唯一一个非 api_prefix 下的 POST 请求。
_PASSWORD_LOGIN_PATH = "token-api/v1/accounts/login/with-password"
_REPORT_TIMEZONE = "Asia/Shanghai"
_MAX_TENANT_CATALOG_ITEMS = 5_000
_CONTEXT_TENANT_REFERENCE_RE = re.compile(
    r"(?:这个|这一个|该|上述|上面(?:提到)?的|刚才(?:提到)?的|前面(?:提到)?的)"
    r"\s*(?:租户|客户|用户)|(?<![A-Za-z0-9_])它(?![A-Za-z0-9_])"
)
_INVALID_TENANT_REFERENCES = frozenset(
    {"这个", "这一个", "该", "上述", "上面的", "刚才的", "前面的", "它"}
)
# 业务 API 的最小权限边界：只允许日报所需的查询和分析接口，禁止写入、删除和任意路径访问。
_ADMIN_READ_ONLY_ROUTES = frozenset(
    {
        ("GET", "clusters"),
        ("GET", "gateway/proxy-configs"),
        ("GET", "gateway/usages"),
        ("GET", "inference/endpoints"),
        ("GET", "inference/model-configs"),
        ("GET", "tenants"),
        ("GET", "billing/provider-prices"),
        ("POST", "analysis/active-tenant-daily-usage/query"),
        ("POST", "analysis/endpoint-max-tpm/daily/query"),
        ("POST", "analysis/token-utilization/query"),
        ("POST", "analysis/token-utilization/daily/query"),
        ("POST", "analysis/model-token-utilization/daily/query"),
        ("POST", "analysis/performance-endpoints/query"),
        ("POST", "analysis/endpoint-tpm-trend/query"),
        ("POST", "analysis/model-performance/query"),
        ("POST", "analysis/provider-performance/query"),
        ("POST", "analysis/provider-daily-traffic/query"),
        ("POST", "analysis/model-machine-usage/query"),
        # Capacity reporting consumes this aggregate trend only. The response
        # has no machine ID and remains read-only at the client allowlist.
        ("POST", "analysis/machine-tpm-trend/query"),
        ("POST", "providers/list"),
        ("POST", "providers/detail"),
        ("POST", "quota-changes/list"),
    }
)


class MagikCubeClient:
    """封装认证、allowlist 校验、HTTP 调用和统一响应解包。"""

    def __init__(
        self,
        config: MagikCubeToolConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        # 只规范化前缀，不修改原始配置；route 本身在 request() 中再做 strip("/")。
        self._api_prefix = config.api_prefix.strip("/")
        # 允许调用方注入 MockTransport，测试时无需真实访问管理面 API。
        headers = {"Accept": "application/json", **config.extra_headers}
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            headers=headers,
            timeout=config.timeout_seconds,
            verify=config.verify_ssl,
            transport=transport,
        )
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self.route_counts: dict[str, int] = {}
        self.request_seconds = 0.0
        self.rate_limit_errors = 0
        self.server_errors = 0

    async def __aenter__(self) -> MagikCubeClient:
        # 凭据优先级：账号密码优先于静态 token，登录成功后用运行时 token 覆盖 header。
        if self._config.account and self._config.password:
            await self._login_with_password()
        elif self._config.access_token:
            self._client.headers["Authorization"] = f"Bearer {self._config.access_token}"
        return self

    async def __aexit__(self, *_args: Any) -> None:
        # 无论请求是否抛异常都关闭连接池，避免定时任务长期积累 socket。
        await self._client.aclose()

    async def _login_with_password(self) -> None:
        # login_path 虽可配置，但必须精确匹配固定路径，防止凭据被转发到非预期 endpoint。
        path = self._config.login_path.lstrip("/")
        if path != _PASSWORD_LOGIN_PATH:
            raise MagikCubeApiError("password login path is not on the strict allowlist")
        response = await self._client.post(
            path,
            json={"account": self._config.account, "password": self._config.password},
            follow_redirects=False,
        )
        data = self._decode_response("POST", self._config.login_path, response)
        # 兼容后端常见的 camelCase 和 snake_case 两种 token 字段命名。
        token = _pick(data, "accessToken", "access_token", default="")
        if not token:
            raise MagikCubeApiError(
                "password login succeeded but returned no access token",
                failure_code="auth_failed",
            )
        self._client.headers["Authorization"] = f"Bearer {token}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # 所有业务请求先过 allowlist，再拼接 api prefix；因此上层 reporter 不能越权调用写接口。
        normalized_method = method.upper()
        normalized_path = path.strip("/")
        allowed_routes = (
            _TOKEN_API_READ_ONLY_ROUTES
            if isinstance(self._config, MagikCubeTokenApiConfig)
            else _ADMIN_READ_ONLY_ROUTES
        )
        if (normalized_method, normalized_path) not in allowed_routes:
            raise MagikCubeApiError(
                f"blocked non-allowlisted Magik Cube API request: "
                f"{normalized_method} /{normalized_path}"
            )
        api_path = "/".join(part for part in (self._api_prefix, normalized_path) if part)
        started = time_module.perf_counter()
        response: httpx.Response | None = None
        try:
            async with self._semaphore:
                for attempt in range(self._config.max_retries + 1):
                    try:
                        response = await self._client.request(
                            normalized_method,
                            api_path,
                            params=params,
                            json=json_body,
                            follow_redirects=False,
                        )
                    except httpx.HTTPError:
                        if attempt >= self._config.max_retries:
                            raise
                        await asyncio.sleep(
                            self._config.retry_backoff_seconds * (2**attempt)
                        )
                        continue
                    if response.status_code == 429:
                        self.rate_limit_errors += 1
                    elif response.status_code >= 500:
                        self.server_errors += 1
                    if (
                        response.status_code == 429 or response.status_code >= 500
                    ) and attempt < self._config.max_retries:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            retry_delay = float(retry_after) if retry_after else 0.0
                        except ValueError:
                            retry_delay = 0.0
                        await asyncio.sleep(
                            max(
                                retry_delay,
                                self._config.retry_backoff_seconds * (2**attempt),
                            )
                        )
                        await response.aclose()
                        continue
                    break
        finally:
            self.route_counts[normalized_path] = self.route_counts.get(normalized_path, 0) + 1
            self.request_seconds += time_module.perf_counter() - started
        if response is None:
            raise MagikCubeApiError(
                f"{method} {path} returned no response",
                failure_code="connection_failed",
            )
        # 统一处理 HTTP 层和业务 code 层错误，让 reporter 只需处理 MagikCubeApiError。
        return self._decode_response(method, path, response)

    @staticmethod
    def _decode_response(
        method: str, path: str, response: httpx.Response
    ) -> dict[str, Any]:
        # 管理面接口通常返回 JSON envelope；非 JSON、HTTP error、业务 code error 都转成统一异常。
        try:
            payload = response.json()
        except ValueError as exc:
            failure_code: ReportFailureCode = (
                "auth_failed"
                if response.status_code in {401, 403}
                else "rate_limited"
                if response.status_code == 429
                else "upstream_failed"
            )
            raise MagikCubeApiError(
                f"{method} {path} returned non-JSON HTTP {response.status_code}",
                failure_code=failure_code,
            ) from exc
        if response.is_error:
            message = payload.get("message") if isinstance(payload, dict) else None
            if response.status_code in {401, 403}:
                failure_code: ReportFailureCode = "auth_failed"
            elif response.status_code == 429:
                failure_code = "rate_limited"
            else:
                failure_code = "upstream_failed"
            raise MagikCubeApiError(
                f"{method} {path} failed with HTTP {response.status_code}: {message or 'unknown error'}",
                failure_code=failure_code,
            )
        if not isinstance(payload, dict):
            raise MagikCubeApiError(f"{method} {path} returned an invalid response")
        if "code" in payload:
            code = payload.get("code")
            if code not in (0, 200, "0", "200", "OK"):
                normalized_code = str(code).strip()
                failure_code = (
                    "auth_failed"
                    if normalized_code in {"401", "403"}
                    else "rate_limited"
                    if normalized_code == "429"
                    else "upstream_failed"
                )
                raise MagikCubeApiError(
                    f"{method} {path} failed: {payload.get('message') or payload.get('reason') or code}",
                    failure_code=failure_code,
                )
            data = payload.get("data")
            # data 不是 object 时按空字典处理，避免上层对 None 做大量防御；列表型结果通常包在 list 字段内。
            return data if isinstance(data, dict) else {}
        return payload


@dataclass(frozen=True)
class _Tenant:
    """内部统一的租户标识；冻结后可安全在并发查询中复用。"""

    tenant_id: str
    name: str
    # 保留后端 tags，便于租户识别和后续扩展查询匹配。
    tags: tuple[str, ...] = ()


_TenantT = TypeVar("_TenantT")


def _match_catalog_tenants(
    catalog: list[_TenantT],
    query: str,
    tenant_mappings: dict[str, str],
) -> list[_TenantT]:
    """Match a catalog deterministically without constructing local tenants."""

    normalized = _normalize_tenant_query(query)
    if not normalized:
        return []

    tenant_ids = [
        item
        for item in catalog
        if _normalize_tenant_query(str(getattr(item, "tenant_id", ""))) == normalized
    ]
    if tenant_ids:
        return tenant_ids

    alias_targets = {
        tenant_id
        for alias, tenant_id in tenant_mappings.items()
        if _normalize_tenant_query(alias) == normalized
    }
    aliases = [
        item
        for item in catalog
        if str(getattr(item, "tenant_id", "")) in alias_targets
    ]
    if aliases:
        return aliases

    exact: list[_TenantT] = []
    partial: list[_TenantT] = []
    for item in catalog:
        values = [
            _normalize_tenant_query(str(getattr(item, "name", ""))),
            *(
                _normalize_tenant_query(str(tag))
                for tag in getattr(item, "tags", ())
            ),
        ]
        if normalized in values:
            exact.append(item)
        elif any(normalized in value for value in values if value):
            partial.append(item)
    return exact or partial


@dataclass
class _TenantMetrics:
    """一个租户的用量汇总，以及不可跨 Endpoint 聚合的 TPM 原始日点。"""

    # key 为 YYYY-MM-DD，value 为该日 Token 总量。
    tokens: dict[str, int]
    # key 为 YYYY-MM-DD，value 为该日请求数。
    requests: dict[str, int]
    # key 为 YYYY-MM-DD，value 为该日所有 endpoint 中的最大 TPM。
    max_tpm: dict[str, int]
    # 记录 max_tpm 对应的 endpoint，便于日报定位峰值来源。
    max_tpm_endpoint: dict[str, str]
    # key=(model, endpoint, date)。avgTpm 只能在同一序列内跨日平均，
    # 不能跨 Endpoint 或客户求和，否则会改变 Cube 接口的统计语义。
    endpoint_tpm: dict[tuple[str, str, str], "_EndpointTpmPoint"] = field(
        default_factory=dict
    )
    # 分片或接口失败时标记为 False；渲染层必须显示“不完整”，不能把缺失当零。
    token_complete: bool = True
    tpm_complete: bool = True


@dataclass(frozen=True)
class _EndpointTpmPoint:
    """Cube 单个 Endpoint 的单日 TPM 点；缺失字段保持 None，不伪装成零。"""

    model: str
    endpoint: str
    date: str
    max_tpm: int | None
    avg_tpm: int | None


@dataclass(frozen=True)
class _DateWindow:
    """Inclusive natural-day window in Asia/Shanghai semantics."""

    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, day: str) -> bool:
        return self.start.isoformat() <= day <= self.end.isoformat()


@dataclass(frozen=True)
class _ComparisonPlan:
    """Primary/comparison windows and the minimal request windows that cover them."""

    primary: _DateWindow
    comparison: _DateWindow | None
    fetch_windows: tuple[_DateWindow, ...]
    same_weekday_comparison: _DateWindow | None = None


@dataclass
class _CacheEntry:
    expires_at: float
    value: Any


class _MagikCubeCache:
    """Process-local TTL cache for low-churn tenant/model metadata only."""

    def __init__(self) -> None:
        self._values: dict[str, _CacheEntry] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        entry = self._values.get(key)
        if entry is None or entry.expires_at <= time_module.monotonic():
            self._values.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return entry.value

    def put(self, key: str, value: Any, ttl_seconds: int) -> None:
        if ttl_seconds > 0:
            self._values[key] = _CacheEntry(time_module.monotonic() + ttl_seconds, value)


def _pick(obj: dict[str, Any], *names: str, default: Any = None) -> Any:
    """按候选字段名读取值，兼容 API 的 camelCase/snake_case 返回格式。"""

    for name in names:
        if name in obj:
            return obj[name]
    return default


def _as_int(value: Any) -> int:
    """把数字、数字字符串或空值归一化为 int；异常值按 0 处理以保证单租户报告可继续。"""

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return 0
        try:
            return int(value, 10)
        except ValueError:
            pass
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _as_optional_int(value: Any) -> int | None:
    """精确解析可选 int64；空值和非法值返回 None 以保留缺失语义。"""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return int(value, 10)
        except ValueError:
            return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _format_number(value: int | float) -> str:
    """把大数转换为日报易读格式：超过 1 万用“万”，超过 1 亿用“亿”。"""

    absolute = abs(value)
    if absolute >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿"
    if absolute >= 10_000:
        return f"{value / 10_000:.2f}万"
    return f"{value:,.0f}"


def _format_million_tokens(value: int) -> str:
    rendered = f"{value / 1_000_000:,.6f}".rstrip("0").rstrip(".")
    return f"{rendered or '0'}M"


def _decoded_tool_arguments(tool_call: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(tool_call, dict):
        return "", {}
    function = tool_call.get("function")
    if isinstance(function, dict):
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
    else:
        name = str(tool_call.get("name") or "")
        arguments = tool_call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return name, {}
    return name, arguments if isinstance(arguments, dict) else {}


def _tenant_from_direct_params(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    selections = params.get("report_selections")
    if isinstance(selections, list):
        for selection in reversed(selections):
            if isinstance(selection, dict):
                value = str(selection.get("tenant_query") or "").strip()
                if value and value not in _INVALID_TENANT_REFERENCES:
                    return value
    value = str(params.get("tenant_query") or "").strip()
    return value if value not in _INVALID_TENANT_REFERENCES else ""


def _tenant_from_history_text(text: str, aliases: list[str]) -> str:
    tenant_ids = re.findall(
        r"(?<![A-Za-z0-9_-])(?:tenant|t)-[A-Za-z0-9]+(?![A-Za-z0-9_-])",
        text,
        re.IGNORECASE,
    )
    if tenant_ids:
        return tenant_ids[-1]
    candidates: list[tuple[int, str]] = []
    folded = text.casefold()
    for alias in aliases:
        start = folded.rfind(alias.casefold())
        if start >= 0:
            candidates.append((start, alias))
    plain = re.sub(r"[`*]", "", text)
    patterns = (
        re.compile(
            r"(?<![A-Za-z0-9_.-])([A-Za-z][A-Za-z0-9_.-]{1,127})\s*"
            r"(?:租户|客户|用户)(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:租户|客户|用户)\s*[：:]?\s*"
            r"([A-Za-z][A-Za-z0-9_.-]{1,127})(?![A-Za-z0-9_.-])",
            re.IGNORECASE,
        ),
    )
    for pattern in patterns:
        for match in pattern.finditer(plain):
            value = match.group(1).strip()
            if value.casefold() not in _INVALID_TENANT_REFERENCES:
                candidates.append((match.start(1), value))
    return max(candidates, default=(-1, ""), key=lambda item: item[0])[1]


def _latest_tenant_from_history(
    history: list[dict[str, Any]],
    aliases: list[str],
) -> str:
    """Return the most recent explicit tenant focus from persisted conversation state."""

    for message in reversed(history[-80:]):
        value = _tenant_from_direct_params(message.get("direct_params"))
        if value:
            return value
        content = message.get("content")
        if message.get("role") in {"assistant", "user"} and isinstance(content, str):
            value = _tenant_from_history_text(content, aliases)
            if value:
                return value
        for tool_call in reversed(message.get("tool_calls") or []):
            name, arguments = _decoded_tool_arguments(tool_call)
            if name in {"magik_cube_daily_report", "magik_cube_admin_api"}:
                value = _tenant_from_direct_params(arguments)
                if value:
                    return value
    return ""


def _format_change(current: int | float, baseline: int | float) -> str:
    """只展示相对变化百分比；零基准使用业务状态，绝不伪造百分比。"""

    if baseline == 0:
        if current == 0:
            return "无变化"
        return "新增"
    change = (current - baseline) / baseline * 100
    arrow = "↑" if change > 0 else "↓" if change < 0 else ""
    return f"{arrow}{abs(change):.1f}%" if arrow else "0.0%"


def _format_delta(current: int | float, baseline: int | float) -> str:
    """Compatibility wrapper for percentage-only user-visible changes."""

    return _format_change(current, baseline)


def _format_quota_field(label: str, change: Any) -> str | None:
    """提取一个配额字段的 old/new 值，仅在实际变化时生成报告片段。"""

    if not isinstance(change, dict):
        return None
    old = _as_int(_pick(change, "oldValue", "old_value"))
    new = _as_int(_pick(change, "newValue", "new_value"))
    if old == new:
        return None
    return f"{label} {_format_number(old)} → {_format_number(new)}"


def _proxy_values(raw: str) -> dict[str, int]:
    """从 Proxy 的 proxy.yaml 中只提取限流字段，忽略其它配置以减少快照噪声。"""

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
    """比较相邻两次 Proxy 限流快照，输出新增、删除和字段变更。"""

    changes: list[str] = []
    for key in sorted(old.keys() | new.keys()):
        if key not in old:
            changes.append(f"{key}：新增，{_format_proxy_values(new[key])}")
            continue
        if key not in new:
            changes.append(f"{key}：已删除")
            continue
        fields = []
        for field_name in sorted(old[key].keys() | new[key].keys()):
            before = old[key].get(field_name)
            after = new[key].get(field_name)
            if before != after:
                fields.append(
                    f"{field_name} {before if before is not None else '无'} → "
                    f"{after if after is not None else '无'}"
                )
        if fields:
            changes.append(f"{key}：" + "；".join(fields))
    return changes


def _format_proxy_values(values: dict[str, int]) -> str:
    """格式化 Proxy 快照中的限流字段；空结果明确提示未识别字段。"""

    if not values:
        return "未识别到限流字段"
    return "，".join(f"{key}={value}" for key, value in sorted(values.items()))


def _normalize_tenant_query(value: str) -> str:
    """统一租户查询词大小写、空白和末尾“用户/客户/租户”后缀。"""

    normalized = "".join(value.casefold().split())
    for suffix in ("用户", "客户", "租户"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def _resolve_model_alias(value: str, aliases: dict[str, str]) -> str:
    """Resolve a user-facing model alias without changing unknown model names."""

    query = value.strip()
    normalized = query.casefold()
    for alias, model_name in aliases.items():
        if alias.strip().casefold() == normalized and model_name.strip():
            return model_name.strip()
    return query


def _shift_month(day: date, months: int) -> date:
    """Shift a date by whole calendar months and clamp to the target month."""

    month_index = day.year * 12 + day.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _chunk_window(window: _DateWindow, max_days: int) -> list[_DateWindow]:
    """Split an inclusive window into non-overlapping chunks."""

    chunks: list[_DateWindow] = []
    cursor = window.start
    while cursor <= window.end:
        chunk_end = min(window.end, cursor + timedelta(days=max_days - 1))
        chunks.append(_DateWindow(cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _plan_comparison_windows(
    start: date,
    end: date,
    *,
    comparison: str = "none",
    compare_start: date | None = None,
    compare_end: date | None = None,
    max_request_days: int = 90,
    max_query_days: int = 366,
) -> _ComparisonPlan:
    """Build exact comparison periods and minimal API request windows."""

    if end < start:
        raise ValueError("end_date must not be earlier than start_date")
    primary = _DateWindow(start, end)
    if (compare_start is None) != (compare_end is None):
        raise ValueError("compare_start_date and compare_end_date must be provided together")
    if compare_start is not None and compare_end is not None:
        if compare_end < compare_start:
            raise ValueError("compare_end_date must not be earlier than compare_start_date")
        baseline = _DateWindow(compare_start, compare_end)
    elif comparison == "none":
        baseline = None
    elif comparison == "previous_period":
        baseline = _DateWindow(
            start - timedelta(days=primary.days),
            start - timedelta(days=1),
        )
    elif comparison == "previous_week":
        baseline = _DateWindow(start - timedelta(days=7), end - timedelta(days=7))
    elif comparison == "previous_month":
        if start.day == 1 and end.day == calendar.monthrange(end.year, end.month)[1]:
            baseline_end = start - timedelta(days=1)
            baseline = _DateWindow(baseline_end.replace(day=1), baseline_end)
        else:
            baseline = _DateWindow(_shift_month(start, -1), _shift_month(end, -1))
    else:
        raise ValueError(
            "comparison must be one of none, previous_period, previous_week, previous_month"
        )

    previous_day = _DateWindow(primary.start - timedelta(days=1), primary.end - timedelta(days=1))
    # Feishu opaque callbacks serialize the explicit D-1 window and set
    # comparison=none. Treat the daily window itself as the invariant so direct,
    # interactive and scheduled reports all retain the D-7 comparison.
    is_standard_daily = primary.days == 1 and (
        comparison == "previous_period" or baseline == previous_day
    )
    same_weekday_comparison = (
        _DateWindow(primary.start - timedelta(days=7), primary.end - timedelta(days=7))
        if is_standard_daily
        else None
    )
    logical_windows = [
        window
        for window in (baseline, same_weekday_comparison, primary)
        if window is not None
    ]
    unique_logical_days = {
        (window.start + timedelta(days=offset)).isoformat()
        for window in logical_windows
        for offset in range(window.days)
    }
    total_days = len(unique_logical_days)
    if total_days > max_query_days:
        raise ValueError(f"total query window exceeds {max_query_days} days")

    request_windows: list[_DateWindow]
    if baseline is None:
        request_windows = [primary]
    else:
        bounding = _DateWindow(min(primary.start, baseline.start), max(primary.end, baseline.end))
        adjacent_or_overlapping = not (
            primary.end + timedelta(days=1) < baseline.start
            or baseline.end + timedelta(days=1) < primary.start
        )
        if adjacent_or_overlapping and bounding.days <= max_request_days:
            request_windows = [bounding]
        else:
            request_windows = [baseline, primary]

    if same_weekday_comparison is not None:
        all_windows = [same_weekday_comparison, *request_windows]
        bounding = _DateWindow(
            min(window.start for window in all_windows),
            max(window.end for window in all_windows),
        )
        request_windows = (
            [bounding]
            if bounding.days <= max_request_days
            else sorted(all_windows, key=lambda window: (window.start, window.end))
        )

    chunks = [
        chunk
        for window in request_windows
        for chunk in _chunk_window(window, max_request_days)
    ]
    return _ComparisonPlan(
        primary,
        baseline,
        tuple(chunks),
        same_weekday_comparison=same_weekday_comparison,
    )


def _window_label(window: _DateWindow) -> str:
    return (
        window.start.isoformat()
        if window.start == window.end
        else f"{window.start.isoformat()} ~ {window.end.isoformat()}"
    )


@dataclass(frozen=True)
class _PeriodStats:
    tokens: int
    requests: int
    peak_tpm: int
    average_tpm: float | None
    tpm_series_count: int
    average_tpm_sample_count: int
    token_sample_count: int
    daily_average_tokens: float
    peak_date: str
    token_complete: bool
    tpm_complete: bool


class MagikCubeReporter:
    """Collect and format one daily report from the admin API."""

    def __init__(
        self,
        client: Any,
        config: MagikCubeToolConfig | MagikCubeTokenApiConfig,
        snapshot_path: Path,
        timezone: str,
        cache: _MagikCubeCache | None = None,
        reporting_actions_enabled: bool = True,
    ) -> None:
        # Reporter 不持有连接生命周期；连接由 MagikCubeClient 的 async context manager 管理。
        self._client = client
        self._config = config
        # snapshot_path 是运行态文件，不进入代码仓库，用于跨次执行比较 Proxy 配置。
        self._snapshot_path = snapshot_path
        self._tz = ZoneInfo(timezone)
        self._cache = cache or _MagikCubeCache()
        self._reporting_actions_enabled = reporting_actions_enabled
        # 单个子查询失败时保留其它数据，并在报告末尾集中呈现 warning。
        self._warnings: list[str] = []

    async def generate(self, report_date: date, *, save_snapshot: bool = True) -> str:
        """生成完整日报，并并发执行彼此独立的统计查询。"""

        # 只有 is_key_account=true 的租户进入默认日报；空结果视为业务错误而不是空日报。
        tenants = await self._list_key_accounts()
        if not tenants:
            raise MagikCubeApiError("未找到 is_key_account=true 的大客户租户")

        # 用量、配额、Proxy、机器和 P/D 彼此独立，从首个租户请求开始就并行执行。
        metrics_task = asyncio.gather(
            *(self._tenant_metrics(tenant, report_date) for tenant in tenants)
        )
        quota_task = asyncio.create_task(self._list_quota_changes(report_date))
        proxy_task = asyncio.create_task(self._proxy_changes(report_date, save_snapshot))
        machines_task = asyncio.create_task(self._machine_usage())
        pd_task = asyncio.create_task(self._observed_pd_ratio())
        quotas = await quota_task
        # 无配额变更时无需为每个租户额外查询 endpoint/model-config 归属。
        resources_task = (
            asyncio.create_task(self._list_customer_resources(tenants)) if quotas else None
        )
        metrics_results, proxy_changes, machines, pd_summary = await asyncio.gather(
            metrics_task, proxy_task, machines_task, pd_task
        )
        resources = await resources_task if resources_task else {}
        metrics = {tenant.tenant_id: value for tenant, value in zip(tenants, metrics_results)}

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
        """按租户或模型查询用量，不写入 Proxy snapshot。"""

        # 指定查询默认不建立快照，避免一次预览/问答改变下一次正式日报的 diff 基线。
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

    async def generate_range_report(
        self,
        plan: _ComparisonPlan,
        *,
        tenant_query: str = "",
        model: str = "",
        breakdown: str = "summary",
        include_tpm: bool = True,
    ) -> str:
        """Generate a deterministic range report without any LLM calculation."""

        tenants = (
            [await self._require_single_tenant(tenant_query)]
            if tenant_query
            else await self._list_key_accounts()
        )
        if not tenants:
            raise MagikCubeApiError(f"未找到匹配客户：{tenant_query or '全部大客户'}")

        summaries = await asyncio.gather(
            *(
                self._tenant_metrics_for_windows(
                    tenant,
                    plan.fetch_windows,
                    model=model,
                    include_tpm=include_tpm,
                )
                for tenant in tenants
            )
        )
        model_results: dict[str, list[tuple[str, _TenantMetrics]]] = {}
        if breakdown == "model" and not model:
            model_lists = await asyncio.gather(*(self._list_tenant_models(item) for item in tenants))
            for tenant, models in zip(tenants, model_lists):
                values = await asyncio.gather(
                    *(
                        self._tenant_metrics_for_windows(
                            tenant,
                            plan.fetch_windows,
                            model=model_name,
                            include_tpm=include_tpm,
                        )
                        for model_name in models
                    )
                )
                model_results[tenant.tenant_id] = list(zip(models, values))

        return self._render_range_report(
            plan,
            tenants,
            summaries,
            model_results,
            tenant_query=tenant_query,
            model=model,
            breakdown=breakdown,
            include_tpm=include_tpm,
        )

    async def generate_token_total(
        self,
        plan: _ComparisonPlan,
        *,
        tenant_query: str,
        model: str = "",
    ) -> str:
        """Return one concise, exact token total for a tenant and date range."""

        tenants = await self._list_matching_tenants(tenant_query)
        if not tenants:
            raise MagikCubeApiError(f"未找到匹配客户：{tenant_query}")
        if len(tenants) > 1:
            options = "、".join(f"{item.name}（{item.tenant_id}）" for item in tenants)
            raise MagikCubeApiError(
                f"租户名称匹配到多个记录，请使用 tenant ID 指定：{options}"
            )
        summaries = await asyncio.gather(
            *(
                self._tenant_metrics_for_windows(
                    tenant,
                    plan.fetch_windows,
                    model=model,
                    include_tpm=False,
                )
                for tenant in tenants
            )
        )
        stats = self._period_stats(self._combine_metrics(summaries), plan.primary)
        scope = "、".join(tenant.name for tenant in tenants)
        if model:
            scope += f" / {model}"
        quality = "完整" if stats.token_complete else "数据不完整"
        lines = [
            f"{scope} 在 {_window_label(plan.primary)} 共使用 "
            f"{_format_million_tokens(stats.tokens)} Token（{stats.tokens:,} Token）。",
            f"请求数 {stats.requests:,}｜口径：租户计费用量｜数据状态：{quality}。",
        ]
        if plan.primary.end >= datetime.now(self._tz).date():
            lines.append("其中今天是截至查询时刻的实时数据，尚未结束。")
        if self._warnings:
            lines.extend(f"数据提示：{warning}" for warning in self._warnings[:10])
        return "\n".join(lines)

    def _tenant_display_label(self, tenant: _Tenant) -> str:
        """Show API catalog identity, optionally with an alias bound to that same ID."""

        alias = next(
            (
                value
                for value, tenant_id in self._config.tenant_mappings.items()
                if tenant_id == tenant.tenant_id
            ),
            "",
        )
        if alias and _normalize_tenant_query(alias) != _normalize_tenant_query(tenant.name):
            return f"{alias}（{tenant.name}）"
        if tenant.tags:
            return f"{tenant.name}（{'、'.join(tenant.tags[:3])}）"
        return tenant.name

    @staticmethod
    def _interaction_base_params(
        plan: _ComparisonPlan,
        *,
        granularity: str,
        include_tpm: bool,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "start_date": plan.primary.start.isoformat(),
            "end_date": plan.primary.end.isoformat(),
            "comparison": "none",
            "breakdown": "model",
            "include_tpm": include_tpm,
            "report_template": "matrix_card",
            "granularity": granularity,
            "interactive": True,
            "save_snapshot": False,
        }
        if plan.comparison:
            params["compare_start_date"] = plan.comparison.start.isoformat()
            params["compare_end_date"] = plan.comparison.end.isoformat()
        return params

    async def prepare_scope_interaction(
        self,
        plan: _ComparisonPlan,
        *,
        tenant_query: str,
        granularity: str,
        include_tpm: bool,
        report_template: str = "matrix_card",
    ) -> ToolResult:
        """Return a channel-agnostic customer/model-scope selection card."""

        tenants = (
            [await self._require_single_tenant(tenant_query)]
            if tenant_query
            else await self._list_key_accounts()
        )
        if not tenants:
            raise MagikCubeApiError(f"未找到匹配客户：{tenant_query or '大客户'}")
        options = [
            {
                # The callback stores the API identity. Labels may use a safe
                # alias, but every selectable customer must originate from Cube.
                "value": tenant.tenant_id,
                "label": self._tenant_display_label(tenant),
                "selected": bool(tenant_query),
            }
            for tenant in tenants[: self._config.max_report_items]
        ]
        base_params = self._interaction_base_params(
            plan, granularity=granularity, include_tpm=include_tpm
        )
        base_params["report_template"] = report_template
        ui = {
            "kind": "magik_report_form",
            "version": 1,
            "phase": "scope",
            "title": "选择报表范围",
            "period": _window_label(plan.primary),
            "base_params": base_params,
            "tenant_options": options,
            "tenant_required": not bool(tenant_query),
            "max_tenants": self._config.interactive_max_tenants,
            "scope_options": [
                {"value": "summary", "label": "汇总"},
                {"value": "all", "label": "所有模型"},
                {"value": "selected", "label": "指定模型"},
            ],
        }
        names = "、".join(item["label"] for item in options)
        content = (
            f"请选择报表范围。周期：{_window_label(plan.primary)}。"
            f"可选客户：{names}。模型范围：汇总、所有模型或指定模型。"
        )
        return ToolResult(content, metadata={OUTBOUND_META_AGENT_UI: ui})

    async def prepare_model_interaction(
        self,
        plan: _ComparisonPlan,
        selections: list[dict[str, Any]],
        *,
        granularity: str,
        include_tpm: bool,
        report_template: str = "matrix_card",
    ) -> ToolResult:
        """Return a second-round model selector for the chosen tenants."""

        tenant_models: list[dict[str, Any]] = []
        for selection in selections:
            query = str(selection.get("tenant_query") or "").strip()
            tenant = await self._require_single_tenant(query)
            models = await self._list_tenant_models(tenant)
            tenant_models.append(
                {
                    "tenant_query": query,
                    "tenant_label": tenant.name,
                    "models": models,
                }
            )
        ui = {
            "kind": "magik_report_form",
            "version": 1,
            "phase": "models",
            "title": "选择模型",
            "period": _window_label(plan.primary),
            "base_params": {
                **self._interaction_base_params(
                    plan, granularity=granularity, include_tpm=include_tpm
                ),
                "report_template": report_template,
            },
            "tenant_models": tenant_models,
            "max_tenants": self._config.interactive_max_tenants,
        }
        count = sum(len(item["models"]) for item in tenant_models)
        content = f"请选择模型。已加载 {len(tenant_models)} 个客户、{count} 个模型。"
        return ToolResult(content, metadata={OUTBOUND_META_AGENT_UI: ui})

    async def generate_matrix_report(
        self,
        plan: _ComparisonPlan,
        selections: list[dict[str, Any]],
        *,
        granularity: str,
        include_tpm: bool,
    ) -> ToolResult:
        """Generate one deterministic report-card payload per selected tenant."""

        tenant_limit = self._config.interactive_max_tenants
        if not selections or len(selections) > tenant_limit:
            raise MagikCubeApiError(f"一次必须选择 1 到 {tenant_limit} 个客户")
        tenant_semaphore = asyncio.Semaphore(2)

        async def load_one(
            selection: dict[str, Any],
        ) -> tuple[_Tenant, _TenantMetrics, list[tuple[str, _TenantMetrics]], str]:
            async with tenant_semaphore:
                query = str(selection.get("tenant_query") or "").strip()
                scope = str(selection.get("model_scope") or "summary")
                tenant = await self._require_single_tenant(query)
                model_names: list[str] = []
                if scope in {"all", "selected"}:
                    available = await self._list_tenant_models(tenant)
                    if scope == "all":
                        model_names = available
                    else:
                        requested = [
                            str(item).strip()
                            for item in selection.get("models", [])
                            if str(item).strip()
                        ]
                        by_key = {item.casefold(): item for item in available}
                        resolved = [
                            (item, _resolve_model_alias(item, self._config.model_aliases))
                            for item in requested
                        ]
                        unknown = [
                            original
                            for original, canonical in resolved
                            if canonical.casefold() not in by_key
                        ]
                        if unknown:
                            raise MagikCubeApiError(
                                f"{tenant.name} 存在未知模型：{'、'.join(unknown)}"
                            )
                        model_names = list(
                            dict.fromkeys(
                                by_key[canonical.casefold()]
                                for _original, canonical in resolved
                            )
                        )
                # `selected` 的顶部指标必须和表格使用同一模型口径；否则会把
                # K3 的模型明细和租户全模型的周期概览混在一张卡里。
                if scope == "selected":
                    model_metrics = await asyncio.gather(
                        *(
                            self._tenant_metrics_for_windows(
                                tenant,
                                plan.fetch_windows,
                                model=model_name,
                                include_tpm=include_tpm,
                            )
                            for model_name in model_names
                        )
                    )
                    summary = self._combine_metrics(list(model_metrics))
                    return tenant, summary, list(zip(model_names, model_metrics)), scope

                summary, *model_metrics = await asyncio.gather(
                    self._tenant_metrics_for_windows(
                        tenant, plan.fetch_windows, include_tpm=include_tpm
                    ),
                    *(
                        self._tenant_metrics_for_windows(
                            tenant,
                            plan.fetch_windows,
                            model=model_name,
                            # 全部模型表不展示逐模型 TPM；只保留一次租户汇总 TPM 查询。
                            include_tpm=False,
                        )
                        for model_name in model_names
                    ),
                )
                return tenant, summary, list(zip(model_names, model_metrics)), scope

        loaded = await asyncio.gather(
            *(load_one(item) for item in selections), return_exceptions=True
        )
        cards: list[dict[str, Any]] = []
        for selection, result in zip(selections, loaded, strict=True):
            if isinstance(result, BaseException):
                failure_code = classify_report_failure(result)
                logger.warning(
                    "Magik Cube matrix tenant query failed: error_type={} failure_code={}",
                    type(result).__name__,
                    failure_code,
                )
                cards.append(
                    self._build_matrix_error_card(
                        plan,
                        str(selection.get("tenant_query") or "未命名客户").strip(),
                        failure_code,
                    )
                )
                continue
            tenant, summary, model_rows, scope = result
            card = self._build_matrix_card(
                plan,
                tenant,
                summary,
                model_rows,
                model_scope=scope,
                granularity=granularity,
                include_tpm=include_tpm,
            )
            period_days = (plan.primary.end - plan.primary.start).days + 1
            subscription_period = (
                "month" if granularity == "week" else "day" if period_days == 1 else "week"
            )
            if self._reporting_actions_enabled:
                period_label = {"day": "日报", "week": "周报", "month": "月报"}[
                    subscription_period
                ]
                card["actions"] = [
                    {
                        "action_id": f"subscribe:{subscription_period}",
                        "label": f"订阅{period_label}",
                        "style": "default",
                        "tool_name": "report_center",
                        "params": {
                            "action": "subscription_setup",
                            "period": subscription_period,
                            "report_params": {
                                "tenant_query": str(selection.get("tenant_query") or ""),
                                "breakdown": "summary" if scope == "summary" else "model",
                                "report_template": "matrix_card",
                                "granularity": granularity,
                                "include_tpm": include_tpm,
                                "comparison": (
                                    "previous_month"
                                    if subscription_period == "month"
                                    else "previous_period"
                                ),
                                "report_selections": [dict(selection)],
                            },
                        },
                        "content": "创建固定报表订阅",
                    }
                ]
            cards.append(card)
        fallback = "\n\n".join(card["fallback_text"] for card in cards)
        ui = {"kind": "magik_report_cards", "version": 1, "cards": cards}
        return ToolResult(fallback, metadata={OUTBOUND_META_AGENT_UI: ui})

    @staticmethod
    def _build_matrix_error_card(
        plan: _ComparisonPlan,
        tenant_query: str,
        failure_code: ReportFailureCode,
    ) -> dict[str, Any]:
        """Represent one failed tenant explicitly without aborting sibling reports."""

        title = f"{tenant_query or '未命名客户'} 报表失败"
        message = report_failure_message(failure_code)
        retry_hint = (
            "请稍后重试"
            if failure_code in {"connection_failed", "rate_limited", "upstream_failed"}
            else "请重新选择 Cube 客户"
        )
        quality = f"数据质量：missing｜{message}；未将缺失数据按零处理"
        fallback = f"{title}｜{_window_label(plan.primary)}\n{quality}"
        return {
            "title": title,
            "subtitle": f"{_window_label(plan.primary)}｜Asia/Shanghai",
            "overview": [message],
            "segments": ["本次未生成统计结果"],
            "table": None,
            "insights": [retry_hint],
            "quality": quality,
            "fallback_text": fallback,
        }

    @staticmethod
    def _segment_pairs(
        plan: _ComparisonPlan, granularity: str
    ) -> list[tuple[str, _DateWindow, _DateWindow | None]]:
        size = 7 if granularity == "week" else 1
        pairs: list[tuple[str, _DateWindow, _DateWindow | None]] = []
        cursor = plan.primary.start
        index = 0
        weekdays = "一二三四五六日"
        while cursor <= plan.primary.end:
            current = _DateWindow(cursor, min(plan.primary.end, cursor + timedelta(days=size - 1)))
            baseline: _DateWindow | None = None
            if plan.comparison:
                baseline_start = plan.comparison.start + timedelta(days=index * size)
                if baseline_start <= plan.comparison.end:
                    baseline = _DateWindow(
                        baseline_start,
                        min(plan.comparison.end, baseline_start + timedelta(days=current.days - 1)),
                    )
            if granularity == "week":
                label = f"W{index + 1}"
            elif plan.primary.days == 7:
                label = f"周{weekdays[current.start.weekday()]}"
            else:
                label = current.start.strftime("%m-%d")
            pairs.append((label, current, baseline))
            cursor = current.end + timedelta(days=1)
            index += 1
        return pairs

    @staticmethod
    def _window_tokens(metrics: _TenantMetrics, window: _DateWindow) -> int:
        return sum(
            metrics.tokens.get((window.start + timedelta(days=offset)).isoformat(), 0)
            for offset in range(window.days)
        )

    def _segment_lines(
        self,
        metrics: _TenantMetrics,
        pairs: list[tuple[str, _DateWindow, _DateWindow | None]],
        *,
        comparison_label: str,
        include_same_weekday: bool = False,
    ) -> list[str]:
        if not metrics.token_complete:
            return ["数据不完整"]
        lines: list[str] = []
        for label, current_window, baseline_window in pairs:
            current = self._window_tokens(metrics, current_window)
            if baseline_window is None:
                change = "无同期数据"
            elif not any(
                baseline_window.contains(day) for day in metrics.tokens
            ):
                change = f"{comparison_label} 暂无可比基准"
            else:
                baseline = self._window_tokens(metrics, baseline_window)
                change = f"{comparison_label} {_format_delta(current, baseline)}"
            if include_same_weekday:
                same_weekday_window = _DateWindow(
                    current_window.start - timedelta(days=7),
                    current_window.end - timedelta(days=7),
                )
                if any(same_weekday_window.contains(day) for day in metrics.tokens):
                    same_weekday = self._window_tokens(metrics, same_weekday_window)
                    change += f"｜较上周同期 {_format_delta(current, same_weekday)}"
                else:
                    change += "｜较上周同期 暂无可比基准"
            lines.append(f"{label} {_format_number(current)}｜{change}")
        return lines

    @staticmethod
    def _comparison_label(plan: _ComparisonPlan) -> str:
        baseline = plan.comparison
        if baseline is None:
            return "无对比周期"
        if plan.primary.days == 1 and baseline.end == plan.primary.start - timedelta(days=1):
            return "较前一日"
        if plan.primary.days == 7 and baseline.end == plan.primary.start - timedelta(days=1):
            return "较上上周"
        if plan.primary.start.day == 1 and baseline.end == plan.primary.start - timedelta(days=1):
            return "较前一月"
        return "较对比周期"

    def _build_matrix_card(
        self,
        plan: _ComparisonPlan,
        tenant: _Tenant,
        summary: _TenantMetrics,
        model_rows: list[tuple[str, _TenantMetrics]],
        *,
        model_scope: str,
        granularity: str,
        include_tpm: bool,
    ) -> dict[str, Any]:
        current = self._period_stats(summary, plan.primary)
        baseline = self._period_stats(summary, plan.comparison) if plan.comparison else None
        same_weekday = (
            self._period_stats(summary, plan.same_weekday_comparison)
            if plan.same_weekday_comparison
            else None
        )
        comparison_label = self._comparison_label(plan)
        pairs = self._segment_pairs(plan, granularity)
        current_has_samples = any(
            plan.primary.contains(day)
            for values in (summary.tokens, summary.requests, summary.max_tpm)
            for day in values
        )
        no_business_data = (
            summary.token_complete and summary.tpm_complete and not current_has_samples
        )
        token_comparisons = [
            f"{comparison_label}（{_window_label(plan.comparison)}）："
            + (
                _format_delta(current.tokens, baseline.tokens)
                if baseline.token_sample_count
                else "暂无可比基准"
            )
            if baseline and plan.comparison
            else "无对比周期"
        ]
        request_comparisons = [
            f"{comparison_label}（{_window_label(plan.comparison)}）："
            + (
                _format_delta(current.requests, baseline.requests)
                if baseline.token_sample_count
                else "暂无可比基准"
            )
            if baseline and plan.comparison
            else "无对比周期"
        ]
        if same_weekday and plan.same_weekday_comparison:
            same_weekday_label = _window_label(plan.same_weekday_comparison)
            token_comparisons.append(
                f"较上周同期（{same_weekday_label}）："
                + (
                    _format_delta(current.tokens, same_weekday.tokens)
                    if same_weekday.token_sample_count
                    else "暂无可比基准"
                )
            )
            request_comparisons.append(
                f"较上周同期（{same_weekday_label}）："
                + (
                    _format_delta(current.requests, same_weekday.requests)
                    if same_weekday.token_sample_count
                    else "暂无可比基准"
                )
            )
        overview = [
            f"Token **{self._value_with_quality(current.tokens, current.token_complete)}**｜"
            + "｜".join(token_comparisons),
            f"请求数 **{self._value_with_quality(current.requests, current.token_complete)}**"
            + "｜"
            + "｜".join(request_comparisons),
        ]
        if include_tpm:
            average_tpm_text = self._average_tpm_text(current)
            average_tpm_comparisons: list[str] = []
            if current.average_tpm is not None and baseline and plan.comparison:
                average_tpm_comparisons.append(
                    f"{comparison_label}（{_window_label(plan.comparison)}）："
                    + (
                        _format_delta(current.average_tpm, baseline.average_tpm)
                        if baseline.average_tpm is not None
                        else "暂无可比基准"
                    )
                )
            if (
                current.average_tpm is not None
                and same_weekday
                and plan.same_weekday_comparison
            ):
                average_tpm_comparisons.append(
                    f"较上周同期（{_window_label(plan.same_weekday_comparison)}）："
                    + (
                        _format_delta(current.average_tpm, same_weekday.average_tpm)
                        if same_weekday.average_tpm is not None
                        else "暂无可比基准"
                    )
                )
            overview.append(
                f"平均 TPM **{average_tpm_text}**"
                + ("｜" + "｜".join(average_tpm_comparisons) if average_tpm_comparisons else "")
            )
            overview.append(
                f"{'最高 Endpoint 峰值 TPM' if current.tpm_series_count > 1 else '峰值 TPM'} "
                f"**{self._value_with_quality(current.peak_tpm, current.tpm_complete)}**"
            )

        tenant_total = current.tokens
        ranked_all = sorted(
            model_rows,
            key=lambda pair: (
                -self._period_stats(pair[1], plan.primary).tokens,
                pair[0].casefold(),
            ),
        )
        ranked: list[tuple[str, _TenantMetrics]] = []
        hidden_zero_models = 0
        for model_name, metrics in ranked_all:
            stats = self._period_stats(metrics, plan.primary)
            prior = self._period_stats(metrics, plan.comparison) if plan.comparison else None
            same_weekday_stats = (
                self._period_stats(metrics, plan.same_weekday_comparison)
                if plan.same_weekday_comparison
                else None
            )
            hide_as_irrelevant = (
                model_scope == "all"
                and prior is not None
                and stats.token_complete
                and prior.token_complete
                and stats.tokens == 0
                and prior.tokens == 0
                and (same_weekday_stats is None or same_weekday_stats.tokens == 0)
            )
            if hide_as_irrelevant:
                hidden_zero_models += 1
            else:
                ranked.append((model_name, metrics))
        rows: list[dict[str, str]] = []
        changes: list[tuple[int, str, int, int]] = []
        new_models: list[str] = []
        stopped_models: list[str] = []
        for model_name, metrics in ranked:
            stats = self._period_stats(metrics, plan.primary)
            prior = self._period_stats(metrics, plan.comparison) if plan.comparison else None
            same_weekday_stats = (
                self._period_stats(metrics, plan.same_weekday_comparison)
                if plan.same_weekday_comparison
                else None
            )
            share = stats.tokens / tenant_total if tenant_total else 0.0
            change = "无对比周期"
            if prior:
                change = (
                    f"{comparison_label} {_format_delta(stats.tokens, prior.tokens)}"
                    if prior.token_sample_count
                    else f"{comparison_label} 暂无可比基准"
                )
            if same_weekday_stats is not None:
                change += (
                    "\n较上周同期 "
                    + (
                        _format_delta(stats.tokens, same_weekday_stats.tokens)
                        if same_weekday_stats.token_sample_count
                        else "暂无可比基准"
                    )
                )
            if prior:
                delta = stats.tokens - prior.tokens
                changes.append((delta, model_name, stats.tokens, prior.tokens))
                if prior.tokens == 0 and stats.tokens > 0:
                    new_models.append(model_name)
                elif prior.tokens > 0 and stats.tokens == 0:
                    stopped_models.append(model_name)
            row = {
                "model": model_name,
                "total": f"{self._value_with_quality(stats.tokens, stats.token_complete)}\n占比 {share:.1%}",
                "change": change if stats.token_complete else "数据不完整",
            }
            if plan.primary.days > 1:
                row["segments"] = "\n".join(
                    self._segment_lines(
                        metrics,
                        pairs,
                        comparison_label=comparison_label,
                    )
                )
            rows.append(row)

        growth = sorted((item for item in changes if item[0] > 0), reverse=True)[:1]
        decline = sorted(item for item in changes if item[0] < 0)[:1]
        insights: list[str] = []
        if growth:
            insights.append(
                f"{comparison_label}增长贡献最大：{growth[0][1]} "
                f"{_format_change(growth[0][2], growth[0][3])}"
            )
        if decline:
            insights.append(
                f"{comparison_label}下降贡献最大：{decline[0][1]} "
                f"{_format_change(decline[0][2], decline[0][3])}"
            )
        if new_models:
            insights.append(f"新增：{'、'.join(sorted(new_models))}")
        if stopped_models:
            insights.append(f"停用：{'、'.join(sorted(stopped_models))}")
        insights = insights[:3] or ["本期无显著模型状态变化"]

        kind = (
            "日报"
            if plan.primary.days == 1
            else "周报"
            if plan.primary.days == 7
            else "月报"
            if plan.primary.start.day == 1
            else "区间报表"
        )
        quality = (
            "完整：无接口失败、分页截断或分片缺失"
            if not self._warnings and summary.token_complete and summary.tpm_complete
            else "数据不完整：" + "；".join(self._warnings[:5] or ["部分查询失败"])
        )
        if no_business_data:
            overview = [report_failure_message("no_business_data")]
            quality += "｜查询已成功，未返回当前周期业务记录"
        if model_scope == "all":
            quality += f"｜已隐藏 {hidden_zero_models} 个两期均为 0 的模型"
        table = None
        if model_scope != "summary" and not no_business_data:
            columns = [
                {"name": "model", "display_name": "模型", "data_type": "text"},
                {"name": "total", "display_name": "周期总量 / 占比", "data_type": "text"},
                {"name": "change", "display_name": "对比变化", "data_type": "text"},
            ]
            if plan.primary.days > 1:
                columns.append(
                    {"name": "segments", "display_name": "分段变化", "data_type": "text"}
                )
            table = {
                "title": "模型矩阵：按 Token 总量降序",
                "page_size": self._config.matrix_page_size,
                "columns": columns,
                "rows": rows,
            }
        daily_lines = (
            []
            if plan.primary.days == 1
            else self._segment_lines(
                summary,
                pairs,
                comparison_label=comparison_label,
                include_same_weekday=False,
            )
        )
        if no_business_data:
            daily_lines = [] if plan.primary.days == 1 else ["当前周期暂无业务记录"]
            insights = ["查询成功，当前周期没有可用于统计的业务样本"]
            rows = []
        tpm_table = (
            self._endpoint_tpm_table(tenant, summary, plan.primary)
            if include_tpm and not no_business_data
            else None
        )
        fallback_lines = [f"{tenant.name} {kind}｜{_window_label(plan.primary)}", *overview]
        if daily_lines:
            fallback_lines.append("分段变化：" + "；".join(daily_lines))
        for row in rows:
            fallback_row = (
                f"{row['model']}｜{row['total'].replace(chr(10), '｜')}｜{row['change']}"
            )
            if row.get("segments"):
                fallback_row += f"｜{row['segments'].replace(chr(10), '；')}"
            fallback_lines.append(fallback_row)
        if tpm_table:
            fallback_lines.append(str(tpm_table["title"]))
            for tpm_row in tpm_table["rows"]:
                fallback_lines.append(
                    f"{tpm_row['tenant']} / {tpm_row['model']} / {tpm_row['endpoint']}｜"
                    f"平均 TPM {tpm_row['avg_tpm']}｜峰值 TPM {tpm_row['peak_tpm']}｜"
                    f"有效样本 {tpm_row['samples']}｜{tpm_row['quality']}"
                )
        fallback_lines.extend(["关键变化：" + "；".join(insights), quality])
        footnote = (
            "来源：Cube Admin / analysis/endpoint-max-tpm/daily/query\n"
            "字段：avgTpm 为单 Endpoint 日平均 TPM，maxTpm 为单 Endpoint 日峰值 TPM\n"
            "聚合：不跨 Endpoint 或客户汇总\n"
            "变化：仅展示相对基准的百分比，不展示绝对增减值"
        )
        fallback_lines.append(footnote)
        comparison_windows = []
        if plan.comparison is not None:
            comparison_windows.append(
                {"label": comparison_label.removeprefix("较"), "window": _window_label(plan.comparison)}
            )
        if plan.same_weekday_comparison is not None:
            comparison_windows.append(
                {"label": "上周同期", "window": _window_label(plan.same_weekday_comparison)}
            )
        return {
            "title": f"{tenant.name} {kind}",
            "subtitle": f"{_window_label(plan.primary)}｜Asia/Shanghai",
            "comparison_windows": comparison_windows,
            "overview": overview,
            "segments": daily_lines,
            "table": table,
            "tpm_table": tpm_table,
            "insights": insights,
            "quality": quality,
            "footnote": footnote,
            "fallback_text": "\n".join(fallback_lines),
        }

    async def _get_pages(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        page_size: int = 500,
        label: str,
    ) -> list[dict[str, Any]]:
        """Fetch GET pagination with controlled concurrency and truncation visibility."""

        base = dict(params or {})

        async def fetch(page: int) -> dict[str, Any]:
            return await self._client.request(
                "GET",
                path,
                params={**base, "page_num": page, "page_size": page_size},
            )

        first = await fetch(1)
        first_items = [item for item in first.get("list") or [] if isinstance(item, dict)]
        total = _as_int(first.get("total"))
        required_pages = max(1, math.ceil(total / page_size)) if total else 1
        page_count = min(required_pages, self._config.max_pages)
        pages = await asyncio.gather(*(fetch(page) for page in range(2, page_count + 1)))
        items = list(first_items)
        for data in pages:
            items.extend(item for item in data.get("list") or [] if isinstance(item, dict))
        if required_pages > self._config.max_pages:
            self._warnings.append(
                f"{label} 分页截断：需要 {required_pages} 页，最多读取 {self._config.max_pages} 页"
            )
        return items

    @staticmethod
    def _parse_tenants(items: list[dict[str, Any]]) -> list[_Tenant]:
        tenants: list[_Tenant] = []
        for item in items:
            tenant_id = str(_pick(item, "tenantId", "tenant_id", default=""))
            if not tenant_id:
                continue
            tenants.append(
                _Tenant(
                    tenant_id=tenant_id,
                    name=str(_pick(item, "tenantName", "tenant_name", default=tenant_id)),
                    tags=tuple(
                        str(tag)
                        for tag in _pick(item, "tenantTags", "tenant_tags", default=[])
                    ),
                )
            )
        return tenants

    async def _list_key_accounts(self) -> list[_Tenant]:
        """分页读取大客户租户，并缓存 300 秒。"""

        cached = self._cache.get("tenants:key-accounts")
        if cached is not None:
            return cached
        items = await self._get_pages(
            "tenants",
            params={"isKeyAccount": "true"},
            label="大客户租户清单",
        )
        tenants = self._parse_tenants(
            [
                item
                for item in items
                if bool(_pick(item, "isKeyAccount", "is_key_account", default=False))
            ]
        )
        self._cache.put("tenants:key-accounts", tenants, self._config.cache_ttl_seconds)
        return tenants

    async def _list_all_tenants(self) -> list[_Tenant]:
        cached = self._cache.get("tenants:all")
        if cached is not None:
            return cached
        items = await self._get_pages("tenants", label="租户清单")
        tenants = self._parse_tenants(items)
        self._cache.put("tenants:all", tenants, self._config.cache_ttl_seconds)
        return tenants

    async def _list_matching_tenants(self, query: str) -> list[_Tenant]:
        """只在 Cube catalog 内按 ID、名称或 tags 匹配客户。"""

        catalog = await self._list_all_tenants()
        return _match_catalog_tenants(catalog, query, self._config.tenant_mappings)

    async def _require_single_tenant(self, query: str) -> _Tenant:
        matches = await self._list_matching_tenants(query)
        if not matches:
            raise MagikCubeTenantResolutionError(
                f"Cube tenant was not found: {query}",
                failure_code="tenant_not_found",
            )
        if len(matches) > 1:
            raise MagikCubeTenantResolutionError(
                f"Cube tenant selection was ambiguous: {query}",
                failure_code="tenant_ambiguous",
            )
        return matches[0]

    def _day_bounds(self, day: date) -> tuple[str, str]:
        """按配置时区生成左闭右开的一天边界，供 API 查询使用。"""

        start = datetime.combine(day, time.min, tzinfo=self._tz)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=self._tz)
        return start.isoformat(), end.isoformat()

    async def _list_tenant_models(self, tenant: _Tenant) -> list[str]:
        """Return all distinct configured model names for one tenant."""

        cache_key = f"models:{tenant.tenant_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            items = await self._get_pages(
                "inference/model-configs",
                params={"tenantId": tenant.tenant_id},
                label=f"{tenant.name} 模型清单",
            )
        except Exception as exc:
            self._warnings.append(
                f"{tenant.name} 模型清单获取失败：{report_failure_message(exc)}"
            )
            return []
        models = sorted(
            {
                str(_pick(item, "model", "modelName", "model_name", default="")).strip()
                for item in items
                if str(_pick(item, "model", "modelName", "model_name", default="")).strip()
            },
            key=str.casefold,
        )
        self._cache.put(cache_key, models, self._config.cache_ttl_seconds)
        return models

    async def _tenant_metrics_for_windows(
        self,
        tenant: _Tenant,
        windows: tuple[_DateWindow, ...],
        *,
        model: str = "",
        include_tpm: bool = True,
    ) -> _TenantMetrics:
        """Query exact windows, filter API over-return, and merge chunks without overlap."""

        tokens: dict[str, int] = {}
        requests: dict[str, int] = {}
        max_tpm: dict[str, int] = {}
        endpoints: dict[str, str] = {}
        endpoint_tpm: dict[tuple[str, str, str], _EndpointTpmPoint] = {}
        token_complete = True
        tpm_complete = True
        chunk_semaphore = asyncio.Semaphore(2)

        async def query_token(window: _DateWindow) -> tuple[dict[str, int], dict[str, int], bool]:
            try:
                async with chunk_semaphore:
                    start_time, _ = self._day_bounds(window.start)
                    _, end_time = self._day_bounds(window.end)
                    body: dict[str, Any] = {
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
                chunk_tokens: dict[str, int] = {}
                chunk_requests: dict[str, int] = {}
                seen: set[tuple[str, str]] = set()
                for index, item in enumerate(data.get("items") or []):
                    item_key = str(
                        _pick(
                            item,
                            "model",
                            "endpoint",
                            "tenantId",
                            "tenant_id",
                            default=index,
                        )
                    )
                    for point in item.get("points") or []:
                        day = str(point.get("date") or "")[:10]
                        dedupe_key = (item_key, day)
                        if not window.contains(day) or dedupe_key in seen:
                            continue
                        seen.add(dedupe_key)
                        chunk_tokens[day] = chunk_tokens.get(day, 0) + _as_int(
                            _pick(point, "totalTokens", "total_tokens")
                        )
                        chunk_requests[day] = chunk_requests.get(day, 0) + _as_int(
                            _pick(point, "requestCount", "request_count")
                        )
                return chunk_tokens, chunk_requests, True
            except Exception as exc:
                target = f" / {model}" if model else ""
                self._warnings.append(
                    f"{tenant.name}{target} Token/请求数 {_window_label(window)} 获取失败："
                    f"{report_failure_message(exc)}"
                )
                return {}, {}, False

        async def query_tpm(
            window: _DateWindow,
        ) -> tuple[
            dict[str, int],
            dict[str, str],
            dict[tuple[str, str, str], _EndpointTpmPoint],
            bool,
        ]:
            if not include_tpm:
                return {}, {}, {}, True
            try:
                async with chunk_semaphore:
                    body: dict[str, Any] = {
                        "startDate": window.start.isoformat(),
                        "endDate": window.end.isoformat(),
                        "tenantId": tenant.tenant_id,
                    }
                    if model:
                        body["model"] = model
                    data = await self._client.request(
                        "POST",
                        "analysis/endpoint-max-tpm/daily/query",
                        json_body=body,
                    )
                chunk_tpm: dict[str, int] = {}
                chunk_endpoints: dict[str, str] = {}
                chunk_points: dict[tuple[str, str, str], _EndpointTpmPoint] = {}
                for item in data.get("items") or []:
                    endpoint = str(item.get("endpoint") or "")
                    item_model = str(item.get("model") or model or "")
                    for point in item.get("points") or []:
                        day = str(point.get("date") or "")[:10]
                        if not window.contains(day):
                            continue
                        peak_value = _as_optional_int(_pick(point, "maxTpm", "max_tpm"))
                        average_value = _as_optional_int(_pick(point, "avgTpm", "avg_tpm"))
                        if peak_value is not None and peak_value >= chunk_tpm.get(day, -1):
                            chunk_tpm[day] = peak_value
                            chunk_endpoints[day] = endpoint
                        if peak_value is not None or average_value is not None:
                            key = (item_model, endpoint, day)
                            chunk_points[key] = _EndpointTpmPoint(
                                model=item_model,
                                endpoint=endpoint,
                                date=day,
                                max_tpm=peak_value,
                                avg_tpm=average_value,
                            )
                return chunk_tpm, chunk_endpoints, chunk_points, True
            except Exception as exc:
                target = f" / {model}" if model else ""
                self._warnings.append(
                    f"{tenant.name}{target} TPM {_window_label(window)} 获取失败："
                    f"{report_failure_message(exc)}"
                )
                return {}, {}, {}, False

        token_results, tpm_results = await asyncio.gather(
            asyncio.gather(*(query_token(window) for window in windows)),
            asyncio.gather(*(query_tpm(window) for window in windows)),
        )
        for chunk_tokens, chunk_requests, complete in token_results:
            token_complete = token_complete and complete
            for day, value in chunk_tokens.items():
                tokens[day] = tokens.get(day, 0) + value
            for day, value in chunk_requests.items():
                requests[day] = requests.get(day, 0) + value
        for chunk_tpm, chunk_endpoints, chunk_points, complete in tpm_results:
            tpm_complete = tpm_complete and complete
            for day, value in chunk_tpm.items():
                if value >= max_tpm.get(day, -1):
                    max_tpm[day] = value
                    endpoints[day] = chunk_endpoints.get(day, "")
            endpoint_tpm.update(chunk_points)
        return _TenantMetrics(
            tokens=tokens,
            requests=requests,
            max_tpm=max_tpm,
            max_tpm_endpoint=endpoints,
            endpoint_tpm=endpoint_tpm,
            token_complete=token_complete,
            tpm_complete=tpm_complete,
        )

    async def _tenant_metrics(
        self, tenant: _Tenant, report_date: date, *, model: str = ""
    ) -> _TenantMetrics:
        """查询一个租户最近 7 天 Token 总量和每日峰值 TPM。"""

        # 兼容旧日报：一次请求覆盖目标日、前日和 7 日前，渲染口径保持不变。
        window = _DateWindow(report_date - timedelta(days=7), report_date)
        return await self._tenant_metrics_for_windows(
            tenant, (window,), model=model, include_tpm=True
        )

    def _render_usage_query(
        self,
        report_date: date,
        tenant_query: str,
        model: str,
        tenants: list[_Tenant],
        metrics: list[_TenantMetrics],
    ) -> str:
        """渲染指定租户/模型的轻量查询结果。"""

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

    @staticmethod
    def _period_stats(metrics: _TenantMetrics, window: _DateWindow) -> _PeriodStats:
        days = [
            (window.start + timedelta(days=offset)).isoformat()
            for offset in range(window.days)
        ]
        daily_tokens = {day: metrics.tokens.get(day, 0) for day in days}
        tokens = sum(daily_tokens.values())
        requests = sum(metrics.requests.get(day, 0) for day in days)
        peak_tpm = max((metrics.max_tpm.get(day, 0) for day in days), default=0)
        tpm_points = [
            point
            for point in metrics.endpoint_tpm.values()
            if window.contains(point.date)
        ]
        tpm_series = {(point.model, point.endpoint) for point in tpm_points}
        average_values = [point.avg_tpm for point in tpm_points if point.avg_tpm is not None]
        average_tpm = (
            sum(average_values) / len(average_values)
            if len(tpm_series) == 1 and average_values
            else None
        )
        peak_date = max(days, key=lambda day: daily_tokens[day]) if days else ""
        return _PeriodStats(
            tokens=tokens,
            requests=requests,
            peak_tpm=peak_tpm,
            average_tpm=average_tpm,
            tpm_series_count=len(tpm_series),
            average_tpm_sample_count=len(average_values),
            token_sample_count=sum(day in metrics.tokens for day in days),
            daily_average_tokens=(tokens / window.days if window.days else 0.0),
            peak_date=peak_date,
            token_complete=metrics.token_complete,
            tpm_complete=metrics.tpm_complete,
        )

    @staticmethod
    def _combine_metrics(items: list[_TenantMetrics]) -> _TenantMetrics:
        combined = _TenantMetrics(tokens={}, requests={}, max_tpm={}, max_tpm_endpoint={})
        combined.token_complete = all(item.token_complete for item in items)
        combined.tpm_complete = all(item.tpm_complete for item in items)
        for item in items:
            for day, value in item.tokens.items():
                combined.tokens[day] = combined.tokens.get(day, 0) + value
            for day, value in item.requests.items():
                combined.requests[day] = combined.requests.get(day, 0) + value
            for day, value in item.max_tpm.items():
                if value >= combined.max_tpm.get(day, -1):
                    combined.max_tpm[day] = value
                    combined.max_tpm_endpoint[day] = item.max_tpm_endpoint.get(day, "")
            combined.endpoint_tpm.update(item.endpoint_tpm)
        return combined

    @staticmethod
    def _value_with_quality(value: int | float, complete: bool) -> str:
        rendered = _format_number(value)
        return rendered if complete else f"{rendered}（数据不完整）"

    @staticmethod
    def _average_tpm_text(stats: _PeriodStats) -> str:
        """Render avgTpm without inventing a cross-Endpoint aggregate."""

        if stats.tpm_series_count > 1:
            return "多 Endpoint/客户，不汇总"
        if stats.average_tpm is None:
            return "暂不可用"
        value = _format_number(stats.average_tpm)
        return value if stats.tpm_complete else f"{value}（数据不完整）"

    def _endpoint_tpm_table(
        self,
        tenant: _Tenant,
        metrics: _TenantMetrics,
        window: _DateWindow,
    ) -> dict[str, Any] | None:
        """Build per-series TPM details; no value is summed across endpoints."""

        grouped: dict[tuple[str, str], list[_EndpointTpmPoint]] = {}
        for point in metrics.endpoint_tpm.values():
            if window.contains(point.date):
                grouped.setdefault((point.model or "-", point.endpoint or "-"), []).append(
                    point
                )
        if not grouped:
            return None

        rows: list[dict[str, str]] = []
        sortable: list[tuple[float, str, str, dict[str, str]]] = []
        for (model, endpoint), points in grouped.items():
            average_values = [point.avg_tpm for point in points if point.avg_tpm is not None]
            peak_values = [point.max_tpm for point in points if point.max_tpm is not None]
            average_tpm = (
                sum(average_values) / len(average_values) if average_values else None
            )
            valid_days = len({point.date for point in points if point.avg_tpm is not None})
            if not metrics.tpm_complete:
                quality = "数据不完整"
            elif average_tpm is None:
                quality = "平均 TPM 暂不可用"
            elif valid_days < window.days:
                quality = f"样本 {valid_days}/{window.days} 天"
            else:
                quality = "完整"
            row = {
                "tenant": tenant.name,
                "model": model,
                "endpoint": endpoint,
                "avg_tpm": _format_number(average_tpm) if average_tpm is not None else "暂不可用",
                "peak_tpm": _format_number(max(peak_values)) if peak_values else "暂不可用",
                "samples": f"{valid_days}/{window.days} 天",
                "quality": quality,
            }
            sort_value = average_tpm if average_tpm is not None else -1
            sortable.append((-sort_value, model.casefold(), endpoint.casefold(), row))
        for _average, _model, _endpoint, row in sorted(sortable):
            rows.append(row)
        return {
            "title": "Endpoint TPM 明细：按平均 TPM 降序",
            "page_size": self._config.matrix_page_size,
            "columns": [
                {"name": "tenant", "display_name": "客户", "data_type": "text"},
                {"name": "model", "display_name": "模型", "data_type": "text"},
                {"name": "endpoint", "display_name": "Endpoint", "data_type": "text"},
                {"name": "avg_tpm", "display_name": "平均 TPM", "data_type": "text"},
                {"name": "peak_tpm", "display_name": "峰值 TPM", "data_type": "text"},
                {"name": "samples", "display_name": "有效样本", "data_type": "text"},
                {"name": "quality", "display_name": "数据质量", "data_type": "text"},
            ],
            "rows": rows,
        }

    def _period_summary_line(
        self,
        label: str,
        stats: _PeriodStats,
        *,
        include_tpm: bool,
    ) -> str:
        values = [
            f"Token {self._value_with_quality(stats.tokens, stats.token_complete)}",
            f"请求数 {self._value_with_quality(stats.requests, stats.token_complete)}",
            f"平均 TPM {self._average_tpm_text(stats)}",
            f"日均Token {_format_number(stats.daily_average_tokens)}",
            f"峰值日 {stats.peak_date or '无'}",
        ]
        if include_tpm:
            peak_label = (
                "最高 Endpoint 峰值 TPM" if stats.tpm_series_count > 1 else "峰值 TPM"
            )
            values.append(
                f"{peak_label} {self._value_with_quality(stats.peak_tpm, stats.tpm_complete)}"
            )
        return f"• {label}：" + "｜".join(values)

    def _render_range_report(
        self,
        plan: _ComparisonPlan,
        tenants: list[_Tenant],
        summaries: list[_TenantMetrics],
        model_results: dict[str, list[tuple[str, _TenantMetrics]]],
        *,
        tenant_query: str,
        model: str,
        breakdown: str,
        include_tpm: bool,
    ) -> str:
        """Render fixed calculations in a stable, reviewable order."""

        combined = self._combine_metrics(summaries)
        current = self._period_stats(combined, plan.primary)
        baseline = (
            self._period_stats(combined, plan.comparison) if plan.comparison else None
        )
        scope = tenant_query or "全部大客户"
        if model:
            scope += f" / {model}"
        lines = [
            f"📊 Magik Cube 确定性报表 · {_window_label(plan.primary)}",
            f"范围：{scope}｜维度：{'模型' if breakdown == 'model' else '汇总'}｜时区：Asia/Shanghai",
            "",
            "一、周期概览",
            self._period_summary_line("本期", current, include_tpm=include_tpm),
        ]
        if baseline and plan.comparison:
            average_change = (
                _format_change(current.average_tpm, baseline.average_tpm)
                if current.average_tpm is not None and baseline.average_tpm is not None
                else "暂无可比基准"
            )
            lines.append(
                f"• 较对比期（{_window_label(plan.comparison)}）："
                f"Token {_format_change(current.tokens, baseline.tokens)}｜"
                f"请求数 {_format_change(current.requests, baseline.requests)}｜"
                f"平均 TPM {average_change}"
            )
        if plan.same_weekday_comparison:
            same_weekday = self._period_stats(combined, plan.same_weekday_comparison)
            average_change = (
                _format_change(current.average_tpm, same_weekday.average_tpm)
                if current.average_tpm is not None and same_weekday.average_tpm is not None
                else "暂无可比基准"
            )
            lines.append(
                f"• 较上周同期（{_window_label(plan.same_weekday_comparison)}）："
                + (
                    f"Token {_format_change(current.tokens, same_weekday.tokens)}｜"
                    f"请求数 {_format_change(current.requests, same_weekday.requests)}｜"
                    f"平均 TPM {average_change}"
                    if same_weekday.token_sample_count
                    else "暂无可比基准"
                )
            )

        if breakdown != "model" or model:
            lines.extend(["", "二、租户明细"])
            for tenant, metrics in sorted(zip(tenants, summaries), key=lambda pair: pair[0].name):
                stats = self._period_stats(metrics, plan.primary)
                detail = self._period_summary_line(tenant.name, stats, include_tpm=include_tpm)
                if plan.comparison:
                    prior = self._period_stats(metrics, plan.comparison)
                    detail += f"｜Token环比 {_format_change(stats.tokens, prior.tokens)}"
                lines.append(detail)
        else:
            lines.extend(["", "二、全部模型（按本期 Token 降序）"])
            for tenant in tenants:
                rows = model_results.get(tenant.tenant_id, [])
                if len(tenants) > 1:
                    lines.append(f"【{tenant.name}】")
                tenant_summary = summaries[tenants.index(tenant)]
                tenant_total = self._period_stats(tenant_summary, plan.primary).tokens
                ranked = sorted(
                    rows,
                    key=lambda pair: (
                        -self._period_stats(pair[1], plan.primary).tokens,
                        pair[0].casefold(),
                    ),
                )
                for model_name, metrics in ranked:
                    stats = self._period_stats(metrics, plan.primary)
                    share = stats.tokens / tenant_total if tenant_total else 0.0
                    fields = [
                        f"Token {self._value_with_quality(stats.tokens, stats.token_complete)}",
                        f"占比 {share:.1%}",
                        f"请求 {_format_number(stats.requests)}",
                        f"平均 TPM {self._average_tpm_text(stats)}",
                    ]
                    if include_tpm:
                        fields.append(
                            f"峰值TPM {self._value_with_quality(stats.peak_tpm, stats.tpm_complete)}"
                        )
                    if plan.comparison:
                        prior = self._period_stats(metrics, plan.comparison)
                        fields.extend(
                            [
                                f"Token变化 {_format_change(stats.tokens, prior.tokens)}",
                                f"请求变化 {_format_change(stats.requests, prior.requests)}",
                                "平均 TPM 变化 "
                                + (
                                    _format_change(stats.average_tpm, prior.average_tpm)
                                    if stats.average_tpm is not None
                                    and prior.average_tpm is not None
                                    else "暂无可比基准"
                                ),
                            ]
                        )
                        if include_tpm:
                            fields.append(
                                f"峰值 TPM 变化 {_format_change(stats.peak_tpm, prior.peak_tpm)}"
                            )
                    lines.append(f"• {model_name}｜" + "｜".join(fields))
                if not ranked:
                    lines.append("• 模型清单不可用，未生成模型明细")

        lines.extend(["", "三、固定分析"])
        if plan.comparison:
            all_rows = [
                (tenant, model_name, metrics)
                for tenant in tenants
                for model_name, metrics in model_results.get(tenant.tenant_id, [])
            ]
            changes = []
            new_models = []
            stopped_models = []
            for tenant, model_name, metrics in all_rows:
                now_stats = self._period_stats(metrics, plan.primary)
                old_stats = self._period_stats(metrics, plan.comparison)
                display_name = (
                    f"{tenant.name}/{model_name}" if len(tenants) > 1 else model_name
                )
                delta = now_stats.tokens - old_stats.tokens
                changes.append((delta, display_name, now_stats.tokens, old_stats.tokens))
                if old_stats.tokens == 0 and now_stats.tokens > 0:
                    new_models.append(display_name)
                if old_stats.tokens > 0 and now_stats.tokens == 0:
                    stopped_models.append(display_name)
            growth = sorted((item for item in changes if item[0] > 0), reverse=True)[:5]
            decline = sorted((item for item in changes if item[0] < 0))[:5]
            lines.append(
                "• 增长排行："
                + (
                    "；".join(
                        f"{name} {_format_change(current_value, baseline_value)}"
                        for _delta, name, current_value, baseline_value in growth
                    )
                    or "无"
                )
            )
            lines.append(
                "• 下降排行："
                + (
                    "；".join(
                        f"{name} {_format_change(current_value, baseline_value)}"
                        for _delta, name, current_value, baseline_value in decline
                    )
                    or "无"
                )
            )
            lines.append(f"• 新增：{'、'.join(sorted(new_models)) or '无'}")
            lines.append(f"• 停用：{'、'.join(sorted(stopped_models)) or '无'}")
        else:
            lines.append("• 未配置对比周期，不计算增长、下降、新增和停用")

        anomalies: list[str] = []
        for tenant, summary in zip(tenants, summaries):
            tenant_total = self._period_stats(summary, plan.primary).tokens
            for model_name, metrics in model_results.get(tenant.tenant_id, []):
                stats = self._period_stats(metrics, plan.primary)
                share = stats.tokens / tenant_total if tenant_total else 0.0
                if share < self._config.trend_min_share:
                    continue
                daily = [
                    metrics.tokens.get(
                        (plan.primary.start + timedelta(days=offset)).isoformat(), 0
                    )
                    for offset in range(plan.primary.days)
                ]
                median = statistics.median(daily) if daily else 0
                threshold = median * self._config.spike_median_multiplier
                peak_days = [
                    (plan.primary.start + timedelta(days=offset)).isoformat()
                    for offset, value in enumerate(daily)
                    if value > threshold and value > 0
                ]
                if peak_days:
                    name = f"{tenant.name}/{model_name}" if len(tenants) > 1 else model_name
                    anomalies.append(f"{name}：{','.join(peak_days)}（占比 {share:.1%}）")
        lines.append(f"• 峰值异常：{'；'.join(anomalies) if anomalies else '无'}")

        lines.extend(["", "四、数据质量"])
        if self._warnings:
            lines.extend(f"• {warning}" for warning in self._warnings[:20])
        else:
            lines.append("• 完整：无接口失败、分页截断或分片缺失")
        return "\n".join(lines)

    async def _list_customer_resources(self, tenants: list[_Tenant]) -> dict[str, _Tenant]:
        """建立 endpoint/model-config ID 到租户的映射，用于归属配额变更。"""

        mapping: dict[str, _Tenant] = {}

        async def load(tenant: _Tenant, path: str, id_names: tuple[str, str]) -> None:
            # 每个租户的 endpoint 和 model-config 查询可并发；失败只影响配额变更归属，不阻断日报。
            try:
                items = await self._get_pages(
                    path,
                    params={"tenantId": tenant.tenant_id},
                    label=f"{tenant.name} {path} 资源映射",
                )
                for item in items:
                    entity_id = str(_pick(item, *id_names, default=""))
                    if entity_id:
                        mapping[entity_id] = tenant
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
        """分页读取目标日已执行的配额变更记录。"""

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
        """过滤并格式化属于大客户的 TPM/RPM/并发变更。"""

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
        """返回配置指定的集群，或从 API 自动发现集群名称。"""

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
        """读取各集群目标 Proxy 的限流字段，并返回快照完整性。"""

        clusters = await self._cluster_names()
        snapshot: dict[str, dict[str, int]] = {}
        # 只有全部集群查询成功时才允许写入新基线，避免部分快照误报“已删除”。
        async def load_cluster(cluster: str) -> tuple[dict[str, dict[str, int]], bool]:
            values: dict[str, dict[str, int]] = {}
            try:
                items = await self._get_pages(
                    "gateway/proxy-configs",
                    params={
                        "clusterName": cluster,
                        "namespace": self._config.proxy_namespace,
                        "labels": self._config.proxy_labels,
                    },
                    label=f"{cluster} Proxy 配置",
                )
                for item in items:
                    name = str(item.get("name") or "unknown")
                    config_data = item.get("data") or {}
                    if not isinstance(config_data, dict):
                        continue
                    raw = config_data.get("proxy.yaml")
                    if isinstance(raw, str):
                        values[f"{cluster}/{name}"] = _proxy_values(raw)
                return values, True
            except Exception as exc:
                self._warnings.append(f"{cluster} Proxy 配置获取失败：{exc}")
                return {}, False

        results = await asyncio.gather(*(load_cluster(cluster) for cluster in clusters))
        complete = all(item[1] for item in results)
        for values, _ in results:
            snapshot.update(values)
        return snapshot, complete

    async def _proxy_changes(self, report_date: date, save_snapshot: bool) -> list[str]:
        """加载旧快照、计算 Proxy 净变化，并在数据完整时原子写入新快照。"""

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
        # 第一次运行只建立基线，不把全部现存配置误报成新增变更。
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
        """获取并按机器数降序排列模型机器使用情况。"""

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
        """统计最近窗口内出现过的 Prefill/Decode Pod，并输出去约分后的观测比例。"""

        now = datetime.now(self._tz)
        start = now - timedelta(minutes=self._config.pd_window_minutes)
        # 使用 set 去重 Pod，统计的是活跃 Pod 数而不是请求数。
        prefill: set[str] = set()
        decode: set[str] = set()
        clusters = self._config.cluster_names or [""]

        async def load_cluster(cluster: str) -> list[dict[str, Any]]:
            params: dict[str, Any] = {
                "startTime": start.isoformat(),
                "endTime": now.isoformat(),
                "sortBy": "reqTimestamp",
                "sortOrder": "desc",
            }
            if cluster:
                params["clusterName"] = cluster
            return await self._get_pages(
                "gateway/usages",
                params=params,
                label=f"{cluster or '默认集群'} P/D 观测",
            )

        try:
            pages = await asyncio.gather(*(load_cluster(cluster) for cluster in clusters))
            for items in pages:
                for item in items:
                    p_name = str(
                        _pick(item, "prefillPodName", "prefill_pod_name", default="")
                    )
                    d_name = str(_pick(item, "podName", "pod_name", default=""))
                    if p_name:
                        prefill.add(p_name)
                    if d_name:
                        decode.add(d_name)
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
        """将各子查询结果渲染成面向运营/值班人员的中文日报。"""

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

        # 当前实现没有告警事件 API，因此明确标注数据边界，不把 HTTP 错误当正式告警。
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


# 交互卡片使用批量 selection；旧的 tenant_query/model 参数继续兼容单租户调用。
_REPORT_SELECTION_SCHEMA = ObjectSchema(
    tenant_query=StringSchema("Configured tenant alias or exact tenant name."),
    model_scope=StringSchema(
        "Report scope for this tenant.", enum=("summary", "all", "selected")
    ),
    models=ArraySchema(
        StringSchema("Exact model name."),
        description="Selected model names when model_scope=selected.",
    ),
    required=["tenant_query", "model_scope", "models"],
    additional_properties=False,
)

# User TokenAPI routes remain separated from the internal Admin allowlist.
_TOKEN_API_READ_ONLY_ROUTES = frozenset(
    {
        ("GET", "usages/token"),
        ("GET", "monthly_bills"),
        ("GET", "bills"),
        ("GET", "bills/details"),
        ("GET", "wallets/balance"),
        ("GET", "wallets/transactions"),
    }
)


# 暴露给 LLM 的参数契约。全部参数可选：默认查询 agent 时区下的昨天，并生成完整日报。
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
        "Optional Cube catalog tenant ID, tenant name, or tenant tag."
    ),
    model=StringSchema("Optional exact model filter, such as GLM-5.2."),
    start_date=StringSchema("Primary inclusive start date in YYYY-MM-DD."),
    end_date=StringSchema("Primary inclusive end date in YYYY-MM-DD."),
    compare_start_date=StringSchema("Optional comparison inclusive start date."),
    compare_end_date=StringSchema("Optional comparison inclusive end date."),
    comparison=StringSchema(
        "Automatic comparison window.",
        enum=("none", "previous_period", "previous_week", "previous_month"),
    ),
    breakdown=StringSchema("Report dimension.", enum=("summary", "model")),
    include_tpm=BooleanSchema(default=True, description="Include daily peak TPM queries."),
    report_template=StringSchema(
        "Output template. usage_total returns one concise exact Token total.",
        enum=("full", "brief", "matrix_card", "usage_total"),
    ),
    granularity=StringSchema(
        "Matrix comparison granularity.", enum=("day", "week")
    ),
    interactive=BooleanSchema(
        default=False,
        description="Return a deterministic parameter card when required slots are missing.",
    ),
    report_selections=ArraySchema(
        _REPORT_SELECTION_SCHEMA,
        description="Per-tenant model selections for matrix reports.",
        max_items=5,
    ),
    required=[],
    description=(
        "Generate a Magik Cube key-account daily report or deterministic date-range report. "
        "For deep analysis, call this tool exactly once and explain its fixed summary without "
        "recalculating any numeric value."
    ),
)


@tool_parameters(_MAGIK_CUBE_PARAMETERS)
class MagikCubeDailyReportTool(Tool):
    """Generate the daily key-account operations report."""

    # 对应配置文件 tools.magikCube；Base 配置层负责 camelCase alias 映射。
    config_key = "magik_cube"

    @classmethod
    def config_cls(cls):
        return MagikCubeToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        # 未显式 enable 时不注册该 Tool，避免无凭据环境出现无效 tool call。
        return ctx.config.magik_cube.enable

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        # Proxy 快照放在 runtime 子目录，和源码/用户 workspace 隔离。
        return cls(
            config=ctx.config.magik_cube,
            timezone=ctx.timezone,
            snapshot_path=get_runtime_subdir("magik_cube") / "proxy_snapshot.json",
            reporting_actions_enabled=bool(ctx.config.reporting.enable),
        )

    def __init__(
        self,
        config: MagikCubeToolConfig | None = None,
        timezone: str = "Asia/Shanghai",
        snapshot_path: Path | None = None,
        reporting_actions_enabled: bool = True,
    ) -> None:
        self._config = config or MagikCubeToolConfig()
        # 业务报表固定使用 Asia/Shanghai；保留参数仅为兼容既有构造接口。
        self._timezone = _REPORT_TIMEZONE
        self._snapshot_path = snapshot_path or (
            get_runtime_subdir("magik_cube") / "proxy_snapshot.json"
        )
        self._cache = _MagikCubeCache()
        self._reporting_actions_enabled = reporting_actions_enabled
        self._intent_candidates = IntentCandidateStore(
            get_runtime_subdir("magik_cube") / "intent_candidates.jsonl",
            retention_days=self._config.intent_candidate_retention_days,
            max_entries=self._config.intent_candidate_max_entries,
        )

    @property
    def name(self) -> str:
        # 该名称同时用于 LLM tool-call、调用预算和测试断言，属于稳定接口。
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
        # 单次范围 Tool 已覆盖主周期和对比周期，禁止 Agent 逐日或逐模型重复调用。
        return 1

    @property
    def read_only(self) -> bool:
        """All remote routes are constrained by the read-only allowlist."""

        return True

    async def resolve_tenant_queries(
        self, queries: list[str]
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """Resolve user-facing tenant names against the live Cube catalog.

        Returns uniquely matched tenants and unresolved entries without exposing
        catalog records outside the requested names. Callers may offer a partial
        confirmation, but must never silently drop unresolved tenants.
        """

        unique_queries = list(
            dict.fromkeys(query.strip() for query in queries if query.strip())
        )
        if len(unique_queries) > 20:
            raise ValueError("tenant resolution supports at most 20 queries")
        resolved: list[dict[str, str]] = []
        unresolved: list[dict[str, str]] = []
        async with MagikCubeClient(self._config) as client:
            reporter = MagikCubeReporter(
                client,
                self._config,
                self._snapshot_path,
                self._timezone,
                self._cache,
                self._reporting_actions_enabled,
            )
            for query in unique_queries:
                matches = await reporter._list_matching_tenants(query)
                if len(matches) != 1:
                    unresolved.append(
                        {
                            "query": query,
                            "reason": "匹配到多个客户" if matches else "未找到客户",
                        }
                    )
                    continue
                tenant = matches[0]
                display_name = next(
                    (
                        alias
                        for alias, tenant_id in self._config.tenant_mappings.items()
                        if tenant_id == tenant.tenant_id
                    ),
                    tenant.name,
                )
                resolved.append(
                    {
                        "query": query,
                        "tenant_id": tenant.tenant_id,
                        "display_name": display_name,
                    }
                )
        return resolved, unresolved

    async def list_tenant_catalog(
        self, *, limit: int = _MAX_TENANT_CATALOG_ITEMS
    ) -> list[dict[str, str]]:
        """Return a bounded live tenant catalog for guided management forms.

        The caller stores only stable tenant IDs and display labels. It must
        still perform RBAC before using this catalog to execute a report.  The
        catalog bound is intentionally independent from the per-report tenant
        fan-out limit: a management form may need to display hundreds of
        selectable customers while a single report remains capped at 20.
        """

        if not 1 <= limit <= _MAX_TENANT_CATALOG_ITEMS:
            raise ValueError(
                "tenant catalog limit must be between 1 and "
                f"{_MAX_TENANT_CATALOG_ITEMS}"
            )
        async with MagikCubeClient(self._config) as client:
            reporter = MagikCubeReporter(
                client,
                self._config,
                self._snapshot_path,
                self._timezone,
                self._cache,
                self._reporting_actions_enabled,
            )
            tenants = await reporter._list_all_tenants()
        if len(tenants) > limit:
            raise ValueError(f"Cube 客户数量超过 {limit} 个，请改为指定客户范围")
        aliases_by_id = {
            tenant_id: alias
            for alias, tenant_id in self._config.tenant_mappings.items()
        }
        return [
            {
                "tenant_id": tenant.tenant_id,
                "display_name": aliases_by_id.get(tenant.tenant_id, tenant.name),
            }
            for tenant in tenants[:limit]
        ]

    async def find_tenant_mentions(
        self, text: str, *, limit: int = 20
    ) -> list[dict[str, str]]:
        """Find explicitly named tenants in a live catalog without truncating matches.

        The selector intentionally caps the displayed catalog at ``limit``.
        Natural-language subscription input is different: a named customer may
        be beyond that first page, so the resolver scans the already bounded
        Cube catalog pagination and returns only labels actually present in the
        input.  No arbitrary text is promoted to a tenant identity.
        """

        if not 1 <= limit <= 20:
            raise ValueError("tenant mention limit must be between 1 and 20")
        if len(text) > 2_000:
            raise ValueError("tenant mention text is too long")
        async with MagikCubeClient(self._config) as client:
            reporter = MagikCubeReporter(
                client,
                self._config,
                self._snapshot_path,
                self._timezone,
                self._cache,
                self._reporting_actions_enabled,
            )
            tenants = await reporter._list_all_tenants()
        aliases_by_id = {
            str(tenant_id): str(alias).strip()
            for alias, tenant_id in self._config.tenant_mappings.items()
            if str(alias).strip() and str(tenant_id).strip()
        }
        folded = text.casefold()
        matches: list[tuple[int, int, _Tenant, str]] = []
        for tenant in tenants:
            labels = [
                aliases_by_id.get(tenant.tenant_id, ""),
                tenant.name,
                tenant.tenant_id,
                *tenant.tags,
            ]
            occurrences: list[tuple[int, int, str]] = []
            for label in dict.fromkeys(str(item).strip() for item in labels if str(item).strip()):
                start = folded.find(label.casefold())
                if start >= 0:
                    occurrences.append((start, -len(label), label))
            if occurrences:
                start, negative_length, label = min(occurrences)
                matches.append((start, negative_length, tenant, label))
        matches.sort(key=lambda item: (item[0], item[1], item[2].tenant_id))
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for _start, _negative_length, tenant, label in matches:
            if tenant.tenant_id in seen:
                continue
            seen.add(tenant.tenant_id)
            result.append(
                {
                    "tenant_id": tenant.tenant_id,
                    "display_name": aliases_by_id.get(tenant.tenant_id, tenant.name),
                    "matched_label": label,
                }
            )
            if len(result) >= limit:
                break
        return result

    async def resolve_models_for_tenants(
        self,
        tenant_ids: list[str],
        model_queries: list[str],
    ) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
        """Validate explicitly requested models against each live tenant catalog."""

        unique_tenants = list(
            dict.fromkeys(item.strip() for item in tenant_ids if item.strip())
        )
        unique_models = list(
            dict.fromkeys(item.strip() for item in model_queries if item.strip())
        )
        if len(unique_tenants) > 20 or len(unique_models) > 20:
            raise ValueError("model resolution supports at most 20 tenants and 20 models")
        resolved: dict[str, list[str]] = {}
        unresolved: list[dict[str, str]] = []
        async with MagikCubeClient(self._config) as client:
            reporter = MagikCubeReporter(
                client,
                self._config,
                self._snapshot_path,
                self._timezone,
                self._cache,
                self._reporting_actions_enabled,
            )
            for tenant_id in unique_tenants:
                tenant = await reporter._require_single_tenant(tenant_id)
                available = await reporter._list_tenant_models(tenant)
                by_name = {item.casefold(): item for item in available}
                matched: list[str] = []
                for query in unique_models:
                    canonical = _resolve_model_alias(query, self._config.model_aliases)
                    actual = by_name.get(canonical.casefold())
                    if actual is None:
                        unresolved.append(
                            {
                                "tenant_id": tenant_id,
                                "model": query,
                                "reason": "该客户实时模型目录中不存在此模型",
                            }
                        )
                        continue
                    matched.append(actual)
                resolved[tenant_id] = list(dict.fromkeys(matched))
        return resolved, unresolved

    async def list_models_for_tenants(
        self, tenant_ids: list[str]
    ) -> dict[str, list[str]]:
        """Load the current model catalog for each tenant without opening a UI.

        Scheduled reports cannot depend on the interactive selector's
        ``agent_ui`` payload: Cron has no user interface to complete a second
        selection step.  This bounded adapter therefore returns only the
        allowlisted model names needed to expand an ``all models`` scope.  An
        empty catalog is treated as an error by the caller because it is not
        distinguishable from a failed catalog request at this boundary.
        """

        unique_tenants = list(
            dict.fromkeys(item.strip() for item in tenant_ids if item.strip())
        )
        if not unique_tenants:
            raise ValueError("at least one tenant is required for model discovery")
        if len(unique_tenants) > 20:
            raise ValueError("model discovery supports at most 20 tenants")
        result: dict[str, list[str]] = {}
        async with MagikCubeClient(self._config) as client:
            reporter = MagikCubeReporter(
                client,
                self._config,
                self._snapshot_path,
                self._timezone,
                self._cache,
                self._reporting_actions_enabled,
            )
            for tenant_id in unique_tenants:
                tenant = await reporter._require_single_tenant(tenant_id)
                models = await reporter._list_tenant_models(tenant)
                if not models:
                    raise MagikCubeApiError(
                        f"Cube model catalog is empty for tenant {tenant_id}",
                        failure_code="upstream_failed",
                    )
                result[tenant_id] = models
        return result

    def match_direct_request(self, text: str) -> dict[str, Any] | None:
        """把明确的中文用量问题直接路由为结构化参数，绕过一次 LLM tool 选择。"""

        raw = text.strip()
        # ReportCenter owns subscription parsing. Returning a usage report here
        # would silently discard customers from a natural-language schedule.
        if is_subscription_intent_candidate(raw):
            return None
        if _CONTEXT_TENANT_REFERENCE_RE.search(raw):
            return None
        # 原因解释和业务建议需要 LLM；让 Agent 调用一次范围 Tool 后只解释确定性摘要。
        if is_deep_analysis_request(raw):
            return None
        promoted = match_promoted_rule(raw)
        if promoted is not None:
            return promoted.to_tool_params(
                today=datetime.now(ZoneInfo(self._timezone)).date()
            )
        # 第一层门槛必须出现用量/Token/TPM 语义，避免拦截普通模型能力或机器状态问题。
        if not re.search(
            r"(?:用量|使用量|使用情况|消耗|报表|周报|月报|日报|用了多少量|多少量|"
            r"(?<![A-Za-z0-9_])token(?![A-Za-z0-9_])|峰值\s*tpm|tpm)",
            raw,
            re.IGNORECASE,
        ):
            return None

        # 使用 ASCII 边界，避免“Kimi-K3模型”在 Unicode \b 上回退成“Kimi-”。
        model_match = re.search(
            r"(?<![A-Za-z0-9._-])(?:GLM|KIMI|MINIMAX|DEEPSEEK|QWEN|VLLM|HY)"
            r"[A-Z0-9._-]*(?![A-Za-z0-9._-])",
            raw,
            re.IGNORECASE,
        )
        model = model_match.group(0) if model_match else ""
        if not model:
            for alias in sorted(self._config.model_aliases, key=len, reverse=True):
                if re.search(
                    rf"(?<![A-Za-z0-9._-]){re.escape(alias)}(?![A-Za-z0-9._-])",
                    raw,
                    re.IGNORECASE,
                ):
                    model = alias
                    break
        if model:
            model = _resolve_model_alias(model, self._config.model_aliases)

        # 去掉不影响实体识别的礼貌词、动作词和相对日期词，再提取“xxx 用户/客户/租户”。
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
        # 已配置 alias 优先，并按长度倒序，避免短 alias 抢先匹配长 alias。
        tenant_query = next(
            (
                alias
                for alias in sorted(self._config.tenant_mappings, key=len, reverse=True)
                if alias.casefold() in raw.casefold()
            ),
            "",
        )
        # “所有客户”等量词代表默认大客户集合，不应误提取为名为“所有”的租户。
        all_customers = bool(
            re.search(
                r"(?:各大|所有|全部|各个|每个|全体)\s*(?:大客户|客户|租户)",
                raw,
            )
        )
        if not tenant_query and not all_customers:
            tenant_match = re.search(
                r"([A-Za-z0-9_\-\u4e00-\u9fff]+)\s*(?:用户|客户|租户)", cleaned
            )
            tenant_query = tenant_match.group(1) if tenant_match else ""
            if tenant_query in _INVALID_TENANT_REFERENCES:
                tenant_query = ""
        if not tenant_query and not all_customers:
            # Magik tenant slug 通常含下划线；允许无需“租户”后缀直接识别。
            slug_match = re.search(
                r"(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9-]*_[A-Za-z0-9_-]+)"
                r"(?![A-Za-z0-9_-])",
                raw,
            )
            tenant_query = slug_match.group(0) if slug_match else ""

        # 没有租户/模型/日报/全量意图时放弃 direct route，交回正常对话流程。
        is_daily_report = "大客户" in raw and "日报" in raw
        is_standard_period_report = bool(
            re.search(
                r"(?:日报|周报|月报|近\s*7\s*天|最近\s*7\s*天|一周|上周|上个月|上月|"
                r"这\s*(?:2|两)\s*天|近\s*(?:2|两)\s*天|最近\s*(?:2|两)\s*天)",
                raw,
            )
        )
        if (
            not tenant_query
            and not model
            and not is_daily_report
            and not is_standard_period_report
            and not all_customers
        ):
            return None

        full_requested = bool(
            re.search(r"(?:完整|详细|明细)\s*(?:日报|周报|月报|报表)", raw)
        )
        all_models_requested = bool(
            re.search(r"(?:各个模型|各模型|全部模型|所有模型|每个模型|模型维度|按模型)", raw)
        )
        summary_requested = bool(re.search(r"(?:汇总|总览|只看总量)", raw))

        # 即时问答不更新 Proxy 基线，避免临时查询污染下一次定时日报的净变化。
        params: dict[str, Any] = {"save_snapshot": False}
        # 不使用 Unicode \b：中文后缀“2026-08-29日”会让 \b 漏掉日期。
        explicit_dates = re.findall(r"(?<![0-9])\d{4}-\d{2}-\d{2}(?![0-9])", raw)
        today = datetime.now(ZoneInfo(self._timezone)).date()
        yesterday = today - timedelta(days=1)
        if len(explicit_dates) >= 2:
            params["start_date"], params["end_date"] = explicit_dates[:2]
            params["comparison"] = "previous_period" if re.search(r"(?:环比|对比)", raw) else "none"
        elif explicit_dates and "日报" in raw:
            # 指定日期优先于默认昨天；普通“日报”仍由下面分支使用昨天。
            selected_date = explicit_dates[0]
            if full_requested:
                params["report_date"] = selected_date
            else:
                params.update(
                    {
                        "start_date": selected_date,
                        "end_date": selected_date,
                        "comparison": "previous_period",
                    }
                )
        elif re.search(r"(?:这|近|最近)\s*(?:2|两)\s*天", raw):
            params.update(
                {
                    "start_date": yesterday.isoformat(),
                    "end_date": today.isoformat(),
                    "comparison": "none",
                }
            )
        elif "上周" in raw or "周报" in raw:
            last_week_start = today - timedelta(days=today.weekday() + 7)
            last_week_end = last_week_start + timedelta(days=6)
            if "上上周" in raw and re.search(r"(?:和|与|对比|比较|环比)", raw):
                params.update(
                    {
                        "start_date": last_week_start.isoformat(),
                        "end_date": last_week_end.isoformat(),
                        "compare_start_date": (last_week_start - timedelta(days=7)).isoformat(),
                        "compare_end_date": (last_week_end - timedelta(days=7)).isoformat(),
                        "comparison": "none",
                    }
                )
            elif "上上周" in raw:
                params.update(
                    {
                        "start_date": (last_week_start - timedelta(days=7)).isoformat(),
                        "end_date": (last_week_end - timedelta(days=7)).isoformat(),
                        "comparison": "previous_period",
                    }
                )
            else:
                params.update(
                    {
                        "start_date": last_week_start.isoformat(),
                        "end_date": last_week_end.isoformat(),
                        "comparison": "previous_period",
                    }
                )
        elif re.search(r"(?:近\s*7\s*天|最近\s*7\s*天|一周)", raw):
            params.update(
                {
                    "start_date": (yesterday - timedelta(days=6)).isoformat(),
                    "end_date": yesterday.isoformat(),
                    "comparison": "previous_period",
                }
            )
        elif re.search(r"(?:上个月|上月|月报)", raw):
            month_end = today.replace(day=1) - timedelta(days=1)
            params.update(
                {
                    "start_date": month_end.replace(day=1).isoformat(),
                    "end_date": month_end.isoformat(),
                    "comparison": "previous_month",
                }
            )
        elif "日报" in raw:
            if full_requested:
                params["report_date"] = yesterday.isoformat()
            else:
                params.update(
                    {
                        "start_date": yesterday.isoformat(),
                        "end_date": yesterday.isoformat(),
                        "comparison": "previous_period",
                    }
                )
        elif explicit_dates:
            params["report_date"] = explicit_dates[0]
        elif "今天" in raw:
            params["report_date"] = today.isoformat()
        elif "前天" in raw:
            params["report_date"] = (today - timedelta(days=2)).isoformat()

        if all_models_requested:
            params["breakdown"] = "model"
            # 模型维度统一走范围引擎；单日也用 1 天主周期与前 1 天对比。
            if "start_date" not in params:
                selected = date.fromisoformat(params.pop("report_date", yesterday.isoformat()))
                params.update(
                    {
                        "start_date": selected.isoformat(),
                        "end_date": selected.isoformat(),
                        "comparison": "previous_period",
                    }
                )
        if "start_date" in params:
            params["include_tpm"] = not bool(
                re.search(r"(?:不含|不要|排除|无需)\s*TPM", raw, re.IGNORECASE)
            )
        if re.search(
            r"(?:多少|合计|总共|一共).*?(?<![A-Za-z0-9_])M(?![A-Za-z0-9_]).*?token|"
            r"(?:多少|合计|总共|一共).*?token.*?(?<![A-Za-z0-9_])M(?![A-Za-z0-9_])",
            raw,
            re.IGNORECASE,
        ):
            params["report_template"] = "usage_total"
            params["include_tpm"] = False
        if tenant_query:
            params["tenant_query"] = tenant_query
        if model:
            params["model"] = model

        wants_card = params.get("report_template") != "usage_total" and not full_requested and bool(
            is_standard_period_report or re.search(r"(?:卡片|矩阵|报表)", raw)
        )
        if wants_card and "start_date" in params:
            params["report_template"] = "matrix_card"
            params["granularity"] = (
                "week" if re.search(r"(?:月报|上个月|上月)", raw) else "day"
            )
            if tenant_query and (all_models_requested or summary_requested or model):
                scope = "all" if all_models_requested else "selected" if model else "summary"
                params["report_selections"] = [
                    {
                        "tenant_query": tenant_query,
                        "model_scope": scope,
                        "models": [model] if model else [],
                    }
                ]
            else:
                # 裸“周报/月报”或只给客户但未给模型范围时，先用卡片补齐参数。
                params["interactive"] = True
        elif full_requested:
            params["report_template"] = "full"
        return params

    def match_contextual_request(
        self,
        text: str,
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not _CONTEXT_TENANT_REFERENCE_RE.search(text):
            return None
        tenant = _latest_tenant_from_history(
            history,
            sorted(self._config.tenant_mappings, key=len, reverse=True),
        )
        if not tenant:
            return None
        rewritten = _CONTEXT_TENANT_REFERENCE_RE.sub(f"{tenant}租户", text, count=1)
        return self.match_direct_request(rewritten)

    def is_direct_intent_candidate(self, text: str) -> bool:
        return (
            self._config.intent_fallback_enabled
            and not _CONTEXT_TENANT_REFERENCE_RE.search(text)
            # Subscription language belongs to ReportCenter.  Prevent the
            # legacy semantic fallback from turning a multi-customer schedule
            # into a narrowed daily-report intent when the new classifier is
            # disabled or unavailable.
            and not is_subscription_intent_candidate(text)
            and is_report_intent_candidate(text)
        )

    async def classify_direct_request(self, text: str, runtime: Any) -> dict[str, Any] | None:
        intent = await classify_report_intent(
            text,
            runtime,
            timeout_seconds=self._config.intent_fallback_timeout_seconds,
        )
        if intent is None:
            return None
        params = intent.to_tool_params(
            today=datetime.now(ZoneInfo(self._timezone)).date()
        )
        outcome = "interactive" if params.get("interactive") else "direct"
        candidate_id = self._intent_candidates.record(text, intent, outcome)
        logger.info(
            "Magik report intent fallback: outcome={} candidate={} has_tenant={} scope={}",
            outcome,
            candidate_id,
            bool(intent.tenant_text),
            intent.model_scope or "missing",
        )
        return params

    def fallback_direct_request(self, text: str) -> dict[str, Any] | None:
        intent = minimal_interactive_intent(text)
        if intent is None:
            return None
        logger.warning("Magik report intent fallback degraded to parameter card")
        return intent.to_tool_params(
            today=datetime.now(ZoneInfo(self._timezone)).date()
        )

    async def execute(
        self,
        report_date: str | None = None,
        save_snapshot: bool = True,
        tenant_query: str | None = None,
        model: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        compare_start_date: str | None = None,
        compare_end_date: str | None = None,
        comparison: str = "none",
        breakdown: str = "summary",
        include_tpm: bool = True,
        report_template: str = "full",
        granularity: str = "day",
        interactive: bool = False,
        report_selections: list[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> str:
        """校验配置与日期，创建只读 client，并分派完整日报或指定用量查询。"""

        # 支持静态 token 或账号密码二选一；账号和密码必须成对出现。
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
        range_values = (start_date, end_date, compare_start_date, compare_end_date)
        if report_date and any(range_values):
            return ToolResult.error("Error: report_date is mutually exclusive with range dates")
        if bool(start_date) != bool(end_date):
            return ToolResult.error("Error: start_date and end_date must be provided together")
        if (compare_start_date or compare_end_date) and not (start_date and end_date):
            return ToolResult.error("Error: comparison dates require start_date and end_date")
        if breakdown not in {"summary", "model"}:
            return ToolResult.error("Error: breakdown must be summary or model")
        if report_template not in {"full", "brief", "matrix_card", "usage_total"}:
            return ToolResult.error(
                "Error: report_template must be full, brief, matrix_card, or usage_total"
            )
        if granularity not in {"day", "week"}:
            return ToolResult.error("Error: granularity must be day or week")
        selections = list(report_selections or [])
        # ReportCenter is the only caller allowed to raise this hidden bound. The
        # public Tool schema rejects unknown arguments before execute(), preserving
        # the legacy interactive limit for LLM and direct compatibility calls.
        trusted_limit = 20 if _kwargs.get("_trusted_selection_limit") == 20 else None
        selection_limit = trusted_limit or self._config.interactive_max_tenants
        if len(selections) > selection_limit:
            return ToolResult.error(
                f"Error: report_selections supports at most "
                f"{selection_limit} tenants"
            )
        for selection in selections:
            if not isinstance(selection, dict):
                return ToolResult.error("Error: each report selection must be an object")
            scope = selection.get("model_scope")
            if scope not in {"summary", "all", "selected"}:
                return ToolResult.error(
                    "Error: model_scope must be summary, all, or selected"
                )
        if report_date:
            try:
                target_date = date.fromisoformat(report_date)
            except ValueError:
                return ToolResult.error("Error: report_date must use YYYY-MM-DD")
        else:
            # 无显式日期时按 agent 配置时区取昨天，避免 UTC 跨日导致日报错位。
            target_date = datetime.now(tz).date() - timedelta(days=1)
        plan: _ComparisonPlan | None = None
        if start_date and end_date:
            try:
                parsed_compare_start = (
                    date.fromisoformat(compare_start_date) if compare_start_date else None
                )
                parsed_compare_end = (
                    date.fromisoformat(compare_end_date) if compare_end_date else None
                )
                plan = _plan_comparison_windows(
                    date.fromisoformat(start_date),
                    date.fromisoformat(end_date),
                    comparison=comparison,
                    compare_start=parsed_compare_start,
                    compare_end=parsed_compare_end,
                    max_request_days=self._config.max_range_days_per_request,
                    max_query_days=self._config.max_query_days,
                )
            except ValueError as exc:
                return ToolResult.error(f"Error: invalid report range: {exc}")
        elif report_template in {"brief", "matrix_card"}:
            plan = _plan_comparison_windows(
                target_date,
                target_date,
                comparison="previous_period",
                max_request_days=self._config.max_range_days_per_request,
                max_query_days=self._config.max_query_days,
            )
        started = time_module.perf_counter()
        cache_hits_before = self._cache.hits
        cache_misses_before = self._cache.misses
        client: MagikCubeClient | None = None
        try:
            # async context 确保认证完成后再查询，并在所有路径关闭 httpx 连接池。
            async with MagikCubeClient(self._config) as client:
                reporter = MagikCubeReporter(
                    client,
                    self._config,
                    self._snapshot_path,
                    self._timezone,
                    self._cache,
                    self._reporting_actions_enabled,
                )
                if report_template in {"brief", "matrix_card"}:
                    assert plan is not None
                    if interactive and not selections:
                        return await reporter.prepare_scope_interaction(
                            plan,
                            tenant_query=(tenant_query or "").strip(),
                            granularity=granularity,
                            include_tpm=include_tpm,
                            report_template=report_template,
                        )
                    if not selections:
                        query = (tenant_query or "").strip()
                        if not query:
                            return ToolResult.error(
                                "Error: structured usage report requires tenant selection"
                            )
                        selections = [
                            {
                                "tenant_query": query,
                                "model_scope": (
                                    "selected" if model else "all" if breakdown == "model" else "summary"
                                ),
                                "models": [model] if model else [],
                            }
                        ]
                    needs_models = any(
                        item.get("model_scope") == "selected" and not item.get("models")
                        for item in selections
                    )
                    if needs_models:
                        if not interactive:
                            return ToolResult.error(
                                "Error: selected model scope requires at least one model"
                            )
                        return await reporter.prepare_model_interaction(
                            plan,
                            selections,
                            granularity=granularity,
                            include_tpm=include_tpm,
                            report_template=report_template,
                        )
                    result = await reporter.generate_matrix_report(
                        plan,
                        selections,
                        granularity=granularity,
                        include_tpm=include_tpm,
                    )
                    return self._compatibility_brief(result) if report_template == "brief" else result
                if report_template == "usage_total":
                    if plan is None or not (tenant_query or "").strip():
                        return ToolResult.error(
                            "Error: usage_total requires tenant_query plus start_date/end_date"
                        )
                    return await reporter.generate_token_total(
                        plan,
                        tenant_query=(tenant_query or "").strip(),
                        model=(model or "").strip(),
                    )
                if plan is not None:
                    return await reporter.generate_range_report(
                        plan,
                        tenant_query=(tenant_query or "").strip(),
                        model=(model or "").strip(),
                        breakdown=breakdown,
                        include_tpm=include_tpm,
                    )
                # 任一过滤条件存在就走轻量指定查询；否则生成含配置、机器和 P/D 的完整日报。
                if tenant_query or model:
                    return await reporter.generate_usage_query(
                        target_date,
                        tenant_query=(tenant_query or "").strip(),
                        model=(model or "").strip(),
                    )
                return await reporter.generate(target_date, save_snapshot=save_snapshot)
        except (MagikCubeApiError, httpx.HTTPError) as exc:
            return ToolResult.error(
                f"Error: failed to generate Magik Cube report: {report_failure_message(exc)}"
            )
        finally:
            elapsed_ms = (time_module.perf_counter() - started) * 1000
            logger.info(
                "Magik Cube report perf: mode={} elapsed_ms={:.0f} api_calls={} "
                "api_wait_ms={:.0f} cache_hits={} cache_misses={} http_429={} http_5xx={}",
                "brief"
                if report_template == "brief"
                else "matrix"
                if report_template == "matrix_card"
                else "usage_total"
                if report_template == "usage_total"
                else "range"
                if plan is not None
                else "daily",
                elapsed_ms,
                sum(client.route_counts.values()) if client else 0,
                client.request_seconds * 1000 if client else 0,
                self._cache.hits - cache_hits_before,
                self._cache.misses - cache_misses_before,
                client.rate_limit_errors if client else 0,
                client.server_errors if client else 0,
            )

    @staticmethod
    def _compatibility_brief(result: ToolResult) -> ToolResult:
        """Reduce a legacy matrix result without changing its already-computed values."""

        ui = result.metadata.get(OUTBOUND_META_AGENT_UI)
        if not isinstance(ui, dict) or ui.get("kind") != "magik_report":
            return result
        compact = deepcopy(ui)
        fallback_lines: list[str] = []
        for card in compact.get("cards") or []:
            if not isinstance(card, dict):
                continue
            title = str(card.get("title") or "Cube 报表")
            card["title"] = title if title.endswith("简报") else f"{title}简报"
            comparison_windows = [
                item for item in card.get("comparison_windows") or [] if isinstance(item, dict)
            ]
            overview = []
            for raw_line in card.get("overview") or []:
                line = str(raw_line)
                previous = re.search(r"较前一日（[^）]+）：([^｜]+)", line)
                weekly = re.search(r"较上周同期（[^）]+）：([^｜]+)", line)
                base = re.split(r"｜较(?:前一日|上周同期)", line, maxsplit=1)[0]
                if previous and weekly:
                    line = f"{base}｜同比：{weekly.group(1)}｜环比：{previous.group(1)}"
                elif previous:
                    line = f"{base}｜环比：{previous.group(1)}"
                overview.append(line)
            card["overview"] = overview
            card["comparison_windows"] = []
            card["segments"] = []
            card["table"] = None
            card["insights"] = []
            fallback_lines.extend([str(card.get("title") or "Cube 简报"), *overview])
            for item in comparison_windows:
                label = str(item.get("label") or "")
                window = str(item.get("window") or "")
                if label == "前一日":
                    fallback_lines.append(f"环比基准：前一日 {window}")
                elif label == "上周同期":
                    fallback_lines.append(f"同比基准：上周同期 {window}")
                elif label:
                    fallback_lines.append(f"环比基准：{label} {window}")
            fallback_lines.append("来源：Cube Admin")
            quality = str(card.get("quality") or "").strip()
            if quality:
                fallback_lines.append(quality)
        return ToolResult(
            "\n".join(fallback_lines) or str(result),
            is_error=result.is_error,
            metadata={**result.metadata, OUTBOUND_META_AGENT_UI: compact},
        )
