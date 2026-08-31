"""Safe, opt-in staging contract validation for the read-only Cube connector."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from nanobot.agent.tools.magik_cube import MagikCubeClient, MagikCubeToolConfig, _pick

_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class CubeContractProbe:
    """One fixed read-only route and the fields required by the normalizer."""

    probe_id: str
    method: str
    route: str
    collection_names: tuple[str, ...]
    required_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CubeContractProbeResult:
    """Safe structural outcome. It deliberately excludes response values and IDs."""

    probe_id: str
    method: str
    route: str
    status: str
    collection: str = ""
    item_count: int = 0
    matched_required_paths: tuple[str, ...] = ()
    missing_required_paths: tuple[str, ...] = ()
    error_type: str = ""


@dataclass(frozen=True, slots=True)
class CubeContractGateResult:
    """Serializable release-gate result without raw Cube response content."""

    contract_version: str
    environment: str
    quality: str
    probes: tuple[CubeContractProbeResult, ...]
    request_count: int
    request_seconds: float
    rate_limit_errors: int
    server_errors: int

    def to_safe_dict(self) -> dict[str, Any]:
        """Return only safe contract metadata suitable for terminal output or audit."""

        return asdict(self)


_PROBES: tuple[CubeContractProbe, ...] = (
    CubeContractProbe(
        "usage",
        "POST",
        "analysis/active-tenant-daily-usage/query",
        ("items",),
        ("items.model", "items.points.date", "items.points.totalTokens", "items.points.requestCount"),
    ),
    CubeContractProbe(
        "tpm",
        "POST",
        "analysis/endpoint-max-tpm/daily/query",
        ("items",),
        ("items.model", "items.endpoint", "items.points.date", "items.points.maxTpm"),
    ),
    CubeContractProbe(
        "performance_endpoints",
        "POST",
        "analysis/performance-endpoints/query",
        ("items", "list"),
        ("items.endpoint", "items.model"),
    ),
    CubeContractProbe(
        "token_utilization",
        "POST",
        "analysis/token-utilization/query",
        ("items", "list"),
        ("items.actualTokens", "items.tpmLimit", "items.utilizationRate"),
    ),
    CubeContractProbe(
        "daily_token_utilization",
        "POST",
        "analysis/token-utilization/daily/query",
        ("items", "list"),
        ("items.actualTokens", "items.tpmLimit", "items.utilizationRate"),
    ),
    CubeContractProbe(
        "model_performance",
        "POST",
        "analysis/model-performance/query",
        ("series", "items", "list"),
        ("series.endpoint", "series.points.timestamp", "series.points.value"),
    ),
    CubeContractProbe(
        "endpoint_tpm_trend",
        "POST",
        "analysis/endpoint-tpm-trend/query",
        ("items", "list"),
        ("items.endpoint", "items.points.date", "items.points.maxTpm"),
    ),
    CubeContractProbe(
        "gateway_usages",
        "GET",
        "gateway/usages",
        ("list",),
        ("list.stream", "list.streamDone", "list.respCode", "list.ttft"),
    ),
)
_PROBE_BY_ID = {probe.probe_id: probe for probe in _PROBES}


class CubeContractGate:
    """Run a bounded staging probe using only existing Cube read-only routes."""

    def __init__(
        self,
        config: MagikCubeToolConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: datetime | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._now = now

    async def run(self, *, tenant_id: str) -> CubeContractGateResult:
        """Validate fixed route shapes without retaining response bodies or identifiers."""

        self._require_staging_read_only_config(tenant_id)
        now = (self._now or datetime.now(_TIMEZONE)).astimezone(_TIMEZONE)
        realtime_end = now.replace(second=0, microsecond=0)
        realtime_start = realtime_end - timedelta(minutes=15)
        daily = realtime_end.date() - timedelta(days=1)
        results: list[CubeContractProbeResult] = []

        async with MagikCubeClient(self._config, transport=self._transport) as client:
            _, usage_result = await self._request_probe(
                client,
                "usage",
                json_body={
                    "startTime": self._day_start(daily).isoformat(),
                    "endTime": self._day_start(daily + timedelta(days=1)).isoformat(),
                    "tenantId": tenant_id,
                    "topN": 0,
                    "timeLevel": "TIME_LEVEL_DAY",
                },
            )
            results.append(usage_result)
            _, tpm_result = await self._request_probe(
                client,
                "tpm",
                json_body={
                    "startDate": daily.isoformat(),
                    "endDate": daily.isoformat(),
                    "tenantId": tenant_id,
                },
            )
            results.append(tpm_result)
            endpoint_data, endpoints_result = await self._request_probe(
                client,
                "performance_endpoints",
                json_body={},
            )
            results.append(endpoints_result)
            _, utilization_result = await self._request_probe(
                client,
                "token_utilization",
                json_body={"startTime": realtime_start.isoformat(), "endTime": realtime_end.isoformat()},
            )
            results.append(utilization_result)
            _, daily_utilization_result = await self._request_probe(
                client,
                "daily_token_utilization",
                json_body={"startDate": daily.isoformat(), "endDate": daily.isoformat()},
            )
            results.append(daily_utilization_result)

            endpoint, model = self._first_endpoint(endpoint_data)
            if endpoint:
                _, performance_result = await self._request_probe(
                    client,
                    "model_performance",
                    json_body={
                        "endpoints": [endpoint],
                        "metric": "TIMESERIES_METRIC_RPM",
                        "startTime": realtime_start.isoformat(),
                        "endTime": realtime_end.isoformat(),
                        "intervalMinutes": 1,
                    },
                )
                results.append(performance_result)
                _, trend_result = await self._request_probe(
                    client,
                    "endpoint_tpm_trend",
                    json_body={"startDate": daily.isoformat(), "endDate": daily.isoformat()},
                )
                results.append(trend_result)
                _, gateway_result = await self._request_probe(
                    client,
                    "gateway_usages",
                    params={
                        "endpoint": endpoint,
                        "model": model,
                        "start_time": realtime_start.isoformat(),
                        "end_time": realtime_end.isoformat(),
                        "page_num": 1,
                        "page_size": 50,
                        "stream": "true",
                        "stream_done": "true",
                    },
                )
                results.append(gateway_result)
            else:
                results.extend(
                    self._skipped_probe("model_performance", "no_endpoint_available"),
                )
                results.extend(
                    self._skipped_probe("endpoint_tpm_trend", "no_endpoint_available"),
                )
                results.extend(
                    self._skipped_probe("gateway_usages", "no_endpoint_available"),
                )

            request_count = sum(client.route_counts.values())
            request_seconds = round(client.request_seconds, 3)
            rate_limit_errors = client.rate_limit_errors
            server_errors = client.server_errors

        return CubeContractGateResult(
            contract_version="cube-staging-v1",
            environment="staging",
            quality=self._quality(results),
            probes=tuple(results),
            request_count=request_count,
            request_seconds=request_seconds,
            rate_limit_errors=rate_limit_errors,
            server_errors=server_errors,
        )

    @staticmethod
    def _day_start(value: date) -> datetime:
        return datetime.combine(value, time.min, tzinfo=_TIMEZONE)

    def _require_staging_read_only_config(self, tenant_id: str) -> None:
        if self._config.deployment_environment != "staging":
            raise ValueError("Cube contract validation requires deployment_environment=staging")
        if not self._config.contract_validation_enabled:
            raise ValueError("Cube contract validation is disabled in configuration")
        if not self._config.enable:
            raise ValueError("Magik Cube connector is disabled")
        if not self._config.base_url or not (self._config.access_token or self._config.password):
            raise ValueError("Cube staging read-only credentials are not configured")
        if not tenant_id.strip():
            raise ValueError("Cube contract validation requires an explicit tenant ID")

    async def _request_probe(
        self,
        client: MagikCubeClient,
        probe_id: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], CubeContractProbeResult]:
        probe = _PROBE_BY_ID[probe_id]
        try:
            data = await client.request(
                probe.method,
                probe.route,
                params=params,
                json_body=json_body,
            )
        except Exception as exc:
            return {}, CubeContractProbeResult(
                probe_id=probe.probe_id,
                method=probe.method,
                route=probe.route,
                status="error",
                error_type=type(exc).__name__,
            )
        return data, _profile_probe(probe, data)

    @staticmethod
    def _first_endpoint(data: Mapping[str, Any]) -> tuple[str, str]:
        values = data.get("items") or data.get("list") or []
        candidates: list[tuple[str, str]] = []
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
            return "", ""
        for item in values:
            if not isinstance(item, Mapping):
                continue
            endpoint = str(
                _pick(item, "endpoint", "endpointName", "endpointId", "endpoint_id", default="")
            ).strip()
            if not endpoint:
                continue
            model = str(_pick(item, "model", "modelName", "model_name", default="")).strip()
            candidates.append((endpoint, model))
        return min(candidates, key=lambda value: (value[0].casefold(), value[1].casefold())) if candidates else ("", "")

    @staticmethod
    def _skipped_probe(probe_id: str, reason: str) -> tuple[CubeContractProbeResult, ...]:
        probe = _PROBE_BY_ID[probe_id]
        return (
            CubeContractProbeResult(
                probe_id=probe.probe_id,
                method=probe.method,
                route=probe.route,
                status="skipped",
                error_type=reason,
            ),
        )

    @staticmethod
    def _quality(results: Sequence[CubeContractProbeResult]) -> str:
        statuses = {result.status for result in results}
        if statuses == {"verified"}:
            return "complete"
        if "verified" in statuses or "no_data" in statuses:
            return "partial"
        return "missing"


def profile_cube_contract_fixture(fixture: Mapping[str, Any]) -> tuple[CubeContractProbeResult, ...]:
    """Validate the committed sanitized fixture against the same runtime expectations."""

    health = fixture.get("health") if isinstance(fixture.get("health"), Mapping) else {}
    payloads = {
        "usage": fixture.get("usage"),
        "tpm": fixture.get("tpm"),
        "performance_endpoints": health.get("performance_endpoints"),
        "token_utilization": health.get("token_utilization"),
        "daily_token_utilization": health.get("daily_token_utilization"),
        "model_performance": health.get("model_performance"),
        "endpoint_tpm_trend": health.get("endpoint_tpm_trend"),
        "gateway_usages": fixture.get("gateway_usages"),
    }
    results: list[CubeContractProbeResult] = []
    for probe_id, payload in payloads.items():
        probe = _PROBE_BY_ID[probe_id]
        if isinstance(payload, Mapping):
            results.append(_profile_probe(probe, payload))
        else:
            results.append(
                CubeContractProbeResult(
                    probe_id=probe.probe_id,
                    method=probe.method,
                    route=probe.route,
                    status="shape_mismatch",
                    missing_required_paths=probe.required_paths,
                )
            )
    return tuple(results)


def compare_metric_summaries(
    legacy: Mapping[str, tuple[float | int | None, float | int | None]],
    candidate: Mapping[str, tuple[float | int | None, float | int | None]],
) -> dict[str, Any]:
    """Return a value-free old/new semantic diff suitable for report-run audit records."""

    legacy_metrics = set(legacy)
    candidate_metrics = set(candidate)
    compared = sorted(legacy_metrics.intersection(candidate_metrics))
    differing = [
        metric
        for metric in compared
        if not _metric_pair_equal(legacy[metric], candidate[metric])
    ]
    return {
        "calculation_version": "cube-shadow-v1",
        "status": "match" if not differing and legacy_metrics == candidate_metrics else "drift",
        "compared_metrics": compared,
        "differing_metrics": differing,
        "legacy_only_metrics": sorted(legacy_metrics - candidate_metrics),
        "candidate_only_metrics": sorted(candidate_metrics - legacy_metrics),
    }


def _profile_probe(probe: CubeContractProbe, data: Mapping[str, Any]) -> CubeContractProbeResult:
    collection, values = _collection_values(data, probe.collection_names)
    if not collection:
        return CubeContractProbeResult(
            probe_id=probe.probe_id,
            method=probe.method,
            route=probe.route,
            status="shape_mismatch",
            missing_required_paths=probe.required_paths,
        )
    if not values:
        return CubeContractProbeResult(
            probe_id=probe.probe_id,
            method=probe.method,
            route=probe.route,
            status="no_data",
            collection=collection,
        )
    normalized_paths = tuple(
        path.replace(probe.collection_names[0], collection, 1)
        for path in probe.required_paths
    )
    matched = tuple(path for path in normalized_paths if _path_exists(data, path.split(".")))
    missing = tuple(path for path in normalized_paths if path not in matched)
    return CubeContractProbeResult(
        probe_id=probe.probe_id,
        method=probe.method,
        route=probe.route,
        status="verified" if not missing else "shape_mismatch",
        collection=collection,
        item_count=len(values),
        matched_required_paths=matched,
        missing_required_paths=missing,
    )


def _collection_values(
    data: Mapping[str, Any], collection_names: Sequence[str]
) -> tuple[str, tuple[Any, ...]]:
    for name in collection_names:
        value = data.get(name)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return name, tuple(value)
    return "", ()


def _path_exists(value: Any, parts: Sequence[str]) -> bool:
    if not parts:
        return True
    if isinstance(value, Mapping):
        next_value = value.get(parts[0])
        return parts[0] in value and _path_exists(next_value, parts[1:])
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_path_exists(item, parts) for item in value)
    return False


def _metric_pair_equal(
    left: tuple[float | int | None, float | int | None],
    right: tuple[float | int | None, float | int | None],
) -> bool:
    return all(_metric_value_equal(first, second) for first, second in zip(left, right, strict=True))


def _metric_value_equal(left: float | int | None, right: float | int | None) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
