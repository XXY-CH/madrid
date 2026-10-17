"""CLI front end. Output is JSON by default so agents can parse it.

  python3 -m madrid open --to "+15551234567"
  python3 -m madrid send --chat <id> "hi there" --mention 3:5=+15551234567
  python3 -m madrid pump            # advance lifecycle
  python3 -m madrid transcript --chat <id>
  python3 -m madrid tapback <guid> love
  python3 -m madrid events --since 0
  python3 -m madrid doctor          # environment / capability report
"""
from __future__ import annotations
import argparse
import json
import os
import sys

from . import applescript, capabilities, engine, model, real_format, realbox, rich

DEFAULT_DB = os.environ.get("MADRID_DB", os.path.expanduser("~/.madrid/sim.db"))


def out(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def parse_mentions(items: list[str], text: str):
    ms = []
    for it in items or []:
        try:
            rng, handle = it.split("=", 1)
            start, length = rng.split(":", 1)
        except ValueError:
            raise SystemExit(f"bad --mention {it!r}; expected START:LENGTH=HANDLE")
        ms.append({"handle": handle, "start": int(start), "length": int(length)})
    for m in ms:
        if m["start"] < 0 or m["start"] + m["length"] > len(text):
            raise SystemExit(f"--mention range {m['start']}+{m['length']} outside text")
    return ms


def parse_attachments(items: list[str]):
    atts = []
    for it in items or []:
        parts = dict(kv.split("=", 1) for kv in it.split(",") if "=" in kv)
        atts.append({"kind": parts.get("kind", "file"), "url": parts.get("url"),
                     "filename": parts.get("filename"),
                     "bundle_id": parts.get("bundle_id"),
                     "size": int(parts.get("size", 0)),
                     "transfer_status": parts.get("transfer_status", "preparing")})
    return atts


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="madrid", description="iMessage simulator + real bridge")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--real", action="store_true",
                    help="drive the real Messages.app (also needs MADRID_ALLOW_REAL_SEND=1)")
    ap.add_argument("--me", default="me")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="capability report + send/receive matrix")
    p = sub.add_parser("accounts", help="list accounts (sdef account objects)")
    p = sub.add_parser("chats", help="list chats")
    p = sub.add_parser("open", help="open/create a chat")
    p.add_argument("--to", nargs="+", action="append", required=True,
                   help="handle(s); repeatable, all values accumulate")
    p.add_argument("--name")
    p.add_argument("--service", default="sims", choices=["ssms", "sims", "srcs"])

    p = sub.add_parser("send", help="send a message")
    p.add_argument("--chat", required=True)
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--format", default="plain", choices=list(rich.VALID_FORMATS),
                   help="rich-text carrier of `text` (html/markdown/plain)")
    p.add_argument("--mention", action="append", default=[],
                   help="START:LENGTH=HANDLE, repeatable")
    p.add_argument("--attach", action="append", default=[],
                   help="kind=image,url=...,filename=...,size=...")
    p.add_argument("--effect", default="none", choices=sorted(model.TEXT_EFFECTS))
    p.add_argument("--reply-to")

    p = sub.add_parser("receive", help="inject an incoming message")
    p.add_argument("--chat", required=True)
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--from", dest="sender")
    p.add_argument("--format", default="plain", choices=list(rich.VALID_FORMATS))
    p.add_argument("--effect", default="none", choices=sorted(model.TEXT_EFFECTS))

    p = sub.add_parser("edit", help="edit a sent message (typewriter send side)")
    p.add_argument("guid")
    p.add_argument("text")
    p.add_argument("--format", default="plain", choices=list(rich.VALID_FORMATS))
    p = sub.add_parser("retract", help="retract (unsend) a sent message")
    p.add_argument("guid")
    p = sub.add_parser("edit-history", help="list edit frames of a message")
    p.add_argument("guid")
    p = sub.add_parser("pump", help="advance pending lifecycle transitions")
    p = sub.add_parser("wait", help="pump until settled")
    p.add_argument("--timeout", type=float, default=8.0)
    p = sub.add_parser("read", help="mark a message read")
    p.add_argument("guid")
    p.add_argument("--chat")
    p = sub.add_parser("tapback", help="add a tapback")
    p.add_argument("guid")
    p.add_argument("kind", choices=sorted(model.TAPBACKS))
    p = sub.add_parser("typing", help="typing indicator")
    p.add_argument("--chat", required=True)
    p.add_argument("--handle")
    p.add_argument("--stop", action="store_true")

    p = sub.add_parser("transcript", help="messages in a chat")
    p.add_argument("--chat", required=True)
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("search", help="search message text")
    p.add_argument("query")
    p.add_argument("--chat")
    p = sub.add_parser("events", help="event log (for agent follow)")
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--limit", type=int, default=200)

    p = sub.add_parser("archive", help="archive/unarchive a chat")
    p.add_argument("--chat", required=True)
    p.add_argument("--no", action="store_true")
    p = sub.add_parser("mute", help="mute/unmute a chat")
    p.add_argument("--chat", required=True)
    p.add_argument("--no", action="store_true")

    p = sub.add_parser("real-body", help="read real attributedBody blobs from a real chat.db (read-only)")
    p.add_argument("--source", default=os.path.expanduser("~/Library/Messages/chat.db"),
                   help="a real Messages database; opened immutable=1, never written")
    p.add_argument("--guid", help="one message guid instead of the newest N")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--decode", action="store_true",
                   help="full Foundation decode of attribute runs (NSUnarchiver; own data only)")
    p.add_argument("--with-text", action="store_true",
                   help="include message text in the output (off by default)")

    p = sub.add_parser("real-chats", help="list real chats from chat.db (read-only)")
    p.add_argument("--source", default=os.path.expanduser("~/Library/Messages/chat.db"))
    p.add_argument("--limit", type=int, default=100)

    p = sub.add_parser("real-transcript", help="decode recent real messages (read-only)")
    p.add_argument("--source", default=os.path.expanduser("~/Library/Messages/chat.db"))
    p.add_argument("--chat", help="real chat guid (from real-chats); omit for any chat")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--with-runs", action="store_true",
                   help="include per-run attributes (spawns the Foundation decoder)")

    p = sub.add_parser("send-shortcut", help="real: headless send via `shortcuts run` (needs MADRID_ALLOW_REAL_SEND)")
    p.add_argument("text")
    p.add_argument("--shortcut", default="madrid send")
    p = sub.add_parser("bridge", help="resident in-process bridge (debugger install-once)")
    p.add_argument("action", choices=["status", "install", "ping"])
    p = sub.add_parser("typewriter", help="real: stream frames into ONE message via edits (bridge-first, UI fallback)")
    p.add_argument("--to", required=True)
    p.add_argument("--frames", required=True, help="frames separated by ';;'")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--format", default="plain", choices=list(rich.VALID_FORMATS))
    p.add_argument("--verify-only", action="store_true",
                   help="no edits: open context menu, report the Edit label, Esc")
    p = sub.add_parser("mark-read", help="real: mark a conversation read (peer-visible if receipts on)")
    p.add_argument("--handle", required=True)
    p = sub.add_parser("stats", help="statistics over the real chat.db (read-only)")
    p.add_argument("--by-chat", action="store_true")
    p.add_argument("--top-contacts", action="store_true")
    p.add_argument("--daily", action="store_true")
    p.add_argument("--media", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("contacts", help="resolve handles to contact names")
    p.add_argument("handles", nargs="+")
    p = sub.add_parser("create-chat", help="real: open a chat with a new handle (even never contacted)")
    p.add_argument("handle")
    p = sub.add_parser("real-accounts", help="real: accounts via Messages.app AppleScript")
    p = sub.add_parser("notify", help="send a macOS notification")
    p.add_argument("title")
    p.add_argument("body", nargs="?", default="")

    p = sub.add_parser("rich", help="convert text between rich formats")
    p.add_argument("text")
    p.add_argument("--from", dest="src_fmt", default="markdown",
                   choices=list(rich.VALID_FORMATS))
    p.add_argument("--to", dest="dst_fmt", default="html",
                   choices=list(rich.VALID_FORMATS))

    p = sub.add_parser("serve", help="HTTP + SSE server for agents (REST + OpenAPI)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--real", action="store_true")
    p.add_argument("--watch", action="store_true")
    p = sub.add_parser("watch", help="watch the real inbox and dispatch envelopes")
    p.add_argument("--source", default=os.path.expanduser("~/Library/Messages/chat.db"))
    p.add_argument("--db", default=DEFAULT_DB, help="sim store to ingest into; '' disables")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--once", action="store_true", help="poll once and exit")
    p.add_argument("--backfill", type=int, default=0)
    p.add_argument("--notify", action="store_true")
    p.add_argument("--webhook")
    p.add_argument("--jsonl")
    p.add_argument("--command")

    p = sub.add_parser("login", help="real: login all accounts")
    p = sub.add_parser("logout", help="real: logout all accounts")
    return ap


def run(ap: argparse.ArgumentParser, argv: list[str]) -> None:
    a = ap.parse_args(argv)
    eng = engine.Engine(a.db, mode="real" if a.real else "sim", me=a.me)
    cmd = a.cmd

    if cmd == "doctor":
        av = applescript.available()
        out({
            "mode": eng.mode,
            "db": eng.store.path,
            "sim": True,
            "real_bridge": {
                "osascript": av["osascript"],
                "sdef": av["sdef"],
                "gate_env": av["gate"],
                "enabled": av["osascript"] and av["sdef"] and av["gate"] and a.real,
            },
            "environment": capabilities.environment(),
            "capabilities": capabilities.matrix(),
            "tapbacks": sorted(model.TAPBACKS),
            "text_effects": sorted(model.TEXT_EFFECTS),
            "services": {"ssms": "SMS", "sims": "iMessage", "srcs": "RCS"},
            "states": [s.value for s in model.State],
        })
    elif cmd == "accounts":
        out([eng.account()])
    elif cmd == "chats":
        out(eng.chats())
    elif cmd == "open":
        handles = [h for group in a.to for h in group]
        out(eng.open_chat(handles, name=a.name, service=a.service))
    elif cmd == "send":
        out(eng.send(a.chat, a.text, mentions=parse_mentions(a.mention, a.text),
                     attachments=parse_attachments(a.attach), effect=a.effect,
                     reply_to=a.reply_to, format=a.format))
    elif cmd == "receive":
        out(eng.receive(a.chat, a.text, sender=a.sender, effect=a.effect,
                        format=a.format))
    elif cmd == "edit":
        out(eng.edit_message(a.guid, a.text, format=a.format))
    elif cmd == "retract":
        out(eng.retract_message(a.guid))
    elif cmd == "edit-history":
        out(eng.edit_history(a.guid))
    elif cmd == "pump":
        out(eng.pump())
    elif cmd == "wait":
        out(eng.wait(timeout=a.timeout))
    elif cmd == "read":
        out(eng.mark_read(a.guid))
    elif cmd == "tapback":
        out(eng.tapback(a.guid, a.kind))
    elif cmd == "typing":
        out(eng.set_typing(a.chat, handle=a.handle, typing=not a.stop))
    elif cmd == "transcript":
        out(eng.transcript(a.chat, limit=a.limit))
    elif cmd == "search":
        out(eng.search(a.query, chat_id=a.chat))
    elif cmd == "events":
        out(eng.events(since_seq=a.since, limit=a.limit))
    elif cmd == "archive":
        eng.archive(a.chat, archived=not a.no); out({"chat_id": a.chat, "archived": not a.no})
    elif cmd == "mute":
        eng.mute(a.chat, muted=not a.no); out({"chat_id": a.chat, "muted": not a.no})
    elif cmd == "real-body":
        out(real_format.read_real_db(a.source, guid=a.guid, limit=a.limit,
                                     decode=a.decode, with_text=a.with_text))
    elif cmd == "real-chats":
        inbox = realbox.RealInbox(a.source)
        try:
            out(inbox.chats(limit=a.limit))
        finally:
            inbox.close()
    elif cmd == "real-transcript":
        inbox = realbox.RealInbox(a.source)
        try:
            out(inbox.transcript(a.chat, limit=a.limit, include_runs=a.with_runs))
        finally:
            inbox.close()
    elif cmd == "send-shortcut":
        out(applescript.send_via_shortcuts(a.text, shortcut=a.shortcut))
    elif cmd == "bridge":
        from madrid import bridge
        if a.action == "status":
            out(bridge.status())
        elif a.action == "ping":
            out({"pong": bridge.ping()})
        else:
            out({"result": bridge.install()})
    elif cmd == "typewriter":
        if a.verify_only:
            from madrid import uiedit
            out(uiedit.verify())
        else:
            frames = a.frames.split(";;")
            out(eng.typewriter(a.to, frames, interval=a.interval, format=a.format))
    elif cmd == "mark-read":
        out(applescript.mark_chat_read(a.handle))
    elif cmd == "stats":
        from madrid import stats as stats_mod
        if a.by_chat:
            out(stats_mod.by_chat(limit=a.limit))
        elif a.top_contacts:
            out(stats_mod.top_contacts(limit=a.limit))
        elif a.daily:
            out(stats_mod.daily_volume())
        elif a.media:
            out(stats_mod.media_by_type())
        else:
            out(stats_mod.overview())
    elif cmd == "contacts":
        from madrid.contacts import ContactResolver, format_handle
        cr = ContactResolver()
        out({h: {"name": cr.resolve(h), "formatted": format_handle(h)} for h in a.handles})
    elif cmd == "create-chat":
        subprocess.run(["open", "-g", f"imessage://{a.handle}"], capture_output=True, timeout=10)
        out({"opened": a.handle, "note": "Messages.app creates the chat; poll real-chats for the guid"})
    elif cmd == "real-accounts":
        out(applescript.account_report())
    elif cmd == "notify":
        from . import watch as watch_mod
        watch_mod.notify_mac(a.title, a.body)
        out({"notified": True, "title": a.title, "body": a.body})
    elif cmd == "rich":
        runs, _inline = rich.runs_from(a.text, a.src_fmt)
        if a.dst_fmt == "html":
            out({"format": "html", "text": rich.html_of(runs)})
        elif a.dst_fmt == "markdown":
            out({"format": "markdown", "text": rich.md_of(runs)})
        else:
            out({"format": "plain", "text": rich.plain(runs)})
    elif cmd == "serve":
        from . import serve as serve_mod
        serve_mod.serve(a.host, a.port, db=a.db, real=a.real, watch=a.watch)
    elif cmd == "watch":
        from . import watch as watch_mod
        watch_mod.main(["--source", a.source, "--db", a.db,
                        "--interval", str(a.interval)]
                       + (["--once"] if a.once else [])
                       + (["--backfill", str(a.backfill)] if a.backfill else [])
                       + (["--notify"] if a.notify else [])
                       + (["--webhook", a.webhook] if a.webhook else [])
                       + (["--jsonl", a.jsonl] if a.jsonl else [])
                       + (["--command", a.command] if a.command else []))
    elif cmd in ("login", "logout"):
        if not a.real:
            raise SystemExit(f"{cmd} is a real-app command; pass --real")
        out({"result": getattr(applescript, cmd)()})
    else:  # pragma: no cover
        raise SystemExit(f"unhandled command: {cmd}")


def main(argv: list[str] | None = None) -> None:
    ap = build_parser()
    run(ap, list(sys.argv[1:] if argv is None else argv))
