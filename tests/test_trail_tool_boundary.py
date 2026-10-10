"""A restricted profile must never register powerful unrelated agent tools."""

from nanobot.agent.tools.registry import ToolRegistry


def test_profile_allowlist_cannot_register_shell_or_plugins():
    """Tool registry is the execution boundary, independent of prompt obedience."""
    registry = ToolRegistry(allowed_names=["mcp_trail_trail_issue"])

    class Dummy:
        name = "exec"

    registry.register(Dummy())
    assert not registry.has("exec")
    Dummy.name = "mcp_trail_trail_issue"
    registry.register(Dummy())
    assert registry.has("mcp_trail_trail_issue")


async def test_excluded_tool_cannot_execute_after_registration_attempt():
    """Prompt injection cannot invoke an unregistered side-effect tool."""
    registry = ToolRegistry(allowed_names=[])

    class Dummy:
        name = "exec"

        async def execute(self, **kwargs):
            raise AssertionError("side effect must never execute")

    registry.register(Dummy())
    result = await registry.execute("exec", {"command": "read credential"})
    assert result.is_error


def test_default_registry_preserves_existing_behavior():
    registry = ToolRegistry()

    class Dummy:
        name = "exec"

    registry.register(Dummy())
    assert registry.has("exec")
