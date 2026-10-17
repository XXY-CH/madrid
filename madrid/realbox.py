"""Real inbox: decode live chat.db rows into standard rich-text envelopes.

Receive side of the real bridge. Everything arrives read-only: the database
is opened with SQLite URI ``immutable=1`` (no journal, no locks, no writes).

Column semantics:

  message.date / date_delivered / date_read / date_played
        Apple-epoch (2001-01-01) nanoseconds; e.g. 813571080503826816
  message.associated_message_type
        2000 love / 2001 like / 2002 dislike / 2003 laugh / 2004 emphasize /
        2005 question / 2006 custom-emoji tapback;
        3000–3005 sticker tapbacks; 3006 sticker removal;
        4000/4001 sticker placed on a message
  message.associated_message_guid  "p:<part>/<target-guid>" (measured)
  message.thread_originator_guid / _part   inline reply target
  message.message_summary_info     bplist00 (plistlib-decodable)
  attachment.transfer_state        {0: finished}

Rich text comes from message.attributedBody via the Foundation decoder
(madrid/native/decode_attributed_body.m — the blob is an NSArchiver typedstream,
; when the decoder is unavailable the text column is used as a
plain-text fallback. Formatting keys are the real ones (madrid.rich).
"""
from __future__ import annotations

import os
import plistlib
import sqlite3
from typing import Any

from . import model, real_format, rich

DEFAULT_DB = os.path.expanduser("~/Library/Messages/chat.db")
ATTACHMENTS_DIR = os.path.expanduser("~/Library/Messages/Attachments")

# Known balloon providers.
BALLOON_PROVIDERS = {
    "com.apple.messages.URLBalloonProvider": "rich_link",
    "com.apple.Handwriting.HandwritingProvider": "handwriting",
    "com.apple.DigitalTouchBalloonProvider": "digital_touch",
}   # MSMessageExtensionBalloonPlugin:* rows are app messages (polls, sticker
    # packs, Apple Pay PeerPayment…); matched by prefix below.


def balloon_kind(bundle_id: str | None) -> str | None:
    if not bundle_id:
        return None
    if bundle_id in BALLOON_PROVIDERS:
        return BALLOON_PROVIDERS[bundle_id]
    if bundle_id.startswith("com.apple.messages.MSMessageExtensionBalloonPlugin"):
        return "app_message"
    return "other_balloon"


def _payload_magic(blob) -> str | None:
    if not blob:
        return None
    b = bytes(blob)
    if b.startswith(b"bplist00"):
        return "bplist"
    if b.startswith(b"\x04\x0bstreamtyped"):
        return "typedstream"
    if b.startswith(b"\x1f\x8b"):
        return "gzip"
    return "binary:" + b[:4].hex()

# Values follow Apple's numeric block layout (2000+i, 3000+i):
# matches the tapback order in Messages.sdef-adjacent ChatKit selectors.
TAPBACK_TYPES = {
    2000: "love", 2001: "like", 2002: "dislike", 2003: "laugh",
    2004: "emphasize", 2005: "question",
    2006: "custom",
    3000: "love", 3001: "like", 3002: "dislike", 3003: "laugh",
    3004: "emphasize", 3005: "question",
    3006: "removed",
    4000: "sticker", 4001: "sticker",
}

SERVICE_MAP = {"iMessage": model.SERVICE_IMESSAGE,
               "SMS": model.SERVICE_SMS,
               "RCS": model.SERVICE_RCS}

TRANSFER_STATES = {0: "finished", 5: "unknown_5"}

APPLE_EPOCH_MS = 978_307_200_000

# Protocol parameters read straight from IMSharedUtilities globals
# (protocol edit/retract parameters):
# retraction == an edit command that retracts part indexes.
EDIT_WINDOW_S = 900.0          # _IMMessageEditTimeout (15 min)
EDIT_HISTORY_LIMIT = 5         # _IMMessageEditHistoryLimit (int)
EDIT_RECEIVED_WINDOW_S = 1200.0  # _IMMessageEditReceivedTimeout (20 min)
RETRACT_WINDOW_S = 120.0       # _IMMessageRetractionTimeout (2 min)
RETRACT_RECEIVED_WINDOW_S = 240.0  # _IMMessageRetractionReceivedTimeout


class RealInboxError(RuntimeError):
    pass


def ns_to_ms(v: int | None) -> int | None:
    """Apple-epoch nanoseconds (2001-based) -> Unix milliseconds."""
    if not v:
        return None
    return v // 1_000_000 + APPLE_EPOCH_MS


def kind_of_mime(mime: str | None, *, is_sticker: bool = False) -> str:
    if is_sticker:
        return "sticker"
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


class RealInbox:
    """Read-only decoder over a real Messages store (chat.db)."""

    def __init__(self, source: str = DEFAULT_DB):
        self.source = source
        if not os.path.exists(source):
            raise RealInboxError(f"no such database: {source}")
        # mode=ro (NOT immutable=1): a long-lived watcher must see later
        # writes, and immutable connections skip SQLite's change detection,
        # so external updates (incl. WAL content) never invalidate the page
        # cache. ro still opens no write transaction and reads the WAL.
        try:
            self._db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            self._db.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error:
            try:
                self._db = sqlite3.connect(f"file:{source}?immutable=1", uri=True)
            except sqlite3.Error as exc:
                raise RealInboxError(f"cannot open {source} read-only: {exc}") from exc
        self._db.row_factory = sqlite3.Row
        self._decoder_ready = False

    def close(self) -> None:
        self._db.close()

    # ---- cursors --------------------------------------------------------
    def cursor(self) -> int:
        """Newest message ROWID — the polling cursor."""
        row = self._db.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()
        return int(row[0])

    def messages_since(self, cursor: int = 0, *, limit: int = 100,
                       include_runs: bool = True) -> list[dict[str, Any]]:
        """Decode every message with ROWID > cursor, oldest first."""
        rows = self._db.execute(
            "SELECT * FROM message WHERE ROWID > ? ORDER BY ROWID LIMIT ?",
            (int(cursor), int(limit))).fetchall()
        return [self.envelope(r, include_runs=include_runs) for r in rows]

    def updates_since(self, last_ms: int, *, limit: int = 100) -> list[dict[str, Any]]:
        """Rows changed since a Unix-ms stamp (message.date_updated).

        Best-effort: date_updated stays 0 until an edit lands, so
        this surfaces edited/retracted/updated messages without new ROWIDs.
        """
        rows = self._db.execute(
            """SELECT * FROM message WHERE date_updated > 0
               ORDER BY date_updated DESC LIMIT ?""", (int(limit),)).fetchall()
        out = []
        for r in rows:
            stamp = ns_to_ms(r["date_updated"])
            if stamp is not None and stamp > last_ms:
                env = self.envelope(r, include_runs=False)
                env["updated_at"] = stamp
                out.append(env)
        return out

    def edits_since_snapshot(self, snapshot: dict[str, int], *,
                             window: int = 500) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Diff date_edited/date_retracted over the recent window.

        Each applied edit refreshes date_edited, so a guid→stamp diff turns
        every edit frame into an event (the receive side of "typewriter via
        edits"). Returns (envelopes-with-change, new snapshot).
        """
        rows = self._db.execute(
            """SELECT ROWID, guid, date_edited, date_retracted FROM message
               WHERE (date_edited > 0 OR date_retracted > 0)
               ORDER BY ROWID DESC LIMIT ?""", (int(window),)).fetchall()
        fresh: dict[str, int] = {}
        events: list[dict[str, Any]] = []
        for r in rows:
            guid = r["guid"]
            stamp = (ns_to_ms(r["date_edited"]) or 0) + (ns_to_ms(r["date_retracted"]) or 0)
            fresh[guid] = stamp
            if stamp != snapshot.get(guid):
                full = self._db.execute(
                    "SELECT * FROM message WHERE guid = ?", (guid,)).fetchone()
                if full is not None:
                    env = self.envelope(full, include_runs=False)
                    env["type"] = "retracted" if full["date_retracted"] else "edited"
                    env["change_kind"] = "edit" if full["date_retracted"] == 0 else "retract"
                    events.append(env)
        return events, fresh


    def receipts_since_snapshot(self, snapshot: dict[str, tuple[int, int]],
                                *, window: int = 500
                                ) -> tuple[list[dict[str, Any]], dict[str, tuple[int, int]]]:
        """Diff date_read/date_delivered over recent OUTGOING rows.

        Peers reading our messages updates those stamps in place (no new
        ROWID), so a guid→(read_ms, delivered_ms) diff turns each "已读 /
        已送达" into an event."""
        rows = self._db.execute(
            """SELECT ROWID, guid, date_read, date_delivered FROM message
               WHERE is_from_me = 1 AND (date_read > 0 OR date_delivered > 0)
               ORDER BY ROWID DESC LIMIT ?""", (int(window),)).fetchall()
        fresh: dict[str, tuple[int, int]] = {}
        events: list[dict[str, Any]] = []
        for r in rows:
            guid = r["guid"]
            stamps = (ns_to_ms(r["date_read"]) or 0, ns_to_ms(r["date_delivered"]) or 0)
            fresh[guid] = stamps
            old = snapshot.get(guid)
            if old == stamps:      # unchanged since the last poll
                continue
            if old is None:        # first sighting: report as the boot snapshot
                old = (0, 0)
            changed = []
            if stamps[0] != old[0]:
                changed.append("read")
            if stamps[1] != old[1]:
                changed.append("delivered")
            chat = self._chat_of(r["ROWID"])
            events.append({"type": "receipt", "guid": guid, "rowid": r["ROWID"],
                           "chat": {"guid": chat.get("guid"),
                                    "handles": chat.get("handles")},
                           "change": changed,
                           "read_at": stamps[0] or None,
                           "delivered_at": stamps[1] or None})
        return events, fresh

    def transcript(self, chat_guid: str | None = None, *, limit: int = 25,
                   include_runs: bool = False) -> list[dict[str, Any]]:
        """Newest messages of one chat (or any chat), oldest first."""
        if chat_guid:
            rows = self._db.execute(
                """SELECT m.* FROM message m
                   JOIN chat_message_join j ON j.message_id = m.ROWID
                   JOIN chat c ON c.ROWID = j.chat_id
                   WHERE c.guid = ? ORDER BY m.ROWID DESC LIMIT ?""",
                (chat_guid, int(limit))).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM message ORDER BY ROWID DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [self.envelope(r, include_runs=include_runs) for r in reversed(rows)]

    def chats(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.execute(
            """SELECT c.* FROM chat c
               ORDER BY (SELECT MAX(message_date) FROM chat_message_join j
                         WHERE j.chat_id = c.ROWID) DESC LIMIT ?""",
            (int(limit),)).fetchall()
        out = []
        for c in rows:
            handles = [r[0] for r in self._db.execute(
                """SELECT h.id FROM chat_handle_join ch JOIN handle h ON h.ROWID = ch.handle_id
                   WHERE ch.chat_id = ? ORDER BY h.id""", (c["ROWID"],))]
            last = self._db.execute(
                """SELECT MAX(message_date) FROM chat_message_join WHERE chat_id = ?""",
                (c["ROWID"],)).fetchone()[0]
            out.append({
                "guid": c["guid"], "rowid": c["ROWID"],
                "name": c["display_name"] or c["chat_identifier"],
                "handles": handles,
                "service": SERVICE_MAP.get(c["service_name"], c["service_name"]),
                "style": c["style"],
                "archived": bool(c["is_archived"]),
                "last_message_at": ns_to_ms(last),
            })
        return out

    # ---- row decode -------------------------------------------------------
    def _chat_of(self, message_rowid: int) -> dict[str, Any]:
        c = self._db.execute(
            """SELECT c.* FROM chat c JOIN chat_message_join j ON j.chat_id = c.ROWID
               WHERE j.message_id = ? ORDER BY j.message_date DESC LIMIT 1""",
            (message_rowid,)).fetchone()
        if c is None:
            return {"guid": None, "rowid": None, "name": None,
                    "handles": [], "service": None, "style": None,
                    "archived": False}
        handles = [r[0] for r in self._db.execute(
            """SELECT h.id FROM chat_handle_join ch JOIN handle h ON h.ROWID = ch.handle_id
               WHERE ch.chat_id = ? ORDER BY h.id""", (c["ROWID"],))]
        return {"guid": c["guid"], "rowid": c["ROWID"],
                "name": c["display_name"] or c["chat_identifier"],
                "handles": handles,
                "service": SERVICE_MAP.get(c["service_name"], c["service_name"]),
                "style": c["style"], "archived": bool(c["is_archived"])}

    def _sender_of(self, r) -> str | None:
        if r["is_from_me"]:
            return "me"
        if r["handle_id"]:
            h = self._db.execute("SELECT id FROM handle WHERE ROWID = ?",
                                 (r["handle_id"],)).fetchone()
            return h[0] if h else None
        if r["other_handle"]:
            h = self._db.execute("SELECT id FROM handle WHERE ROWID = ?",
                                 (r["other_handle"],)).fetchone()
            return h[0] if h else str(r["other_handle"])
        return None

    def _attachments_of(self, message_rowid: int) -> list[dict[str, Any]]:
        out = []
        for a in self._db.execute(
                """SELECT at.* FROM attachment at
                   JOIN message_attachment_join j ON j.attachment_id = at.ROWID
                   WHERE j.message_id = ? ORDER BY at.ROWID""", (message_rowid,)):
            fname = a["filename"] or ""
            state = a["transfer_state"]
            out.append({
                "guid": a["guid"],
                "kind": kind_of_mime(a["mime_type"], is_sticker=bool(a["is_sticker"])),
                "path": fname if os.path.isabs(fname) else os.path.join(ATTACHMENTS_DIR, fname),
                "filename": os.path.basename(fname) if fname else None,
                "mime": a["mime_type"],
                "uti": a["uti"],
                "size": a["total_bytes"],
                "transfer_state": state,
                "transfer_status": TRANSFER_STATES.get(state, f"state_{state}"),
                "sticker": bool(a["is_sticker"]),
                "outgoing": bool(a["is_outgoing"]),
            })
        return out

    def _summary_of(self, blob) -> dict[str, Any]:
        """message_summary_info is a binary plist; surface its keys only."""
        if not blob:
            return {}
        try:
            return plistlib.loads(bytes(blob))
        except Exception:
            return {"_undecodable": True, "bytes": len(blob)}

    def _edit_info(self, r) -> dict[str, Any]:
        """Decoded edit/retract state from message_summary_info."""
        raw = self._summary_of(r["message_summary_info"])
        otr = raw.get("otr") or {}
        return {
            "edited_at": ns_to_ms(r["date_edited"]),
            "retracted_parts": list(raw.get("rp") or []),
            "original_text_ranges": {
                str(k): {"lo": v.get("lo"), "le": v.get("le")}
                for k, v in (otr.items() if isinstance(otr, dict) else [])},
            "history_limit": EDIT_HISTORY_LIMIT,
            "raw_keys": sorted(k for k in raw.keys()),
        }

    def _decode_body(self, r) -> tuple[list[model.Run], list[dict[str, Any]]]:
        """Runs from attributedBody via Foundation; fallback to the text column."""
        blob = r["attributedBody"]
        if blob:
            try:
                self._ensure_decoder()
            except real_format.RealFormatError:
                self._decoder_ready = False
            if self._decoder_ready:
                try:
                    d = real_format.foundation_decode(bytes(blob), with_text=True)
                    return self._runs_from_foundation(d), []
                except (real_format.RealFormatError, OSError, ValueError):
                    pass
        text = r["text"] or ""
        return ([model.Run(text=text)] if text else []), []

    def _ensure_decoder(self) -> None:
        if self._decoder_ready or \
                os.access(real_format.HELPER_BINARY, os.X_OK):
            self._decoder_ready = True
            return
        real_format.build_helper()          # raises RealFormatError without clang
        self._decoder_ready = True

    @staticmethod
    def _runs_from_foundation(d: dict[str, Any]) -> list[model.Run]:
        """[{name, value, location, length}] -> attribute-run list."""
        text = d.get("text") or ""
        n = d.get("length") or len(text)
        if not text:
            return []
        marks: list[tuple[int, int, str, Any]] = []
        for run in d.get("runs", []):
            try:
                loc, ln = int(run["location"]), int(run["length"])
            except (KeyError, TypeError, ValueError):
                continue
            value = run.get("value")
            if isinstance(value, str) and value.startswith("<"):
                continue        # opaque class placeholder from the helper
            marks.append((loc, min(loc + ln, n), run["name"], value))
        bounds = sorted({0, n} | {b for s, e, _, _ in marks for b in (s, e)})
        out: list[model.Run] = []
        for i in range(len(bounds) - 1):
            lo, hi = bounds[i], bounds[i + 1]
            if hi <= lo or lo >= len(text):
                continue
            attrs: dict[str, Any] = {}
            for s, e, name, value in marks:
                if s <= lo and hi <= e:
                    attrs[name] = value
            chunk = text[lo:hi]
            if out and out[-1].attributes == attrs:
                out[-1].text += chunk
            else:
                out.append(model.Run(text=chunk, attributes=attrs))
        return out or [model.Run(text=text)]

    # ---- the envelope ------------------------------------------------------
    def envelope(self, r, *, include_runs: bool = True) -> dict[str, Any]:
        runs, _ = self._decode_body(r)
        assoc = r["associated_message_type"] or 0
        chat = self._chat_of(r["ROWID"])
        sender = self._sender_of(r)
        semantics = run_semantics(runs)

        env: dict[str, Any] = {
            "type": "message",
            "guid": r["guid"],
            "rowid": r["ROWID"],
            "chat": chat,
            "from_me": bool(r["is_from_me"]),
            "sender": sender,
            "service": SERVICE_MAP.get(r["service"], r["service"]),
            "date": ns_to_ms(r["date"]),
            "delivered_at": ns_to_ms(r["date_delivered"]),
            "read_at": ns_to_ms(r["date_read"]),
            "text": rich.plain(runs) or (r["text"] or ""),
            "html": rich.html_of(runs) if runs else None,
            "attachments": self._attachments_of(r["ROWID"]),
            "reply_to": ({"guid": r["thread_originator_guid"],
                          "part": r["thread_originator_part"]}
                         if r["thread_originator_guid"] else None),
            "mentions": semantics["mentions"],
            "text_effect": semantics["text_effect"],
            "mention_unseen": bool(r["has_unseen_mention"]),
            "expressive_style": r["expressive_send_style_id"],
            "balloon": ({"bundle_id": r["balloon_bundle_id"],
                         "kind": balloon_kind(r["balloon_bundle_id"]),
                         "payload_format": _payload_magic(r["payload_data"]),
                         "payload_bytes": len(r["payload_data"] or b"")}
                        if r["balloon_bundle_id"] or r["payload_data"] else None),
            "audio": {"message": bool(r["is_audio_message"]),
                      "played": bool(r["is_played"]),
                      "played_at": ns_to_ms(r["date_played"])}
                       if r["is_audio_message"] else None,
            "edited_at": ns_to_ms(r["date_edited"]),
            "retracted_at": ns_to_ms(r["date_retracted"]),
            "error": r["error"],
            "is_empty": bool(r["is_empty"]),
            "item_type": r["item_type"],
            "part_count": r["part_count"],
        }
        if include_runs:
            env["runs"] = [{"text": x.text, "attributes": x.attributes}
                           for x in rich.merge(runs)]

        # tapbacks / stickers
        if assoc in TAPBACK_TYPES:
            kind = TAPBACK_TYPES[assoc]
            target, part = self._split_assoc(r["associated_message_guid"])
            env["type"] = "sticker" if kind == "sticker" else "tapback"
            env["tapback"] = {
                "kind": kind,
                "emoji": r["associated_message_emoji"],
                "raw_type": assoc,
                "target": target,
                "target_part": part,
                "from": sender,
            }
            env["text"] = (r["text"] or "").replace(rich.OBJECT_CHAR, "").strip()

        # edits keep their history in message_summary_info (plist); key
        # semantics:
        #   rp  = retracted part indexes (retract == edit that retracts parts)
        #   otr = original text range per part {part: {lo, le}}
        #   history beyond IMMessageEditHistoryLimit (5) is dropped
        if r["date_edited"]:
            env["edits"] = self._edit_info(r)

        # group / system messages
        if r["group_title"] or r["group_action_type"] or r["is_system_message"]:
            env["system"] = {
                "group_title": r["group_title"],
                "group_action_type": r["group_action_type"],
                "is_system_message": bool(r["is_system_message"]),
            }

        # edits keep their history in message_summary_info (plist)
        if r["date_edited"]:
            env["summary_info_keys"] = sorted(self._summary_of(r["message_summary_info"]))

        if env["retracted_at"]:
            env["type"] = "retracted"
            env["text"] = ""
            env["html"] = None
            env["runs"] = []
        return env

    @staticmethod
    def _split_assoc(guid: str | None) -> tuple[str | None, int | None]:
        """'p:<part>/<guid>' -> (guid, part); other shapes pass through."""
        if not guid:
            return None, None
        if guid.startswith("p:") and "/" in guid:
            part, _, target = guid[2:].partition("/")
            try:
                return target, int(part)
            except ValueError:
                return guid[2:], None
        return guid, None


def run_semantics(runs: list[model.Run]) -> dict[str, Any]:
    """Structured mention/effect extraction from decoded runs. Understands
    both the real keys (__kIMMentionAttributeName, __kIMTextEffectAttributeName,
    and the sim keys."""
    mentions: list[dict[str, Any]] = []
    effects: list[dict[str, Any]] = []
    pos = 0
    for r in runs:
        mention = None
        for key in rich.MENTION_KEYS:
            if r.attributes.get(key):
                mention = r.attributes[key]
                break
        if mention:
            mentions.append({"handle": mention, "start": pos,
                             "length": len(r.text)})
        effect = None
        for key in rich.EFFECT_KEYS:
            if r.attributes.get(key):
                effect = r.attributes[key]
                break
        if effect:
            effects.append({"effect": effect, "start": pos, "length": len(r.text)})
        pos += len(r.text)
    return {"mentions": mentions,
            "text_effect": effects[0]["effect"] if len(effects) == 1
            and effects[0]["start"] == 0 and not mentions else
            (effects or None)}


def preview(env: dict[str, Any], *, width: int = 80) -> str:
    """One-line human preview of an envelope (for notifications)."""
    if env["type"] == "tapback":
        tb = env.get("tapback") or {}
        kind = tb.get("emoji") or tb.get("kind") or "?"
        return f"tapback {kind} -> {(tb.get('target') or '')[:8]}"
    if env["type"] == "retracted":
        return "unsent a message"
    text = (env.get("text") or "").replace("\n", " ").strip()
    if env["attachments"]:
        kinds = "+".join(sorted({a["kind"] for a in env["attachments"]}))
        text = (text + " " if text else "") + f"[{kinds}]"
    return text[:width]
