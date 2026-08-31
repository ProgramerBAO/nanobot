"""Allow-listed Grafana data connector for deterministic reports."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from nanobot.reporting.contracts import (
    CANONICAL_DIMENSIONS,
    CANONICAL_METRICS,
    ReportDataset,
    ReportQuery,
    SecretRef,
)
from nanobot.reporting.registry import (
    ConnectorCapabilities,
    ConnectorManifest,
    ConnectorPlugin,
)
from nanobot.reporting.secrets import resolve_secret, secret_ref_from_value

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_PLACEHOLDER_RE = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_ALLOWED_VARIABLES = frozenset(
    {"tenant", "project", "model", "endpoint", "environment", "provider", "cluster"}
)
_INTERNAL_FILTERS = frozenset({"model_scope", "models", "query_definition", "grafana_query_id"})


class GrafanaConnectorError(RuntimeError):
    """Safe connector error that does not carry response bodies or credentials."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class GrafanaQueryDefinition:
    """Approved Grafana query; the expression is deployment-owned, not user input."""

    query_id: str
    metric: str
    expression: str
    datasource_uid: str = ""
    dimensions: tuple[str, ...] = ("date",)
    allowed_filters: frozenset[str] = frozenset()
    step_seconds: int = 60

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.query_id):
            raise ValueError("invalid Grafana query_id")
        if self.metric not in CANONICAL_METRICS:
            raise ValueError(f"unsupported canonical metric: {self.metric}")
        if not self.expression.strip():
            raise ValueError("Grafana query expression is empty")
        if not self.dimensions or not set(self.dimensions) <= CANONICAL_DIMENSIONS:
            raise ValueError("Grafana query dimensions must be canonical")
        if not self.allowed_filters <= _ALLOWED_VARIABLES:
            raise ValueError("Grafana query filters contain unsupported variables")
        if self.step_seconds < 1:
            raise ValueError("Grafana query step_seconds must be positive")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GrafanaQueryDefinition":
        known = {
            "query_id", "queryId", "metric", "expression", "datasource_uid", "datasourceUid",
            "dimensions", "allowed_filters", "allowedFilters", "step_seconds", "stepSeconds",
        }
        unknown = set(value) - known
        if unknown:
            raise ValueError(f"unsupported Grafana query fields: {', '.join(sorted(unknown))}")
        return cls(
            query_id=str(value.get("query_id") or value.get("queryId") or "").strip(),
            metric=str(value.get("metric") or "").strip(),
            expression=str(value.get("expression") or ""),
            datasource_uid=str(value.get("datasource_uid") or value.get("datasourceUid") or "").strip(),
            dimensions=tuple(str(item) for item in value.get("dimensions") or ("date",)),
            allowed_filters=frozenset(
                str(item) for item in value.get("allowed_filters") or value.get("allowedFilters") or ()
            ),
            step_seconds=int(value.get("step_seconds") or value.get("stepSeconds") or 60),
        )


@dataclass(frozen=True, slots=True)
class GrafanaConnectorConfig:
    base_url: str
    datasource_uid: str = ""
    service_account_token: SecretRef | None = None
    query_definitions: tuple[GrafanaQueryDefinition, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    max_window_days: int = 90
    max_step_seconds: int = 3600
    timeout_seconds: float = 10.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.25

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GrafanaConnectorConfig":
        enabled = value.get("enabled", False)
        if not enabled:
            raise ValueError("Grafana connector is disabled")
        raw_queries = value.get("query_definitions") or value.get("queryDefinitions") or ()
        if isinstance(raw_queries, Mapping):
            raw_queries = [
                dict(item, query_id=key) if isinstance(item, Mapping) else item
                for key, item in raw_queries.items()
            ]
        if not isinstance(raw_queries, Sequence) or isinstance(raw_queries, (str, bytes)):
            raise ValueError("Grafana query_definitions must be a list or mapping")
        if any(not isinstance(item, (GrafanaQueryDefinition, Mapping)) for item in raw_queries):
            raise ValueError("Grafana query_definitions items must be objects")
        definitions = tuple(
            item if isinstance(item, GrafanaQueryDefinition) else GrafanaQueryDefinition.from_mapping(item)
            for item in raw_queries
            if isinstance(item, (GrafanaQueryDefinition, Mapping))
        )
        return cls(
            base_url=str(value.get("base_url") or value.get("baseUrl") or "").strip(),
            datasource_uid=str(value.get("datasource_uid") or value.get("datasourceUid") or "").strip(),
            service_account_token=secret_ref_from_value(
                value.get("service_account_token") or value.get("serviceAccountToken")
            ),
            query_definitions=definitions,
            allowed_hosts=tuple(str(item).strip().lower() for item in value.get("allowed_hosts") or value.get("allowedHosts") or ()),
            max_window_days=int(
                value.get("max_window_days")
                if value.get("max_window_days") is not None
                else value.get("maxWindowDays", 90)
            ),
            max_step_seconds=int(
                value.get("max_step_seconds")
                if value.get("max_step_seconds") is not None
                else value.get("maxStepSeconds", 3600)
            ),
            timeout_seconds=float(
                value.get("timeout_seconds")
                if value.get("timeout_seconds") is not None
                else value.get("timeoutSeconds", 10.0)
            ),
            max_retries=int(
                value.get("max_retries")
                if value.get("max_retries") is not None
                else value.get("maxRetries", 2)
            ),
            retry_backoff_seconds=float(
                value.get("retry_backoff_seconds")
                if value.get("retry_backoff_seconds") is not None
                else value.get("retryBackoffSeconds", 0.25)
            ),
        )


Sleep = Callable[[float], Awaitable[None]]


class GrafanaConnector(ConnectorPlugin):
    """Query only pre-approved Grafana datasource definitions."""

    def __init__(
        self,
        config: GrafanaConnectorConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        parsed = urlparse(config.base_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host or parsed.username:
            raise ValueError("Grafana base_url must be an absolute HTTP(S) URL without userinfo")
        allowed_hosts = config.allowed_hosts or (host,)
        if host not in allowed_hosts:
            raise ValueError("Grafana base_url host is not in allowed_hosts")
        if not config.query_definitions:
            raise ValueError("Grafana requires at least one approved query definition")
        if config.max_window_days < 1 or config.max_step_seconds < 1:
            raise ValueError("Grafana query limits must be positive")
        if config.timeout_seconds <= 0 or config.max_retries < 0 or config.max_retries > 5:
            raise ValueError("invalid Grafana timeout or retry configuration")
        self.config = config
        self._base_url = config.base_url.rstrip("/")
        self._http_client = http_client
        self._sleep = sleep
        self._definitions = {item.query_id: item for item in config.query_definitions}
        if len(self._definitions) != len(config.query_definitions):
            raise ValueError("Grafana query_id values must be unique")
        self._metric_definitions: dict[str, list[GrafanaQueryDefinition]] = {}
        for item in config.query_definitions:
            self._metric_definitions.setdefault(item.metric, []).append(item)
        metrics = frozenset(item.metric for item in config.query_definitions)
        dimensions = frozenset(
            dimension for item in config.query_definitions for dimension in item.dimensions
        )
        self.manifest = ConnectorManifest(
            connector_id="grafana",
            display_name="Grafana",
            version="1.0",
            auth_methods=("service_account",) if config.service_account_token else ("none",),
            capabilities=ConnectorCapabilities(
                metrics=metrics,
                dimensions=dimensions,
                max_window_days=config.max_window_days,
                supports_bulk_dimensions=True,
                read_only=True,
            ),
            config_schema={
                "base_url": {"type": "string", "format": "uri"},
                "datasource_uid": {"type": "string"},
                "service_account_token": {"type": "SecretRef"},
                "query_definitions": {"type": "array", "item": "GrafanaQueryDefinition"},
            },
            secret_fields=frozenset({"service_account_token"}),
            allowed_hosts=allowed_hosts,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], **kwargs: Any) -> "GrafanaConnector":
        return cls(GrafanaConnectorConfig.from_mapping(value), **kwargs)

    async def health_check(self) -> dict[str, Any]:
        payload = await self._request_json("GET", "/api/health")
        database = str(payload.get("database") or "ok")
        return {
            "status": "ok" if database == "ok" else "degraded",
            "version": str(payload.get("version") or ""),
            "configured_queries": len(self._definitions),
        }

    async def discover_catalog(self) -> dict[str, list[str]]:
        return {
            "query_definitions": sorted(self._definitions),
            "metrics": sorted(self.manifest.capabilities.metrics),
            "dimensions": sorted(self.manifest.capabilities.dimensions),
        }

    async def query(self, query: ReportQuery) -> ReportDataset:
        self._validate_query(query)
        definitions = self._select_definitions(query)
        variables = self._query_variables(query)
        payload = {
            "from": f"{query.start_date.isoformat()}T00:00:00Z",
            "to": f"{(query.end_date + timedelta(days=1)).isoformat()}T00:00:00Z",
            "queries": [
                self._grafana_query(item, query, variables, index)
                for index, item in enumerate(definitions)
            ],
        }
        try:
            response = await self._request_json("POST", "/api/ds/query", payload)
        except GrafanaConnectorError as exc:
            return ReportDataset(
                rows=(),
                quality="missing",
                warnings=(f"grafana_{exc.code}",),
                source=self.manifest.connector_id,
            )
        return self._dataset_from_response(response, definitions)

    def _validate_query(self, query: ReportQuery) -> None:
        if query.connector_id != self.manifest.connector_id:
            raise ValueError("ReportQuery connector_id does not match Grafana")
        if query.end_date < query.start_date:
            raise ValueError("Grafana query end_date must not be earlier than start_date")
        days = (query.end_date - query.start_date).days + 1
        if days > self.config.max_window_days:
            raise ValueError("Grafana query exceeds the configured time window")
        if query.step_seconds is not None and not 1 <= query.step_seconds <= self.config.max_step_seconds:
            raise ValueError("Grafana query step_seconds exceeds the configured limit")

    def _select_definitions(self, query: ReportQuery) -> tuple[GrafanaQueryDefinition, ...]:
        query_id = query.query_id or str(
            query.filters.get("query_definition") or query.filters.get("grafana_query_id") or ""
        )
        if query_id:
            definition = self._definitions.get(query_id)
            if definition is None:
                raise ValueError("Grafana query definition is not approved")
            if query.metrics and definition.metric not in query.metrics:
                raise ValueError("Grafana query definition does not provide requested metric")
            return (definition,)
        selected: list[GrafanaQueryDefinition] = []
        for metric in query.metrics:
            options = self._metric_definitions.get(metric, [])
            if len(options) != 1:
                raise ValueError("Grafana metric requires one explicit query definition")
            selected.append(options[0])
        if not selected:
            raise ValueError("Grafana query requires canonical metrics")
        return tuple(selected)

    @staticmethod
    def _query_variables(query: ReportQuery) -> dict[str, str]:
        raw = {
            key: value
            for key, value in query.filters.items()
            if key not in _INTERNAL_FILTERS and key not in {"step_seconds"}
        }
        if query.filters.get("model_scope") == "selected":
            models = [str(item).strip() for item in query.filters.get("models") or () if str(item).strip()]
            raw["model"] = "|".join(models)
        result: dict[str, str] = {}
        for key, value in raw.items():
            if key not in _ALLOWED_VARIABLES:
                raise ValueError(f"Grafana filter is not allowed: {key}")
            if value in (None, "", [], ()):
                continue
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                value = "|".join(str(item) for item in value)
            result[key] = str(value)
        return result

    def _grafana_query(
        self,
        definition: GrafanaQueryDefinition,
        query: ReportQuery,
        variables: Mapping[str, str],
        index: int,
    ) -> dict[str, Any]:
        allowed = definition.allowed_filters
        unknown = set(variables) - allowed
        if unknown:
            raise ValueError(
                f"Grafana query {definition.query_id} does not allow filters: {', '.join(sorted(unknown))}"
            )
        regex_fields = frozenset()
        if query.filters.get("model_scope") == "selected":
            regex_fields = frozenset({"model"})
        expression = self._render_expression(
            definition.expression, variables, allowed, regex_fields=regex_fields
        )
        step = min(query.step_seconds or definition.step_seconds, self.config.max_step_seconds)
        return {
            "refId": f"Q{index}",
            "datasource": {
                "uid": definition.datasource_uid or self.config.datasource_uid,
            },
            "model": {
                "refId": f"Q{index}",
                "expr": expression,
                "format": "time_series",
                "intervalMs": step * 1000,
                "interval": f"{step}s",
            },
        }

    @staticmethod
    def _render_expression(
        expression: str,
        variables: Mapping[str, str],
        allowed: frozenset[str],
        *,
        regex_fields: frozenset[str] = frozenset(),
    ) -> str:
        placeholders = set(_PLACEHOLDER_RE.findall(expression))
        unknown = placeholders - allowed
        if unknown:
            raise ValueError("Grafana expression contains an unapproved variable")

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            value = variables.get(name, ".*")
            # Values are inserted only into deployment-owned PromQL strings.
            if name in regex_fields:
                value = "|".join(re.escape(item) for item in value.split("|"))
            else:
                value = re.escape(value)
            return value.replace('"', r'\"')

        return _PLACEHOLDER_RE.sub(replace, expression)

    async def _request_json(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.config.service_account_token:
            headers["Authorization"] = "Bearer " + resolve_secret(self.config.service_account_token)
        for attempt in range(self.config.max_retries + 1):
            try:
                if self._http_client is not None:
                    response = await self._http_client.request(
                        method, self._base_url + path, json=payload, headers=headers
                    )
                else:
                    timeout = httpx.Timeout(self.config.timeout_seconds)
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        response = await client.request(
                            method, self._base_url + path, json=payload, headers=headers
                        )
            except httpx.TimeoutException:
                if attempt < self.config.max_retries:
                    await self._sleep(self.config.retry_backoff_seconds * (2**attempt))
                    continue
                raise GrafanaConnectorError("timeout") from None
            except httpx.TransportError:
                if attempt < self.config.max_retries:
                    await self._sleep(self.config.retry_backoff_seconds * (2**attempt))
                    continue
                raise GrafanaConnectorError("transport_error") from None
            if response.status_code in {401, 403}:
                raise GrafanaConnectorError("auth_error", status_code=response.status_code)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.config.max_retries:
                    await self._sleep(self.config.retry_backoff_seconds * (2**attempt))
                    continue
                raise GrafanaConnectorError(
                    "rate_limited" if response.status_code == 429 else "upstream_5xx",
                    status_code=response.status_code,
                )
            if response.status_code >= 400:
                raise GrafanaConnectorError("upstream_4xx", status_code=response.status_code)
            try:
                value = response.json()
            except ValueError:
                raise GrafanaConnectorError("invalid_json") from None
            if not isinstance(value, dict):
                raise GrafanaConnectorError("invalid_payload")
            return value
        raise GrafanaConnectorError("request_failed")

    def _dataset_from_response(
        self, payload: Mapping[str, Any], definitions: tuple[GrafanaQueryDefinition, ...]
    ) -> ReportDataset:
        results = payload.get("results")
        if not isinstance(results, Mapping):
            return ReportDataset(
                rows=(), quality="missing", warnings=("grafana_invalid_results",), source="grafana"
            )
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        failed = 0
        for index, definition in enumerate(definitions):
            result = results.get(f"Q{index}")
            if not isinstance(result, Mapping):
                failed += 1
                warnings.append(f"query_missing:{definition.query_id}")
                continue
            if result.get("error"):
                failed += 1
                warnings.append(f"query_error:{definition.query_id}")
                continue
            rows.extend(self._rows_from_result(result, definition))
        if failed == len(definitions):
            quality = "missing"
        elif failed:
            quality = "partial"
        else:
            quality = "complete"
        return ReportDataset(
            rows=tuple(rows),
            quality=quality,
            warnings=tuple(warnings),
            source="grafana",
        )

    @staticmethod
    def _rows_from_result(
        result: Mapping[str, Any], definition: GrafanaQueryDefinition
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for frame in result.get("frames") or ():
            if not isinstance(frame, Mapping):
                continue
            schema = frame.get("schema") if isinstance(frame.get("schema"), Mapping) else {}
            fields = schema.get("fields") or frame.get("fields") or ()
            data = frame.get("data") if isinstance(frame.get("data"), Mapping) else {}
            values = data.get("values") or ()
            if not isinstance(fields, Sequence) or not isinstance(values, Sequence):
                continue
            names = [str(field.get("name") or "") if isinstance(field, Mapping) else "" for field in fields]
            time_index = next(
                (idx for idx, name in enumerate(names) if name.casefold() in {"time", "timestamp"}),
                None,
            )
            value_index = next(
                (idx for idx in range(len(names)) if idx != time_index and names[idx]), None
            )
            if value_index is None or value_index >= len(values):
                continue
            count = len(values[value_index]) if isinstance(values[value_index], Sequence) else 0
            for row_index in range(count):
                row: dict[str, Any] = {
                    "metric": definition.metric,
                    "value": values[value_index][row_index],
                }
                if time_index is not None and time_index < len(values):
                    time_values = values[time_index]
                    if isinstance(time_values, Sequence) and row_index < len(time_values):
                        row["timestamp"] = time_values[row_index]
                value_field = fields[value_index]
                labels = value_field.get("labels") if isinstance(value_field, Mapping) else {}
                if isinstance(labels, Mapping):
                    row.update({str(key): value for key, value in labels.items()})
                rows.append(row)
        for table in result.get("tables") or ():
            if not isinstance(table, Mapping):
                continue
            columns = [str(item) for item in table.get("columns") or ()]
            for values in table.get("rows") or ():
                if not isinstance(values, Sequence):
                    continue
                row = {column: value for column, value in zip(columns, values, strict=False)}
                row["metric"] = definition.metric
                row["value"] = row.get("value", row.get(definition.metric, ""))
                rows.append(row)
        return rows
