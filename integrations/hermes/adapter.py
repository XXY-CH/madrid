"""iMessage (madrid) platform adapter for the Hermes gateway.

Transport is madrid's HTTP+SSE interface — no UI, no scripting:

  inbound   GET  {MADRID_URL}/events        (SSE; madrid --watch ingests the
                                             real chat.db read-only)
  outbound  POST {MADRID_URL}/send          (markdown doc mode: real rich
                                             text; tables/code → inline PNG)
  optional  POST {MADRID_URL}/typewriter    (edit-frame streaming)
  optional  POST {MADRID_URL}/tapback       (reactions on our messages)

chat_id = the peer handle (E.164 phone or email). Group chats are addressed
by their madrid chat guid. Sessions: one Hermes session per chat_id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms._shared import (
    env_is_connected as _env_is_connected,
    get_scoped_secret as _get_scoped_secret,
    send_error,
)

try:
    import aiohttp
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None
    web = None

logger = logging.getLogger(__name__)

_E164 = re.compile(r"^\+?[0-9]{7,15}$")


def _madrid_url() -> str:
    return (_get_scoped_secret("MADRID_URL", "http://127.0.0.1:8787")
            or "http://127.0.0.1:8787").rstrip("/")


def _send_format() -> str:
    fmt = _get_scoped_secret("MADRID_SEND_FORMAT", "markdown").lower()
    return fmt if fmt in ("markdown", "html", "plain") else "markdown"


def check_imessage_requirements() -> bool:
    return AIOHTTP_AVAILABLE and bool(_madrid_url())


class ImessageAdapter(BasePlatformAdapter):
    """madrid-backed iMessage channel for the Hermes gateway."""

    MAX_MESSAGE_LENGTH = 4000           # iMessage practical text limit
    splits_long_messages = True
    supports_draft_streaming = True     # typewriter edit-frames

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("imessage"))
        self._url = _madrid_url()
        self._session: Optional[aiohttp.ClientSession] = None
        self._sse_task: Optional[asyncio.Task] = None
        self._last_seq = 0

    # -- lifecycle ---------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not AIOHTTP_AVAILABLE:
            self._set_fatal_error("imessage_no_aiohttp",
                                   "[imessage] aiohttp not installed", retryable=False)
            return False
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=10))
            async with self._session.get(f"{self._url}/health",
                                         timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200:
                    raise RuntimeError(f"health {r.status}")
        except Exception as e:
            self._set_fatal_error("imessage_unreachable",
                                  f"[imessage] madrid not reachable at {self._url}: {e}",
                                  retryable=True)
            return False
        # resume from the newest event, don't replay history
        try:
            async with self._session.get(f"{self._url}/events",
                                         timeout=aiohttp.ClientTimeout(total=5)) as r:
                pass    # SSE stays open; seq comes from the first event
        except Exception:
            pass
        self._sse_task = asyncio.create_task(self._sse_loop())
        self._running = True
        self._wire_plugin_handlers(None)
        logger.info("[imessage] connected to madrid at %s (SSE /events)", self._url)
        return True

    async def disconnect(self) -> None:
        if self._sse_task:
            self._sse_task.cancel()
            self._sse_task = None
        if self._session:
            await self._session.close()
            self._session = None
        self._running = False
        logger.info("[imessage] disconnected")

    # -- outbound ----------------------------------------------------------

    async def send(
        self, chat_id: str, content: str, reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send markdown to a chat; madrid converts to rich text natively."""
        if not self._session:
            return SendResult(success=False, error="not connected")
        payload: Dict[str, Any] = {
            "handle": chat_id,
            "text": content,
            "format": _send_format(),
        }
        if reply_to:
            payload["reply_to"] = reply_to
        try:
            async with self._session.post(
                    f"{self._url}/send", json=payload,
                    timeout=aiohttp.ClientTimeout(total=60)) as r:
                body = await r.json()
                if r.status >= 400:
                    return SendResult(success=False,
                                      error=body.get("error", f"madrid {r.status}"))
                return SendResult(success=True, message_id=body.get("guid", ""))
        except Exception as e:
            logger.error("[imessage] send to %s failed: %s", chat_id, e)
            return SendResult(success=False, error=str(e))

    async def send_draft(self, chat_id: str, draft: str, *,
                         draft_id: Optional[str] = None,
                         is_final: bool = False,
                         metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Typewriter streaming: first frame sends, later frames edit."""
        if not self._session:
            return SendResult(success=False, error="not connected")
        tw = _get_scoped_secret("MADRID_TYPEWRITER", "").lower() == "true"
        if not tw:
            if is_final:
                return await self.send(chat_id, draft, metadata=metadata)
            return SendResult(success=True, message_id=draft_id or "")
        payload = {"handle": chat_id, "frames": [draft], "format": _send_format()}
        try:
            async with self._session.post(
                    f"{self._url}/typewriter", json=payload,
                    timeout=aiohttp.ClientTimeout(total=120)) as r:
                body = await r.json()
                if r.status >= 400:
                    return SendResult(success=False,
                                      error=body.get("error", f"madrid {r.status}"))
                results = body.get("results", [])
                return SendResult(success=True,
                                  message_id=str(results[-1].get("frame", "")) if results else "")
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "group" if ";" in chat_id else "dm"}

    def format_message(self, content: str) -> str:
        return content      # madrid's markdown mode handles rich conversion

    # -- inbound (SSE) -------------------------------------------------------

    async def _sse_loop(self) -> None:
        """Consume madrid's /events SSE stream; dispatch inbound messages."""
        assert self._session
        while self._running:
            try:
                async with self._session.get(f"{self._url}/events?since={self._last_seq}") as resp:
                    async for line in resp.content:
                        if not line:
                            continue
                        text = line.decode("utf-8", "replace").strip()
                        if text.startswith("id:"):
                            try:
                                self._last_seq = int(text[3:].strip())
                            except ValueError:
                                pass
                        elif text.startswith("data:"):
                            try:
                                ev = json.loads(text[5:].strip())
                            except json.JSONDecodeError:
                                continue
                            self._dispatch_event(ev)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("[imessage] SSE dropped (%s); reconnecting in 3s", e)
                await asyncio.sleep(3)

    def _dispatch_event(self, ev: Dict[str, Any]) -> None:
        kind = ev.get("kind", "")
        payload = ev.get("payload") or {}
        # skip our own ingested sends and non-message events
        if payload.get("from_me"):
            return
        if kind == "real.received" or (kind == "message.received" and payload.get("source") == "real"):
            text = payload.get("text") or ""
            if not text.strip():
                return
            sender = payload.get("sender") or "unknown"
            chat = payload.get("chat") or {}
            chat_id = (chat.get("handles") or [sender])[0] if chat.get("handles") else sender
            guid = payload.get("guid") or ""
            if not self._is_sender_authorized(sender):
                logger.info("[imessage] unauthorized sender %s dropped", sender)
                return
            source = self.build_source(
                chat_id=chat_id, chat_name=chat.get("name") or chat_id,
                chat_type="dm", user_id=sender, user_name=sender,
                message_id=guid)
            event = MessageEvent(
                text=text, message_type=MessageType.TEXT, source=source,
                raw_message=payload, message_id=guid)
            task = asyncio.create_task(self.handle_message(event))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            logger.info("[imessage] inbound from %s: %s", sender, text[:60])


# -- standalone send + plugin registration --------------------------------------

async def _standalone_send(pconfig, chat_id, message, *, thread_id=None,
                           media_files=None, force_document=False):
    if not AIOHTTP_AVAILABLE:
        return send_error("aiohttp not installed. Run: pip install aiohttp")
    url = _madrid_url()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                    f"{url}/send",
                    json={"handle": chat_id, "text": message,
                          "format": _send_format()},
                    timeout=aiohttp.ClientTimeout(total=60)) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    return send_error(f"madrid error ({resp.status}): {body.get('error')}")
                return {"success": True, "platform": "imessage",
                        "chat_id": chat_id, "message_id": body.get("guid", "")}
    except Exception as e:
        return send_error(f"iMessage send failed: {e}")


_is_connected = _env_is_connected("MADRID_URL")


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="imessage", label="iMessage (madrid)",
        adapter_factory=ImessageAdapter,
        check_fn=check_imessage_requirements,
        is_connected=_is_connected,
        required_env=["MADRID_URL"],
        install_hint="Run madrid: python3 -m madrid serve --watch",
        allowed_users_env="MADRID_ALLOWED_USERS",
        allow_all_env="MADRID_ALLOW_ALL_USERS",
        cron_deliver_env_var="MADRID_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=4000, pii_safe=False,
        emoji="💬", allow_update_command=True)
