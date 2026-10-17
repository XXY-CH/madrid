"""HTTP + SSE server: the REST surface for non-MCP agents (Hermes & co.).

One process, stdlib only:

  python3 -m madrid serve --port 8787 [--real] [--watch]

Routes (OpenAPI 3.1 served at /openapi.json):

  GET  /health                     liveness + mode + version
  GET  /capabilities               feature matrix
  GET  /chats                      list sim chats
  POST /chats                      open/create {handles, name?, service?}
  GET  /chats/{id}/transcript      messages, oldest first (?limit=)
  POST /send                       {chat_id, text, format?, attachments?, ...}
  POST /edit                       {guid, text, format?}      (typewriter)
  POST /retract                    {guid}
  POST /tapback                    {guid, kind}
  GET  /real/chats                 real chats (read-only)      (?limit=)
  GET  /real/transcript            decode recent real messages (?chat=&limit=)
  GET  /events                     SSE stream of the event log
                                    (sim + ingested real events, live)

Real sends keep the double gate: serve with --real AND MADRID_ALLOW_REAL_SEND=1.
--watch additionally ingests the real inbox into the event log (read-only).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, capabilities, engine as engine_mod, model, realbox, rich

STOP = threading.Event()


# ---- OpenAPI -----------------------------------------------------------------

def openapi_spec() -> dict:
    msg_props = {
        "chat_id": {"type": "string"},
        "text": {"type": "string"},
        "format": {"type": "string", "enum": list(rich.VALID_FORMATS),
                   "default": "plain"},
        "mentions": {"type": "array", "items": {"type": "object", "properties": {
            "handle": {"type": "string"}, "start": {"type": "integer"},
            "length": {"type": "integer"}}}},
        "attachments": {"type": "array", "items": {"type": "object"}},
        "effect": {"type": "string"},
        "reply_to": {"type": "string"},
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "madrid", "version": __version__,
                 "description": "iMessage rich-text send/receive bridge for agents"},
        "servers": [{"url": "/"}],
        "paths": {
            "/health": {"get": {"summary": "liveness", "responses": {"200": {"description": "ok"}}}},
            "/capabilities": {"get": {"summary": "feature matrix",
                                      "responses": {"200": {"description": "ok"}}}},
            "/chats": {
                "get": {"summary": "list chats", "responses": {"200": {"description": "ok"}}},
                "post": {"summary": "open/create chat",
                         "requestBody": {"content": {"application/json": {"schema": {
                             "type": "object", "required": ["handles"],
                             "properties": {"handles": {"type": "array", "items": {"type": "string"}},
                                            "name": {"type": "string"},
                                            "service": {"type": "string", "enum": ["sims", "ssms", "srcs"]}}}}}},
                         "responses": {"200": {"description": "ok"}}}},
            "/chats/{id}/transcript": {
                "get": {"summary": "chat transcript (oldest first)",
                        "parameters": [{"name": "id", "in": "path", "schema": {"type": "string"}},
                                       {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}}],
                        "responses": {"200": {"description": "ok"}}}},
            "/send": {"post": {"summary": "send a message",
                       "requestBody": {"content": {"application/json": {"schema": {
                           "type": "object", "required": ["chat_id"],
                           "properties": msg_props}}}},
                       "responses": {"200": {"description": "sent (sim or real)"},
                                     "403": {"description": "real send not gated"}}}},
            "/edit": {"post": {"summary": "edit a sent message (typewriter frame)",
                      "requestBody": {"content": {"application/json": {"schema": {
                          "type": "object", "required": ["guid", "text"],
                          "properties": {"guid": {"type": "string"}, "text": {"type": "string"},
                                         "format": msg_props["format"]}}}}},
                      "responses": {"200": {"description": "ok"}}}},
            "/retract": {"post": {"summary": "retract (unsend) a sent message",
                         "requestBody": {"content": {"application/json": {"schema": {
                             "type": "object", "required": ["guid"],
                             "properties": {"guid": {"type": "string"}}}}}},
                         "responses": {"200": {"description": "ok"}}}},
            "/typewriter": {"post": {"summary": "stream frames into ONE real message via edits (experimental)",
                           "requestBody": {"content": {"application/json": {"schema": {
                               "type": "object", "required": ["handle", "frames"],
                               "properties": {"handle": {"type": "string"},
                                   "frames": {"type": "array", "items": {"type": "string"}},
                                   "interval": {"type": "number", "default": 1.0},
                                   "format": {"type": "string", "enum": ["plain", "html", "markdown"]}}}}}},
                           "responses": {"200": {"description": "ok"},
                                         "403": {"description": "not in real mode"}}}},
            "/mark-read": {"post": {"summary": "mark a conversation read (peer-visible if the user sends read receipts)",
                          "requestBody": {"content": {"application/json": {"schema": {
                              "type": "object", "required": ["handle"],
                              "properties": {"handle": {"type": "string"}}}}}},
                          "responses": {"200": {"description": "ok"},
                                        "403": {"description": "not in real mode"}}}},
            "/tapback": {"post": {"summary": "add a tapback",
                         "requestBody": {"content": {"application/json": {"schema": {
                             "type": "object", "required": ["guid", "kind"],
                             "properties": {"guid": {"type": "string"},
                                            "kind": {"type": "string", "enum": sorted(model.TAPBACKS)}}}}}},
                         "responses": {"200": {"description": "ok"}}}},
            "/real/chats": {"get": {"summary": "real chats (read-only)",
                            "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                            "responses": {"200": {"description": "ok"}}}},
            "/real/transcript": {"get": {"summary": "decode recent real messages",
                                "parameters": [{"name": "chat", "in": "query", "schema": {"type": "string"}},
                                               {"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                                "responses": {"200": {"description": "ok"}}}},
            "/events": {"get": {"summary": "SSE stream of the event log",
                        "responses": {"200": {"description": "text/event-stream"}}}},
        },
    }


# ---- handler -------------------------------------------------------------------

def make_handler(eng: engine_mod.Engine) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        engine = eng

        def log_message(self, fmt, *args):          # quiet
            pass

        # -- helpers
        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _q(self) -> dict:
            return {k: v[0] for k, v in
                    urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}

        # -- routing
        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            q = self._q()
            try:
                if path == "/health":
                    return self._json({"ok": True, "mode": self.engine.mode,
                                       "version": __version__})
                if path == "/capabilities":
                    return self._json({"environment": capabilities.environment(),
                                       "capabilities": capabilities.matrix()})
                if path == "/openapi.json":
                    return self._json(openapi_spec())
                if path == "/chats":
                    return self._json(self.engine.chats())
                if path.startswith("/chats/") and path.endswith("/transcript"):
                    chat_id = urllib.parse.unquote(path[len("/chats/"):-len("/transcript")])
                    return self._json(self.engine.transcript(chat_id,
                                                             limit=int(q.get("limit", 50))))
                if path == "/stats":
                    from madrid import stats as stats_mod
                    sub = q.get("sub")
                    if sub == "by-chat":
                        return self._json(stats_mod.by_chat(limit=int(q.get("limit", 20))))
                    if sub == "contacts":
                        return self._json(stats_mod.top_contacts(limit=int(q.get("limit", 10))))
                    if sub == "daily":
                        return self._json(stats_mod.daily_volume())
                    if sub == "media":
                        return self._json(stats_mod.media_by_type())
                    return self._json(stats_mod.overview())
                if path == "/contacts":
                    from madrid.contacts import ContactResolver, format_handle
                    cr = ContactResolver()
                    handles = q.get("handles", "").split(",") if q.get("handles") else []
                    return self._json({h: {"name": cr.resolve(h), "formatted": format_handle(h)}
                                       for h in handles if h.strip()})
                if path == "/real/chats":
                    inbox = realbox.RealInbox()
                    try:
                        return self._json(inbox.chats(limit=int(q.get("limit", 100))))
                    finally:
                        inbox.close()
                if path == "/real/transcript":
                    inbox = realbox.RealInbox()
                    try:
                        return self._json(inbox.transcript(q.get("chat"),
                                                           limit=int(q.get("limit", 25))))
                    finally:
                        inbox.close()
                if path == "/events":
                    return self._sse()
                return self._json({"error": "not found", "path": path}, 404)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            try:
                b = self._body()
                if path == "/chats":
                    return self._json(self.engine.open_chat(b["handles"], name=b.get("name"),
                                                            service=b.get("service", "sims")))
                if path == "/send":
                    return self._json(self.engine.send(
                        b["chat_id"], b.get("text", ""), mentions=b.get("mentions"),
                        attachments=b.get("attachments"), effect=b.get("effect", "none"),
                        reply_to=b.get("reply_to"), format=b.get("format", "plain")))
                if path == "/edit":
                    return self._json(self.engine.edit_message(
                        b["guid"], b.get("text", ""), format=b.get("format", "plain")))
                if path == "/retract":
                    return self._json(self.engine.retract_message(b["guid"]))
                if path == "/typewriter":
                    if self.engine.mode != "real":
                        return self._json({"error": "serve with --real"}, 403)
                    return self._json(self.engine.typewriter(
                        b["handle"], b["frames"],
                        interval=float(b.get("interval", 1.0)),
                        format=b.get("format", "plain")))
                if path == "/mark-read":
                    from . import applescript as _as
                    if self.engine.mode != "real":
                        return self._json({"error": "serve with --real to mark read"}, 403)
                    return self._json(_as.mark_chat_read(b["handle"]))
                if path == "/tapback":
                    return self._json(self.engine.tapback(b["guid"], b["kind"]))
                return self._json({"error": "not found", "path": path}, 404)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)

        # -- SSE: tail the event log
        def _sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            last = int(self._q().get("since", 0))
            try:
                while not STOP.is_set():
                    events = self.engine.events(since_seq=last, limit=100)
                    for e in events:
                        last = max(last, e["seq"])
                        payload = json.dumps(e, ensure_ascii=False)
                        self.wfile.write(f"id: {e['seq']}\ndata: {payload}\n\n".encode())
                    if events:
                        self.wfile.flush()
                    else:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    STOP.wait(1.0)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8787, *, db: str | None = None,
          real: bool = False, watch: bool = False) -> None:
    db = db or os.environ.get("MADRID_DB", os.path.expanduser("~/.madrid/sim.db"))
    eng = engine_mod.Engine(db, mode="real" if real else "sim")
    if watch:
        from . import watch as watch_mod
        inbox = realbox.RealInbox()
        watch_mod.Watcher(inbox, st=eng.store, interval=2.0).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(eng))
    print(json.dumps({"serving": f"http://{host}:{port}", "mode": eng.mode,
                      "events": "/events (SSE)", "spec": "/openapi.json",
                      "watch": watch}, ensure_ascii=False), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        httpd.server_close()


def main(argv: list[str] | None = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="madrid serve",
                                 description="HTTP + SSE bridge for agents")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--db", default=None)
    ap.add_argument("--real", action="store_true",
                    help="real send mode (also needs MADRID_ALLOW_REAL_SEND=1)")
    ap.add_argument("--watch", action="store_true",
                    help="also ingest the real inbox into the event log (read-only)")
    a = ap.parse_args(argv)
    serve(a.host, a.port, db=a.db, real=a.real, watch=a.watch)


if __name__ == "__main__":
    main()
