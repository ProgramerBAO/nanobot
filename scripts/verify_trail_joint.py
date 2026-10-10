"""Opt-in cross-repository harness; invoked by Trail's guarded integration test.

REST and signed webhook traffic are real. Feishu is captured without external
side effects. Fixture credentials remain in environment and never in output.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from aiohttp.test_utils import TestServer
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from nanobot.integrations.trail.client import TrailClient, load_settings
from nanobot.integrations.trail.receiver import create_app
from nanobot.integrations.trail.store import InboxStore


class CaptureSender:
    """Template-only Feishu substitute; does not invoke models or the Feishu API."""

    def __init__(self):
        self.messages = []

    async def send(self, recipient, content, event_id):
        assert recipient == "ou_joint_test"
        assert "project_id=" in content and "issue_id=" in content
        self.messages.append(event_id)
        return f"om_joint_{len(self.messages)}"


def unpack(result):
    """Use SDK structured results, falling back to JSON text for compatibility."""
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


async def main():
    settings = load_settings()
    pid = int(os.environ["JOINT_PROJECT_ID"])
    iid = int(os.environ["JOINT_ISSUE_ID"])
    sender = CaptureSender()
    with tempfile.TemporaryDirectory(prefix="trail-joint-") as directory:
        store = InboxStore(Path(directory) / "inbox.db")
        server = TestServer(create_app(settings, store, TrailClient(settings), sender), host="127.0.0.1")
        await server.start_server()
        print(json.dumps({"receiver_url": str(server.make_url("/")).rstrip("/")}), flush=True)
        try:
            async with asyncio.timeout(60):
                while store.snapshot(time.time())["counts"].get("sent", 0) != 3:
                    await asyncio.sleep(.05)
                # Wait for the six HTTP receipt calls to finish before revoking.
                await asyncio.sleep(.2)
                parameters = StdioServerParameters(command=sys.executable,
                    args=["-m", "nanobot.integrations.trail.mcp_server"], env=dict(os.environ))
                async with stdio_client(parameters) as (reader, writer), ClientSession(reader, writer) as session:
                    await session.initialize()
                    iso = datetime.now(timezone.utc).isocalendar()
                    calls = [
                        ("trail_projects", {}),
                        ("trail_issue", {"project_id": pid, "issue_id": iid}),
                        ("trail_search_issues", {"project_id": pid, "query": "测试", "limit": 10}),
                        ("trail_weekly_report", {"project_id": settings.csi_project_id}),
                        ("trail_say_do", {"project_id": settings.csi_project_id, "year": iso.year, "week": iso.week}),
                    ]
                    for name, args in calls:
                        result = unpack(await session.call_tool(name, args))
                        assert result["status"] == "ok", (name, result.get("code"))
                        assert settings.api_token.get_secret_value() not in json.dumps(result)
                    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                        response = await client.post(settings.base_url + "/__test/revoke",
                            headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()})
                        assert response.status_code == 204
                    denied = unpack(await session.call_tool("trail_issue", {"project_id": pid, "issue_id": iid}))
                    assert denied["status"] == "error" and denied["code"] == "forbidden"
                assert len(sender.messages) == len(set(sender.messages)) == 3
                print(json.dumps({"sent": 3, "tools": 5, "revoked": True}), flush=True)
        finally:
            try:
                await server.close()
            finally:
                # Also release the Windows file lock if protocol cancellation
                # interrupted aiohttp cleanup. close is deliberately idempotent.
                store.close()


if __name__ == "__main__":
    asyncio.run(main())
