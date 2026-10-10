"""Opt-in Trail webhook receiver and deterministic Feishu reminder worker.

Run separately from the agent: no LLM dependency, shared conversation memory,
or automatic business writes. Only a persisted inbox receipt returns HTTP 202.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from aiohttp import web

from nanobot.config.schema import TrailIntegrationConfig
from nanobot.integrations.trail.client import REMINDERS, TrailClient, TrailError, load_settings
from nanobot.integrations.trail.store import InboxFullError, InboxStore

log = logging.getLogger(__name__)
REASONS = {"issue.due_soon": "即将到期", "issue.overdue": "首次承诺已逾期", "issue.update_overdue": "约定更新已逾期"}
STATE = web.AppKey("trail_state", dict)


class SendError(Exception):
    """Classify explicit rejection versus potentially completed remote send."""

    def __init__(self, code: str, *, retryable: bool = False, unknown: bool = False):
        super().__init__(code)
        self.code, self.retryable, self.unknown = code, retryable, unknown


class ReminderSender(Protocol):
    """Send one template and return its remote message ID; never enqueue silently."""

    async def send(self, recipient: str, content: str, event_id: str) -> str: ...


class FeishuSender:
    """Reuse configured Feishu application credentials and the existing SDK.

    No internal retry: an explicit 429 may retry through the durable worker;
    timeout/transport/server uncertainty requires manual reconciliation.
    """

    def __init__(self, config):
        import lark_oapi as lark
        from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN

        if not config.enabled or not config.app_id or not config.app_secret:
            raise ValueError("selected Feishu instance must be enabled and configured")
        self._client = (lark.Client.builder().app_id(config.app_id).app_secret(config.app_secret)
                        .domain(LARK_DOMAIN if config.domain == "lark" else FEISHU_DOMAIN)
                        .timeout(10).build())

    async def send(self, recipient: str, content: str, event_id: str) -> str:
        """Send a single bounded text message with a deterministic provider UUID."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        request = (CreateMessageRequest.builder().receive_id_type("open_id").request_body(
            CreateMessageRequestBody.builder().receive_id(recipient).msg_type("text")
            .content(json.dumps({"text": content}, ensure_ascii=False))
            .uuid(hashlib.sha256(f"{event_id}:{recipient}".encode()).hexdigest()[:32]).build()
        ).build())
        try:
            async with asyncio.timeout(20):
                response = await self._client.im.v1.message.acreate(request)
        except Exception as exc:
            raise SendError("send_result_unknown", unknown=True) from exc
        status = getattr(getattr(response, "raw", None), "status_code", None)
        if status == 429:
            raise SendError("rate_limited", retryable=True)
        if status is None or status >= 500:
            raise SendError("send_result_unknown", unknown=True)
        if not response.success():
            raise SendError("send_rejected")
        message_id = getattr(getattr(response, "data", None), "message_id", None)
        if not message_id:
            raise SendError("missing_message_id", unknown=True)
        return str(message_id)


def validate_event(body: bytes, config: TrailIntegrationConfig, now: float) -> dict[str, Any]:
    """Strict minimum projection and explicit identity/project binding.

    Fresh request signatures cannot revive events older than 24 hours. UTC
    timestamps are required; user text is not accepted in the event payload.
    """
    try:
        env = json.loads(body)
        if not isinstance(env, dict) or set(env) != {"event_id", "type", "aggregate_id", "entity_version", "occurred_at", "payload"}:
            raise ValueError()
        if not isinstance(env["event_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", env["event_id"]):
            raise ValueError()
        if env["type"] not in REMINDERS:
            raise ValueError()
        for field in ("aggregate_id", "entity_version"):
            if type(env[field]) is not int or not 0 < env[field] <= 2**63 - 1:
                raise ValueError()
        occurred = datetime.fromisoformat(env["occurred_at"].replace("Z", "+00:00"))
        if occurred.tzinfo is None or occurred.utcoffset().total_seconds() != 0 or not -300 <= now - occurred.timestamp() <= 86400:
            raise ValueError()
        payload = env["payload"]
        if not isinstance(payload, dict) or set(payload) != {"schema", "issue_id", "project_id", "service_user_id", "subscription_id"}:
            raise ValueError()
        if payload["schema"] != "trail_reminder_v1":
            raise ValueError()
        for field in ("issue_id", "project_id", "service_user_id", "subscription_id"):
            if type(payload[field]) is not int or not 0 < payload[field] <= 2**63 - 1:
                raise ValueError()
        if (payload["issue_id"] != env["aggregate_id"] or payload["service_user_id"] != config.service_user_id
                or payload["project_id"] not in config.project_ids):
            raise ValueError()
        return env
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise TrailError("invalid_event") from exc


def render_template(event: dict[str, Any], issue: dict[str, Any], evaluated_at: str) -> str:
    """Plain text only: no model execution or interpretation of issue titles."""
    subject = " ".join(str(issue.get("subject", "")).split())[:300]
    event_type = event["type"]
    field = {"issue.due_soon": "due_date", "issue.overdue": "original_due_at", "issue.update_overdue": "next_update_at"}[event_type]
    return (f"Trail · {REASONS[event_type]}\n{issue.get('number', '')} {subject}\n"
            f"约定时间：{issue.get(field) or '未提供'}\n核验时间：{evaluated_at}\n"
            f"{issue['url']}\n可在私聊中询问此工单的最新情况与跟进建议。")


async def process_once(store: InboxStore, client: TrailClient, sender: ReminderSender, *, now_fn=time.time, jitter_fn=random.uniform) -> bool:
    """One job with bounded check retries and conservative send uncertainty.

    Persist 'sending' before the external side effect. Cancellation after that
    point becomes unknown; startup never silently replays it.
    """
    now = now_fn()
    job = await asyncio.to_thread(store.claim, now)
    if job is None:
        return False
    env, payload = job["envelope"], job["envelope"]["payload"]
    try:
        if now - datetime.fromisoformat(env["occurred_at"].replace("Z", "+00:00")).timestamp() > 86400:
            await asyncio.to_thread(store.finish, job, "cancelled", "event_expired")
            return True
        # Fetch metadata first, then check current authority immediately before send.
        issue = await client.issue(payload["issue_id"], payload["project_id"])
        current = await client.reminder_status(payload["issue_id"], env["type"], payload["subscription_id"])
        if current["project_id"] != payload["project_id"]:
            raise TrailError("project_mismatch")
        if not current["eligible"]:
            await asyncio.to_thread(store.finish, job, "cancelled", "condition_no_longer_valid")
            return True
        text = render_template(env, issue, current["evaluated_at"])
        await asyncio.to_thread(store.finish, job, "sending")
        try:
            message_id = await sender.send(job["recipient"], text, env["event_id"])
        except asyncio.CancelledError:
            await asyncio.to_thread(store.finish, job, "unknown", "shutdown_during_send")
            raise
        except Exception as exc:
            if not isinstance(exc, SendError):
                exc = SendError("send_result_unknown", unknown=True)
            if exc.unknown:
                await asyncio.to_thread(store.finish, job, "unknown", exc.code)
            elif exc.retryable and job["attempts"] < 3:
                await asyncio.to_thread(store.finish, job, "received", exc.code,
                                        next_retry_at=now + min(120, 60 * 2**(job["attempts"] - 1)) * jitter_fn(.8, 1.2))
            else:
                await asyncio.to_thread(store.finish, job, "failed", exc.code)
            return True
        await asyncio.to_thread(store.finish, job, "sent", message_id=message_id)
        log.info("trail_reminder_sent", extra={"event_id": env["event_id"]})
    except TrailError as exc:
        if exc.retryable and job["attempts"] < 3:
            await asyncio.to_thread(store.finish, job, "received", exc.code,
                                    next_retry_at=now + min(120, 60 * 2**(job["attempts"] - 1)) * jitter_fn(.8, 1.2))
        else:
            await asyncio.to_thread(store.finish, job, "cancelled" if exc.code in {"forbidden", "unauthorized", "not_found", "identity_mismatch"} else "failed", exc.code)
        log.warning("trail_reminder_check_failed", extra={"event_id": env["event_id"], "outcome": exc.code})
    return True


def create_app(config: TrailIntegrationConfig, store: InboxStore, client: TrailClient, sender: ReminderSender, *, start_worker: bool = True, now_fn=time.time) -> web.Application:
    """Build a bounded signed receiver; all collaborators are injectable for tests."""
    if len(config.webhook_secret.get_secret_value()) < 16 or not re.fullmatch(r"ou_[A-Za-z0-9_-]{1,100}", config.feishu_open_id):
        raise ValueError("receiver requires a strong webhook secret and explicit Feishu open_id")
    app = web.Application(client_max_size=32 * 1024)
    app[STATE] = {"worker_failed": False, "counts": {"accepted": 0, "duplicate": 0, "signature_rejected": 0, "invalid_event": 0}}

    async def receive(request: web.Request) -> web.Response:
        body = await request.read()
        ts = request.headers.get("X-Trail-Timestamp", "")
        signature = request.headers.get("X-Trail-Signature", "")
        now = now_fn()
        valid_time = ts.isdigit() and len(ts) <= 12 and abs(now - int(ts)) <= 300
        expected = "sha256=" + hmac.new(config.webhook_secret.get_secret_value().encode(),
            b"POST\n" + request.path.encode() + b"\n" + ts.encode() + b"\n" + body, hashlib.sha256).hexdigest()
        if not valid_time or not hmac.compare_digest(expected, signature):
            app[STATE]["counts"]["signature_rejected"] += 1
            return web.json_response({"code": "invalid_signature"}, status=401)
        try:
            event = validate_event(body, config, now)
            if request.headers.get("X-Trail-Event-ID") != event["event_id"] or request.headers.get("X-Trail-Event-Type") != event["type"]:
                raise TrailError("invalid_event")
            inserted = await asyncio.to_thread(store.receive, event, config.feishu_open_id, now)
        except TrailError:
            app[STATE]["counts"]["invalid_event"] += 1
            return web.json_response({"code": "invalid_event"}, status=422)
        except (InboxFullError, sqlite3.Error):
            return web.json_response({"code": "receipt_unavailable"}, status=503)
        app[STATE]["counts"]["accepted" if inserted else "duplicate"] += 1
        return web.json_response({"status": "accepted" if inserted else "duplicate"}, status=202)

    async def health(request: web.Request) -> web.Response:
        state = app[STATE]
        try:
            snapshot = await asyncio.to_thread(store.snapshot, now_fn())
        except sqlite3.Error:
            return web.json_response({"status": "not_ready"}, status=503)
        ready = not state["worker_failed"] and not snapshot["counts"].get("unknown", 0) and snapshot["oldest_pending_seconds"] < 300
        return web.json_response({"status": "ready" if ready else "degraded", "queue": snapshot, "requests": state["counts"]}, status=200 if ready else 503)

    async def worker():
        try:
            while True:
                if not await process_once(store, client, sender, now_fn=now_fn):
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            app[STATE]["worker_failed"] = True
            log.error("trail_reminder_worker_failed")

    async def metrics(request: web.Request) -> web.Response:
        """Expose bounded operational labels; never label events or recipients."""
        try:
            snapshot = await asyncio.to_thread(store.snapshot, now_fn())
        except sqlite3.Error:
            return web.Response(status=503, text="inbox unavailable\n")
        lines = ["# TYPE trail_inbox_jobs gauge"]
        for status in ("received", "checking", "sending", "sent", "cancelled", "failed", "unknown"):
            lines.append(f'trail_inbox_jobs{{status="{status}"}} {snapshot["counts"].get(status, 0)}')
        lines.extend(["# TYPE trail_inbox_oldest_pending_seconds gauge",
                      f'trail_inbox_oldest_pending_seconds {snapshot["oldest_pending_seconds"]}',
                      "# TYPE trail_receiver_requests_total counter"])
        for outcome, count in app[STATE]["counts"].items():
            lines.append(f'trail_receiver_requests_total{{outcome="{outcome}"}} {count}')
        return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")

    async def lifecycle(application: web.Application):
        task = asyncio.create_task(worker()) if start_worker else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await client.close()
            await asyncio.to_thread(store.close)

    app.cleanup_ctx.append(lifecycle)
    app.router.add_post("/integrations/trail/events", receive)
    app.router.add_get("/healthz", health)
    app.router.add_get("/metrics", metrics)
    return app


def main() -> None:
    """Start loopback-only receiver using an explicitly selected nanobot profile."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Dedicated nanobot config path; never auto-modified")
    parser.add_argument("--port", type=int, default=8091, help="Loopback receiver port (default 8091)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    try:
        from nanobot.channels.feishu.config import FeishuConfig
        from nanobot.channels.feishu.instances import managed_feishu_instance_specs
        from nanobot.config.loader import load_config, resolve_config_env_vars

        settings = load_settings()
        if not args.config.is_file():
            raise ValueError("dedicated profile must exist")
        config = resolve_config_env_vars(load_config(args.config))
        configure = config.channels.model_dump().get("feishu", {})
        specs = managed_feishu_instance_specs(configure)
        selected = next((item for item in specs if item.instance_id == settings.feishu_instance_id), None)
        if selected is None:
            raise ValueError("selected Feishu instance not found")
        feishu = FeishuConfig.model_validate(selected.config)
        if feishu.allow_from != [settings.feishu_open_id]:
            raise ValueError("pilot profile must allow exactly the bound recipient")
        if not settings.state_db or not Path(settings.state_db).is_absolute():
            raise ValueError("TRAIL_STATE_DB must be an absolute path")
        sender = FeishuSender(feishu)
        store = InboxStore(Path(settings.state_db))
        app = create_app(settings, store, TrailClient(settings), sender)
    except (ValueError, TypeError, OSError):
        parser.exit(2, "Invalid Trail receiver configuration; check integration runbook (inputs omitted)\n")
    # Keep diagnostic fields visible without serializing arbitrary log records,
    # exceptions, HTTP bodies or credentials from third-party SDKs.
    class SafeFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            item = {"service": "trail-receiver", "level": record.levelname,
                    "time": datetime.now(timezone.utc).isoformat(), "operation": record.getMessage()}
            for key in ("event_id", "outcome", "duration_seconds"):
                if hasattr(record, key):
                    item[key] = getattr(record, key)
            return json.dumps(item)

    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter())
    integration_log = logging.getLogger("nanobot.integrations.trail")
    integration_log.addHandler(handler)
    integration_log.setLevel(logging.INFO)
    integration_log.propagate = False
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None, shutdown_timeout=25)


if __name__ == "__main__":
    main()
