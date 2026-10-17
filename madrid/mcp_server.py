"""Minimal MCP server over stdio — JSON-RPC 2.0, no third-party SDK.

Lets an agent call the simulator as tools. Start it and point an MCP client
at `python3 -m madrid.mcp_server`.

Register in Qwen Code / Hermes / any MCP client settings under mcpServers, e.g.
  "madrid": { "command": "python3", "args": ["-m", "madrid.mcp_server"],
            "cwd": "/Users/xiexingyu/Documents/Projects/message" }

Protocol implemented: initialize, tools/list, tools/call, ping,
notifications/initialized. Real sends stay behind the same double gate.

Receiving: `watch_start` polls the real chat.db in a background thread and
fans decoded envelopes into the event log (`events`), an optional webhook,
JSONL file or macOS notifications — see docs/agent-integration.md.
"""
from __future__ import annotations
import json
import os
import sys

from . import applescript, capabilities, engine, model, real_format, realbox, rich
from . import watch as watch_mod

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "madrid"
SERVER_VERSION = "0.9.0"

DB = os.environ.get("MADRID_DB", os.path.expanduser("~/.madrid/sim.db"))
ENG = engine.Engine(DB, mode="sim")


def _t(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": desc,
        "inputSchema": {"type": "object", "properties": props,
                       "required": required or [],
                       "additionalProperties": False},
    }


TOOLS = [
    _t("doctor", "Report simulator capabilities, states, tapbacks, effects and whether the real Messages.app bridge is enabled.", {}),
    _t("accounts", "List accounts as the real app's sdef account objects (service type, connection status).", {}),
    _t("chats", "List all chats with handles and last message time.", {}),
    _t("open_chat", "Open or create a chat with one or more handles.",
       {"handles": {"type": "array", "items": {"type": "string"},
                    "description": "Phone numbers or emails"},
        "name": {"type": "string", "description": "Optional group name"},
        "service": {"type": "string", "enum": ["sims", "ssms", "srcs"]}},
       ["handles"]),
    _t("send", "Send a message. `format` selects the rich-text carrier of `text`: plain | html (<b> <i> <u> <s> <a href> <br> <span data-mention> <span data-effect> <img data-attachment>) | markdown (**b** *i* __u__ ~~s~~ [l](url)). Mentions are character ranges into the plain text.",
       {"chat_id": {"type": "string"},
        "text": {"type": "string"},
        "format": {"type": "string", "enum": ["plain", "html", "markdown"],
                   "description": "rich-text carrier of text (default plain)"},
        "mentions": {"type": "array", "items": {"type": "object", "properties": {
            "handle": {"type": "string"}, "start": {"type": "integer"},
            "length": {"type": "integer"}, "type": {"type": "string"}},
            "required": ["handle", "start", "length"], "additionalProperties": False}},
        "attachments": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["file", "image", "video", "audio",
                                                "sticker", "appStrip", "location"]},
            "url": {"type": "string"}, "filename": {"type": "string"},
            "bundle_id": {"type": "string"}, "size": {"type": "integer"}},
            "additionalProperties": False}},
        "effect": {"type": "string", "enum": sorted(model.TEXT_EFFECTS)},
        "reply_to": {"type": "string", "description": "guid of the message being replied to"}},
       ["chat_id"]),
    _t("receive", "Inject an incoming (or with from_me, an outgoing echo) message; same formats as send.",
       {"chat_id": {"type": "string"}, "text": {"type": "string"},
        "sender": {"type": "string"}, "from_me": {"type": "boolean"},
        "format": {"type": "string", "enum": ["plain", "html", "markdown"]},
        "effect": {"type": "string", "enum": sorted(model.TEXT_EFFECTS)}},
       ["chat_id"]),
    _t("pump", "Advance pending outgoing messages one lifecycle step (sending→sent→delivered→read).", {}),
    _t("wait", "Pump until nothing is pending, or timeout.",
       {"timeout": {"type": "number"}}),
    _t("mark_read", "Mark a message read.", {"guid": {"type": "string"}}, ["guid"]),
    _t("tapback", "Add a tapback to a message.",
       {"guid": {"type": "string"},
        "kind": {"type": "string", "enum": sorted(model.TAPBACKS)}}, ["guid", "kind"]),
    _t("edit_message", "Edit a sent outgoing message (the send side of typewriter-via-edits). Enforces the 15-minute protocol window; history keeps the newest 5 frames.",
       {"guid": {"type": "string"}, "text": {"type": "string"},
        "format": {"type": "string", "enum": ["plain", "html", "markdown"]}},
       ["guid", "text"]),
    _t("retract_message", "Retract (unsend) a sent outgoing message within the 2-minute window.",
       {"guid": {"type": "string"}}, ["guid"]),
    _t("edit_history", "List the kept edit frames of a message (newest 5).",
       {"guid": {"type": "string"}}, ["guid"]),
    _t("bridge_status", "Resident in-process bridge status: prerequisites (csrutil debug exemption, DevToolsSecurity) and whether the dispatcher is live.",
       {}),
    _t("bridge_install", "Install the resident dispatcher into Messages (one-time debugger attach, then native-speed socket commands). Needs MADRID_ALLOW_REAL_SEND=1 and the csrutil debug exemption.",
       {}),
    _t("real_typewriter", "Real typewriter: frame 0 is sent, later frames edit the newest bubble so one message 'types itself'. Experimental — run uiedit verify on a self-chat first. Needs MADRID_ALLOW_REAL_SEND=1.",
       {"handle": {"type": "string"},
        "frames": {"type": "array", "items": {"type": "string"}},
        "interval": {"type": "number"}, "format": {"type": "string"}},
       ["handle", "frames"]),
    _t("uiedit_verify", "UI-edit probe: opens the context menu on the newest bubble, reports items incl. the localized Edit label, dismisses without editing.",
       {}),
    _t("real_mark_read", "Real: mark a conversation read (peer sees a receipt only if Send Read Receipts is on). Needs MADRID_ALLOW_REAL_SEND=1.",
       {"handle": {"type": "string"}}, ["handle"]),
    _t("set_typing", "Set or clear the typing indicator.",
       {"chat_id": {"type": "string"}, "handle": {"type": "string"},
        "typing": {"type": "boolean"}}, ["chat_id"]),
    _t("transcript", "Read a chat's transcript, oldest first.",
       {"chat_id": {"type": "string"}, "limit": {"type": "integer"}}, ["chat_id"]),
    _t("search", "Search message text.",
       {"query": {"type": "string"}, "chat_id": {"type": "string"}}, ["query"]),
    _t("events", "Poll the event log since a sequence number — the agent's inbox.",
       {"since_seq": {"type": "integer"}, "limit": {"type": "integer"}}),
    _t("set_archived", "Archive or unarchive a chat.",
       {"chat_id": {"type": "string"}, "archived": {"type": "boolean"}}, ["chat_id"]),
    _t("set_muted", "Mute or unmute a chat.",
       {"chat_id": {"type": "string"}, "muted": {"type": "boolean"}}, ["chat_id"]),
    _t("real_body", "Read real attributedBody blobs from a real chat.db (read-only, immutable=1). Reports the NSArchiver typedstream header, class table and __kIM attribute keys; --decode also returns attribute runs via Foundation. Message text is omitted unless with_text is true.",
       {"source": {"type": "string", "description": "path to a real chat.db"},
        "guid": {"type": "string"}, "limit": {"type": "integer"},
        "decode": {"type": "boolean"}, "with_text": {"type": "boolean"}}),
    _t("real_send", "Send for real through Messages.app. Needs MADRID_ALLOW_REAL_SEND=1; externally visible.",
       {"to": {"type": "string", "description": "Handle, or a chat name for groups"},
        "text": {"type": "string"}, "chat": {"type": "boolean",
                                              "description": "Treat 'to' as a chat name"}},
       ["to", "text"]),
    _t("real_accounts", "Real Apple ID accounts as Messages.app sees them (AppleScript, sdef account objects: id/description/enabled/connection). Login/logout of the Apple ID itself happens in Messages settings; this reports and can activate via login/logout tools.",
       {}),
    _t("login", "Real: log in to all Messages.app accounts (the Apple ID must already be configured in Messages settings).",
       {}),
    _t("logout", "Real: log out of all Messages.app accounts.", {}),
    _t("real_chats", "List real chats from chat.db, newest activity first (read-only): guid, handles, service, name.",
       {"limit": {"type": "integer"}}),
    _t("real_transcript", "Decode recent real messages (read-only) into standard rich envelopes: text + html + attachments + tapbacks + replies + audio + edits + read receipts.",
       {"chat": {"type": "string", "description": "real chat guid from real_chats"},
        "limit": {"type": "integer"},
        "with_runs": {"type": "boolean", "description": "include per-run attributes"}},
       []),
    _t("watch_start", "Start watching the real chat.db in the background. New messages, tapbacks, stickers, replies, edits and retractions are ingested into this server's event log (poll `events`) and optionally pushed to a webhook / JSONL file / macOS notifications.",
       {"interval": {"type": "number"}, "notify": {"type": "boolean"},
        "webhook": {"type": "string", "description": "POST each envelope here"},
        "jsonl": {"type": "string", "description": "append each envelope to this file"},
        "backfill": {"type": "integer", "description": "start N messages back (default 0: only new)"}},
       []),
    _t("watch_stop", "Stop the background chat.db watcher.", {}),
    _t("watch_status", "Watcher status: running, cursor, dispatched count, active sinks.", {}),
    _t("notify", "Send a macOS notification (title + body).",
       {"title": {"type": "string"}, "body": {"type": "string"}}, ["title"]),
    _t("rich_convert", "Convert text between the standard rich formats (plain/html/markdown) and inspect runs.",
       {"text": {"type": "string"}, "from": {"type": "string",
                                             "enum": ["plain", "html", "markdown"]},
        "to": {"type": "string", "enum": ["plain", "html", "markdown"]}},
       ["text"]),
]


_WATCHER: watch_mod.Watcher | None = None


def _get_watcher() -> watch_mod.Watcher:
    global _WATCHER
    if _WATCHER is None:
        inbox = realbox.RealInbox()
        _WATCHER = watch_mod.Watcher(inbox, st=ENG.store, interval=2.0)
    return _WATCHER


def call_tool(name: str, args: dict):
    if name == "doctor":
        av = applescript.available()
        return {"mode": ENG.mode, "db": ENG.store.path,
                "real_bridge_enabled": bool(av["osascript"] and av["sdef"] and av["gate"]),
                "bridge": av,
                "environment": capabilities.environment(),
                "capabilities": capabilities.matrix(),
                "states": [s.value for s in model.State],
                "tapbacks": sorted(model.TAPBACKS),
                "text_effects": sorted(model.TEXT_EFFECTS),
                "services": {"ssms": "SMS", "sims": "iMessage", "srcs": "RCS"}}
    if name == "accounts":
        return [ENG.account()]
    if name == "chats":
        return ENG.chats()
    if name == "open_chat":
        return ENG.open_chat(args["handles"], name=args.get("name"),
                              service=args.get("service", "sims"))
    if name == "send":
        return ENG.send(args["chat_id"], args.get("text", ""),
                        mentions=args.get("mentions") or [],
                        attachments=args.get("attachments") or [],
                        effect=args.get("effect", "none"),
                        reply_to=args.get("reply_to"),
                        format=args.get("format", "plain"))
    if name == "receive":
        return ENG.receive(args["chat_id"], args.get("text", ""),
                           sender=args.get("sender"), effect=args.get("effect", "none"),
                           format=args.get("format", "plain"),
                           from_me=bool(args.get("from_me", False)))
    if name == "pump":
        return ENG.pump()
    if name == "wait":
        return ENG.wait(timeout=float(args.get("timeout", 8.0)))
    if name == "mark_read":
        return ENG.mark_read(args["guid"])
    if name == "tapback":
        return ENG.tapback(args["guid"], args["kind"])
    if name == "edit_message":
        return ENG.edit_message(args["guid"], args.get("text", ""),
                                format=args.get("format", "plain"))
    if name == "retract_message":
        return ENG.retract_message(args["guid"])
    if name == "edit_history":
        return ENG.edit_history(args["guid"])
    if name == "bridge_status":
        from madrid import bridge
        return bridge.status()
    if name == "bridge_install":
        from madrid import bridge
        return {"result": bridge.install()}
    if name == "real_typewriter":
        return ENG.typewriter(args["handle"], args["frames"],
                              interval=float(args.get("interval", 1.0)),
                              format=args.get("format", "plain"))
    if name == "uiedit_verify":
        from madrid import uiedit
        return uiedit.verify()
    if name == "real_mark_read":
        return applescript.mark_chat_read(args["handle"])
    if name == "set_typing":
        return ENG.set_typing(args["chat_id"], handle=args.get("handle"),
                               typing=bool(args.get("typing", True)))
    if name == "transcript":
        return ENG.transcript(args["chat_id"], limit=int(args.get("limit", 50)))
    if name == "search":
        return ENG.search(args["query"], chat_id=args.get("chat_id"))
    if name == "events":
        return ENG.events(since_seq=int(args.get("since_seq", 0)),
                          limit=int(args.get("limit", 200)))
    if name == "set_archived":
        ENG.archive(args["chat_id"], archived=bool(args.get("archived", True)))
        return {"chat_id": args["chat_id"], "archived": args.get("archived", True)}
    if name == "set_muted":
        ENG.mute(args["chat_id"], muted=bool(args.get("muted", True)))
        return {"chat_id": args["chat_id"], "muted": args.get("muted", True)}
    if name == "real_body":
        return real_format.read_real_db(
            args.get("source") or os.path.expanduser("~/Library/Messages/chat.db"),
            guid=args.get("guid"), limit=int(args.get("limit", 5)),
            decode=bool(args.get("decode")), with_text=bool(args.get("with_text")))
    if name == "real_send":
        if args.get("chat"):
            return {"result": applescript.send_text_to_chat(args["text"], chat_name=args["to"])}
        return {"result": applescript.send_text(args["text"], to=args["to"])}
    if name == "real_accounts":
        return applescript.account_report()
    if name == "login":
        return {"result": applescript.login()}
    if name == "logout":
        return {"result": applescript.logout()}
    if name == "real_chats":
        inbox = realbox.RealInbox()
        try:
            return inbox.chats(limit=int(args.get("limit", 100)))
        finally:
            inbox.close()
    if name == "real_transcript":
        inbox = realbox.RealInbox()
        try:
            return inbox.transcript(args.get("chat"), limit=int(args.get("limit", 25)),
                                    include_runs=bool(args.get("with_runs")))
        finally:
            inbox.close()
    if name == "watch_start":
        w = _get_watcher()
        w.interval = float(args.get("interval", 2.0))
        w.on_notify = bool(args.get("notify", False))
        w.webhook_url = args.get("webhook") or w.webhook_url
        w.jsonl_path = args.get("jsonl") or w.jsonl_path
        if int(args.get("backfill", 0)) > 0:
            w.cursor = max(0, w.cursor - int(args["backfill"]))
        return w.start()
    if name == "watch_stop":
        return _WATCHER.stop() if _WATCHER is not None else {"running": False}
    if name == "watch_status":
        return _WATCHER.status() if _WATCHER is not None else {"running": False}
    if name == "notify":
        watch_mod.notify_mac(str(args.get("title", "madrid")),
                             str(args.get("body", "")))
        return {"notified": True}
    if name == "rich_convert":
        src = args.get("from", "markdown")
        runs, _inline = rich.runs_from(args["text"], src)
        dst = args.get("to", "html")
        text = {"html": rich.html_of, "markdown": rich.md_of}.get(
            dst, rich.plain)(runs)
        return {"from": src, "to": dst, "text": text,
                "plain": rich.plain(runs),
                "runs": [{"text": r.text, "attributes": r.attributes}
                         for r in rich.merge(runs)]}
    raise ValueError(f"unknown tool: {name}")


def _read_message(stream) -> dict | None:
    """Read one newline-delimited JSON-RPC message (lenient about framing)."""
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        return msg if isinstance(msg, dict) else None


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def _result(mid, result) -> None:
    _write({"jsonrpc": "2.0", "id": mid,
            "result": {"content": [{"type": "text",
                                    "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                      "isError": False}})


def _error(mid, code: int, text: str) -> None:
    _write({"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": text}})


def serve(stdin=None, stdout=None) -> None:
    stream = stdin or sys.stdin
    sink = stdout or sys.stdout

    def write(obj: dict) -> None:
        sink.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
        sink.flush()

    def raw_result(mid, payload) -> None:
        write({"jsonrpc": "2.0", "id": mid, "result": payload})

    def tool_result(mid, payload) -> None:
        write({"jsonrpc": "2.0", "id": mid,
              "result": {"content": [{"type": "text",
                                      "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                        "isError": False}})

    def fail(mid, code: int, text: str) -> None:
        write({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}})

    while True:
        msg = _read_message(stream)
        if msg is None:
            return
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            raw_result(mid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            raw_result(mid, {})
        elif method == "tools/list":
            raw_result(mid, {"tools": TOOLS})
        elif method == "tools/call":
            args = msg.get("arguments") or {}
            try:
                tool_result(mid, call_tool(msg.get("name", ""), args))
            except Exception as exc:  # surface the failure, keep the server alive
                fail(mid, -32603, f"{type(exc).__name__}: {exc}")
        elif mid is not None:
            fail(mid, -32601, f"method not found: {method}")


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
