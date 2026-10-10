# Trail 验证范围

命令、依赖与故障分类见 [TRAIL_INTEGRATION](TRAIL_INTEGRATION.md)。新增 suite 使用 SQLite、aiohttp test server、真实 stdio MCP、真实 Feishu SDK 请求构造+替身 response，无真实外部写入。覆盖验签/重放/容量/重复/重启/unknown/取消/权限/返回裁剪/模板/工具注册allowlist/线程清理锁。

扩大回归：registry、tests/config、skills loader、MCP连接/瞬态重试、loop tool context/runner。已知原始基线 HTTP MCP shutdown reconnect 用例失败单独记录，不删除断言或称全量绿色。跨仓库 TestNanobotJointHTTP 使用真实 Trail REST/PG，脚本不可直接对业务环境运行。

格式遵守 AGENTS.md：不使用 ruff format；scoped ruff check。Python compileall 与 wheel 构建分别验证语法与打包；已有 NANOBOT_SKIP_WEBUI_BUILD=1 表示未验证 WebUI 构建。

真实飞书、模型调用/prompt injection、团队权限隔离、生产部署和回滚不在替身证据内；单人试点启用前必须补齐真实验收。
