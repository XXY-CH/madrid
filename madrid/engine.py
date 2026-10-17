"""The simulator engine.

Two modes:
  sim   (default) — everything local; no network, no real message leaves
          the machine. State transitions follow the lifecycle observed in
          ChatKit/IMCore (pending -> sending -> sent -> delivered -> read).
  real            — hands the text to the actual Messages.app through
          AppleScript (`send <text> to <participant|chat>`, from
          Messages.sdef) so behaviour is the real app's, not an imitation.
          Off unless explicitly requested; the app may need Accessibility
          permission and will focus the foreground window.

State advancement is done by pump(), not by background threads, so an
agent's calls are deterministic and replayable.
"""
from __future__ import annotations
import fnmatch
import os
import random
from typing import Any

from . import applescript, mddoc, model, rich, richtext, store, uiedit

DEFAULT_LATENCY = (0.15, 0.6)      # send -> sent
DEFAULT_DELIVERY = (0.3, 1.4)       # sent -> delivered
DEFAULT_READ = (1.5, 5.0)           # delivered -> read



def _stage_attachment(path: str) -> str:
    """Stage a file into Messages' attachments dir for reliable sending.
    Files outside that dir can hit sandbox-related send failures."""
    staged_dir = os.path.expanduser("~/Library/Messages/Attachments/madrid")
    os.makedirs(staged_dir, exist_ok=True)
    import shutil
    dest = os.path.join(staged_dir, os.path.basename(path))
    if not os.path.exists(dest) or os.path.getsize(dest) != os.path.getsize(path):
        shutil.copy2(path, dest)
    return dest


class Engine:
    def __init__(self, db_path: str, *, mode: str = "sim", me: str = "me",
                 latency: tuple[float, float] = DEFAULT_LATENCY,
                 delivery: tuple[float, float] = DEFAULT_DELIVERY,
                 read: tuple[float, float] = DEFAULT_READ,
                 seed: int | None = None):
        if mode not in ("sim", "real"):
            raise ValueError(f"unknown mode: {mode}")
        self.store = store.Store(db_path)
        self.mode = mode
        self.me = me
        self.latency = latency
        self.delivery = delivery
        self.read = read
        self._rng = random.Random(seed)
        self._pending_after: dict[str, int] = {}   # guid -> ms when it may advance

    # ---- helpers --------------------------------------------------------
    def _after(self, span: tuple[float, float]) -> int:
        return model.now_ms() + int(self._rng.uniform(*span) * 1000)

    def account(self, *, service: str = model.SERVICE_IMESSAGE,
                status: str = "connected") -> dict[str, Any]:
        """Mirror of the sdef `account` object."""
        if status not in model.CONNECTION_STATUSES:
            raise ValueError(f"unknown connection status: {status}")
        return {
            "id": f"acct-{service}",
            "name": self.me,
            "service type": {"ssms": "SMS", "sims": "iMessage", "srcs": "RCS"}.get(service, "?"),
            "enabled": True,
            "connection status": status,
            "code": model.CONNECTION_STATUSES[status],
        }

    # ---- chats ----------------------------------------------------------
    def open_chat(self, handles: list[str], *, name: str | None = None,
                  service: str = model.SERVICE_IMESSAGE) -> dict[str, Any]:
        handles = [h for h in handles if h]
        if not handles:
            raise ValueError("a chat needs at least one handle")
        chat_id = "chat-" + "-".join(sorted(handles))
        self.store.upsert_chat(chat_id, account_guid=f"acct-{service}",
                               service=service, name=name,
                               style=2 if len(handles) > 1 else 1,
                               handles=handles)
        self.store.log_event("chat.open", chat_id=chat_id, payload={"handles": handles})
        return next(c for c in self.store.chats() if c["id"] == chat_id)

    def chats(self) -> list[dict[str, Any]]:
        return self.store.chats()

    def archive(self, chat_id: str, *, archived: bool = True) -> None:
        self.store.set_chat_flag(chat_id, "is_archived", archived)

    def mute(self, chat_id: str, *, muted: bool = True) -> None:
        self.store.set_chat_flag(chat_id, "is_muted", muted)

    # ---- compose / send -----------------------------------------------
    def send(self, chat_id: str, text: str = "", *,
             mentions: list[dict[str, Any]] | None = None,
             attachments: list[dict[str, Any]] | None = None,
             effect: str = "none", reply_to: str | None = None,
             service: str | None = None, format: str = "plain") -> dict[str, Any]:
        """Send a message. `format` selects the rich-text carrier for `text`:

          plain     raw text (mentions/effect still applied as attributes)
          html      standard subset: <b> <i> <u> <s> <a href> <br>
                    <span data-mention> <span data-effect> <img data-attachment>
          markdown  inline subset: **b** *i* __u__ ~~s~~ [l](url)

        Inline <img>/<audio>/<video> markers become attachments with their
        character position (syndication range), like Apple's inline parts.
        """
        chat = next((c for c in self.store.chats() if c["id"] == chat_id), None)
        if chat is None:
            raise ValueError(f"no such chat: {chat_id} (open one first)")
        if not text and not attachments:
            raise ValueError("a message needs text or an attachment")
        if format not in rich.VALID_FORMATS:
            raise ValueError(f"unknown format: {format!r} "
                             f"(expected one of {rich.VALID_FORMATS})")

        runs, inline = rich.runs_from(text, format)
        md_cards: list[dict[str, Any]] = []
        if format == "markdown":
            runs, cards = mddoc.md_to_mixed(text)
            if cards:
                md_cards = mddoc.render_cards(cards)
                for spec in md_cards:
                    pos = spec.pop("position", None)
                    spec["syndication_ranges"] = [[pos, 1]] if pos is not None else []
        ms = [model.Mention(handle=m["handle"], range_start=int(m["start"]),
                            range_length=int(m["length"]),
                            entity_type=m.get("type", "person"))
              for m in (mentions or [])]
        plain_text = rich.plain(runs)
        for m in ms:
            if m.range_start < 0 or m.range_start + m.range_length > len(plain_text):
                raise ValueError(f"mention range {m.range} outside text of length {len(plain_text)}")
        runs = richtext.decorate(runs, ms, effect)

        atts: list[model.Attachment] = []
        for a in (attachments or []) + md_cards:
            atts.append(self._attachment_of(a))
        for a in inline:
            spec = dict(a)
            if not spec.get("url") and spec.get("src"):
                spec["url"] = spec["src"]
            spec.setdefault("kind", "file")
            att = self._attachment_of(spec)
            pos = spec.get("position")
            if isinstance(pos, int):
                att.syndication_ranges = [(pos, 1)]
            atts.append(att)

        msg = model.Message(
            chat_id=chat_id, direction=model.DIRECTION_OUTGOING,
            service=service or chat["service"], state=model.State.PENDING,
            runs=runs, attachments=atts, mentions=ms, effect=effect,
            reply_to_guid=reply_to, sender=self.me)

        if reply_to and self.store.get_message(reply_to) is None:
            raise ValueError(f"reply_to guid not found: {reply_to}")

        self.store.save_message(msg)
        self.store.log_event("message.compose", guid=msg.guid, chat_id=chat_id,
                             payload={"text": plain_text, "parts": len(atts),
                                      "mentions": len(ms), "effect": effect,
                                      "format": format})

        if self.mode == "real":
            return self._send_via_app(msg)

        msg.advance(model.State.SENDING)
        self._pending_after[msg.guid] = self._after(self.latency)
        self.store.save_message(msg)
        return self._describe(msg, format=format)

    def _attachment_of(self, a: dict[str, Any]) -> model.Attachment:
        kind = a.get("kind", "file")
        if kind not in ("file", "image", "video", "audio", "sticker", "appStrip", "location"):
            raise ValueError(f"unknown attachment kind: {kind}")
        return model.Attachment(
            kind=kind, url=a.get("url"), bundle_id=a.get("bundle_id"),
            filename=a.get("filename"), size=int(a.get("size", 0)),
            transfer_status=a.get("transfer_status", "preparing"),
            syndication_ranges=[tuple(r) for r in a.get("syndication_ranges", [])])

    def _send_via_app(self, msg: model.Message) -> dict[str, Any]:
        """Real send: the sdef `send` verb takes plain text or a file, so
        text goes as its plain projection and attachments as files; rich
        formatting that cannot ride the verb is reported as dropped."""
        handles = self._handles_of(msg.chat_id)
        target = handles[0] if len(handles) == 1 else None
        dropped: list[str] = []
        rich_rides = False
        if any(r.attributes for r in msg.runs if
               {k for k in r.attributes} - {model.ATTR_EFFECT}):
            # rich text CAN ride the real bridge via pasteboard+UI:
            # bold/italic/underline/strikethrough/links. Mentions/effects
            # still cannot and stay dropped.
            try:
                card_atts = [a for a in msg.attachments if a.syndication_ranges]
                if card_atts:
                    doc = mddoc.composite_html(
                        msg.runs, [{"url": a.url} for a in card_atts])
                else:
                    doc = rich.html_of(msg.runs) or ""
                applescript.send_rich_via_ui(doc, handle=target or handles[0],
                                              press_return=True)
                rich_rides = True
            except applescript.AppleScriptError:
                dropped.append("rich_formatting")
        if msg.effect not in ("none",):
            dropped.append("effect")
        if msg.mentions:
            dropped.append("mentions")
        if msg.reply_to_guid:
            dropped.append("reply_context")
        try:
            if msg.text and not rich_rides:
                if target:
                    applescript.send_text(msg.text, to=target)
                else:
                    applescript.send_text_to_chat_id(msg.text, chat_id=msg.chat_id)
            for a in msg.attachments:
                if a.syndication_ranges and rich_rides:
                    continue          # inline card already rode the paste
                path = (a.url or "").removeprefix("file://") if a.url else None
                if path and os.path.exists(path) and self.mode == "real":
                    path = _stage_attachment(path)   # reliable for AppleScript send
                if path and os.path.exists(path):
                    if target:
                        applescript.send_file(path, to=target)
                    else:
                        applescript.send_file_to_chat_id(path, chat_id=msg.chat_id)
                elif not msg.text:
                    raise applescript.AppleScriptError(
                        f"attachment has no sendable local file: {a.url!r}")
        except applescript.AppleScriptError as exc:
            msg.error = str(exc)
            msg.advance(model.State.FAILED)
            self.store.save_message(msg)
            self.store.log_event("message.failed", guid=msg.guid, chat_id=msg.chat_id,
                                 payload={"error": str(exc)})
            return self._describe(msg, bridge="osascript", dropped=dropped)
        msg.advance(model.State.SENT)
        self.store.save_message(msg)
        self.store.log_event("message.sent_via_app", guid=msg.guid, chat_id=msg.chat_id)
        return self._describe(msg, bridge="osascript", dropped=dropped)

    # ---- lifecycle ----------------------------------------------------
    def pump(self) -> dict[str, Any]:
        """Advance every pending outgoing message that is due."""
        now = model.now_ms()
        moved: list[dict[str, Any]] = []
        rows = self.store._db.execute(
            """SELECT id, state, delivered_at FROM message
               WHERE direction=? AND state IN (?,?,?) ORDER BY date""",
            (model.DIRECTION_OUTGOING,
             model.State.SENDING.value, model.State.SENT.value,
             model.State.DELIVERED.value)).fetchall()
        for r in rows:
            guid, state = r["id"], model.State(r["state"])
            due = self._pending_after.get(guid)
            if due is None:
                due = self._pending_after.setdefault(guid, now)
            if now < due:
                continue
            row = self.store.get_message(guid)
            if row is None:
                continue
            m = self._hydrate(row)
            if state is model.State.SENDING:
                m.advance(model.State.SENT)
                self._pending_after[guid] = self._after(self.delivery)
            elif state is model.State.SENT:
                m.advance(model.State.DELIVERED)
                self._pending_after[guid] = self._after(self.read)
            elif state is model.State.DELIVERED:
                m.advance(model.State.READ)
                self._pending_after.pop(guid, None)
            self.store.save_message(m)
            self.store.log_event(f"message.{m.state.value}", guid=guid, chat_id=m.chat_id)
            moved.append({"guid": guid, "state": m.state.value})
        return {"advanced": moved, "still_pending": len(self._pending_after)}

    def wait(self, timeout: float = 8.0) -> dict[str, Any]:
        """pump() until nothing is pending or timeout."""
        import time
        end = time.time() + timeout
        while self._pending_after and time.time() < end:
            self.pump()
            if self._pending_after:
                time.sleep(0.05)
        return {"settled": not self._pending_after,
                "pending": list(self._pending_after)}

    def receive(self, chat_id: str, text: str = "", *, sender: str | None = None,
                effect: str = "none", format: str = "plain",
                attachments: list[dict[str, Any]] | None = None,
                from_me: bool = False) -> dict[str, Any]:
        """Inject an incoming (or, with from_me, an outgoing echo) message.
        `format` is the same rich-text carrier as send()."""
        handles = self._handles_of(chat_id)
        sender = sender or (handles[0] if handles else "unknown")
        runs, inline = rich.runs_from(text, format)
        for a in inline:
            spec = dict(a)
            spec.setdefault("kind", "file")
            if not spec.get("url") and spec.get("src"):
                spec["url"] = spec["src"]
            (attachments := attachments or []).append(spec)
        atts = [model.Attachment(kind=a.get("kind", "file"), url=a.get("url"),
                                   filename=a.get("filename"), size=int(a.get("size", 0)))
                for a in (attachments or [])]
        msg = model.Message(chat_id=chat_id,
                            direction=model.DIRECTION_OUTGOING if from_me
                            else model.DIRECTION_INCOMING,
                            state=model.State.RECEIVED, runs=runs, attachments=atts,
                            effect=effect, sender=sender)
        self.store.save_message(msg)
        self.store.log_event("message.received", guid=msg.guid, chat_id=chat_id,
                             payload={"from": sender, "text": msg.text})
        return self._describe(msg)

    def mark_read(self, guid: str) -> dict[str, Any]:
        row = self.store.get_message(guid)
        if row is None:
            raise ValueError(f"no such message: {guid}")
        m = self._hydrate(row)
        m.advance(model.State.READ)
        self.store.save_message(m)
        self.store.log_event("message.read", guid=guid, chat_id=m.chat_id)
        return self._describe(m)

    def tapback(self, guid: str, kind: str) -> dict[str, Any]:
        taps = self.store.add_tapback(guid, kind)
        if taps is None:
            raise ValueError(f"no such message: {guid}")
        self.store.log_event("message.tapback", guid=guid,
                             payload={"kind": kind, "count": taps[kind]})
        return {"guid": guid, "tapbacks": taps}

    # ---- edit / retract (mirrors the protocol, windows included) ----------
    EDIT_WINDOW_MS = 900_000       # _IMMessageEditTimeout
    EDIT_RECEIVED_WINDOW_MS = 1_200_000
    RETRACT_WINDOW_MS = 120_000    # _IMMessageRetractionTimeout
    RETRACT_RECEIVED_WINDOW_MS = 240_000

    def _edit_base(self, guid: str) -> tuple[model.Message, Any]:
        row = self.store.get_message(guid)
        if row is None:
            raise ValueError(f"no such message: {guid}")
        if row["direction"] != model.DIRECTION_OUTGOING:
            raise ValueError("only your own messages can be edited or retracted")
        return self._hydrate(row), row

    def edit_message(self, guid: str, text: str, *,
                     format: str = "plain") -> dict[str, Any]:
        """Edit an outgoing message (the send side of "typewriter via edits").
        Enforces the protocol's 15-minute window; history keeps the newest
        5 frames (_IMMessageEditHistoryLimit)."""
        if format not in rich.VALID_FORMATS:
            raise ValueError(f"unknown format: {format!r}")
        if self.mode == "real":
            raise NotImplementedError(
                "real-send editing needs the app UI (context-menu Edit) or the "
                "in-process bridge; the store-only edit would not reach the wire")
        msg, row = self._edit_base(guid)
        now = model.now_ms()
        if now - msg.date > self.EDIT_WINDOW_MS:
            raise ValueError(
                f"edit window exceeded: message is {int((now - msg.date) / 1000)}s old, "
                f"window is {self.EDIT_WINDOW_MS // 1000}s")
        runs, inline = rich.runs_from(text, format)
        for spec in inline:
            spec.setdefault("kind", "file")
            if not spec.get("url") and spec.get("src"):
                spec["url"] = spec["src"]
        msg.runs = runs
        msg.mentions = []
        self.store.save_message(msg)
        ordinal = self.store.record_edit(guid, text=msg.text,
                                         html=rich.html_of(runs) or None)
        self.store.set_edited(guid, edited_at=now)
        self.store.log_event("message.edited", guid=guid, chat_id=msg.chat_id,
                             payload={"ordinal": ordinal, "text": msg.text,
                                      "format": format})
        return {"guid": guid, "ordinal": ordinal, "text": msg.text,
                "html": rich.html_of(runs) or None,
                "history_limit": self.store.EDIT_HISTORY_LIMIT,
                "window_ms": self.EDIT_WINDOW_MS,
                "history": self.store.edit_history(guid)}

    def retract_message(self, guid: str) -> dict[str, Any]:
        """Retract (unsend) an outgoing message — protocol-wise an edit command
        that retracts all part indexes (chat.db: is_empty + rp=[0..n])."""
        msg, row = self._edit_base(guid)
        now = model.now_ms()
        if now - msg.date > self.RETRACT_WINDOW_MS:
            raise ValueError(
                f"retract window exceeded: message is {int((now - msg.date) / 1000)}s old, "
                f"window is {self.RETRACT_WINDOW_MS // 1000}s")
        msg.runs = []
        self.store.save_message(msg)
        self.store.record_edit(guid, text="", html=None)
        self.store.set_edited(guid, edited_at=now, retracted_at=now)
        self.store.log_event("message.retracted", guid=guid, chat_id=msg.chat_id)
        return {"guid": guid, "retracted": True,
                "window_ms": self.RETRACT_WINDOW_MS}

    def typewriter(self, handle: str, frames: list[str], *,
                   interval: float = 1.0, format: str = "plain",
                   run_edit=None) -> dict[str, Any]:
        """Real typewriter: frame 0 goes out via the pasteboard channel,
        each later frame UI-edits the newest bubble (the message we just
        sent — stable invariant). Experimental; verify() uiedit first on a
        self-chat. Returns per-frame results."""
        if self.mode != "real":
            raise ValueError("typewriter() is the REAL path; use send+edit_message in sim")
        if not frames:
            raise ValueError("typewriter needs at least one frame")
        if format == "plain":
            first_html = frames[0]
        else:
            runs0, _ = rich.runs_from(frames[0], format)
            first_html = rich.html_of(runs0)
        applescript.send_rich_via_ui(first_html, handle=handle, press_return=True)
        results = [{"frame": 0, "via": "pasteboard", "text": frames[0]}]
        import time as _t

        # priority: resident in-process bridge (native speed); UI-edit is the
        # fallback when the bridge is absent or its edit op is not wired yet
        bridge_edit = None
        if run_edit is None:
            from . import bridge as bridge_mod
            if bridge_mod.available():
                bridge_edit = bridge_mod.edit
        ui_edit = run_edit or uiedit.edit_last
        via = "bridge" if bridge_edit else "ui-edit"
        for i, frame in enumerate(frames[1:], 1):
            _t.sleep(interval)
            if bridge_edit is not None:
                try:
                    res = bridge_edit(frame)
                    results.append({"frame": i, "via": "bridge", "text": frame, **res})
                    continue
                except Exception:
                    bridge_edit = None          # fall through to UI
                    via = "ui-edit"
            res = ui_edit(frame)
            results.append({"frame": i, "via": via, "text": frame, **res})
        return {"handle": handle, "frames": len(frames), "results": results}

    def edit_history(self, guid: str) -> list[dict[str, Any]]:
        return self.store.edit_history(guid)

    def set_typing(self, chat_id: str, *, handle: str | None = None,
                   typing: bool = True) -> dict[str, Any]:
        self.store.set_typing(chat_id, handle or self.me, typing)
        self.store.log_event("typing" if typing else "typing.stopped", chat_id=chat_id)
        return {"chat_id": chat_id, "typing": typing}

    # ---- search -------------------------------------------------------
    def search(self, query: str, *, chat_id: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM message WHERE text LIKE ?"
        args: list[object] = [f"%{query}%"]
        if chat_id:
            q += " AND chat_id=?"
            args.append(chat_id)
        q += " ORDER BY date DESC LIMIT 100"
        return [store.clean_row(r) for r in self.store._db.execute(q, args)]

    def transcript(self, chat_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.messages(chat_id, limit=limit)

    def events(self, *, since_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.events(since_seq=since_seq, limit=limit)

    # ---- internals ----------------------------------------------------
    def _handles_of(self, chat_id: str) -> list[str]:
        rows = self.store._db.execute(
            """SELECT h.id FROM chat_handle ch JOIN handle h ON h.id=ch.handle_id
               WHERE ch.chat_id=? ORDER BY h.id""", (chat_id,)).fetchall()
        return [r["id"] for r in rows]

    def _hydrate(self, row) -> model.Message:
        runs = richtext.decode_runs(row["attributed_body"]) if row["attributed_body"] else \
               ([model.Run(text=row["text"])] if row["text"] else [])
        atts = [model.Attachment(
            kind=a["kind"], url=a["url"], guid=a["guid"], bundle_id=a["bundle_id"],
            filename=a["filename"], size=a["size"], transfer_status=a["transfer_status"],
            syndication_ranges=[tuple(r) for r in json_loads(a["syndication_ranges"])])
            for a in self.store._db.execute(
                "SELECT * FROM attachment WHERE message_id=? ORDER BY ordinal", (row["id"],))]
        return model.Message(
            guid=row["id"], chat_id=row["chat_id"], direction=row["direction"],
            service=row["service"], state=model.State(row["state"]), runs=runs,
            attachments=atts, effect=row["effect"] or "none",
            tapbacks=json_loads(row["tapbacks"]), reply_to_guid=row["reply_to_guid"],
            sender=row["sender"], date=row["date"], delivered_date=row["delivered_at"],
            read_date=row["read_at"], error=row["error"],
            source=row["source"] if "source" in row.keys() else "sim",
            real_rowid=row["real_rowid"] if "real_rowid" in row.keys() else None)

    def _describe(self, m: model.Message, **extra: Any) -> dict[str, Any]:
        d = {
            "guid": m.guid,
            "chat_id": m.chat_id,
            "direction": m.direction,
            "state": m.state.value,
            "service": m.service,
            "text": m.text,
            "html": rich.html_of(m.runs) if m.runs else None,
            "part": m.part_class,
            "effect": m.effect,
            "tapbacks": m.tapbacks,
            "attachments": [{"guid": a.guid, "kind": a.kind, "uti": a.kind,
                             "bundleIdentifier": a.bundle_id, "filename": a.filename,
                             "size": a.size,
                             "transferStatus": model.TRANSFER_STATUSES.get(a.transfer_status,
                                                                            a.transfer_status)}
                            for a in m.attachments],
            "mentions": [{"handle": x.handle, "start": x.range_start,
                         "length": x.range_length} for x in m.mentions],
            "attributedBody": richtext.b64(richtext.encode(m)) if m.runs else None,
            "sdef_id": m.sdef_id,
            "source": m.source,
        }
        d.update(extra)
        return d


def json_loads(s: str):
    import json
    return json.loads(s or "null") if s else []
