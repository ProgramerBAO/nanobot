"""Tests for the catalog-backed, read-only Magik Cube Admin API tool."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.magik_cube import MagikCubeToolConfig
from nanobot.agent.tools.magik_cube_admin import (
    MagikCubeAdminApiTool,
    _load_catalog,
    _sanitize_response,
)

ROOT = Path(__file__).parents[2]
OPENAPI_PATH = ROOT / "run/magik-cube/app/admin/internal/server/openapi.yaml"
DOCS_PATH = ROOT / "docs/magik-cube-admin-api.md"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def test_catalog_covers_every_upstream_admin_operation() -> None:
    source_bytes = OPENAPI_PATH.read_bytes()
    openapi = yaml.safe_load(source_bytes)
    expected = {
        (operation["operationId"], method.upper(), path)
        for path, path_item in openapi["paths"].items()
        for method, operation in path_item.items()
        if method in HTTP_METHODS
    }
    catalog = _load_catalog()
    actual = {
        (operation["operationId"], operation["method"], operation["path"])
        for operation in catalog["operations"]
    }

    assert actual == expected
    assert catalog["operationCount"] == 206
    assert catalog["readOnlyOperationCount"] == 98
    assert catalog["sourceSha256"] == hashlib.sha256(source_bytes).hexdigest()


def test_generated_docs_list_every_operation() -> None:
    docs = DOCS_PATH.read_text(encoding="utf-8")
    for operation in _load_catalog()["operations"]:
        assert f"`{operation['operationId']}`" in docs


async def test_search_finds_read_and_blocked_write_operations() -> None:
    tool = MagikCubeAdminApiTool(MagikCubeToolConfig(api_prefix="/api/admin-manager"))

    read_result = json.loads(
        await tool.execute(action="search", query="钱包", access="read", limit=10)
    )
    write_result = json.loads(
        await tool.execute(action="search", query="CreateRole", access="write", limit=10)
    )

    assert read_result["catalog"] == {"operations": 206, "readOnly": 98}
    assert read_result["results"][0]["operationId"] == "BillingAdminService_ListWallets"
    assert read_result["results"][0]["path"] == "/api/admin-manager/billing/wallets"
    assert write_result["results"][0]["access"] == "blocked-write"


async def test_describe_expands_request_schema() -> None:
    tool = MagikCubeAdminApiTool()

    result = json.loads(
        await tool.execute(
            action="describe",
            operation_id="AnalysisAdminService_QueryTokenOverview",
            include_response_schema=True,
        )
    )

    assert result["access"] == "read"
    assert result["requestSchema"]["type"] == "object"
    assert "start_date" in result["requestSchema"]["properties"]
    assert result["responseSchema"]["type"] == "object"


async def test_call_logs_in_and_invokes_catalogued_get() -> None:
    seen: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "path": request.url.path,
                "query": request.url.query.decode(),
                "authorization": request.headers.get("authorization"),
            }
        )
        if request.url.path == "/token-api/v1/accounts/login/with-password":
            assert json.loads(request.content) == {"account": "readonly", "password": "pw"}
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "runtime"}})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"list": [{"tenantId": "tenant-1"}], "total": 1},
            },
        )

    tool = MagikCubeAdminApiTool(
        MagikCubeToolConfig(
            enable=True,
            base_url="https://cube.example",
            api_prefix="/api/admin-manager",
            account="readonly",
            password="pw",
        ),
        transport=httpx.MockTransport(handler),
    )

    result = json.loads(
        await tool.execute(
            action="call",
            operation_id="UserAdminService_ListTenants",
            query_params={"page_num": 1, "page_size": 20, "is_key_account": True},
        )
    )

    assert result["data"]["total"] == 1
    assert seen == [
        {
            "path": "/token-api/v1/accounts/login/with-password",
            "query": "",
            "authorization": None,
        },
        {
            "path": "/api/admin-manager/tenants",
            "query": "page_num=1&page_size=20&is_key_account=true",
            "authorization": "Bearer runtime",
        },
    ]


async def test_call_supports_catalogued_read_only_post() -> None:
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0, "data": {"totalTokens": 42}})

    tool = MagikCubeAdminApiTool(
        MagikCubeToolConfig(
            base_url="https://cube.example",
            api_prefix="/api/admin-manager",
            access_token="token",
        ),
        transport=httpx.MockTransport(handler),
    )

    result = json.loads(
        await tool.execute(
            action="call",
            operation_id="AnalysisAdminService_QueryTokenOverview",
            body={"tenant_id": "tenant-1"},
        )
    )

    assert result["data"] == {"totalTokens": 42}
    assert seen == {
        "path": "/api/admin-manager/analysis/token-overview/query",
        "body": {"tenant_id": "tenant-1"},
    }


@pytest.mark.parametrize(
    "operation_id",
    [
        "AuthzAdminService_CreateRole",
        "InferenceAdminService_UpdateInferenceEndpoint",
        "BillingAdminService_DeleteSku",
        "AnalysisAdminService_BackfillTenantTokenDailyStats",
    ],
)
async def test_call_blocks_write_operations_before_network(operation_id: str) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("blocked write reached network")

    tool = MagikCubeAdminApiTool(
        MagikCubeToolConfig(base_url="https://cube.example", access_token="token"),
        transport=httpx.MockTransport(handler),
    )

    result = await tool.execute(action="call", operation_id=operation_id, body={})

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert "blocked write operation" in result


async def test_call_validates_path_and_query_parameters_before_network() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid request reached network")

    tool = MagikCubeAdminApiTool(
        MagikCubeToolConfig(base_url="https://cube.example", access_token="token"),
        transport=httpx.MockTransport(handler),
    )

    missing_path = await tool.execute(
        action="call",
        operation_id="ClusterAdminService_GetComputeNode",
        path_params={"cluster_name": "prod"},
    )
    unexpected_query = await tool.execute(
        action="call",
        operation_id="UserAdminService_ListTenants",
        query_params={"unknown": "value"},
    )

    assert missing_path.is_error is True
    assert "missing path parameters: node_name" in missing_path
    assert unexpected_query.is_error is True
    assert "unexpected query parameters: unknown" in unexpected_query


async def test_call_accepts_documented_array_query_parameter() -> None:
    seen_query = ""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_query
        seen_query = request.url.query.decode()
        return httpx.Response(200, json={"code": 0, "data": {"data": []}})

    tool = MagikCubeAdminApiTool(
        MagikCubeToolConfig(base_url="https://cube.example", access_token="token"),
        transport=httpx.MockTransport(handler),
    )

    result = await tool.execute(
        action="call",
        operation_id="InferenceAdminService_QueryInferenceTimeseriesData",
        query_params={
            "token_buckets": [
                "TOKEN_BUCKET_INPUT_0_32K",
                "TOKEN_BUCKET_INPUT_32K_64K",
            ]
        },
    )

    assert not isinstance(result, ToolResult)
    assert seen_query == (
        "token_buckets=TOKEN_BUCKET_INPUT_0_32K&"
        "token_buckets=TOKEN_BUCKET_INPUT_32K_64K"
    )


def test_response_sanitizer_redacts_credentials_without_hiding_usage_tokens() -> None:
    value = {
        "apiKey": "secret-api-key",
        "totalTokens": 123,
        "encoded": '{"authorization":"Bearer secret","prompt_tokens":42}',
    }

    assert _sanitize_response(value) == {
        "apiKey": "[REDACTED]",
        "totalTokens": 123,
        "encoded": {"authorization": "[REDACTED]", "prompt_tokens": 42},
    }
