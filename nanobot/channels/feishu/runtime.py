"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from nanobot.bus.events import (
    INBOUND_META_DIRECT_TOOL,
    OUTBOUND_META_AGENT_UI,
    OutboundMessage,
)
from nanobot.bus.outbound_events import ProgressEvent
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.contracts import ChannelInstanceSpec
from nanobot.channels.feishu.config import FeishuConfig, feishu_default_config
from nanobot.channels.feishu.instances import (
    DEFAULT_INSTANCE_ID,
    feishu_app_identity_key,
    feishu_instance_specs,
    runtime_channel_name,
    update_feishu_instance_preserving_shape,
    upsert_feishu_instance,
)
from nanobot.channels.feishu.websocket import get_feishu_ws_runner
from nanobot.command.router import normalize_command_text
from nanobot.config.paths import get_media_dir
from nanobot.pairing import clear_channel
from nanobot.runtime_context import (
    RUNTIME_CONTEXT_INPUT_META,
    RuntimeContextBlock,
    wrap_runtime_context_lines,
)
from nanobot.utils.helpers import safe_filename
from nanobot.utils.logging_bridge import redirect_lib_logging

if TYPE_CHECKING:
    from lark_oapi.api.im.v1.model import MentionEvent, P2ImMessageReceiveV1

FEISHU_AVAILABLE = importlib.util.find_spec("lark_oapi") is not None
_LOGIN_CONSOLE = Console()
_LARK_RUNTIME_LOCK = threading.Lock()


def _is_group_history_summary_request(text: str) -> bool:
    """Recognize group-history requests that need an immediate visible acknowledgement."""
    compact = re.sub(r"\s+", "", text or "")
    scope_terms = ("群里", "群聊", "群消息", "聊天记录", "历史消息", "全群")
    action_terms = ("总结", "汇总", "梳理", "回顾", "统计", "有哪些", "查看", "看看", "分析")
    return any(term in compact for term in scope_terms) and any(
        term in compact for term in action_terms
    )


def _identity_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_lark_runtime() -> tuple[Any, str, str]:
    """Import the heavy Feishu SDK lazily.

    lark_oapi imports a large generated API surface at module import time, so
    keep it out of channel discovery and constructor paths.
    """
    import sys

    # The SDK creates a module-global event loop while importing its WebSocket
    # client. Multiple Feishu instances start concurrently, so serialize this
    # one-time import and cleanup rather than allowing two worker threads to
    # close the same loop.
    with _LARK_RUNTIME_LOCK:
        ws_client_already_imported = "lark_oapi.ws.client" in sys.modules
        import lark_oapi as lark
        import lark_oapi.ws.client as lark_ws_client
        from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN

        if (
            not ws_client_already_imported
            and threading.current_thread() is not threading.main_thread()
        ):
            import_loop = getattr(lark_ws_client, "loop", None)
            if (
                import_loop is not None
                and not import_loop.is_running()
                and not import_loop.is_closed()
            ):
                import_loop.close()
            lark_ws_client.loop = None
            with suppress(Exception):
                asyncio.set_event_loop(None)

        return lark, FEISHU_DOMAIN, LARK_DOMAIN


def fetch_feishu_app_identity(
    app_id: str,
    app_secret: str,
    domain: str = "feishu",
) -> dict[str, str]:
    """Fetch the user-facing Feishu/Lark app identity for display.

    This is best-effort metadata for WebUI presentation.  Callers should treat
    an empty result as a normal fallback path.
    """
    if not FEISHU_AVAILABLE or not app_id or not app_secret:
        return {}

    try:
        lark, feishu_domain, lark_domain = _load_lark_runtime()
        from lark_oapi.api.application.v6.model.get_application_request import (
            GetApplicationRequest,
        )

        sdk_domain = lark_domain if domain == "lark" else feishu_domain
        client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .domain(sdk_domain)
            .timeout(5)
            .build()
        )
        request = GetApplicationRequest.builder().app_id(app_id).lang("zh_cn").build()
        response = client.application.v6.application.get(request)
        if hasattr(response, "success") and not response.success():
            return {}

        app = getattr(getattr(response, "data", None), "app", None)
        if app is None:
            return {}

        identity: dict[str, str] = {}
        display_name = str(getattr(app, "app_name", "") or "").strip()
        avatar_url = str(getattr(app, "avatar_url", "") or "").strip()
        if display_name:
            identity["displayName"] = display_name
        if avatar_url:
            identity["avatarUrl"] = avatar_url
        if identity:
            identity["identityFetchedAt"] = _identity_timestamp()
        return identity
    except Exception:
        return {}


# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


def _extract_share_card_content(content_json: dict, msg_type: str) -> str:
    """Extract text representation from share cards and interactive messages."""
    parts = []

    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")

    return "\n".join(parts) if parts else f"[{msg_type}]"


def _extract_interactive_content(content: dict) -> list[str]:
    """Recursively extract text and links from interactive card content."""
    parts = []

    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []

    if not isinstance(content, dict):
        return parts

    # user_dsl: original card definition (richest source for rendered cards)
    user_dsl = content.get("user_dsl")
    if isinstance(user_dsl, str) and user_dsl.strip():
        try:
            dsl = json.loads(user_dsl)
            if isinstance(dsl, dict):
                parts.extend(_extract_interactive_content(dsl))
                if parts:
                    return parts
        except (json.JSONDecodeError, TypeError):
            pass

    if "title" in content:
        title = content["title"]
        if isinstance(title, dict):
            title_content = title.get("content", "") or title.get("text", "")
            if title_content:
                parts.append(f"title: {title_content}")
        elif isinstance(title, str):
            parts.append(f"title: {title}")

    # Top-level elements: flat list or nested list format
    elements = content.get("elements")
    if isinstance(elements, list):
        if elements and isinstance(elements[0], list):
            # Nested list: [[{tag:"text",text:"..."}], ...]
            for row in elements:
                if isinstance(row, list):
                    for element in row:
                        parts.extend(_extract_element_content(element))
        else:
            # Flat list: [{tag:"markdown",content:"..."}, ...]
            for element in elements:
                parts.extend(_extract_element_content(element))

    # Body elements (schema 2.0)
    body = content.get("body", {})
    if isinstance(body, dict):
        body_elements = body.get("elements")
        if isinstance(body_elements, list):
            for element in body_elements:
                parts.extend(_extract_element_content(element))

    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))

    header = content.get("header", {})
    if header:
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")

    return parts


def _extract_element_content(element: dict) -> list[str]:
    """Extract content from a single card element."""
    parts = []

    if not isinstance(element, dict):
        return parts

    tag = element.get("tag", "")

    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)

    elif tag == "text":
        text = element.get("text", "")
        if isinstance(text, str) and text.strip():
            parts.append(text)

    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str):
            parts.append(text)
        for field in element.get("fields", []):
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    c = field_text.get("content", "")
                    if c:
                        parts.append(c)

    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)

    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            c = text.get("content", "")
            if c:
                parts.append(c)
        url = element.get("url", "") or element.get("multi_url", {}).get("url", "")
        if url:
            parts.append(f"link: {url}")

    elif tag == "img":
        alt = element.get("alt", {})
        parts.append(alt.get("content", "[image]") if isinstance(alt, dict) else "[image]")

    elif tag == "note":
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    elif tag == "column_set":
        for col in element.get("columns", []):
            for ce in col.get("elements", []):
                parts.extend(_extract_element_content(ce))

    elif tag == "plain_text":
        content = element.get("content", "")
        if content:
            parts.append(content)

    elif tag == "table":
        columns = [
            (column["name"], str(column.get("display_name") or column["name"]))
            for column in (element.get("columns") or [])
            if isinstance(column, dict) and column.get("name")
        ]
        rows = element.get("rows", [])
        if columns:
            parts.append(" | ".join(header for _, header in columns))
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                values = []
                for name, _ in columns:
                    value = row.get(name)
                    if isinstance(value, list):
                        value = " ".join(str(item).strip() for item in value if item is not None)
                    values.append("" if value is None else str(value).strip())
                row_text = " | ".join(values).strip()
                if row_text:
                    parts.append(row_text)

    else:
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    return parts


def _extract_post_content(content_json: dict) -> tuple[str, list[str]]:
    """Extract text and image keys from Feishu post (rich text) message.

    Handles three payload shapes:
    - Direct:    {"title": "...", "content": [[...]]}
    - Localized: {"zh_cn": {"title": "...", "content": [...]}}
    - Wrapped:   {"post": {"zh_cn": {"title": "...", "content": [...]}}}
    """

    def _parse_block(block: dict) -> tuple[str | None, list[str]]:
        if not isinstance(block, dict) or not isinstance(block.get("content"), list):
            return None, []
        texts, images = [], []
        if title := block.get("title"):
            texts.append(title)
        for row in block["content"]:
            if not isinstance(row, list):
                continue
            for el in row:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    texts.append(el.get("text", ""))
                elif tag == "at":
                    texts.append(f"@{el.get('user_name', 'user')}")
                elif tag == "code_block":
                    lang = el.get("language", "")
                    code_text = el.get("text", "")
                    texts.append(f"\n```{lang}\n{code_text}\n```\n")
                elif tag == "img" and (key := el.get("image_key")):
                    images.append(key)
        return (" ".join(texts).strip() or None), images

    # Unwrap optional {"post": ...} envelope
    root = content_json
    if isinstance(root, dict) and isinstance(root.get("post"), dict):
        root = root["post"]
    if not isinstance(root, dict):
        return "", []

    # Direct format
    if "content" in root:
        text, imgs = _parse_block(root)
        if text or imgs:
            return text or "", imgs

    # Localized: prefer known locales, then fall back to any dict child
    for key in ("zh_cn", "en_us", "ja_jp"):
        if key in root:
            text, imgs = _parse_block(root[key])
            if text or imgs:
                return text or "", imgs
    for val in root.values():
        if isinstance(val, dict):
            text, imgs = _parse_block(val)
            if text or imgs:
                return text or "", imgs

    return "", []


def _extract_post_text(content_json: dict) -> str:
    """Extract plain text from Feishu post (rich text) message content.

    Legacy wrapper for _extract_post_content, returns only text.
    """
    text, _ = _extract_post_content(content_json)
    return text


# =============================================================================
# QR scan-to-create onboarding
#
# Device-code flow: user scans a QR code with the Feishu/Lark mobile app and
# the platform creates a fully configured bot application automatically.
# =============================================================================

_ONBOARD_ACCOUNTS_URLS = {
    "feishu": "https://accounts.feishu.cn",
    "lark": "https://accounts.larksuite.com",
}
_REGISTRATION_PATH = "/oauth/v1/app/registration"
_ONBOARD_REQUEST_TIMEOUT_S = 10


def _accounts_base_url(domain: str) -> str:
    return _ONBOARD_ACCOUNTS_URLS.get(domain, _ONBOARD_ACCOUNTS_URLS["feishu"])


def _post_registration(base_url: str, body: dict[str, str]) -> dict:
    """POST form-encoded data to the registration endpoint, return parsed JSON.

    The registration endpoint returns JSON even on HTTP errors (e.g. poll
    returns authorization_pending as a 400). We always parse the body.
    """
    import httpx

    url = f"{base_url}{_REGISTRATION_PATH}"
    resp = httpx.post(
        url,
        data=body,
        timeout=_ONBOARD_REQUEST_TIMEOUT_S,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        return resp.json()
    except json.JSONDecodeError:
        resp.raise_for_status()
        return {}


def _init_registration(domain: str = "feishu") -> None:
    """Verify the environment supports client_secret auth. Raises RuntimeError if not."""
    base_url = _accounts_base_url(domain)
    res = _post_registration(base_url, {"action": "init"})
    methods = res.get("supported_auth_methods") or []
    if "client_secret" not in methods:
        raise RuntimeError(
            f"Feishu / Lark registration does not support client_secret auth. "
            f"Supported: {methods}"
        )


def _begin_registration(domain: str = "feishu") -> dict:
    """Start the device-code flow. Returns device_code, qr_url, interval, expire_in."""
    base_url = _accounts_base_url(domain)
    res = _post_registration(base_url, {
        "action": "begin",
        "archetype": "PersonalAgent",
        "auth_method": "client_secret",
        "request_user_info": "open_id",
    })
    device_code = res.get("device_code")
    if not device_code:
        raise RuntimeError("Feishu / Lark registration did not return a device_code")
    qr_url = res.get("verification_uri_complete", "")
    if not qr_url:
        raise RuntimeError("Feishu / Lark registration did not return a login URL")
    return {
        "device_code": device_code,
        "qr_url": qr_url,
        "interval": res.get("interval") or 5,
        "expire_in": res.get("expire_in") or 600,
    }


def _poll_registration(
    *,
    device_code: str,
    interval: int,
    expire_in: int,
    domain: str = "feishu",
) -> dict | None:
    """Poll until the user scans the QR code, or timeout/denial.

    Returns dict with app_id, app_secret, domain on success, None on failure.
    """
    deadline = time.monotonic() + expire_in
    current_domain = domain

    while time.monotonic() < deadline:
        try:
            res = poll_registration_once(device_code=device_code, domain=current_domain)
        except Exception:
            time.sleep(interval)
            continue

        current_domain = res.get("domain", current_domain)

        if res.get("status") == "succeeded":
            return {
                "app_id": res["app_id"],
                "app_secret": res["app_secret"],
                "domain": res.get("domain", current_domain),
            }

        if res.get("status") == "failed":
            _LOGIN_CONSOLE.print("[yellow]Authorization was cancelled or expired.[/yellow]")
            return None

        # authorization_pending or unknown — keep polling
        time.sleep(interval)

    _LOGIN_CONSOLE.print("[yellow]Authorization timed out.[/yellow]")
    return None


def poll_registration_once(
    *,
    device_code: str,
    domain: str = "feishu",
) -> dict:
    """Poll the Feishu/Lark device-code flow once.

    This non-blocking shape is used by WebUI. The CLI keeps using
    ``_poll_registration`` to wait in the terminal.
    """
    current_domain = domain
    base_url = _accounts_base_url(current_domain)
    res = _post_registration(base_url, {
        "action": "poll",
        "device_code": device_code,
        "tp": "ob_app",
    })

    user_info = res.get("user_info") or {}
    tenant_brand = user_info.get("tenant_brand")
    if tenant_brand == "lark":
        current_domain = "lark"

    if res.get("client_id") and res.get("client_secret"):
        return {
            "status": "succeeded",
            "app_id": res["client_id"],
            "app_secret": res["client_secret"],
            "domain": current_domain,
        }

    error = res.get("error", "")
    if error in ("access_denied", "expired_token"):
        return {
            "status": "failed",
            "error": error,
            "domain": current_domain,
        }

    return {
        "status": "pending",
        "domain": current_domain,
    }


def _saved_feishu_instance_identity_key(
    feishu_cfg: Any,
    defaults: dict[str, Any],
    instance_id: str,
) -> str:
    for spec in feishu_instance_specs(feishu_cfg, defaults):
        if spec.instance_id == instance_id:
            return feishu_app_identity_key(
                str(spec.config.get("appId") or spec.config.get("app_id") or ""),
                str(spec.config.get("domain") or "feishu"),
            )
    return ""


def _saved_feishu_instance_for_identity(
    feishu_cfg: Any,
    defaults: dict[str, Any],
    app_id: str,
    domain: str,
) -> ChannelInstanceSpec | None:
    identity_key = feishu_app_identity_key(app_id, domain)
    if not identity_key:
        return None
    for spec in feishu_instance_specs(feishu_cfg, defaults):
        saved_identity = feishu_app_identity_key(
            str(spec.config.get("appId") or spec.config.get("app_id") or ""),
            str(spec.config.get("domain") or "feishu"),
        )
        if saved_identity == identity_key:
            return spec
    return None


def sync_saved_feishu_identity_boundary(
    *,
    instance_id: str,
    app_id: str,
    domain: str,
) -> bool:
    """Persist the Feishu app identity marker and clear access if it changed.

    WebUI connect normally handles this at save time. This startup check catches
    manual config edits so approved users do not accidentally carry over to a
    different Feishu/Lark app in the same local instance slot.
    """
    current_identity_key = feishu_app_identity_key(app_id, domain)
    if not current_identity_key:
        return False

    from nanobot.config.loader import load_config, save_config

    full_config = load_config()
    feishu_cfg = getattr(full_config.channels, "feishu", None) or {}
    if not isinstance(feishu_cfg, dict):
        feishu_cfg = {}

    defaults = feishu_default_config()
    previous_identity_key = ""
    for spec in feishu_instance_specs(feishu_cfg, defaults):
        if spec.instance_id == instance_id:
            previous_identity_key = str(
                spec.config.get("identityKey") or spec.config.get("identity_key") or ""
            )
            break

    access_cleared = bool(previous_identity_key and previous_identity_key != current_identity_key)
    values: dict[str, Any] = {"identityKey": current_identity_key}
    if access_cleared:
        values["allowFrom"] = []
        values["allow_from"] = []
        clear_channel(runtime_channel_name("feishu", instance_id))

    if not previous_identity_key or access_cleared:
        feishu_cfg = update_feishu_instance_preserving_shape(
            feishu_cfg,
            defaults,
            instance_id,
            values,
        )
        setattr(full_config.channels, "feishu", feishu_cfg)
        save_config(full_config)

    return access_cleared


def save_registration_result(
    result: dict,
    *,
    instance_id: str = DEFAULT_INSTANCE_ID,
    name: str | None = None,
) -> str:
    """Persist a successful Feishu/Lark registration result to config.json."""
    from nanobot.config.loader import load_config, save_config

    full_config = load_config()
    feishu_cfg = getattr(full_config.channels, "feishu", None) or {}
    if not isinstance(feishu_cfg, dict):
        feishu_cfg = {}
    defaults = feishu_default_config()
    app_id = str(result["app_id"]).strip()
    domain = str(result.get("domain", "feishu") or "feishu").strip().lower()
    domain = "lark" if domain == "lark" else "feishu"
    existing = _saved_feishu_instance_for_identity(feishu_cfg, defaults, app_id, domain)
    effective_instance_id = existing.instance_id if existing is not None else instance_id
    previous_identity_key = _saved_feishu_instance_identity_key(
        feishu_cfg,
        defaults,
        effective_instance_id,
    )
    next_identity_key = feishu_app_identity_key(app_id, domain)
    identity_changed = bool(previous_identity_key and previous_identity_key != next_identity_key)
    identity: dict[str, str] = {}
    with suppress(Exception):
        identity = fetch_feishu_app_identity(
            app_id,
            str(result["app_secret"]),
            domain,
        )
    default_name = (
        "nanobot"
        if effective_instance_id == DEFAULT_INSTANCE_ID
        else f"nanobot {effective_instance_id}"
    )
    existing_name = existing.config.get("name") if existing is not None else None
    saved_name = (
        existing_name
        if existing is not None and existing.instance_id != instance_id
        else name
    )
    values = {
        "name": str(saved_name or default_name),
        "appId": app_id,
        "appSecret": result["app_secret"],
        "domain": domain,
        "identityKey": next_identity_key,
        "enabled": True,
        **identity,
    }
    if identity_changed:
        values["allowFrom"] = []
        values["allow_from"] = []
        clear_channel(runtime_channel_name("feishu", effective_instance_id))
    feishu_cfg = upsert_feishu_instance(
        feishu_cfg,
        defaults,
        effective_instance_id,
        values,
    )
    setattr(full_config.channels, "feishu", feishu_cfg)
    save_config(full_config)
    return effective_instance_id


def refresh_saved_feishu_identities(
    config: Any | None = None,
    *,
    config_path: Path | None = None,
    instance_id: str | None = None,
) -> bool:
    """Backfill missing Feishu assistant display identity in saved config.

    Existing users may already have working App ID/Secret credentials from
    older builds. Fetch identity only when an instance has credentials but no
    identity metadata at all, then persist the attempt so Settings does not hit
    Feishu on every render.
    """
    if not FEISHU_AVAILABLE:
        return False

    from nanobot.config.loader import load_config, save_config

    full_config = config or load_config()
    feishu_cfg = getattr(full_config.channels, "feishu", None)
    defaults = feishu_default_config()
    specs = feishu_instance_specs(feishu_cfg, defaults)
    if instance_id:
        specs = [spec for spec in specs if spec.instance_id == instance_id]
    updated = False

    for spec in specs:
        instance = spec.config
        if (
            instance.get("displayName")
            or instance.get("avatarUrl")
            or instance.get("identityFetchedAt")
        ):
            continue

        app_id = str(instance.get("appId") or instance.get("app_id") or "").strip()
        app_secret = str(instance.get("appSecret") or instance.get("app_secret") or "").strip()
        if not app_id or not app_secret:
            continue

        identity = fetch_feishu_app_identity(
            app_id,
            app_secret,
            str(instance.get("domain") or "feishu"),
        )
        if not identity:
            identity = {"identityFetchedAt": _identity_timestamp()}

        feishu_cfg = update_feishu_instance_preserving_shape(
            feishu_cfg,
            defaults,
            spec.instance_id,
            identity,
        )
        updated = True

    if not updated:
        return False

    setattr(full_config.channels, "feishu", feishu_cfg)
    save_config(full_config, config_path)
    return True


def qr_register(
    *,
    initial_domain: str = "feishu",
) -> dict | None:
    """Run the Feishu / Lark scan-to-create QR registration flow.

    Returns on success:
        {
            "app_id": str,
            "app_secret": str,
            "domain": "feishu" | "lark",
        }

    Returns None on expected failures (network, auth denied, timeout).
    Unexpected errors (bugs, protocol regressions) propagate to the caller.
    """
    import httpx

    try:
        return _qr_register_inner(initial_domain=initial_domain)
    except (RuntimeError, OSError, json.JSONDecodeError, httpx.HTTPError) as exc:
        _LOGIN_CONSOLE.print(
            f"[yellow]Unable to start Feishu/Lark login:[/yellow] {escape(str(exc))}"
        )
        return None


def _print_qr_code(url: str) -> None:
    """Print QR code as ASCII art if qrcode package is available, otherwise print URL."""
    try:
        import qrcode as qr_lib

        _LOGIN_CONSOLE.print("\n[bold]Scan with Feishu or Lark[/bold]\n")
        qr = qr_lib.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        _LOGIN_CONSOLE.print()
    except ImportError:
        _LOGIN_CONSOLE.print()
        _LOGIN_CONSOLE.print(Panel.fit(Text(url), title="Open with Feishu or Lark", border_style="cyan"))
        _LOGIN_CONSOLE.print()


def _qr_register_inner(
    *,
    initial_domain: str,
) -> dict | None:
    """Run init → begin → poll. Raises on network/protocol errors."""
    _LOGIN_CONSOLE.print("[cyan]Preparing Feishu/Lark login...[/cyan]")
    _init_registration(initial_domain)
    begin = _begin_registration(initial_domain)

    _print_qr_code(begin["qr_url"])

    with _LOGIN_CONSOLE.status("Waiting for authorization in Feishu/Lark...", spinner="dots"):
        return _poll_registration(
            device_code=begin["device_code"],
            interval=begin["interval"],
            expire_in=begin["expire_in"],
            domain=initial_domain,
        )


_STREAM_ELEMENT_ID = "streaming_md"
_NEW_SESSION_DIVIDER_CONTENT = json.dumps({
    "type": "divider",
    "params": {"divider_text": {"text": "New session started."}},
})


@dataclass
class _FeishuStreamBuf:
    """Per-chat streaming accumulator using CardKit streaming API."""

    text: str = ""
    card_id: str | None = None
    sequence: int = 0
    last_edit: float = 0.0


@dataclass
class _FeishuCardInteraction:
    """Short-lived server-side state for one report interaction card."""

    owner_open_id: str
    chat_id: str
    metadata: dict[str, Any]
    expires_at: float
    phase: str
    base_params: dict[str, Any]
    option_values: dict[str, Any]
    selected_tenants: list[str]
    selected_models: dict[str, list[str]]
    max_tenants: int
    consumed: bool = False


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using WebSocket long connection.

    Uses WebSocket to receive events - no public IP or webhook required.

    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """

    name = "feishu"
    display_name = "Feishu"

    _STREAM_EDIT_INTERVAL = 0.5  # throttle between CardKit streaming updates

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return feishu_default_config()

    @classmethod
    def refresh_feature_metadata(
        cls,
        config_path: Path,
        *,
        instance_id: str = DEFAULT_INSTANCE_ID,
    ) -> bool:
        from nanobot.config.loader import load_config

        return refresh_saved_feishu_identities(
            load_config(config_path),
            config_path=config_path,
            instance_id=instance_id,
        )

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = FeishuConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_runner = get_feishu_ws_runner()
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # Ordered dedup cache
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream_bufs: dict[str, _FeishuStreamBuf] = {}
        self._bot_open_id: str | None = None
        self._bot_message_ids: OrderedDict[str, bool] = OrderedDict()
        self._background_tasks: set[asyncio.Task] = set()
        self._reaction_ids: dict[str, str] = {}  # message_id → reaction_id
        self._card_interactions: dict[str, _FeishuCardInteraction] = {}
        self._card_interaction_lock = threading.Lock()

    # ------------------------------------------------------------------
    # QR login — writes credentials directly to config.json
    # ------------------------------------------------------------------

    async def login(self, force: bool = False) -> bool:
        """Perform QR code scan-to-create login for Feishu/Lark.

        Uses the Feishu device-code registration flow to create a new bot
        application automatically.  Opens a URL for the user to authorize
        with the Feishu or Lark mobile app.

        On success, writes ``appId``, ``appSecret``, and ``domain`` to
        ``channels.feishu`` in ``config.json`` and sets ``enabled: true``.

        Args:
            force: If True, clear existing credentials and force re-authentication.

        Returns True on success.
        """
        if force:
            self.config.app_id = ""
            self.config.app_secret = ""

        if self.config.app_id and self.config.app_secret:
            _LOGIN_CONSOLE.print("[green]Feishu/Lark is already authenticated.[/green]")
            _LOGIN_CONSOLE.print("Use --force to re-authenticate with a new bot.\n")
            return True

        _LOGIN_CONSOLE.print("Authorize with the mobile app. nanobot will save the new bot credentials.\n")

        result = qr_register(initial_domain=self.config.domain or "feishu")
        if not result:
            _LOGIN_CONSOLE.print(
                "[yellow]Login was not completed.[/yellow] "
                "Run 'nanobot channels login feishu --force' to retry."
            )
            return False

        self.config.app_id = result["app_id"]
        self.config.app_secret = result["app_secret"]
        self.config.domain = result.get("domain", "feishu")

        save_registration_result(
            result,
            instance_id=self.config.instance_id,
            name=self.config.name,
        )

        _LOGIN_CONSOLE.print("\n[green]Feishu/Lark login complete.[/green]")
        _LOGIN_CONSOLE.print(f"App ID: {escape(result['app_id'])}")
        _LOGIN_CONSOLE.print(f"Domain: {escape(self.config.domain)}")
        return True

    @staticmethod
    def _register_optional_event(builder: Any, method_name: str, handler: Any) -> Any:
        """Register an event handler only when the SDK supports it."""
        method = getattr(builder, method_name, None)
        return method(handler) if callable(method) else builder

    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            self.logger.error("SDK not installed. Run: nanobot plugins enable feishu")
            return

        if not self.config.app_id or not self.config.app_secret:
            self.logger.error(
                "app_id and app_secret not configured. "
                "Run 'nanobot channels login feishu' to set up via QR code."
            )
            return

        if sync_saved_feishu_identity_boundary(
            instance_id=self.config.instance_id,
            app_id=self.config.app_id,
            domain=self.config.domain,
        ):
            self.config.identity_key = feishu_app_identity_key(self.config.app_id, self.config.domain)
            self.config.allow_from = []
            self.logger.info(
                "Feishu app identity changed for {}; cleared paired users for this assistant",
                self.name,
            )

        lark, feishu_domain, lark_domain = await asyncio.to_thread(_load_lark_runtime)

        redirect_lib_logging("Lark")

        self._running = True
        self._loop = asyncio.get_running_loop()

        # Create Lark client for sending messages
        domain = lark_domain if self.config.domain == "lark" else feishu_domain
        self._client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        builder = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(self._on_message_sync)
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_created_v1", self._on_reaction_created
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_deleted_v1", self._on_reaction_deleted
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_message_read_v1", self._on_message_read
        )
        builder = self._register_optional_event(
            builder, "register_p2_card_action_trigger", self._on_card_action_sync
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
            self._on_bot_p2p_chat_entered,
        )
        # Silence "processor not found" errors when bots are added/removed from groups.
        # These events carry no actionable data for the agent.
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_member_bot_added_v1",
            lambda _: None,
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_member_bot_deleted_v1",
            lambda _: None,
        )
        event_handler = builder.build()

        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            domain=domain,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        await self._ws_runner.start_client(self.name, self._ws_client)

        # Fetch bot's own open_id for accurate @mention matching
        self._bot_open_id = await asyncio.get_running_loop().run_in_executor(
            None, self._fetch_bot_open_id
        )
        if self._bot_open_id:
            self.logger.info("bot open_id: {}", self._bot_open_id)
        else:
            self.logger.warning("Could not fetch bot open_id; @mention matching may be inaccurate")

        self.logger.info("bot started with WebSocket long connection")
        self.logger.info("No public IP required - using WebSocket to receive events")

        # Expose the live channel so agent tools can call the Feishu API.
        from nanobot.channels.runtime_registry import register_channel

        register_channel(self)

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """
        Stop the Feishu bot.

        Notice: lark.ws.Client does not expose stop method， simply exiting the program will close the client.

        Reference: https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/ws/client.py#L86
        """
        self._running = False
        from nanobot.channels.runtime_registry import unregister_channel

        unregister_channel(self.name)
        await self._ws_runner.stop_client(self.name)
        self.logger.info("bot stopped")

    def _fetch_bot_open_id(self) -> str | None:
        """Fetch the bot's own open_id via GET /open-apis/bot/v3/info."""
        try:
            import lark_oapi as lark

            request = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({lark.AccessTokenType.APP})
                .build()
            )
            response = self._client.request(request)
            if response.success():
                import json

                data = json.loads(response.raw.content)
                bot = (data.get("data") or data).get("bot") or data.get("bot") or {}
                return bot.get("open_id")
            self.logger.warning("Failed to get bot info: code={}, msg={}", response.code, response.msg)
            return None
        except Exception as e:
            self.logger.warning("Error fetching bot info: {}", e)
            return None

    @staticmethod
    def _resolve_mentions(text: str, mentions: list[MentionEvent] | None) -> str:
        """Replace @_user_n placeholders with actual user info from mentions.

        Args:
            text: The message text containing @_user_n placeholders
            mentions: List of mention objects from Feishu message

        Returns:
            Text with placeholders replaced by @姓名 (open_id)
        """
        if not mentions or not text:
            return text

        for mention in mentions:
            key = mention.key or None
            if not key:
                continue
            # Feishu placeholders are numbered keys like @_user_1. Keep
            # punctuation-adjacent mentions valid without matching @_user_10.
            pattern = rf"{re.escape(key)}(?![A-Za-z0-9_])"
            if not re.search(pattern, text):
                continue

            user_id_obj = mention.id or None
            if not user_id_obj:
                continue

            open_id = user_id_obj.open_id
            user_id = user_id_obj.user_id
            name = mention.name or key

            # Format: @姓名 (open_id, user_id: xxx)
            if open_id and user_id:
                replacement = f"@{name} ({open_id}, user id: {user_id})"
            elif open_id:
                replacement = f"@{name} ({open_id})"
            else:
                replacement = f"@{name}"

            text = re.sub(pattern, replacement, text)

        return text

    def _is_bot_mention_event(self, mention: Any) -> bool:
        mid = getattr(mention, "id", None)
        if not mid:
            return False

        mention_open_id = getattr(mid, "open_id", None) or ""
        bot_open_id = getattr(self, "_bot_open_id", None) or ""
        if bot_open_id:
            return mention_open_id == bot_open_id

        # Fallback heuristic when bot open_id is unavailable.
        return not getattr(mid, "user_id", None) and mention_open_id.startswith("ou_")

    def _strip_leading_bot_mention(
        self, text: str, mentions: list[MentionEvent] | None
    ) -> str:
        """Remove a required leading bot mention before slash command routing."""
        if not mentions or not text:
            return text

        candidate = text.lstrip()
        for mention in mentions:
            key = getattr(mention, "key", None) or ""
            if not key or not re.match(rf"{re.escape(key)}(?![A-Za-z0-9_])", candidate):
                continue
            if not self._is_bot_mention_event(mention):
                continue

            stripped = candidate[len(key) :].strip()
            return stripped or text

        return text

    def _is_bot_mentioned(self, message: Any) -> bool:
        """Check if the bot is @mentioned in the message."""
        raw_content = message.content or ""
        if "@_all" in raw_content:
            return True

        for mention in getattr(message, "mentions", None) or []:
            if self._is_bot_mention_event(mention):
                return True
        return False

    def _is_group_message_for_bot(self, message: Any) -> bool:
        """Allow group messages when policy is open or bot is @mentioned."""
        if self.config.group_policy == "open":
            return True
        return self._is_bot_mentioned(message)

    def _remember_bot_message(self, message_id: str | None) -> None:
        if not isinstance(message_id, str) or not message_id:
            return
        self._bot_message_ids[message_id] = True
        self._bot_message_ids.move_to_end(message_id)
        while len(self._bot_message_ids) > 1000:
            self._bot_message_ids.popitem(last=False)

    def _message_is_from_this_bot_sync(self, message_id: str) -> bool:
        """Verify message ownership through Feishu after an in-memory cache miss."""
        from lark_oapi.api.im.v1 import GetMessageRequest

        try:
            request = GetMessageRequest.builder().message_id(message_id).build()
            response = self._client.im.v1.message.get(request)
            if not response.success():
                return False
            items = getattr(response.data, "items", None) or []
            if not items:
                return False
            sender = getattr(items[0], "sender", None)
            sender_id = getattr(sender, "id", None) or ""
            sender_id_type = getattr(sender, "id_type", None) or ""
            open_bot_id = getattr(sender, "open_bot_id", None) or ""
            sender_type = getattr(sender, "sender_type", None) or ""
            if sender_type not in {"app", "bot"}:
                return False
            return bool(
                (self._bot_open_id and open_bot_id == self._bot_open_id)
                or (self._bot_open_id and sender_id == self._bot_open_id)
                or (
                    self.config.app_id
                    and sender_id_type == "app_id"
                    and sender_id == self.config.app_id
                )
            )
        except Exception as exc:
            self.logger.debug("could not verify bot message {}: {}", message_id, exc)
            return False

    async def _is_bot_thread_message(self, message: Any) -> bool:
        if not self.config.follow_bot_threads:
            return False
        # ``root_id`` is also present on ordinary quoted replies.  Only a
        # real Feishu topic has ``thread_id`` (``omt_...``), so never relax
        # mention gating without it.
        if not getattr(message, "thread_id", None):
            return False
        candidates = []
        for value in (
            getattr(message, "root_id", None),
        ):
            if isinstance(value, str) and value and value not in candidates:
                candidates.append(value)
        if not candidates:
            return False
        for message_id in candidates:
            cached = self._bot_message_ids.get(message_id)
            if cached is True:
                return True
            if self._client and await asyncio.get_running_loop().run_in_executor(
                None, self._message_is_from_this_bot_sync, message_id
            ):
                self._remember_bot_message(message_id)
                return True
        return False

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> str | None:
        """Sync helper for adding reaction (runs in thread pool)."""
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        try:
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )

            response = self._client.im.v1.message_reaction.create(request)

            if not response.success():
                self.logger.warning(
                    "Failed to add reaction: code={}, msg={}", response.code, response.msg
                )
                return None
            else:
                self.logger.debug("Added {} reaction to message {}", emoji_type, message_id)
                return response.data.reaction_id if response.data else None
        except Exception as e:
            self.logger.warning("Error adding reaction: {}", e)
            return None

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> str | None:
        """Add a reaction emoji to a message.

        Returns the reaction_id on success, None on failure.
        When called via a tracked background task, the returned reaction_id
        is stored in ``_reaction_ids`` for later cleanup by ``send_delta``.

        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client:
            return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    def _remove_reaction_sync(self, message_id: str, reaction_id: str) -> None:
        """Sync helper for removing reaction (runs in thread pool)."""
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

        try:
            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )

            response = self._client.im.v1.message_reaction.delete(request)
            if response.success():
                self.logger.debug("Removed reaction {} from message {}", reaction_id, message_id)
            else:
                self.logger.debug(
                    "Failed to remove reaction: code={}, msg={}", response.code, response.msg
                )
        except Exception as e:
            self.logger.debug("Error removing reaction: {}", e)

    async def _remove_reaction(self, message_id: str, reaction_id: str) -> None:
        """
        Remove a reaction emoji from a message (non-blocking).

        Used to clear the "processing" indicator after bot replies.
        """
        if not self._client or not reaction_id:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._remove_reaction_sync, message_id, reaction_id)

    def _on_background_task_done(self, task: asyncio.Task) -> None:
        """Callback: remove from tracking set and log unhandled exceptions."""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self.logger.warning("Background task failed: {}", exc)

    def _on_reaction_added(self, message_id: str, task: asyncio.Task) -> None:
        """Callback: store reaction_id after background add-reaction completes."""
        if task.cancelled():
            return
        # Failures already logged by _on_background_task_done.
        with suppress(Exception):
            reaction_id = task.result()
            if reaction_id:
                self._reaction_ids[message_id] = reaction_id
        # Trim cache to prevent unbounded growth
        if len(self._reaction_ids) > 500:
            self._reaction_ids.pop(next(iter(self._reaction_ids)))

    @staticmethod
    def _stream_key(chat_id: str, metadata: dict[str, Any] | None = None) -> str:
        """Scope streaming buffers to the inbound message when available."""
        meta = metadata or {}
        return meta.get("message_id") or chat_id

    # Regex to match markdown tables (header + separator + data rows)
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

    # Markdown formatting patterns that should be stripped from plain-text
    # surfaces like table cells and heading text.
    _MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    _MD_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")
    _MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
    _MD_STRIKE_RE = re.compile(r"~~(.+?)~~")

    @classmethod
    def _strip_md_formatting(cls, text: str) -> str:
        """Strip markdown formatting markers from text for plain display.

        Feishu table cells do not support markdown rendering, so we remove
        the formatting markers to keep the text readable.
        """
        # Remove bold markers
        text = cls._MD_BOLD_RE.sub(r"\1", text)
        text = cls._MD_BOLD_UNDERSCORE_RE.sub(r"\1", text)
        # Remove italic markers
        text = cls._MD_ITALIC_RE.sub(r"\1", text)
        # Remove strikethrough markers
        text = cls._MD_STRIKE_RE.sub(r"\1", text)
        return text

    @classmethod
    def _parse_md_table(cls, table_text: str) -> dict | None:
        """Parse a markdown table into a Feishu table element."""
        lines = [_line.strip() for _line in table_text.strip().split("\n") if _line.strip()]
        if len(lines) < 3:
            return None

        def split(_line: str) -> list[str]:
            return [c.strip() for c in _line.strip("|").split("|")]

        headers = [cls._strip_md_formatting(h) for h in split(lines[0])]
        rows = [[cls._strip_md_formatting(c) for c in split(_line)] for _line in lines[2:]]
        columns = [
            {"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
            for i, h in enumerate(headers)
        ]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [
                {f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows
            ],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """Split content into div/markdown + table elements for Feishu card."""
        protected = content
        code_blocks: list[str] = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1)

        elements, last_end = [], 0
        for m in self._TABLE_RE.finditer(protected):
            before = protected[last_end : m.start()]
            if before.strip():
                elements.extend(self._split_headings(before))
            elements.append(
                self._parse_md_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)}
            )
            last_end = m.end()
        remaining = protected[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))

        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    @staticmethod
    def _split_elements_by_table_limit(
        elements: list[dict], max_tables: int = 1
    ) -> list[list[dict]]:
        """Split card elements into groups with at most *max_tables* table elements each.

        Feishu cards have a hard limit of one table per card (API error 11310).
        When the rendered content contains multiple markdown tables each table is
        placed in a separate card message so every table reaches the user.
        """
        if not elements:
            return [[]]
        groups: list[list[dict]] = []
        current: list[dict] = []
        table_count = 0
        for el in elements:
            if el.get("tag") == "table":
                if table_count >= max_tables:
                    if current:
                        groups.append(current)
                    current = []
                    table_count = 0
                current.append(el)
                table_count += 1
            else:
                current.append(el)
        if current:
            groups.append(current)
        return groups or [[]]

    def _prune_card_interactions(self) -> None:
        now = time.monotonic()
        with self._card_interaction_lock:
            expired = [
                key
                for key, value in self._card_interactions.items()
                if value.expires_at <= now
            ]
            for key in expired:
                self._card_interactions.pop(key, None)

    @staticmethod
    def _card_header(title: str, subtitle: str = "") -> dict[str, Any]:
        content = title if not subtitle else f"{title}｜{subtitle}"
        return {
            "template": "blue",
            "title": {"tag": "plain_text", "content": content},
        }

    def _register_scope_interaction(
        self, ui: dict[str, Any], msg: OutboundMessage
    ) -> tuple[str, _FeishuCardInteraction]:
        nonce = uuid.uuid4().hex
        options: dict[str, Any] = {}
        selected: list[str] = []
        for index, item in enumerate(ui.get("tenant_options") or []):
            if not isinstance(item, dict):
                continue
            opaque = f"{nonce}:t{index}"
            query = str(item.get("value") or "").strip()
            if not query:
                continue
            options[opaque] = query
            if item.get("selected"):
                selected.append(query)
        state = _FeishuCardInteraction(
            owner_open_id=str(msg.metadata.get("sender_open_id") or ""),
            chat_id=msg.chat_id,
            metadata=dict(msg.metadata),
            expires_at=time.monotonic() + 600,
            phase="scope",
            base_params=dict(ui.get("base_params") or {}),
            option_values=options,
            selected_tenants=selected,
            selected_models={},
            max_tenants=int(ui.get("max_tenants") or 5),
        )
        with self._card_interaction_lock:
            self._card_interactions[nonce] = state
        return nonce, state

    def _register_model_interaction(
        self, ui: dict[str, Any], msg: OutboundMessage
    ) -> tuple[str, _FeishuCardInteraction]:
        nonce = uuid.uuid4().hex
        options: dict[str, Any] = {}
        tenants: list[str] = []
        index = 0
        for group in ui.get("tenant_models") or []:
            if not isinstance(group, dict):
                continue
            query = str(group.get("tenant_query") or "").strip()
            if not query:
                continue
            tenants.append(query)
            for model in group.get("models") or []:
                opaque = f"{nonce}:m{index}"
                options[opaque] = (query, str(model))
                index += 1
        state = _FeishuCardInteraction(
            owner_open_id=str(msg.metadata.get("sender_open_id") or ""),
            chat_id=msg.chat_id,
            metadata=dict(msg.metadata),
            expires_at=time.monotonic() + 600,
            phase="models",
            base_params=dict(ui.get("base_params") or {}),
            option_values=options,
            selected_tenants=tenants,
            selected_models={},
            max_tenants=int(ui.get("max_tenants") or 5),
        )
        with self._card_interaction_lock:
            self._card_interactions[nonce] = state
        return nonce, state

    def _register_subscription_interaction(
        self, ui: dict[str, Any], msg: OutboundMessage
    ) -> tuple[str, _FeishuCardInteraction, list[dict[str, str]], list[dict[str, str]]]:
        nonce = uuid.uuid4().hex
        options: dict[str, Any] = {}
        schedule_options: list[dict[str, str]] = []
        schedule_specs: list[tuple[str, dict[str, Any]]] = [
            ("日报 · 每个工作日", {"period": "day", "daily_mode": "workdays"}),
            ("日报 · 每天", {"period": "day", "daily_mode": "every_day"}),
        ]
        schedule_specs.extend(
            (f"周报 · 每周{label}", {"period": "week", "weekday": weekday})
            for weekday, label in enumerate("一二三四五六日", start=1)
        )
        schedule_specs.extend(
            (f"月报 · 每月 {month_day} 日", {"period": "month", "month_day": month_day})
            for month_day in range(1, 29)
        )
        for index, (label, params) in enumerate(schedule_specs):
            opaque = f"{nonce}:s{index}"
            options[opaque] = params
            schedule_options.append({"label": label, "value": opaque})
        time_options: list[dict[str, str]] = []
        for index in range(48):
            clock = f"{index // 2:02d}:{(index % 2) * 30:02d}"
            opaque = f"{nonce}:t{index}"
            options[opaque] = clock
            time_options.append({"label": clock, "value": opaque})
        default_period = str(ui.get("default_period") or "week")
        default_schedule_params = {
            "day": {"period": "day", "daily_mode": "workdays"},
            "week": {"period": "week", "weekday": 1},
            "month": {"period": "month", "month_day": 1},
        }.get(default_period, {"period": "week", "weekday": 1})
        requested_time = str(ui.get("default_time") or "10:00")
        default_time = requested_time if any(
            item["label"] == requested_time for item in time_options
        ) else "10:00"
        state = _FeishuCardInteraction(
            owner_open_id=str(msg.metadata.get("sender_open_id") or ""),
            chat_id=msg.chat_id,
            metadata=dict(msg.metadata),
            expires_at=time.monotonic() + 600,
            phase="subscription",
            base_params={
                "action": "subscribe",
                "report_params": dict(ui.get("report_params") or {}),
                **default_schedule_params,
                "send_time": default_time,
            },
            option_values=options,
            selected_tenants=[],
            selected_models={},
            max_tenants=0,
        )
        with self._card_interaction_lock:
            self._card_interactions[nonce] = state
        return nonce, state, schedule_options, time_options

    def _build_subscription_card(
        self, ui: dict[str, Any], msg: OutboundMessage
    ) -> dict[str, Any]:
        nonce, _state, schedules, times = self._register_subscription_interaction(ui, msg)
        default_period = str(ui.get("default_period") or "week")
        default_schedule_index = {"day": 0, "week": 2, "month": 9}.get(default_period, 2)
        default_schedule = schedules[default_schedule_index]["value"]
        default_time_label = str(ui.get("default_time") or "10:00")
        default_time = next(
            (item["value"] for item in times if item["label"] == default_time_label),
            times[20]["value"],
        )
        return {
            "config": {"wide_screen_mode": True},
            "header": self._card_header(str(ui.get("title") or "设置报表订阅")),
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**时区**：{ui.get('timezone') or 'Asia/Shanghai'}",
                },
                {
                    "tag": "form",
                    "name": f"report_subscription_{nonce[:8]}",
                    "elements": [
                        {
                            "tag": "select_static",
                            "name": "schedule",
                            "required": True,
                            "width": "fill",
                            "placeholder": {"tag": "plain_text", "content": "发送周期"},
                            "initial_option": default_schedule,
                            "options": [
                                {
                                    "text": {"tag": "plain_text", "content": item["label"]},
                                    "value": item["value"],
                                }
                                for item in schedules
                            ],
                        },
                        {
                            "tag": "select_static",
                            "name": "send_time",
                            "required": True,
                            "width": "fill",
                            "placeholder": {"tag": "plain_text", "content": "发送时间"},
                            "initial_option": default_time,
                            "options": [
                                {
                                    "text": {"tag": "plain_text", "content": item["label"]},
                                    "value": item["value"],
                                }
                                for item in times
                            ],
                        },
                        {
                            "tag": "button",
                            "name": "submit_subscription",
                            "text": {"tag": "plain_text", "content": "创建订阅"},
                            "type": "primary",
                            "complex_interaction": True,
                            "action_type": "form_submit",
                            "value": {
                                "interaction_id": nonce,
                                "action": "submit_subscription",
                            },
                        },
                    ],
                },
            ],
        }

    def _build_scope_card(self, ui: dict[str, Any], msg: OutboundMessage) -> dict[str, Any]:
        nonce, state = self._register_scope_interaction(ui, msg)
        elements: list[dict[str, Any]] = [
            {
                "tag": "markdown",
                "content": (
                    f"**周期**：{ui.get('period', '')}\n"
                    f"最多选择 {state.max_tenants} 个客户，随后选择模型范围。"
                ),
            }
        ]
        tenant_items = [
            item for item in ui.get("tenant_options") or [] if isinstance(item, dict)
        ]
        scope_actions: list[dict[str, Any]] = []
        for item in ui.get("scope_options") or []:
            if not isinstance(item, dict):
                continue
            action = {
                "tag": "button",
                "name": f"scope_{item['value']}",
                "text": {"tag": "plain_text", "content": str(item["label"])},
                "type": "primary" if item["value"] == "all" else "default",
                "value": {
                    "interaction_id": nonce,
                    "action": "scope",
                    "scope": item["value"],
                },
            }
            if ui.get("tenant_required"):
                action["action_type"] = "form_submit"
                action["complex_interaction"] = True
            scope_actions.append(action)
        if ui.get("tenant_required"):
            options = []
            opaque_keys = list(state.option_values)
            for index, item in enumerate(tenant_items):
                if index >= len(opaque_keys):
                    break
                options.append(
                    {
                        "text": {
                            "tag": "plain_text",
                            "content": str(item.get("label") or item.get("value") or ""),
                        },
                        "value": opaque_keys[index],
                    }
                )
            elements.append(
                {
                    "tag": "form",
                    "name": f"magik_scope_{nonce[:8]}",
                    "elements": [
                        {
                            "tag": "multi_select_static",
                            "name": "tenants",
                            "required": True,
                            "width": "fill",
                            "placeholder": {"tag": "plain_text", "content": "选择客户"},
                            "options": options,
                        },
                        {
                            "tag": "column_set",
                            "flex_mode": "none",
                            "horizontal_spacing": "default",
                            "columns": [
                                {
                                    "tag": "column",
                                    "width": "weighted",
                                    "weight": 1,
                                    "elements": [action],
                                }
                                for action in scope_actions
                            ],
                        },
                    ],
                }
            )
        else:
            labels = "、".join(str(item.get("label") or "") for item in tenant_items)
            elements.append({"tag": "markdown", "content": f"**客户**：{labels}"})
            elements.append({"tag": "action", "actions": scope_actions})
        return {
            "config": {"wide_screen_mode": True},
            "header": self._card_header(str(ui.get("title") or "选择报表范围")),
            "elements": elements,
        }

    def _build_model_card(self, ui: dict[str, Any], msg: OutboundMessage) -> dict[str, Any]:
        nonce, state = self._register_model_interaction(ui, msg)
        options = []
        labels: dict[tuple[str, str], str] = {}
        for group in ui.get("tenant_models") or []:
            if not isinstance(group, dict):
                continue
            query = str(group.get("tenant_query") or "")
            tenant_label = str(group.get("tenant_label") or query)
            for model in group.get("models") or []:
                labels[(query, str(model))] = (
                    str(model) if len(state.selected_tenants) == 1 else f"{tenant_label} / {model}"
                )
        for opaque, value in state.option_values.items():
            query, model = value
            options.append(
                {
                    "text": {
                        "tag": "plain_text",
                        "content": labels.get((query, model), model),
                    },
                    "value": opaque,
                }
            )
        return {
            "config": {"wide_screen_mode": True},
            "header": self._card_header(str(ui.get("title") or "选择模型")),
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**周期**：{ui.get('period', '')}\n请选择一个或多个模型。",
                },
                {
                    "tag": "form",
                    "name": f"magik_models_{nonce[:8]}",
                    "elements": [
                        {
                            "tag": "multi_select_static",
                            "name": "models",
                            "required": True,
                            "width": "fill",
                            "placeholder": {"tag": "plain_text", "content": "选择模型"},
                            "options": options,
                        },
                        {
                            "tag": "button",
                            "name": "submit_models",
                            "text": {
                                "tag": "plain_text",
                                "content": "生成报表",
                            },
                            "type": "primary",
                            "complex_interaction": True,
                            "action_type": "form_submit",
                            "value": {
                                "interaction_id": nonce,
                                "action": "submit_models",
                            },
                        },
                    ],
                },
            ],
        }

    @staticmethod
    def _callback_option_values(value: Any) -> list[str]:
        """Normalize Feishu standalone-select and form-submit option values."""

        if isinstance(value, str):
            with suppress(ValueError):
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [str(item) for item in parsed]
            return [value]
        if isinstance(value, list | tuple | set):
            return [str(item) for item in value]
        return []

    def _build_report_card(
        self, card: dict[str, Any], msg: OutboundMessage | None = None
    ) -> dict[str, Any]:
        elements: list[dict[str, Any]] = [
            {
                "tag": "markdown",
                "content": "**周期概览**\n" + "\n".join(
                    f"• {item}" for item in card.get("overview") or []
                ),
            },
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": "**分段总量**\n" + "\n".join(
                    f"• {item}" for item in card.get("segments") or []
                ),
            },
        ]
        table = card.get("table")
        if isinstance(table, dict):
            elements.extend(
                [
                    {"tag": "hr"},
                    {"tag": "markdown", "content": "**模型矩阵**"},
                    {
                        "tag": "table",
                        "page_size": int(table.get("page_size") or 8),
                        "columns": list(table.get("columns") or []),
                        "rows": list(table.get("rows") or []),
                    },
                ]
            )
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": "**关键变化**\n" + "\n".join(
                        f"• {item}" for item in card.get("insights") or []
                    ),
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": str(card.get("quality") or ""),
                        }
                    ],
                },
            ]
        )
        raw_actions = [
            item for item in card.get("actions") or [] if isinstance(item, dict)
        ]
        if raw_actions and msg is not None:
            nonce, _state, actions = self._register_report_document_interaction(
                raw_actions, msg
            )
            buttons = []
            for opaque, item in actions:
                buttons.append(
                    {
                        "tag": "button",
                        "name": f"report_action_{opaque.rsplit(':', 1)[-1]}",
                        "text": {
                            "tag": "plain_text",
                            "content": str(item.get("label") or "订阅"),
                        },
                        "type": str(item.get("style") or "default"),
                        "value": {
                            "interaction_id": nonce,
                            "action": "report_document",
                            "action_token": opaque,
                        },
                    }
                )
            if buttons:
                elements.append({"tag": "action", "actions": buttons})
        return {
            "config": {"wide_screen_mode": True},
            "header": self._card_header(
                str(card.get("title") or "Magik Cube 报表"),
                str(card.get("subtitle") or ""),
            ),
            "elements": elements,
        }

    def _resolve_report_document_action(self, item: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve a public action id to a server-side direct Tool call."""

        tool_name = str(item.get("tool_name") or "")
        params = item.get("params")
        if tool_name and isinstance(params, dict):
            return {
                "tool_name": tool_name,
                "params": dict(params),
                "content": str(item.get("content") or "继续执行报表操作"),
            }
        action_id = str(item.get("action_id") or "")
        if action_id.startswith("generate:"):
            period = action_id.partition(":")[2]
            if period not in {"day", "week", "month", "recent7"}:
                return None
            from nanobot.agent.reporting.magik_cube_intent import ReportIntent

            params = ReportIntent(report_kind=period).to_tool_params(  # type: ignore[arg-type]
                today=datetime.now(ZoneInfo("Asia/Shanghai")).date()
            )
            return {
                "tool_name": "magik_cube_daily_report",
                "params": params,
                "content": f"生成固定{period}报",
            }
        if action_id in {"examples", "recent", "subscriptions"}:
            return {
                "tool_name": "report_center",
                "params": {"action": action_id},
                "content": "打开报表中心",
            }
        if action_id == "request_access":
            return {
                "tool_name": "report_center",
                "params": {"action": "request_access"},
                "content": "申请报表权限",
            }
        match = re.fullmatch(r"subscription:(enable|disable|remove):([0-9a-f]{1,64})", action_id)
        if match:
            operation, subscription_id = match.groups()
            return {
                "tool_name": "report_center",
                "params": {
                    "action": f"subscription_{operation}",
                    "subscription_id": subscription_id,
                },
                "content": "管理固定报表订阅",
            }
        return None

    def _register_report_document_interaction(
        self,
        actions: list[dict[str, Any]],
        msg: OutboundMessage,
    ) -> tuple[str, _FeishuCardInteraction, list[tuple[str, dict[str, Any]]]]:
        nonce = uuid.uuid4().hex
        options: dict[str, Any] = {}
        resolved: list[tuple[str, dict[str, Any]]] = []
        for index, item in enumerate(actions):
            target = self._resolve_report_document_action(item)
            if target is None:
                continue
            opaque = f"{nonce}:a{index}"
            options[opaque] = target
            resolved.append((opaque, item))
        state = _FeishuCardInteraction(
            owner_open_id=str(msg.metadata.get("sender_open_id") or ""),
            chat_id=msg.chat_id,
            metadata=dict(msg.metadata),
            expires_at=time.monotonic() + 600,
            phase="report_document",
            base_params={},
            option_values=options,
            selected_tenants=[],
            selected_models={},
            max_tenants=0,
        )
        if resolved:
            with self._card_interaction_lock:
                self._card_interactions[nonce] = state
        return nonce, state, resolved

    def _build_report_document(
        self, ui: dict[str, Any], msg: OutboundMessage
    ) -> dict[str, Any]:
        raw_actions: list[dict[str, Any]] = []
        for block in ui.get("blocks") or []:
            if isinstance(block, dict) and block.get("kind") == "actions":
                raw_actions.extend(
                    item
                    for item in (block.get("data") or {}).get("actions") or []
                    if isinstance(item, dict)
                )
        nonce, _state, actions = self._register_report_document_interaction(raw_actions, msg)
        action_map = {id(item): opaque for opaque, item in actions}
        elements: list[dict[str, Any]] = []
        for block in ui.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("kind")
            data = block.get("data") if isinstance(block.get("data"), dict) else {}
            if kind == "markdown":
                elements.append({"tag": "markdown", "content": str(data.get("content") or "")})
            elif kind == "note":
                elements.append(
                    {
                        "tag": "note",
                        "elements": [
                            {"tag": "plain_text", "content": str(data.get("content") or "")}
                        ],
                    }
                )
            elif kind == "metrics":
                values = data.get("items") or []
                elements.append(
                    {
                        "tag": "markdown",
                        "content": "\n".join(
                            f"**{item.get('label', '')}**：{item.get('value', '')}"
                            for item in values
                            if isinstance(item, dict)
                        ),
                    }
                )
            elif kind == "table":
                elements.append(
                    {
                        "tag": "table",
                        "page_size": int(data.get("page_size") or 8),
                        "columns": list(data.get("columns") or []),
                        "rows": list(data.get("rows") or []),
                    }
                )
            elif kind == "actions":
                buttons = []
                for item in data.get("actions") or []:
                    if not isinstance(item, dict):
                        continue
                    opaque = action_map.get(id(item))
                    if not opaque:
                        continue
                    buttons.append(
                        {
                            "tag": "button",
                            "name": f"report_action_{opaque.rsplit(':', 1)[-1]}",
                            "text": {"tag": "plain_text", "content": str(item.get("label") or "打开")},
                            "type": str(item.get("style") or "default"),
                            "value": {
                                "interaction_id": nonce,
                                "action": "report_document",
                                "action_token": opaque,
                            },
                        }
                    )
                if buttons:
                    elements.append({"tag": "action", "actions": buttons})
        return {
            "config": {"wide_screen_mode": True},
            "header": self._card_header(
                str(ui.get("title") or "报表中心"), str(ui.get("subtitle") or "")
            ),
            "elements": elements,
        }

    def _build_agent_ui_cards(
        self, ui: Any, msg: OutboundMessage
    ) -> list[dict[str, Any]]:
        if not isinstance(ui, dict):
            return []
        self._prune_card_interactions()
        kind = ui.get("kind")
        if kind == "magik_report_form":
            phase = ui.get("phase")
            if phase == "scope":
                return [self._build_scope_card(ui, msg)]
            if phase == "models":
                return [self._build_model_card(ui, msg)]
        if kind == "magik_report_cards":
            return [
                self._build_report_card(card, msg)
                for card in ui.get("cards") or []
                if isinstance(card, dict)
            ]
        if kind == "report_document":
            return [self._build_report_document(ui, msg)]
        if kind == "report_subscription_form":
            return [self._build_subscription_card(ui, msg)]
        return []

    def _split_headings(self, content: str) -> list[dict]:
        """Split content by headings, converting headings to div elements."""
        protected = content
        code_blocks = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1)

        elements = []
        last_end = 0
        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end : m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = self._strip_md_formatting(m.group(2).strip())
            display_text = f"**{text}**" if text else ""
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": display_text,
                    },
                }
            )
            last_end = m.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    # ── Smart format detection ──────────────────────────────────────────
    # Patterns that indicate "complex" markdown needing card rendering
    _COMPLEX_MD_RE = re.compile(
        r"```"  # fenced code block
        r"|^\|.+\|.*\n\s*\|[-:\s|]+\|"  # markdown table (header + separator)
        r"|^#{1,6}\s+",  # headings
        re.MULTILINE,
    )

    # Simple markdown patterns (bold, italic, strikethrough)
    _SIMPLE_MD_RE = re.compile(
        r"\*\*.+?\*\*"  # **bold**
        r"|__.+?__"  # __bold__
        r"|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"  # *italic* (single *)
        r"|~~.+?~~",  # ~~strikethrough~~
        re.DOTALL,
    )

    # Markdown link: [text](url)
    _MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

    # Unordered list items
    _LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)

    # Ordered list items
    _OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)

    # Max length for plain text format
    _TEXT_MAX_LEN = 200

    # Max length for post (rich text) format; beyond this, use card
    _POST_MAX_LEN = 2000

    @classmethod
    def _detect_msg_format(cls, content: str) -> str:
        """Determine the optimal Feishu message format for *content*.

        Returns one of:
        - ``"text"``        – plain text, short and no markdown
        - ``"post"``        – rich text (links only, moderate length)
        - ``"interactive"`` – card with full markdown rendering
        """
        stripped = content.strip()

        # Complex markdown (code blocks, tables, headings) → always card
        if cls._COMPLEX_MD_RE.search(stripped):
            return "interactive"

        # Long content → card (better readability with card layout)
        if len(stripped) > cls._POST_MAX_LEN:
            return "interactive"

        # Has bold/italic/strikethrough → card (post format can't render these)
        if cls._SIMPLE_MD_RE.search(stripped):
            return "interactive"

        # Has list items → card (post format can't render list bullets well)
        if cls._LIST_RE.search(stripped) or cls._OLIST_RE.search(stripped):
            return "interactive"

        # Has links → post format (supports <a> tags)
        if cls._MD_LINK_RE.search(stripped):
            return "post"

        # Short plain text → text format
        if len(stripped) <= cls._TEXT_MAX_LEN:
            return "text"

        # Medium plain text without any formatting → post format
        return "post"

    @classmethod
    def _markdown_to_post(
        cls, content: str, *, mention_open_id: str | None = None
    ) -> str:
        """Convert markdown content to Feishu post message JSON.

        Handles links ``[text](url)`` as ``a`` tags; everything else as ``text`` tags.
        Each line becomes a paragraph (row) in the post body.
        """
        lines = content.strip().split("\n")
        paragraphs: list[list[dict]] = []

        if mention_open_id:
            paragraphs.append(
                [
                    {"tag": "at", "user_id": mention_open_id},
                    {"tag": "text", "text": " "},
                ]
            )

        for line in lines:
            elements: list[dict] = []
            last_end = 0

            for m in cls._MD_LINK_RE.finditer(line):
                # Text before this link
                before = line[last_end : m.start()]
                if before:
                    elements.append({"tag": "text", "text": before})
                elements.append(
                    {
                        "tag": "a",
                        "text": m.group(1),
                        "href": m.group(2),
                    }
                )
                last_end = m.end()

            # Remaining text after last link
            remaining = line[last_end:]
            if remaining:
                elements.append({"tag": "text", "text": remaining})

            # Empty line → empty paragraph for spacing
            if not elements:
                elements.append({"tag": "text", "text": ""})

            paragraphs.append(elements)

        post_body = {
            "zh_cn": {
                "content": paragraphs,
            }
        }
        return json.dumps(post_body, ensure_ascii=False)

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
    _AUDIO_EXTS = {".opus"}
    _VIDEO_EXTS = {".mp4", ".mov", ".avi"}
    _FILE_TYPE_MAP = {
        ".opus": "opus",
        ".mp4": "mp4",
        ".pdf": "pdf",
        ".doc": "doc",
        ".docx": "doc",
        ".xls": "xls",
        ".xlsx": "xls",
        ".ppt": "ppt",
        ".pptx": "ppt",
    }

    def _upload_image_sync(self, file_path: str) -> str | None:
        """Upload an image to Feishu and return the image_key."""
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        try:
            with open(file_path, "rb") as f:
                request = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder().image_type("message").image(f).build()
                    )
                    .build()
                )
                response = self._client.im.v1.image.create(request)
                if response.success():
                    image_key = response.data.image_key
                    self.logger.debug("Uploaded image {}: {}", os.path.basename(file_path), image_key)
                    return image_key
                else:
                    self.logger.error(
                        "Failed to upload image: code={}, msg={}", response.code, response.msg
                    )
                    return None
        except Exception:
            self.logger.exception("Error uploading image {}", file_path)
            return None

    def _upload_file_sync(self, file_path: str) -> str | None:
        """Upload a file to Feishu and return the file_key."""
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        ext = os.path.splitext(file_path)[1].lower()
        file_type = self._FILE_TYPE_MAP.get(ext, "stream")
        file_name = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                request = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(f)
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.file.create(request)
                if response.success():
                    file_key = response.data.file_key
                    self.logger.debug("Uploaded file {}: {}", file_name, file_key)
                    return file_key
                else:
                    self.logger.error(
                        "Failed to upload file: code={}, msg={}", response.code, response.msg
                    )
                    return None
        except Exception:
            self.logger.exception("Error uploading file {}", file_path)
            return None

    def _download_image_sync(
        self, message_id: str, image_key: str
    ) -> tuple[bytes | None, str | None]:
        """Download an image from Feishu message by message_id and image_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(image_key)
                .type("image")
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                # GetMessageResourceRequest returns BytesIO, need to read bytes
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                self.logger.error(
                    "Failed to download image: code={}, msg={}", response.code, response.msg
                )
                return None, None
        except Exception:
            self.logger.exception("Error downloading image {}", image_key)
            return None, None

    def _download_file_sync(
        self, message_id: str, file_key: str, resource_type: str = "file"
    ) -> tuple[bytes | None, str | None]:
        """Download a file/audio/media from a Feishu message by message_id and file_key."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        # Feishu resource download API only accepts 'image' or 'file' as type.
        # Both 'audio' and 'media' (video) messages use type='file' for download.
        if resource_type in ("audio", "media"):
            resource_type = "file"

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                self.logger.error(
                    "Failed to download {}: code={}, msg={}",
                    resource_type,
                    response.code,
                    response.msg,
                )
                return None, None
        except Exception:
            self.logger.exception("Error downloading {} {}", resource_type, file_key)
            return None, None

    @staticmethod
    def _safe_media_filename(filename: str | None, fallback: str) -> str:
        """Return a local-only filename for downloaded Feishu media."""
        candidate = filename or fallback
        # Feishu/Lark filenames come from message metadata. Treat both POSIX
        # and Windows separators as path boundaries before applying the shared
        # filename sanitizer so downloads cannot escape the channel media dir.
        candidate = os.path.basename(candidate.replace("\\", "/"))
        candidate = safe_filename(candidate)
        if candidate in ("", ".", ".."):
            return safe_filename(fallback) or uuid.uuid4().hex
        return candidate

    async def _download_and_save_media(
        self, msg_type: str, content_json: dict, message_id: str | None = None
    ) -> tuple[str | None, str]:
        """
        Download media from Feishu and save to local disk.

        Returns:
            (file_path, content_text) - file_path is None if download failed
        """
        loop = asyncio.get_running_loop()
        media_dir = get_media_dir("feishu")

        data, filename = None, None
        fallback_filename = uuid.uuid4().hex

        if msg_type == "image":
            image_key = content_json.get("image_key")
            if image_key and message_id:
                fallback_filename = f"{image_key[:16]}.jpg"
                data, filename = await loop.run_in_executor(
                    None, self._download_image_sync, message_id, image_key
                )
                if not filename:
                    filename = fallback_filename

        elif msg_type in ("audio", "file", "media"):
            file_key = content_json.get("file_key")
            if not file_key:
                self.logger.warning("{} message missing file_key: {}", msg_type, content_json)
                return None, f"[{msg_type}: missing file_key]"
            if not message_id:
                self.logger.warning("{} message missing message_id", msg_type)
                return None, f"[{msg_type}: missing message_id]"

            fallback_filename = file_key[:16]
            data, filename = await loop.run_in_executor(
                None, self._download_file_sync, message_id, file_key, msg_type
            )

            if not data:
                self.logger.warning("{} download failed: file_key={}", msg_type, file_key)
                return None, f"[{msg_type}: download failed]"

            if not filename:
                filename = fallback_filename

            # Feishu voice messages are opus in OGG container.
            # Use .ogg extension for better Whisper compatibility.
            if msg_type == "audio":
                if not any(filename.endswith(ext) for ext in (".opus", ".ogg", ".oga")):
                    filename = f"{filename}.ogg"

        if data and filename:
            filename = self._safe_media_filename(filename, fallback_filename)
            file_path = media_dir / filename
            file_path.write_bytes(data)
            path_str = str(file_path)
            self.logger.debug("Downloaded {} to {}", msg_type, path_str)
            return path_str, f"[{msg_type}: {path_str}]"

        return None, f"[{msg_type}: download failed]"

    _REPLY_CONTEXT_MAX_LEN = 200

    def _get_message_content_sync(self, message_id: str) -> str | None:
        """Fetch the text content of a Feishu message by ID (synchronous).

        Returns a "[Reply to: ...]" context string, or None on failure.
        """
        from lark_oapi.api.im.v1 import GetMessageRequest

        try:
            request = GetMessageRequest.builder().message_id(message_id).build()
            response = self._client.im.v1.message.get(request)
            if not response.success():
                self.logger.debug(
                    "could not fetch parent message {}: code={}, msg={}",
                    message_id,
                    response.code,
                    response.msg,
                )
                return None
            items = getattr(response.data, "items", None)
            if not items:
                return None
            msg_obj = items[0]
            raw_content = getattr(msg_obj, "body", None)
            raw_content = getattr(raw_content, "content", None) if raw_content else None
            if not raw_content:
                return None
            try:
                content_json = json.loads(raw_content)
            except (json.JSONDecodeError, TypeError):
                return None
            msg_type = getattr(msg_obj, "msg_type", "")
            if msg_type == "text":
                text = content_json.get("text", "").strip()
            elif msg_type == "post":
                text, _ = _extract_post_content(content_json)
                text = text.strip()
            else:
                text = ""
            if not text:
                return None
            if len(text) > self._REPLY_CONTEXT_MAX_LEN:
                text = text[: self._REPLY_CONTEXT_MAX_LEN] + "..."
            return f"[Reply to: {text}]"
        except Exception as e:
            self.logger.debug("error fetching parent message {}: {}", message_id, e)
            return None

    def get_chat_history_sync(
        self,
        chat_id: str,
        limit: int | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        *,
        thread_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch messages in a chat or topic visible to the bot (synchronous).

        Returns a list of ``{sender_id, msg_type, text, create_time_ms, image_keys, file_key,
        file_name}`` dicts ordered oldest → newest. Only works for chats the bot is a member
        of, and only covers messages the bot is authorised to read.

        When ``thread_id`` is provided, Feishu's ``thread`` container is used;
        normal chat history only exposes topic root messages in message-mode groups.

        All visible pages are fetched. For a chat container, every discovered topic is
        expanded through its ``thread_id`` and merged with ordinary chat messages.
        ``start_time_ms`` / ``end_time_ms`` are applied locally because Feishu does not
        support time filters for thread containers. ``limit`` remains only for backwards
        call compatibility and is intentionally ignored.
        """
        from lark_oapi.api.im.v1 import ListMessageRequest

        try:
            container_type = "thread" if thread_id else "chat"
            container_id = thread_id or chat_id
            out: list[dict[str, Any]] = []
            page_token: str | None = None

            while True:
                builder = (
                    ListMessageRequest.builder()
                    .container_id_type(container_type)
                    .container_id(container_id)
                    .page_size(50)
                    .sort_type("ByCreateTimeDesc")
                    .with_sender_name(True)
                )
                if page_token:
                    builder.page_token(page_token)
                response = self._client.im.v1.message.list(builder.build())
                if not response.success():
                    self.logger.warning(
                        "list chat history failed: code={}, msg={}", response.code, response.msg
                    )
                    break
                items = getattr(response.data, "items", None) or []
                if not items:
                    break

                for msg_obj in items:
                    ct_ms = getattr(msg_obj, "create_time", None)
                    try:
                        ct_ms = int(ct_ms) if ct_ms is not None else None
                    except (TypeError, ValueError):
                        ct_ms = None
                    sender = getattr(msg_obj, "sender", None)
                    raw_sender_id = getattr(sender, "id", None)
                    sender_id = (
                        raw_sender_id
                        if isinstance(raw_sender_id, str)
                        else getattr(raw_sender_id, "open_id", None)
                    ) or "unknown"
                    raw_sender_name = getattr(sender, "sender_name", None)
                    sender_name = raw_sender_name.strip() if isinstance(raw_sender_name, str) else ""
                    body = getattr(msg_obj, "body", None)
                    raw_content = getattr(body, "content", None) if body else None
                    content_json: dict = {}
                    if raw_content:
                        try:
                            content_json = json.loads(raw_content)
                        except (json.JSONDecodeError, TypeError):
                            content_json = {}
                    msg_type = getattr(msg_obj, "msg_type", "") or ""
                    text = ""
                    image_keys: list[str] = []
                    file_key = None
                    file_name = None
                    if msg_type == "text":
                        text = content_json.get("text", "").strip()
                    elif msg_type == "post":
                        text, image_keys = _extract_post_content(content_json)
                        text = text.strip()
                    elif msg_type == "image":
                        image_keys = [content_json.get("image_key", "")] if content_json.get(
                            "image_key"
                        ) else []
                    elif msg_type == "file":
                        file_key = content_json.get("file_key")
                        file_name = content_json.get("file_name")
                        text = "[file: {}]".format(file_name or file_key or "?")
                    elif msg_type in ("share_chat", "share_user", "interactive",
                                       "share_calendar_event", "system", "merge_forward"):
                        text = _extract_share_card_content(content_json, msg_type)
                    else:
                        text = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")
                    out.append(
                        {
                            "message_id": getattr(msg_obj, "message_id", None),
                            "root_id": getattr(msg_obj, "root_id", None),
                            "parent_id": getattr(msg_obj, "parent_id", None),
                            "thread_id": getattr(msg_obj, "thread_id", None),
                            "sender_id": sender_id,
                            "sender_name": sender_name,
                            "msg_type": msg_type,
                            "text": text,
                            "create_time_ms": ct_ms,
                            "image_keys": image_keys,
                            "file_key": file_key,
                            "file_name": file_name,
                        }
                    )

                has_more = getattr(response.data, "has_more", False)
                page_token = getattr(response.data, "page_token", None) or None
                if not has_more or not page_token:
                    break

            if not thread_id:
                topic_ids = sorted({m["thread_id"] for m in out if m.get("thread_id")})
                for topic_id in topic_ids:
                    out.extend(
                        self.get_chat_history_sync(
                            chat_id,
                            None,
                            None,
                            None,
                            thread_id=topic_id,
                        )
                    )

            deduped: dict[str, dict[str, Any]] = {}
            anonymous: list[dict[str, Any]] = []
            for item in out:
                message_id = item.get("message_id")
                if message_id:
                    deduped[message_id] = item
                else:
                    anonymous.append(item)
            out = list(deduped.values()) + anonymous
            if start_time_ms is not None:
                out = [m for m in out if (m.get("create_time_ms") or 0) >= start_time_ms]
            if end_time_ms is not None:
                out = [m for m in out if (m.get("create_time_ms") or 0) <= end_time_ms]
            out.sort(key=lambda m: m["create_time_ms"] or 0)  # oldest first
            return out
        except Exception as e:
            self.logger.exception(
                "error fetching {} history for {}: {}",
                container_type,
                container_id,
                e,
            )
            return []

    _USER_NAME_CACHE_TTL_S = 600

    def _get_user_name_sync(self, open_id: str) -> str:
        """Resolve an open_id to a display name via contacts API (cached 10 min)."""
        if not open_id or open_id == "unknown":
            return ""
        cache = getattr(self, "_user_name_cache", None)
        if cache is None:
            cache = {}
            self._user_name_cache = cache
        import time as _time

        hit = cache.get(open_id)
        if hit is not None and (_time.time() - hit[1]) < self._USER_NAME_CACHE_TTL_S:
            return hit[0]
        name = ""
        try:
            from lark_oapi.api.contact.v3 import GetUserRequest

            request = GetUserRequest.builder().user_id_type("open_id").user_id(open_id).build()
            response = self._client.contact.v3.user.get(request)
            if response.success() and response.data and response.data.user:
                user = response.data.user
                raw_name = getattr(user, "name", None)
                name = raw_name.strip() if isinstance(raw_name, str) else ""
        except Exception as e:
            self.logger.debug("resolve user name failed for {}: {}", open_id, e)
        cache[open_id] = (name, _time.time())
        return name

    # ---- Feishu Docs (docx) & Sheets fetchers ----------------------------

    def get_docx_raw_content_sync(self, doc_token: str) -> tuple[str, str]:
        """Fetch a Feishu cloud docx's plain-text content.

        Returns ``(title, text)``; on failure ``title`` is empty and ``text`` carries
        an error description.
        """
        try:
            import lark_oapi as lark

            # raw_content endpoint: GET /open-apis/docx/v1/documents/{document_id}/raw_content
            request = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri(f"/open-apis/docx/v1/documents/{doc_token}/raw_content")
                .token_types({lark.AccessTokenType.TENANT})
                .build()
            )
            response = self._client.request(request)
            if response.success():
                payload = json.loads(response.raw.content)
                data = payload.get("data") or {}
                return str(data.get("title") or ""), str(data.get("content") or "")
            return "", f"error code={response.code} msg={response.msg}"
        except Exception as e:
            self.logger.exception("error fetching docx {}: {}", doc_token, e)
            return "", f"exception: {e}"

    def get_sheet_values_sync(self, sheet_token: str, range_: str = "") -> tuple[str, str]:
        """Fetch Feishu spreadsheet cell values as CSV-ish text.

        ``range_`` is optional range like ``"<sheetId>!A1:D200"``. When omitted we
        fetch metadata for the first sheet, then read its first ~200 rows × 26 cols.
        Returns ``(title_or_sheet, text)``.
        """
        try:
            import lark_oapi as lark

            if not range_:
                # Query metadata for the first sheet id
                meta_req = (
                    lark.BaseRequest.builder()
                    .http_method(lark.HttpMethod.GET)
                    .uri(f"/open-apis/sheets/v3/spreadsheets/{sheet_token}/sheets/query")
                    .token_types({lark.AccessTokenType.TENANT})
                    .build()
                )
                meta_resp = self._client.request(meta_req)
                if not meta_resp.success():
                    return "", f"meta error code={meta_resp.code} msg={meta_resp.msg}"
                meta = json.loads(meta_resp.raw.content)
                sheets = (meta.get("data") or {}).get("sheets") or []
                if not sheets:
                    return "", "(no sheets in spreadsheet)"
                first = sheets[0]
                sheet_id = first.get("sheet_id")
                title = first.get("title") or ""
                range_ = f"{sheet_id}!A1:Z200"
            else:
                title = range_

            values_req = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri(f"/open-apis/sheets/v2/spreadsheets/{sheet_token}/values/{range_}")
                .token_types({lark.AccessTokenType.TENANT})
                .build()
            )
            resp = self._client.request(values_req)
            if not resp.success():
                return "", f"values error code={resp.code} msg={resp.msg}"
            payload = json.loads(resp.raw.content)
            value_range = (payload.get("data") or {}).get("valueRange") or {}
            values = value_range.get("values") or []
            lines: list[str] = []
            for row in values:
                cells: list[str] = []
                for cell in row:
                    if isinstance(cell, dict):
                        cells.append(str(cell.get("text") or cell.get("link") or ""))
                    elif isinstance(cell, list):
                        cells.append(" ".join(str(x) for x in cell if x))
                    else:
                        cells.append("" if cell is None else str(cell))
                lines.append("\t".join(cells).rstrip())
            return str(title), "\n".join(lines)
        except Exception as e:
            self.logger.exception("error fetching sheet {}: {}", sheet_token, e)
            return "", f"exception: {e}"

    def _reply_message_sync(self, parent_message_id: str, msg_type: str, content: str, *, reply_in_thread: bool = False) -> bool:
        """Reply to an existing Feishu message using the Reply API (synchronous).

        Args:
            reply_in_thread: If True, reply as a thread/topic message
                in the Feishu client.
        """
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        try:
            body_builder = ReplyMessageRequestBody.builder().msg_type(msg_type).content(content)
            if reply_in_thread:
                body_builder = body_builder.reply_in_thread(True)
            request = (
                ReplyMessageRequest.builder()
                .message_id(parent_message_id)
                .request_body(body_builder.build())
                .build()
            )
            response = self._client.im.v1.message.reply(request)
            if not response.success():
                self.logger.error(
                    "Failed to reply to message {}: code={}, msg={}, log_id={}",
                    parent_message_id,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                if msg_type == "interactive":
                    return self._reply_interactive_fallback_sync(
                        parent_message_id,
                        content,
                        reply_in_thread=reply_in_thread,
                    )
                return False
            sent_message_id = getattr(getattr(response, "data", None), "message_id", None)
            self._remember_bot_message(sent_message_id)
            self.logger.debug("reply sent to message {}", parent_message_id)
            return True
        except Exception:
            self.logger.exception("Error replying to message {}", parent_message_id)
            if msg_type == "interactive":
                return self._reply_interactive_fallback_sync(
                    parent_message_id,
                    content,
                    reply_in_thread=reply_in_thread,
                )
            return False

    @staticmethod
    def _interactive_content_to_text(content: str) -> str | None:
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return None
        parts = [part.strip() for part in _extract_interactive_content(payload) if part.strip()]
        text = "\n".join(parts).strip()
        return text or None

    @staticmethod
    def _fallback_text_chunks(text: str, limit: int = 3500) -> list[str]:
        text = text.strip()
        if not text:
            return []
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, limit)
            if split_at < limit // 2:
                split_at = limit
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        return [chunk for chunk in chunks if chunk]

    def _reply_interactive_fallback_sync(
        self,
        parent_message_id: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> bool:
        text = self._interactive_content_to_text(content)
        if not text:
            return False
        sent = False
        for chunk in self._fallback_text_chunks(text):
            body = json.dumps({"text": chunk}, ensure_ascii=False)
            sent = self._reply_message_sync(
                parent_message_id,
                "text",
                body,
                reply_in_thread=reply_in_thread,
            ) or sent
        if sent:
            self.logger.warning("Sent Feishu interactive reply as text fallback")
        return sent

    def _send_interactive_fallback_sync(
        self,
        receive_id_type: str,
        receive_id: str,
        content: str,
    ) -> str | None:
        text = self._interactive_content_to_text(content)
        if not text:
            return None
        last_message_id: str | None = None
        for chunk in self._fallback_text_chunks(text):
            body = json.dumps({"text": chunk}, ensure_ascii=False)
            message_id = self._send_message_sync(receive_id_type, receive_id, "text", body)
            if message_id:
                last_message_id = message_id
        if last_message_id:
            self.logger.warning("Sent Feishu interactive message as text fallback")
        return last_message_id

    def _should_use_reply_in_thread(self, metadata: dict[str, Any]) -> bool:
        """Return whether a group reply should create a Feishu thread/topic."""
        return (
            metadata.get("chat_type", "group") == "group"
            and self.config.reply_to_message
        )

    def _thread_reply_target(self, metadata: dict[str, Any]) -> str | None:
        """Return the message_id that should receive a Reply API response."""
        message_id = metadata.get("message_id")
        if not message_id:
            return None
        if metadata.get("chat_type", "group") != "group":
            return message_id if self.config.reply_to_message else None
        if (
            metadata.get("thread_id")
            or metadata.get("root_id")
            or self.config.reply_to_message
            or self.config.quote_group_replies
        ):
            return message_id
        return None

    def _thread_mention_open_id(self, metadata: dict[str, Any]) -> str | None:
        if not self.config.mention_thread_sender:
            return None
        if metadata.get("chat_type", "group") != "group":
            return None
        if not metadata.get("thread_id"):
            return None
        sender_id = metadata.get("sender_open_id")
        return sender_id if isinstance(sender_id, str) and sender_id.startswith("ou_") else None

    def _send_message_sync(
        self, receive_id_type: str, receive_id: str, msg_type: str, content: str
    ) -> str | None:
        """Send a single message and return the message_id on success."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)
            if not response.success():
                self.logger.error(
                    "Failed to send {} message: code={}, msg={}, log_id={}",
                    msg_type,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                if msg_type == "interactive":
                    return self._send_interactive_fallback_sync(
                        receive_id_type,
                        receive_id,
                        content,
                    )
                return None
            msg_id = getattr(response.data, "message_id", None)
            self._remember_bot_message(msg_id)
            self.logger.debug("{} message sent to {}: {}", msg_type, receive_id, msg_id)
            return msg_id
        except Exception:
            self.logger.exception("Error sending {} message", msg_type)
            return None

    def _create_streaming_card_sync(
        self,
        receive_id_type: str,
        chat_id: str,
        reply_message_id: str | None = None,
        *,
        reply_in_thread: bool = False,
    ) -> str | None:
        """Create a CardKit streaming card, send it to chat, return card_id.

        When *reply_message_id* is provided the card is delivered via the
        reply API. *reply_in_thread* controls whether Feishu creates a
        thread/topic for that reply. Otherwise the plain create-message API is
        used.
        """
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody

        card_json = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True, "streaming_mode": True},
            "body": {
                "elements": [{"tag": "markdown", "content": "", "element_id": _STREAM_ELEMENT_ID}]
            },
        }
        try:
            request = (
                CreateCardRequest.builder()
                .request_body(
                    CreateCardRequestBody.builder()
                    .type("card_json")
                    .data(json.dumps(card_json, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card.create(request)
            if not response.success():
                self.logger.warning(
                    "Failed to create streaming card: code={}, msg={}", response.code, response.msg
                )
                return None
            card_id = getattr(response.data, "card_id", None)
            if card_id:
                card_content = json.dumps(
                    {"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False
                )
                if reply_message_id:
                    sent = self._reply_message_sync(
                        reply_message_id, "interactive", card_content,
                        reply_in_thread=reply_in_thread,
                    )
                else:
                    sent = self._send_message_sync(
                        receive_id_type, chat_id, "interactive", card_content,
                    ) is not None
                if sent:
                    return card_id
                self.logger.warning(
                    "Created streaming card {} but failed to send it to {}", card_id, chat_id
                )
            return None
        except Exception as e:
            self.logger.warning("Error creating streaming card: {}", e)
            return None

    def _stream_update_text_sync(self, card_id: str, content: str, sequence: int) -> bool:
        """Stream-update the markdown element on a CardKit card (typewriter effect)."""
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        try:
            request = (
                ContentCardElementRequest.builder()
                .card_id(card_id)
                .element_id(_STREAM_ELEMENT_ID)
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(content)
                    .sequence(sequence)
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card_element.content(request)
            if not response.success():
                self.logger.warning(
                    "Failed to stream-update card {}: code={}, msg={}",
                    card_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            self.logger.warning("Error stream-updating card {}: {}", card_id, e)
            return False

    def _set_streaming_mode_sync(self, card_id: str, enabled: bool, sequence: int) -> bool:
        """Set CardKit streaming_mode using a strictly increasing sequence."""
        from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody

        settings_payload = json.dumps({"config": {"streaming_mode": enabled}}, ensure_ascii=False)
        try:
            request = (
                SettingsCardRequest.builder()
                .card_id(card_id)
                .request_body(
                    SettingsCardRequestBody.builder()
                    .settings(settings_payload)
                    .sequence(sequence)
                    .uuid(str(uuid.uuid4()))
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card.settings(request)
            if not response.success():
                self.logger.warning(
                    "Failed to set streaming={} on card {}: code={}, msg={}",
                    enabled,
                    card_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            self.logger.warning("Error setting streaming={} on card {}: {}", enabled, card_id, e)
            return False

    def _close_streaming_mode_sync(self, card_id: str, sequence: int) -> bool:
        """Turn off CardKit streaming_mode so the chat list preview exits the streaming placeholder.

        Per Feishu docs, streaming cards keep a generating-style summary in the session list until
        streaming_mode is set to false via card settings (after final content update).
        Sequence must strictly exceed the previous card OpenAPI operation on this entity.
        """
        return self._set_streaming_mode_sync(card_id, False, sequence)

    def _stream_update_text_with_reopen_sync(
        self,
        card_id: str,
        content: str,
        sequence: int,
    ) -> tuple[bool, int]:
        if self._stream_update_text_sync(card_id, content, sequence):
            return True, sequence
        sequence += 1
        if not self._set_streaming_mode_sync(card_id, True, sequence):
            return False, sequence
        sequence += 1
        return self._stream_update_text_sync(card_id, content, sequence), sequence

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
        *,
        stream_id: str | None = None,
        stream_end: bool = False,
        resuming: bool = False,
    ) -> None:
        """Progressive streaming via CardKit: create card on first delta, stream-update on subsequent.

        Supported metadata keys:
            message_id:  Original message id (used with stream end for reaction cleanup).
            chat_type:   "group" or "p2p" — controls reply-in-thread for streaming cards.
        """
        if not self._client:
            return
        meta = metadata or {}
        stream_key = self._stream_key(chat_id, meta)
        loop = asyncio.get_running_loop()
        rid_type = "chat_id" if chat_id.startswith("oc_") else "open_id"

        # --- stream end: final update or fallback ---
        if stream_end:
            message_id = meta.get("message_id")
            # Only finalize the OnIt -> DONE reaction transition on the truly
            # final stream end. resuming=True means the agent will keep
            # working (more tool-call rounds), so leave the reaction state
            # in place — otherwise the OnIt indicator disappears prematurely
            # and the DONE reaction fires after every tool call.
            if message_id and not resuming:
                reaction_id = self._reaction_ids.pop(message_id, None)
                if reaction_id:
                    await self._remove_reaction(message_id, reaction_id)
                # Add completion emoji if configured
                if self.config.done_emoji:
                    await self._add_reaction(message_id, self.config.done_emoji)

            buf = self._stream_bufs.pop(stream_key, None)
            if not buf or not buf.text:
                return
            # Try to finalize via streaming card; if that fails (e.g.
            # streaming mode was closed by Feishu due to timeout), fall
            # back to sending a regular interactive card.
            if buf.card_id:
                buf.sequence += 1
                ok, buf.sequence = await loop.run_in_executor(
                    None,
                    self._stream_update_text_with_reopen_sync,
                    buf.card_id,
                    buf.text,
                    buf.sequence,
                )
                if ok:
                    buf.sequence += 1
                    closed = await loop.run_in_executor(
                        None,
                        self._close_streaming_mode_sync,
                        buf.card_id,
                        buf.sequence,
                    )
                    if not closed:
                        buf.sequence += 1
                        await loop.run_in_executor(
                            None,
                            self._close_streaming_mode_sync,
                            buf.card_id,
                            buf.sequence,
                        )
                    return
                buf.sequence += 1
                await loop.run_in_executor(
                    None,
                    self._close_streaming_mode_sync,
                    buf.card_id,
                    buf.sequence,
                )
                self.logger.warning(
                    "Streaming card {} final update failed, falling back to regular card",
                    buf.card_id,
                )
            for chunk in self._split_elements_by_table_limit(
                self._build_card_elements(buf.text)
            ):
                card = json.dumps(
                    {"config": {"wide_screen_mode": True}, "elements": chunk},
                    ensure_ascii=False,
                )
                # Fallback replies stay in existing topics, but only create a
                # new topic when reply-to-message is enabled.
                fallback_msg_id = self._thread_reply_target(meta)
                if fallback_msg_id:
                    await loop.run_in_executor(
                        None, lambda: self._reply_message_sync(
                            fallback_msg_id, "interactive", card,
                            reply_in_thread=self._should_use_reply_in_thread(meta),
                        ),
                    )
                else:
                    await loop.run_in_executor(
                        None, self._send_message_sync, rid_type, chat_id, "interactive", card
                    )
            return

        # --- accumulate delta ---
        buf = self._stream_bufs.get(stream_key)
        if buf is None:
            mention_open_id = self._thread_mention_open_id(meta)
            prefix = f"<at id={mention_open_id}></at>\n\n" if mention_open_id else ""
            buf = _FeishuStreamBuf(text=prefix)
            self._stream_bufs[stream_key] = buf
        buf.text += delta
        if not buf.text.strip():
            return

        now = time.monotonic()
        if buf.card_id is None:
            # Use the Reply API for existing topics, and only create new topics
            # when reply-to-message is enabled.
            use_reply_in_thread = self._should_use_reply_in_thread(meta)
            reply_msg_id = self._thread_reply_target(meta)
            card_id = await loop.run_in_executor(
                None,
                lambda: self._create_streaming_card_sync(
                    rid_type,
                    chat_id,
                    reply_msg_id,
                    reply_in_thread=use_reply_in_thread,
                ),
            )
            if card_id:
                ok, sequence = await loop.run_in_executor(
                    None, self._stream_update_text_with_reopen_sync, card_id, buf.text, 1
                )
                if ok:
                    buf.card_id = card_id
                    buf.sequence = sequence
                    buf.last_edit = now
                else:
                    await loop.run_in_executor(
                        None, self._close_streaming_mode_sync, card_id, sequence + 1
                    )
        elif (now - buf.last_edit) >= self._STREAM_EDIT_INTERVAL:
            ok, buf.sequence = await loop.run_in_executor(
                None,
                self._stream_update_text_with_reopen_sync,
                buf.card_id,
                buf.text,
                buf.sequence + 1,
            )
            if ok:
                buf.last_edit = now
            else:
                buf.sequence += 1
                await loop.run_in_executor(
                    None,
                    self._close_streaming_mode_sync,
                    buf.card_id,
                    buf.sequence,
                )
                buf.card_id = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu, including media (images/files) if present."""
        if not self._client:
            self.logger.warning("client not initialized")
            return

        try:
            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()

            # Handle tool hint messages.  When a streaming card is active for
            # this chat, inline the hint into the card instead of sending a
            # separate message so the user experience stays cohesive.
            progress_event = msg.event if isinstance(msg.event, ProgressEvent) else None

            if progress_event and progress_event.tool_hint:
                hint = (msg.content or "").strip()
                if not hint:
                    return
                buf = self._stream_bufs.get(self._stream_key(msg.chat_id, msg.metadata))
                if buf and buf.card_id:
                    # Delegate to send_delta so tool hints get the same
                    # throttling (and card creation) as regular text deltas.
                    await self.send_delta(
                        msg.chat_id,
                        "\n\n" + self._format_tool_hint_delta(hint) + "\n\n",
                        metadata=msg.metadata,
                    )
                    return
                # No active streaming card — send as a regular interactive card
                # with the same 🔧 prefix style. Existing topics stay threaded;
                # new topics are created only when reply-to-message is enabled.
                card = json.dumps(
                    {"config": {"wide_screen_mode": True}, "elements": [
                        {"tag": "markdown", "content": self._format_tool_hint_delta(hint)},
                    ]},
                    ensure_ascii=False,
                )
                _th_msg_id = self._thread_reply_target(msg.metadata)
                if _th_msg_id:
                    await loop.run_in_executor(
                        None, lambda: self._reply_message_sync(
                            _th_msg_id, "interactive", card,
                            reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                        ),
                    )
                else:
                    await loop.run_in_executor(
                        None, self._send_message_sync, receive_id_type, msg.chat_id, "interactive", card
                    )
                return

            if (
                msg.content.strip() == "New session started."
                and msg.metadata.get("chat_type") == "p2p"
                and not msg.media
                and not msg.buttons
            ):
                return

            # Determine whether the first message should quote the user's message.
            # Only the very first send (media or text) in this call uses reply; subsequent
            # chunks/media fall back to plain create to avoid redundant quote bubbles.
            # Always target message_id — the Feishu Reply API keeps replies in the
            # same topic automatically when the target message is inside a topic.
            reply_message_id = (
                self._thread_reply_target(msg.metadata)
                if progress_event is None
                else None
            )
            in_topic = bool(msg.metadata.get("thread_id"))
            mention_open_id = self._thread_mention_open_id(msg.metadata)

            first_send = True  # tracks whether the reply has already been used

            def _do_send(m_type: str, content: str) -> None:
                """Send via reply (first message) or create (subsequent).

                Group chats only set reply_in_thread=True when
                reply_to_message is enabled; otherwise a Reply API call for an
                existing topic must not create a new topic.
                """
                nonlocal first_send
                if reply_message_id:
                    # If we're in a topic, always use reply to stay in the topic
                    if in_topic:
                        ok = self._reply_message_sync(
                            reply_message_id, m_type, content,
                            reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                        )
                        if ok:
                            return
                    elif first_send:
                        # If we're not in a topic but replying to message, only first uses reply
                        first_send = False
                        ok = self._reply_message_sync(
                            reply_message_id, m_type, content,
                            reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                        )
                        if ok:
                            return
                    # Fall back to regular send if reply fails
                message_id = self._send_message_sync(
                    receive_id_type,
                    msg.chat_id,
                    m_type,
                    content,
                )
                if not message_id:
                    raise RuntimeError(f"Feishu {m_type} message was not delivered")

            for file_path in msg.media:
                if not os.path.isfile(file_path):
                    self.logger.warning("Media file not found: {}", file_path)
                    continue
                ext = os.path.splitext(file_path)[1].lower()
                if ext in self._IMAGE_EXTS:
                    key = await loop.run_in_executor(None, self._upload_image_sync, file_path)
                    if key:
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            "image",
                            json.dumps({"image_key": key}, ensure_ascii=False),
                        )
                else:
                    key = await loop.run_in_executor(None, self._upload_file_sync, file_path)
                    if key:
                        # Feishu's OpenAPI names video messages "media".
                        # Use "audio" for audio, "media" for video, "file" for documents.
                        # Feishu requires these specific msg_types for inline playback.
                        if ext in self._AUDIO_EXTS:
                            media_type = "audio"
                        elif ext in self._VIDEO_EXTS:
                            media_type = "media"
                        else:
                            media_type = "file"
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            media_type,
                            json.dumps({"file_key": key}, ensure_ascii=False),
                        )

            agent_ui = msg.metadata.get(OUTBOUND_META_AGENT_UI)
            structured_cards = self._build_agent_ui_cards(agent_ui, msg)
            if structured_cards:
                for card in structured_cards:
                    await loop.run_in_executor(
                        None,
                        _do_send,
                        "interactive",
                        json.dumps(card, ensure_ascii=False),
                    )
                return

            if msg.content and msg.content.strip():
                fmt = self._detect_msg_format(msg.content)

                if fmt == "text":
                    # Short plain text – send as simple text message
                    text = msg.content.strip()
                    if mention_open_id:
                        text = f'<at user_id="{mention_open_id}">用户</at> {text}'
                    text_body = json.dumps({"text": text}, ensure_ascii=False)
                    await loop.run_in_executor(None, _do_send, "text", text_body)

                elif fmt == "post":
                    # Medium content with links – send as rich-text post
                    post_body = self._markdown_to_post(
                        msg.content, mention_open_id=mention_open_id
                    )
                    await loop.run_in_executor(None, _do_send, "post", post_body)

                else:
                    # Complex / long content – send as interactive card
                    elements = self._build_card_elements(msg.content)
                    if mention_open_id:
                        elements.insert(
                            0,
                            {
                                "tag": "markdown",
                                "content": f"<at id={mention_open_id}></at>",
                            },
                        )
                    for chunk in self._split_elements_by_table_limit(elements):
                        card = {"config": {"wide_screen_mode": True}, "elements": chunk}
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            "interactive",
                            json.dumps(card, ensure_ascii=False),
                        )

        except Exception:
            self.logger.exception("Error sending message")
            raise

    @staticmethod
    def _card_callback_response(kind: str, content: str) -> Any:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        return P2CardActionTriggerResponse(
            {"toast": {"type": kind, "content": content}}
        )

    @staticmethod
    def _normalize_card_action_value(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            with suppress(ValueError):
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
        return {}

    @staticmethod
    def _interaction_id_from_option(value: Any) -> str:
        raw = str(value or "")
        return raw.split(":", 1)[0] if ":" in raw else ""

    async def _resume_tool_interaction(
        self,
        state: _FeishuCardInteraction,
        tool_name: str,
        params: dict[str, Any],
        content: str,
    ) -> None:
        metadata = dict(state.metadata)
        metadata.pop(OUTBOUND_META_AGENT_UI, None)
        metadata[INBOUND_META_DIRECT_TOOL] = {
            "name": tool_name,
            "params": params,
        }
        metadata["direct_request_text"] = content
        await self._handle_message(
            sender_id=state.owner_open_id,
            chat_id=state.chat_id,
            content=content,
            metadata=metadata,
            is_dm=metadata.get("chat_type") == "p2p",
        )

    async def _resume_card_interaction(
        self,
        state: _FeishuCardInteraction,
        params: dict[str, Any],
    ) -> None:
        await self._resume_tool_interaction(
            state,
            "magik_cube_daily_report",
            params,
            "继续生成 Magik Cube 交互式报表",
        )

    def _schedule_tool_resume(
        self,
        state: _FeishuCardInteraction,
        tool_name: str,
        params: dict[str, Any],
        content: str,
    ) -> bool:
        if not self._loop or not self._loop.is_running():
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._resume_tool_interaction(state, tool_name, params, content), self._loop
        )

        def log_failure(done: Any) -> None:
            with suppress(Exception):
                exc = done.exception()
                if exc:
                    self.logger.warning(
                        "Report card callback failed: tool={} error_type={}",
                        tool_name,
                        type(exc).__name__,
                    )

        future.add_done_callback(log_failure)
        return True

    def _schedule_card_resume(
        self, state: _FeishuCardInteraction, params: dict[str, Any]
    ) -> bool:
        return self._schedule_tool_resume(
            state,
            "magik_cube_daily_report",
            params,
            "继续生成 Magik Cube 交互式报表",
        )

    def _on_card_action_sync(self, data: Any) -> Any:
        """Validate a Feishu card action and enqueue the next read-only tool stage."""

        try:
            event = getattr(data, "event", None)
            action = getattr(event, "action", None)
            operator = getattr(event, "operator", None)
            operator_open_id = str(getattr(operator, "open_id", "") or "")
            value = self._normalize_card_action_value(getattr(action, "value", None))
            options = self._callback_option_values(getattr(action, "options", None))
            option = getattr(action, "option", None)
            if option and not options:
                options = self._callback_option_values(option)
            raw_form_value = getattr(action, "form_value", None)
            form_value = raw_form_value if isinstance(raw_form_value, dict) else {}
            resume_tool_name = "magik_cube_daily_report"
            resume_content = "继续生成 Magik Cube 交互式报表"
            interaction_id = str(value.get("interaction_id") or "")
            if not interaction_id and options:
                interaction_id = self._interaction_id_from_option(options[0])
            if not interaction_id:
                return self._card_callback_response("error", "无效的报表操作")

            self.logger.debug(
                "Magik card callback received: name={} action={} option_count={} form_fields={}",
                str(getattr(action, "name", "") or ""),
                str(value.get("action") or ""),
                len(options),
                sorted(str(item) for item in form_value),
            )

            with self._card_interaction_lock:
                state = self._card_interactions.get(interaction_id)
                if state is None or state.expires_at <= time.monotonic():
                    self._card_interactions.pop(interaction_id, None)
                    return self._card_callback_response("error", "选择卡已过期，请重新发起报表")
                if not state.owner_open_id or operator_open_id != state.owner_open_id:
                    return self._card_callback_response("error", "只有报表发起人可以操作")
                if state.consumed:
                    return self._card_callback_response("info", "该操作已提交，请勿重复点击")

                if state.phase == "report_document":
                    action_token = str(value.get("action_token") or "")
                    target = state.option_values.get(action_token)
                    if not isinstance(target, dict):
                        return self._card_callback_response("error", "无效的报表操作")
                    resume_tool_name = str(target.get("tool_name") or "")
                    raw_params = target.get("params")
                    if not resume_tool_name or not isinstance(raw_params, dict):
                        return self._card_callback_response("error", "无效的报表操作")
                    params = dict(raw_params)
                    resume_content = str(target.get("content") or "继续执行报表操作")
                    state.consumed = True
                else:
                    params = {}

                if state.phase != "report_document" and "tenants" in form_value:
                    submitted_tenants = self._callback_option_values(form_value["tenants"])
                    selected = [
                        state.option_values[item]
                        for item in submitted_tenants
                        if item in state.option_values
                    ]
                    if len(selected) > state.max_tenants:
                        return self._card_callback_response(
                            "error", f"一次最多选择 {state.max_tenants} 个客户"
                        )
                    state.selected_tenants = selected

                if state.phase != "report_document" and "models" in form_value:
                    submitted_models = self._callback_option_values(form_value["models"])
                    selected_models: dict[str, list[str]] = {}
                    for item in submitted_models:
                        mapped = state.option_values.get(item)
                        if not isinstance(mapped, tuple) or len(mapped) != 2:
                            continue
                        tenant_query, model = mapped
                        selected_models.setdefault(tenant_query, []).append(model)
                    state.selected_models = selected_models

                name = str(getattr(action, "name", "") or "")
                if state.phase == "report_document":
                    pass
                elif name == "tenants":
                    selected = [
                        state.option_values[item]
                        for item in options
                        if item in state.option_values
                    ]
                    if len(selected) > state.max_tenants:
                        return self._card_callback_response(
                            "error", f"一次最多选择 {state.max_tenants} 个客户"
                        )
                    state.selected_tenants = selected
                    return self._card_callback_response(
                        "success", f"已选择 {len(selected)} 个客户"
                    )
                if name == "models":
                    selected_models: dict[str, list[str]] = {}
                    for item in options:
                        mapped = state.option_values.get(item)
                        if not isinstance(mapped, tuple) or len(mapped) != 2:
                            continue
                        tenant_query, model = mapped
                        selected_models.setdefault(tenant_query, []).append(model)
                    state.selected_models = selected_models
                    count = sum(len(items) for items in selected_models.values())
                    return self._card_callback_response("success", f"已选择 {count} 个模型")

                action_name = value.get("action")
                if state.phase == "report_document":
                    pass
                elif state.phase == "subscription":
                    if action_name != "submit_subscription":
                        return self._card_callback_response("error", "未知的订阅操作")
                    params = dict(state.base_params)
                    if "schedule" in form_value:
                        schedule_values = self._callback_option_values(form_value["schedule"])
                        schedule_params = (
                            state.option_values.get(schedule_values[0])
                            if len(schedule_values) == 1
                            else None
                        )
                        if not isinstance(schedule_params, dict):
                            return self._card_callback_response("error", "请选择有效的发送周期")
                        params.update(schedule_params)
                    if "send_time" in form_value:
                        time_values = self._callback_option_values(form_value["send_time"])
                        send_time = (
                            state.option_values.get(time_values[0])
                            if len(time_values) == 1
                            else None
                        )
                        if not isinstance(send_time, str):
                            return self._card_callback_response("error", "请选择有效的发送时间")
                        params["send_time"] = send_time
                    state.consumed = True
                    resume_tool_name = "report_center"
                    resume_content = "创建固定报表订阅"
                elif action_name == "scope":
                    scope = value.get("scope")
                    if scope not in {"summary", "all", "selected"}:
                        return self._card_callback_response("error", "无效的模型范围")
                    if not state.selected_tenants:
                        return self._card_callback_response("error", "请先选择客户")
                    state.consumed = True
                    params = dict(state.base_params)
                    params["interactive"] = scope == "selected"
                    params["report_selections"] = [
                        {
                            "tenant_query": tenant,
                            "model_scope": scope,
                            "models": [],
                        }
                        for tenant in state.selected_tenants
                    ]
                elif action_name == "submit_models":
                    if not state.selected_models:
                        return self._card_callback_response("error", "请先选择模型")
                    missing = [
                        tenant
                        for tenant in state.selected_tenants
                        if not state.selected_models.get(tenant)
                    ]
                    if missing:
                        return self._card_callback_response(
                            "error", "每个客户至少选择一个模型"
                        )
                    state.consumed = True
                    params = dict(state.base_params)
                    params["interactive"] = False
                    params["report_selections"] = [
                        {
                            "tenant_query": tenant,
                            "model_scope": "selected",
                            "models": state.selected_models[tenant],
                        }
                        for tenant in state.selected_tenants
                    ]
                else:
                    return self._card_callback_response("error", "未知的报表操作")

            if state.phase in {"report_document", "subscription"}:
                scheduled = self._schedule_tool_resume(
                    state, resume_tool_name, params, resume_content
                )
            else:
                scheduled = self._schedule_card_resume(state, params)
            if not scheduled:
                with self._card_interaction_lock:
                    state.consumed = False
                return self._card_callback_response("error", "Gateway 当前不可用，请稍后重试")
            if state.phase == "report_document":
                message = "正在处理"
            elif state.phase == "subscription":
                message = "正在创建订阅"
            else:
                message = "正在加载模型" if params.get("interactive") else "正在生成报表"
            return self._card_callback_response("success", message)
        except Exception as exc:
            self.logger.warning("Error processing Feishu card callback: {}", exc)
            return self._card_callback_response("error", "报表操作失败，请重新发起")

    def _on_message_sync(self, data: Any) -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if not self._running:
            return
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """Handle incoming message from Feishu."""
        if not self._running:
            return
        try:
            event = data.event
            message = event.message
            sender = event.sender

            self.logger.debug("raw message: {}", message.content)
            self.logger.debug("mentions: {}", getattr(message, "mentions", None))

            message_id = message.message_id

            # Skip bot messages
            if sender.sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type
            msg_type = message.message_type

            # Early permission check — avoid API lookups and other side effects
            # for unauthorized users. Group chats are silently ignored; DMs
            # get a pairing code.
            if not self.is_allowed(sender_id):
                if chat_type == "p2p":
                    await self._handle_message(
                        sender_id=sender_id,
                        chat_id=sender_id,
                        content="",
                        is_dm=True,
                    )
                return

            if chat_type == "group" and not self._is_group_message_for_bot(message):
                if not await self._is_bot_thread_message(message):
                    self.logger.debug("skipping group message (not mentioned or bot thread)")
                    return

            # Deduplication check
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            # Trim cache
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            # Add reaction (non-blocking — tracked background task)
            task = asyncio.create_task(
                self._add_reaction(message_id, self.config.react_emoji)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)
            task.add_done_callback(lambda t: self._on_reaction_added(message_id, t))

            sender_name = ""
            if self._client:
                sender_name = await asyncio.get_running_loop().run_in_executor(
                    None,
                    self._get_user_name_sync,
                    sender_id,
                )

            sender_context: list[RuntimeContextBlock] = []
            if sender_name:
                identity_json = json.dumps(
                    {"name": sender_name, "open_id": sender_id},
                    ensure_ascii=False,
                )
                sender_context.append(
                    RuntimeContextBlock(
                        source="feishu_sender_identity",
                        content=wrap_runtime_context_lines(
                            [
                                "Current Feishu sender identity (JSON):",
                                identity_json,
                            ]
                        ),
                    )
                )

            # Parse content
            content_parts = []
            media_paths = []

            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            if msg_type == "text":
                text = content_json.get("text", "")
                if text:
                    mentions = getattr(message, "mentions", None)
                    text = self._strip_leading_bot_mention(text, mentions)
                    text = self._resolve_mentions(text, mentions)
                    content_parts.append(text)

            elif msg_type == "post":
                text, image_keys = _extract_post_content(content_json)
                if text:
                    content_parts.append(text)
                # Download images embedded in post
                for img_key in image_keys:
                    file_path, content_text = await self._download_and_save_media(
                        "image", {"image_key": img_key}, message_id
                    )
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type in ("image", "audio", "file", "media"):
                file_path, content_text = await self._download_and_save_media(
                    msg_type, content_json, message_id
                )
                if file_path:
                    media_paths.append(file_path)

                if msg_type == "audio" and file_path:
                    transcription = await self.transcribe_audio(file_path)
                    if transcription:
                        content_text = f"[transcription: {transcription}]"

                content_parts.append(content_text)

            elif msg_type in (
                "share_chat",
                "share_user",
                "interactive",
                "share_calendar_event",
                "system",
                "merge_forward",
            ):
                # Handle share cards and interactive messages
                text = _extract_share_card_content(content_json, msg_type)
                if text:
                    content_parts.append(text)

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            # Extract reply context (parent/root message IDs)
            parent_id = getattr(message, "parent_id", None) or None
            root_id = getattr(message, "root_id", None) or None
            thread_id = getattr(message, "thread_id", None) or None
            direct_request_text = "\n".join(content_parts) if content_parts else ""

            # Prepend quoted message text when the user replied to another message
            if parent_id and self._client:
                loop = asyncio.get_running_loop()
                reply_ctx = await loop.run_in_executor(
                    None, self._get_message_content_sync, parent_id
                )
                if reply_ctx:
                    content_parts.insert(0, reply_ctx)

            content = "\n".join(content_parts) if content_parts else ""

            if not content and not media_paths:
                return

            if chat_type == "p2p" and normalize_command_text(content).lower() == "/new":
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    self._send_message_sync,
                    "open_id",
                    sender_id,
                    "system",
                    _NEW_SESSION_DIVIDER_CONTENT,
                )

            # Build session key for conversation isolation.
            # If topic_isolation is True: each topic gets its own session via root_id/message_id.
            # If topic_isolation is False: all messages in group share the same session.
            # Private chat: no override — same behavior as Telegram/Slack.
            if chat_type == "group":
                if self.config.topic_isolation:
                    session_key = f"{self.name}:{chat_id}:{root_id or message_id}"
                else:
                    session_key = f"{self.name}:{chat_id}"
            else:
                session_key = None

            # Forward to message bus
            reply_to = chat_id if chat_type == "group" else sender_id
            inbound_metadata = {
                "message_id": message_id,
                "chat_type": chat_type,
                "msg_type": msg_type,
                "parent_id": parent_id,
                "root_id": root_id,
                "thread_id": thread_id,
                "sender_open_id": sender_id,
                "sender_name": sender_name,
                "direct_request_text": direct_request_text,
                **({RUNTIME_CONTEXT_INPUT_META: sender_context} if sender_context else {}),
            }
            if chat_type == "group" and _is_group_history_summary_request(direct_request_text):
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=self.name,
                        chat_id=reply_to,
                        content="收到，正在读取群聊及相关话题的完整历史并整理，请稍候。",
                        metadata=inbound_metadata,
                        event=ProgressEvent(tool_hint=True),
                    )
                )
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata=inbound_metadata,
                session_key=session_key,
                is_dm=chat_type == "p2p",
            )

        except Exception:
            self.logger.exception("Error processing message")

    def _on_reaction_created(self, data: Any) -> None:
        """Ignore reaction events so they do not generate SDK noise."""
        pass

    def _on_reaction_deleted(self, data: Any) -> None:
        """Ignore reaction deleted events so they do not generate SDK noise."""
        pass

    def _on_message_read(self, data: Any) -> None:
        """Ignore read events so they do not generate SDK noise."""
        pass

    def _on_bot_p2p_chat_entered(self, data: Any) -> None:
        """Send the versioned report capability home once to an authorized user."""

        if not self._running or not self._loop or not self._loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(
            self._send_report_home_onboarding(data), self._loop
        )

    async def _send_report_home_onboarding(self, data: Any) -> None:
        event = getattr(data, "event", None)
        operator_id = getattr(event, "operator_id", None)
        user_id = str(getattr(operator_id, "open_id", "") or "")
        chat_id = str(getattr(event, "chat_id", "") or user_id)
        if not user_id or not chat_id or not self.is_allowed(user_id):
            return
        from nanobot.config.loader import load_config
        from nanobot.reporting import build_default_registry, configured_report_state_store
        from nanobot.reporting.capabilities import home_document

        config = load_config()
        reporting_config = config.tools.reporting
        if not bool(getattr(reporting_config, "enable", True)):
            return
        version = int(reporting_config.onboarding_version)
        store = configured_report_state_store()
        if store.onboarding_seen(self.name, user_id, version):
            return
        registry = build_default_registry(
            magik_enabled=bool(config.tools.magik_cube.enable)
        )
        document = home_document(
            registry,
            store,
            channel=self.name,
            user_id=user_id,
        )
        await self.send(
            OutboundMessage(
                channel=self.name,
                chat_id=chat_id,
                content=document.fallback_text,
                metadata={
                    "sender_open_id": user_id,
                    "chat_type": "p2p",
                    OUTBOUND_META_AGENT_UI: document.to_agent_ui(),
                },
            )
        )
        store.mark_onboarding_seen(self.name, user_id, version)
        self.logger.info(
            "Report onboarding delivered: channel={} version={}", self.name, version
        )

    @staticmethod
    def _format_tool_hint_lines(tool_hint: str) -> str:
        """Split tool hints across lines on top-level call separators only."""
        parts: list[str] = []
        buf: list[str] = []
        depth = 0
        in_string = False
        quote_char = ""
        escaped = False

        for i, ch in enumerate(tool_hint):
            buf.append(ch)

            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote_char:
                    in_string = False
                continue

            if ch in {'"', "'"}:
                in_string = True
                quote_char = ch
                continue

            if ch == "(":
                depth += 1
                continue

            if ch == ")" and depth > 0:
                depth -= 1
                continue

            if ch == "," and depth == 0:
                next_char = tool_hint[i + 1] if i + 1 < len(tool_hint) else ""
                if next_char == " ":
                    parts.append("".join(buf).rstrip())
                    buf = []

        if buf:
            parts.append("".join(buf).strip())

        return "\n".join(part for part in parts if part)

    def _format_tool_hint_delta(self, tool_hint: str) -> str:
        """Format a tool hint string with the 🔧 prefix for each line."""
        lines = self.__class__._format_tool_hint_lines(tool_hint).split("\n")
        return "\n".join(
            f"{self.config.tool_hint_prefix} {ln}" for ln in lines if ln.strip()
        )
