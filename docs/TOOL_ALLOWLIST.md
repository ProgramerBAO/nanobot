# 精确工具注册白名单

`tools.allowedTools` 为可选 exact 工具名数组，默认 null 沿用现有行为，[] 拒绝所有工具；最多256个唯一名称，不能使用通配符。AgentLoop 将其传入 ToolRegistry，未列入的 built-in、plugin、MCP 工具不注册，也不能通过 execute 调用；MCP 重连沿用同一白名单。配置变更需要重启该 profile。

此设置只限制模型可调用工具，不提供逐用户身份代理。使用独立 workspace/profile、明确渠道 allowFrom、普通服务账号；共享管理员Token不能靠白名单变安全。用于Trail的实际五个包装名与受控环境配置见后续集成文档。

回归入口：`.venv\Scripts\python.exe -m pytest tests/test_trail_tool_boundary.py -q`；覆盖排除工具执行、动态注册及默认兼容。不是真实模型prompt injection测试。
