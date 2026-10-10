"""Bounded read-only Trail REST adapter shared by reminders and MCP.

No arbitrary URLs, database access, retries, or model-controlled credentials.
Operator-configured origin and projects narrow the server's current authority.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import httpx

from nanobot.config.schema import TrailIntegrationConfig
from nanobot.security.network import PinnedDNSAsyncTransport, configure_ssrf_whitelist

log = logging.getLogger(__name__)
REMINDERS = frozenset({"issue.due_soon", "issue.overdue", "issue.update_overdue"})
ISSUE_FIELDS = frozenset({
    "id", "number", "project_id", "subject", "status_id", "tracker_id", "assigned_to_id",
    "due_date", "original_due_at", "next_update_at", "cs_level", "closed_on", "updated_at",
})


class TrailError(Exception):
    """Safe typed failure; never includes response bodies, tokens or private URLs."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def load_settings() -> TrailIntegrationConfig:
    """Read explicit TRAIL_* environment values; fail startup on invalid input.

    JSON arrays are required for PROJECT_IDS and ALLOWED_CIDRS. No secrets are
    printed, and validation errors must be reported without their input values.
    """
    values: dict[str, Any] = {}
    for name in TrailIntegrationConfig.model_fields:
        raw = os.environ.get(f"TRAIL_{name.upper()}")
        if raw is not None:
            values[name] = json.loads(raw) if name in {"project_ids", "allowed_cidrs"} else raw
    return TrailIntegrationConfig.model_validate(values)


class TrailClient:
    """One bounded client, up to two in-flight queries, with no internal retries."""

    def __init__(self, config: TrailIntegrationConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        configure_ssrf_whitelist(config.allowed_cidrs)
        self._http = httpx.AsyncClient(
            base_url=config.base_url, follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(5, connect=2, pool=1),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            transport=transport or PinnedDNSAsyncTransport(allow_loopback=config.allow_loopback),
            headers={"Authorization": f"Bearer {config.api_token.get_secret_value()}"},
        )
        self._gate = asyncio.Semaphore(2)
        self._requests: deque[float] = deque()

    async def close(self) -> None:
        """Release connections after receiver shutdown or MCP exit."""
        await self._http.aclose()

    def require_project(self, project_id: int) -> None:
        """Fail closed even for public projects outside the explicit allowlist."""
        if type(project_id) is not int or project_id not in self.config.project_ids:
            raise TrailError("project_not_allowed")

    async def get(self, path: str, params: dict[str, Any] | None = None, *, authorized_project: int | None = None) -> Any:
        """GET a code-owned relative path with 8s total and 256KiB body limits."""
        started = time.monotonic()
        outcome = "ok"
        try:
            while self._requests and self._requests[0] < started - 60:
                self._requests.popleft()
            if len(self._requests) >= 120:
                raise TrailError("local_rate_limited", retryable=True)
            self._requests.append(started)
            if path != "/integrations/trail/identity":
                identity_params = {"project_id": authorized_project} if authorized_project is not None else None
                identity = await self.get("/integrations/trail/identity", identity_params)
                if not isinstance(identity, dict) or identity.get("user_id") != self.config.service_user_id or identity.get("scope") != "issues:read":
                    raise TrailError("identity_mismatch")
            async with asyncio.timeout(8), self._gate:
                async with self._http.stream("GET", path, params=params) as response:
                    if response.status_code in {401, 403, 404}:
                        raise TrailError({401: "unauthorized", 403: "forbidden", 404: "not_found"}[response.status_code])
                    if response.status_code == 429 or response.status_code >= 500:
                        raise TrailError("upstream_unavailable", retryable=True)
                    if response.status_code != 200:
                        raise TrailError("invalid_response")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 256 * 1024:
                            raise TrailError("response_too_large")
                    try:
                        return json.loads(body)
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise TrailError("invalid_response") from exc
        except (httpx.HTTPError, TimeoutError) as exc:
            outcome = "network_unavailable"
            raise TrailError(outcome, retryable=True) from exc
        except TrailError as exc:
            outcome = exc.code
            raise
        finally:
            log.info("trail_query", extra={"outcome": outcome, "duration_seconds": time.monotonic() - started})

    async def projects(self) -> list[dict[str, Any]]:
        """List current readable projects intersected with the configured scope."""
        rows = await self.get("/projects")
        if not isinstance(rows, list):
            raise TrailError("invalid_response")
        result = []
        for row in rows:
            if not isinstance(row, dict) or row.get("id") not in self.config.project_ids:
                continue
            try:
                await self.get("/integrations/trail/identity", {"project_id": row["id"]})
            except TrailError as exc:
                if exc.code == "forbidden":
                    continue
                raise
            result.append({key: row[key] for key in ("id", "identifier", "name") if key in row})
        return result

    def project_issue(self, row: Any) -> dict[str, Any]:
        """Project trusted fields only; user text remains explicitly untrusted."""
        if not isinstance(row, dict):
            raise TrailError("invalid_response")
        if type(row.get("id")) is not int or row["id"] <= 0:
            raise TrailError("invalid_response")
        self.require_project(row.get("project_id"))
        out = {key: value for key, value in row.items() if key in ISSUE_FIELDS}
        out["subject"] = str(out.get("subject", ""))[:500]
        out["url"] = f"{self.config.base_url}/?project_id={out['project_id']}&issue_id={out.get('id')}"
        return out

    async def issue(self, issue_id: int, project_id: int) -> dict[str, Any]:
        """Fetch one issue; verify its project after server authorization."""
        self.require_project(project_id)
        if type(issue_id) is not int or issue_id <= 0:
            raise TrailError("invalid_input")
        row = await self.get(f"/issues/{issue_id}", authorized_project=project_id)
        out = self.project_issue(row)
        if out["project_id"] != project_id or out["id"] != issue_id:
            raise TrailError("project_mismatch")
        return out

    async def search(self, project_id: int, query: str = "", limit: int = 20, before_id: int | None = None) -> dict[str, Any]:
        """Search within one project using Trail's existing keyset pagination."""
        self.require_project(project_id)
        if not isinstance(query, str) or len(query) > 200 or not 1 <= limit <= 50:
            raise TrailError("invalid_input")
        params: dict[str, Any] = {"project_id": project_id, "q": query, "limit": limit}
        if before_id is not None:
            if before_id <= 0:
                raise TrailError("invalid_input")
            params["before_id"] = before_id
        rows = await self.get("/issues", params, authorized_project=project_id)
        if not isinstance(rows, list) or len(rows) > limit:
            raise TrailError("invalid_response")
        items = [self.project_issue(row) for row in rows]
        if any(row["project_id"] != project_id for row in items):
            raise TrailError("project_mismatch")
        return {"items": items, "page_full": len(items) == limit,
                "next_before_id": items[-1]["id"] if len(items) == limit else None}

    async def reminder_status(self, issue_id: int, event_type: str, subscription_id: int) -> dict[str, Any]:
        """Revalidate subscription, Token identity and current reminder facts."""
        if event_type not in REMINDERS or issue_id <= 0 or subscription_id <= 0:
            raise TrailError("invalid_input")
        row = await self.get(f"/issues/{issue_id}/reminder_status", {
            "event_type": event_type, "subscription_id": subscription_id,
        })
        if not isinstance(row, dict) or type(row.get("eligible")) is not bool:
            raise TrailError("invalid_response")
        self.require_project(row.get("project_id"))
        if row.get("user_id") != self.config.service_user_id or row.get("issue_id") != issue_id or row.get("event_type") != event_type:
            raise TrailError("identity_mismatch")
        return row

    async def report(self, kind: str, project_id: int, params: dict[str, Any]) -> dict[str, Any]:
        """Fetch only the fixed CSI report endpoints after resolving the real CSI ID."""
        self.require_project(project_id)
        projects = await self.projects()
        if (project_id != self.config.csi_project_id or not any(
                row.get("id") == project_id and row.get("identifier") == "customer-issues"
                for row in projects)):
            raise TrailError("csi_project_not_allowed")
        if kind not in {"weekly", "say-do"}:
            raise TrailError("invalid_input")
        row = await self.get(f"/reports/csi/{kind}", params, authorized_project=project_id)
        if not isinstance(row, dict):
            raise TrailError("invalid_response")
        for item in row.get("items", []):
            if not isinstance(item, dict) or item.get("project_id") != project_id:
                raise TrailError("project_mismatch")
        # Option catalogs are not needed by an agent and can inflate context.
        return {key: value for key, value in row.items() if key not in {"customers", "assignees"}}


def result_metadata(data: Any) -> dict[str, Any]:
    """Mark query freshness and user-entered text without asserting model safety."""
    return {"status": "ok", "queried_at": datetime.now(timezone.utc).isoformat(),
            "untrusted_text": True, "data": data}
