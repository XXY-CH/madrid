"""Watch the real inbox and push decoded envelopes to agent-visible sinks.

This is the "收通知" half of the bridge: a pull-based poller (deterministic,
no FSEvents race) over chat.db that turns every new message, tapback,
sticker, edit, retraction or system event into a standard rich envelope and
fans it out to:

  store    the local sim store's event log (`events` tool / `madrid events`)
           — always on when a store is given; the agent's inbox
  notify   a macOS notification via osascript (opt-in; shows previews)
  webhook  HTTP POST of the envelope JSON (opt-in; e.g. Hermes ingest)
  jsonl    append one JSON line to a file (opt-in)
  command  run a command with the envelope JSON as $MADRID_EVENT (opt-in)

Polling, not push: macOS gives third-party processes no supported push
channel for new iMessages; chat.db polling with an immutable connection is
the reliable, read-only mechanism (see README "真实边界").
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
from typing import Any, Callable

from . import applescript, model, realbox, rich, store as store_mod


def notify_mac(title: str, body: str) -> None:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{esc(body)}" with title "{esc(title)}"'],
            capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def post_webhook(url: str, payload: dict[str, Any], *, timeout: float = 5.0) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=timeout)
    except OSError:
        pass            # sinks must never kill the watch loop


def append_jsonl(path: str, payload: dict[str, Any]) -> None:
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def run_command(command: str, payload: dict[str, Any]) -> None:
    env = dict(os.environ, MADRID_EVENT=json.dumps(payload, ensure_ascii=False))
    try:
        subprocess.run(command, shell=True, env=env, capture_output=True,
                       timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass


class Watcher:
    """Poll realbox and fan envelopes out. Thread-safe start/stop so an MCP
    server can host one in the background."""

    def __init__(self, inbox: realbox.RealInbox, *, st: store_mod.Store | None = None,
                 on_notify: bool = False, webhook_url: str | None = None,
                 jsonl_path: str | None = None, command: str | None = None,
                 interval: float = 2.0, sink: Callable[[dict[str, Any]], None] | None = None):
        self.inbox = inbox
        self.store = st
        self.on_notify = on_notify
        self.webhook_url = webhook_url
        self.jsonl_path = jsonl_path
        self.command = command
        self.interval = interval
        self.sink = sink
        self.cursor = inbox.cursor()
        self.last_update_ms = int(time.time() * 1000)
        self.seen_guids: set[str] = set()
        self._edit_snapshot: dict[str, int] = {}   # guid -> edited+retracted stamp
        self._receipt_snapshot: dict = {}          # guid -> (read, delivered) ms
        self.count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ---- one poll -------------------------------------------------------
    def poll(self) -> list[dict[str, Any]]:
        """Fetch everything new once. Returns the envelopes, oldest first."""
        envs = self.inbox.messages_since(self.cursor, include_runs=False)
        updates = self.inbox.updates_since(self.last_update_ms)
        try:
            edit_events, self._edit_snapshot = self.inbox.edits_since_snapshot(
                self._edit_snapshot)
        except Exception:
            edit_events = []
        try:
            receipt_events, self._receipt_snapshot = \
                self.inbox.receipts_since_snapshot(self._receipt_snapshot)
        except Exception:
            receipt_events = []
        now_ms = int(time.time() * 1000)
        fresh = []
        for env in envs:
            self.cursor = max(self.cursor, env["rowid"])
            if env["guid"] in self.seen_guids:
                continue
            self.seen_guids.add(env["guid"])
            fresh.append(env)
        for env in updates:
            if env["guid"] not in self.seen_guids:
                self.seen_guids.add(env["guid"])
                env = {**env, "kind": "updated"}
                fresh.append(env)
        for env in edit_events:
            # edit frames are re-dispatches of the SAME guid on purpose —
            # the typewriter stream — so they bypass seen_guids dedupe
            fresh.append({**env, "kind": "edit-frame"})
        for env in receipt_events:
            # receipt updates land on already-seen rows on purpose too
            fresh.append({**env, "kind": "receipt"})
        self.last_update_ms = now_ms
        for env in fresh:
            self.dispatch(env)
        self.count += len(fresh)
        return fresh

    def dispatch(self, env: dict[str, Any]) -> None:
        kind = env.get("kind") or "new"
        if self.store is not None:
            self._ingest(env, kind)
            self.store.log_event(
                f"real.{'updated' if kind == 'updated' else _event_kind(env)}",
                guid=env.get("guid"), chat_id=(env.get("chat") or {}).get("guid"),
                payload=_slim(env))
        payload = {**env, "event": kind}
        if self.sink is not None:
            try:
                self.sink(payload)
            except Exception:
                pass
        if self.on_notify:
            title = ((env.get("chat") or {}).get("name")
                     or (env.get("chat") or {}).get("handles")
                     or ["iMessage"])[0]
            if isinstance(title, list):
                title = title[0] if title else "iMessage"
            notify_mac(str(title), realbox.preview(env))
        if self.webhook_url:
            post_webhook(self.webhook_url, payload)
        if self.jsonl_path:
            append_jsonl(self.jsonl_path, payload)
        if self.command:
            run_command(self.command, payload)

    # ---- store ingestion --------------------------------------------------
    def _ingest(self, env: dict[str, Any], kind: str) -> None:
        chat = env.get("chat") or {}
        chat_id = chat.get("guid") or f"real-{env.get('rowid')}"
        handles = [h for h in (chat.get("handles") or []) if h]
        if handles:
            self.store.upsert_chat(chat_id, account_guid="real",
                                   service=env.get("service") or model.SERVICE_IMESSAGE,
                                   name=chat.get("name"), style=2 if len(handles) > 1 else 1,
                                   handles=handles)
        runs = [model.Run(text=r["text"], attributes=r.get("attributes") or {})
                for r in env.get("runs") or ([{"text": env.get("text") or ""}]
                                             if env.get("text") else [])]
        atts = [model.Attachment(kind=a.get("kind", "file"), url=a.get("path"),
                                 guid=a.get("guid") or model.new_guid(),
                                 filename=a.get("filename"), size=a.get("size") or 0,
                                 transfer_status=_transfer_status(a))
                for a in env.get("attachments") or []]
        from_me = bool(env.get("from_me"))
        if from_me:
            state = model.State.READ if env.get("read_at") else \
                    model.State.DELIVERED if env.get("delivered_at") else model.State.SENT
        else:
            state = model.State.RECEIVED
        msg = model.Message(
            guid=env["guid"], chat_id=chat_id,
            direction=model.DIRECTION_OUTGOING if from_me else model.DIRECTION_INCOMING,
            service=env.get("service") or model.SERVICE_IMESSAGE,
            state=state, runs=runs, attachments=atts,
            reply_to_guid=(env.get("reply_to") or {}).get("guid"),
            sender=env.get("sender") or ("me" if from_me else None),
            date=env.get("date") or model.now_ms(),
            delivered_date=env.get("delivered_at"), read_date=env.get("read_at"),
            source="real", real_rowid=env.get("rowid"))
        self.store.save_message(msg)

        tb = env.get("tapback")
        if tb and tb.get("target"):
            target = self.store.get_message(tb["target"])
            if target is not None:
                kind_name = tb.get("kind") or "love"
                if kind_name in model.TAPBACKS:
                    self.store.add_tapback(tb["target"], kind_name)

    # ---- lifecycle -----------------------------------------------------
    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:
                pass         # transient sqlite lock / offline db: retry next tick
            self._stop.wait(self.interval)

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._stop.clear()
            self._thread = threading.Thread(target=self.run_forever,
                                            name="madrid-watch", daemon=True)
            self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 2)
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "source": self.inbox.source,
            "cursor": self.cursor,
            "dispatched": self.count,
            "interval": self.interval,
            "sinks": {"store": self.store is not None, "notify": self.on_notify,
                      "webhook": bool(self.webhook_url), "jsonl": bool(self.jsonl_path),
                      "command": bool(self.command)},
        }


def _event_kind(env: dict[str, Any]) -> str:
    t = env.get("type") or "message"
    return {"message": "received"}.get(t, t)


def _slim(env: dict[str, Any]) -> dict[str, Any]:
    keep = ("type", "guid", "rowid", "from_me", "sender", "service", "date",
            "text", "reply_to", "tapback", "attachments", "edited_at",
            "retracted_at", "mention_unseen", "expressive_style", "audio")
    return {k: env[k] for k in keep if k in env}


def _transfer_status(a: dict[str, Any]) -> str:
    state = a.get("transfer_state")
    for name, code in model.TRANSFER_STATUSES.items():
        if a.get("transfer_status") == code:
            return name
    return "finished" if state == 0 else "waiting"


def main(argv: list[str] | None = None) -> None:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        prog="madrid watch", description="watch the real inbox and dispatch envelopes")
    ap.add_argument("--source", default=realbox.DEFAULT_DB)
    ap.add_argument("--db", default=os.environ.get(
        "MADRID_DB", os.path.expanduser("~/.madrid/sim.db")),
        help="sim store to ingest into (agent inbox); --db '' to disable")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    ap.add_argument("--backfill", type=int, default=0,
                    help="start N messages back instead of at the newest row")
    ap.add_argument("--notify", action="store_true",
                    help="macOS notification for every new envelope")
    ap.add_argument("--webhook")
    ap.add_argument("--jsonl")
    ap.add_argument("--command",
                    help="run command with the envelope JSON in $MADRID_EVENT")
    a = ap.parse_args(argv)

    inbox = realbox.RealInbox(a.source)
    st = store_mod.Store(a.db) if a.db else None
    w = Watcher(inbox, st=st, on_notify=a.notify, webhook_url=a.webhook,
                jsonl_path=a.jsonl, command=a.command, interval=a.interval)
    if a.backfill > 0:
        w.cursor = max(0, w.cursor - a.backfill)
    try:
        if a.once:
            for env in w.poll():
                print(json.dumps(env, ensure_ascii=False))
        else:
            print(json.dumps({"watching": a.source, **w.status()},
                             ensure_ascii=False), flush=True)
            w.run_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
