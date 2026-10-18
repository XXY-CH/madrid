"""JSON-RPC 2.0 over stdio — a long-running API for agents and gateways.

Protocol: one JSON-RPC request per line on stdin, one response per line on
stdout. Notifications (no id) get no response. Compatible with any JSON-RPC
client library.

Start:
    python3 -m madrid rpc

Methods mirror the HTTP API:
    chats.list() → [{id, name, handles, service, ...}]
    chats.open(handles, [name]) → chat object
    messages.transcript(chat_id, [limit]) → [envelope]
    messages.search(query, [chat_id]) → [envelope]
    messages.send(chat_id, text, [format, mentions, attachments, effect, reply_to]) → result
    messages.edit(guid, text, [format]) → result
    messages.retract(guid) → result
    messages.tapback(guid, kind) → result
    messages.markRead(guid) → result
    typing.set(chat_id, [typing]) → result
    events.poll(since_seq, [limit]) → [event]
    stats.overview() / stats.byChat([limit]) / stats.topContacts([limit])
    real.chats([limit]) / real.transcript([chat, limit])
    system.ping() → {pong}
    system.info() → {version, mode, capabilities}
"""
from __future__ import annotations

import json
import os
import sys

from . import __version__, capabilities, model, realbox


def _build_engine():
    from . import engine as engine_mod
    db = os.environ.get("MADRID_DB", os.path.expanduser("~/.madrid/sim.db"))
    mode = "real" if os.environ.get("MADRID_REAL") == "1" else "sim"
    return engine_mod.Engine(db, mode=mode)


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}


def dispatch(eng, method: str, params: dict):
    """Route a JSON-RPC method to engine/realbox calls."""
    p = params or {}

    if method == "system.ping":
        return {"pong": True, "version": __version__}
    if method == "system.info":
        return {"version": __version__, "mode": eng.mode,
                "capabilities": capabilities.matrix()}

    if method == "chats.list":
        return eng.chats()
    if method == "chats.open":
        return eng.open_chat(p["handles"], name=p.get("name"),
                             service=p.get("service", "sims"))

    if method == "messages.transcript":
        return eng.transcript(p["chat_id"], limit=int(p.get("limit", 50)))
    if method == "messages.search":
        return eng.search(p["query"], chat_id=p.get("chat_id"))
    if method == "messages.send":
        return eng.send(p["chat_id"], p.get("text", ""),
                        mentions=p.get("mentions"),
                        attachments=p.get("attachments"),
                        effect=p.get("effect", "none"),
                        reply_to=p.get("reply_to"),
                        format=p.get("format", "plain"))
    if method == "messages.edit":
        return eng.edit_message(p["guid"], p.get("text", ""),
                                format=p.get("format", "plain"))
    if method == "messages.retract":
        return eng.retract_message(p["guid"])
    if method == "messages.tapback":
        return eng.tapback(p["guid"], p["kind"])
    if method == "messages.markRead":
        return eng.mark_read(p["guid"])

    if method == "typing.set":
        return eng.set_typing(p["chat_id"], handle=p.get("handle"),
                              typing=bool(p.get("typing", True)))

    if method == "events.poll":
        return eng.events(since_seq=int(p.get("since_seq", 0)),
                          limit=int(p.get("limit", 200)))

    if method == "stats.overview":
        from . import stats
        return stats.overview()
    if method == "stats.byChat":
        from . import stats
        return stats.by_chat(limit=int(p.get("limit", 20)))
    if method == "stats.topContacts":
        from . import stats
        return stats.top_contacts(limit=int(p.get("limit", 10)))

    if method == "real.chats":
        inbox = realbox.RealInbox()
        try:
            return inbox.chats(limit=int(p.get("limit", 50)))
        finally:
            inbox.close()
    if method == "real.transcript":
        inbox = realbox.RealInbox()
        try:
            return inbox.transcript(p.get("chat"), limit=int(p.get("limit", 25)))
        finally:
            inbox.close()

    if method == "real.markRead":
        from . import applescript
        return applescript.mark_chat_read(p["handle"])

    raise ValueError(f"unknown method: {method}")


def serve(stdin=None, stdout=None) -> None:
    """Main loop: read line-delimited JSON-RPC from stdin, write to stdout."""
    stream = stdin or sys.stdin
    sink = stdout or sys.stdout
    eng = _build_engine()

    while True:
        line = stream.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            sink.write(json.dumps(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": "parse error"}}) + "\n")
            sink.flush()
            continue

        if not isinstance(req, dict):
            continue
        rid = req.get("id")
        method = req.get("method", "")

        if rid is None:
            continue          # notification — no response

        try:
            result = dispatch(eng, method, req.get("params") or {})
            resp = _ok(rid, result)
        except ValueError as e:
            resp = _err(rid, -32602, str(e))
        except NotImplementedError as e:
            resp = _err(rid, -32601, str(e))
        except Exception as e:
            resp = _err(rid, -32603, f"{type(e).__name__}: {e}")

        sink.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sink.flush()


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
