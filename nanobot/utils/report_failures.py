"""Safe, channel-neutral report failure classification."""

from __future__ import annotations

from typing import Literal

import httpx

ReportFailureCode = Literal[
    "tenant_not_found",
    "tenant_ambiguous",
    "connection_failed",
    "auth_failed",
    "rate_limited",
    "upstream_failed",
    "no_business_data",
]

_FAILURE_MESSAGES: dict[str, str] = {
    "tenant_not_found": "客户标识已失效，请重新选择客户",
    "tenant_ambiguous": "匹配到多个客户，请从列表中精确选择",
    "connection_failed": "Cube 暂时无法连接，本次未取得数据",
    "auth_failed": "Cube 查询权限或凭据异常",
    "rate_limited": "Cube 请求受限，请稍后重试",
    "upstream_failed": "Cube 服务异常，本次数据不可用",
    "no_business_data": "查询成功，当前周期暂无业务数据",
}
_TRANSIENT_FAILURES = frozenset(
    {"connection_failed", "rate_limited", "upstream_failed"}
)


class ReportFailureError(RuntimeError):
    """Exception carrying a safe failure category instead of raw upstream data."""

    def __init__(self, message: str, *, failure_code: ReportFailureCode) -> None:
        super().__init__(message)
        self.failure_code = failure_code


def classify_report_failure(exc: BaseException) -> ReportFailureCode:
    """Map an exception to a stable code safe for documents and audit records."""

    explicit = getattr(exc, "failure_code", "")
    if explicit in _FAILURE_MESSAGES:
        return explicit
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError)):
        return "connection_failed"
    return "upstream_failed"


def report_failure_message(value: str | BaseException) -> str:
    """Return user-facing text without exposing exception details."""

    code = classify_report_failure(value) if isinstance(value, BaseException) else value
    return _FAILURE_MESSAGES.get(code, _FAILURE_MESSAGES["upstream_failed"])


def report_failure_code_from_warning(warning: str) -> ReportFailureCode | None:
    """Extract a failure code from a normalized warning prefix."""

    code = str(warning).partition(":")[0].strip()
    return code if code in _FAILURE_MESSAGES else None  # type: ignore[return-value]


def is_transient_report_failure(value: str | BaseException) -> bool:
    """Return whether a failure may succeed on one bounded delayed retry."""

    code = (
        classify_report_failure(value)
        if isinstance(value, BaseException)
        else report_failure_code_from_warning(value)
    )
    return code in _TRANSIENT_FAILURES
