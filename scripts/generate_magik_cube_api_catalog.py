#!/usr/bin/env python3
"""Generate nanobot's Magik Cube Admin API catalog from the upstream OpenAPI file."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
READ_OPERATION_PREFIXES = ("Get", "List", "Query")


def _is_read_only(method: str, operation_id: str) -> bool:
    action = operation_id.rsplit("_", 1)[-1]
    return method == "get" or action.startswith(READ_OPERATION_PREFIXES)


def _catalog(source: Path) -> dict[str, Any]:
    source_bytes = source.read_bytes()
    document = yaml.safe_load(source_bytes)
    operations: list[dict[str, Any]] = []
    for path, path_item in document.get("paths", {}).items():
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses", {})
            success = responses.get("200", {}).get("content", {}).get("application/json", {})
            request = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
            )
            operation_id = operation.get("operationId", "")
            operations.append(
                {
                    "operationId": operation_id,
                    "service": (operation.get("tags") or [""])[0],
                    "method": method.upper(),
                    "path": path,
                    "summary": operation.get("summary", ""),
                    "description": operation.get("description", ""),
                    "readOnly": _is_read_only(method, operation_id),
                    "parameters": operation.get("parameters", []),
                    "requestRequired": bool(operation.get("requestBody", {}).get("required")),
                    "requestSchema": request.get("schema", {}),
                    "responseSchema": success.get("schema", {}),
                }
            )
    operations.sort(key=lambda item: (item["service"], item["path"], item["method"]))
    return {
        "source": str(source.as_posix()),
        "sourceSha256": hashlib.sha256(source_bytes).hexdigest(),
        "operationCount": len(operations),
        "readOnlyOperationCount": sum(item["readOnly"] for item in operations),
        "operations": operations,
        "schemas": document.get("components", {}).get("schemas", {}),
    }


def _write_docs(catalog: dict[str, Any], destination: Path) -> None:
    operations = catalog["operations"]
    counts = Counter(item["service"] for item in operations)
    read_counts = Counter(item["service"] for item in operations if item["readOnly"])
    lines = [
        "# Magik Cube Admin API 接口目录",
        "",
        "> 此文件由 `scripts/generate_magik_cube_api_catalog.py` 从 Magik Cube Admin OpenAPI",
        "> 自动生成。它只总结接口，不会修改 `run/magik-cube`。",
        "",
        f"共 **{catalog['operationCount']}** 个操作，其中 **{catalog['readOnlyOperationCount']}** 个可由",
        "`magik_cube_admin_api` 只读调用；其余写操作只展示在目录中，工具会在发出网络请求前阻止。",
        "",
        "只读判定规则：HTTP `GET`，或 RPC 操作名以 `Get`、`List`、`Query` 开头。",
        "登录接口仅用于取得临时 Bearer Token，不计入 Admin API 操作数。",
        "",
        "## 模块汇总",
        "",
        "| 模块 | 全部 | 可调用只读 |",
        "| --- | ---: | ---: |",
    ]
    for service in sorted(counts):
        lines.append(f"| {service} | {counts[service]} | {read_counts[service]} |")
    for service in sorted(counts):
        lines.extend(
            [
                "",
                f"## {service}",
                "",
                "| 权限 | 方法 | 生产网关路径 | Operation ID | 说明 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in operations:
            if item["service"] != service:
                continue
            access = "只读" if item["readOnly"] else "禁止调用（写）"
            public_path = item["path"].replace("/api/v1", "/api/admin-manager", 1)
            summary = (item["summary"] or item["description"] or "—").replace("|", "\\|")
            lines.append(
                f"| {access} | {item['method']} | `{public_path}` | "
                f"`{item['operationId']}` | {summary} |"
            )
    lines.extend(
        [
            "",
            "## nanobot 使用方式",
            "",
            "生产环境配置使用 `https://www.magikcloud.cn`（裸域会 301 跳转；工具为防止凭据",
            "被重定向而不会跟随该跳转）：",
            "",
            "```json",
            "{",
            '  "tools": {',
            '    "magikCube": {',
            '      "enable": true,',
            '      "baseUrl": "https://www.magikcloud.cn",',
            '      "apiPrefix": "/api/admin-manager",',
            '      "account": "${MAGIK_CUBE_ACCOUNT}",',
            '      "password": "${MAGIK_CUBE_PASSWORD}"',
            "    }",
            "  }",
            "}",
            "```",
            "",
            "账号密码只应通过环境变量提供，不要写入仓库。工具登录后只在当前请求的内存中",
            "保存临时 Token。",
            "",
            "1. `action=search`：按模块、路径、Operation ID 或中文说明检索接口。",
            "2. `action=describe`：查看路径参数、查询参数、请求体和响应 Schema。",
            "3. `action=call`：按 Operation ID 调用；只能调用目录中标记为只读的操作。",
            "",
        ]
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("run/magik-cube/app/admin/internal/server/openapi.yaml"),
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("nanobot/agent/tools/magik_cube_admin_api.json"),
    )
    parser.add_argument(
        "--docs",
        type=Path,
        default=Path("docs/magik-cube-admin-api.md"),
    )
    args = parser.parse_args()
    catalog = _catalog(args.source)
    args.catalog.parent.mkdir(parents=True, exist_ok=True)
    args.catalog.write_text(
        json.dumps(catalog, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _write_docs(catalog, args.docs)


if __name__ == "__main__":
    main()
