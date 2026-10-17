"""The send/receive capability matrix — what iMessage can carry, and which
side of this bridge can actually do it.

"一切 iMessage 里能发送的都要有对应的接收方法和发送方法" — this table is the
answer, and it is honest about the real boundary:

  real send goes through Messages.sdef's `send` verb, whose direct parameter
  is `text | file` — so plain text and attachments ship; rich formatting
  rides as the plain projection (reported as dropped); effects/mentions/
  tapbacks/replies are app-UI-only and cannot be scripted.

  real receive decodes chat.db read-only — text, rich runs, attachments,
  tapbacks, stickers, replies, audio, edits, retractions, read receipts and
  system events are all recoverable from columns + attributedBody. Typing
  indicators are ephemeral (never persisted) and are sim-only.
"""
from __future__ import annotations

import shutil

from . import applescript, real_format, realbox

# feature: (sim_send, sim_recv, real_send, real_recv, real_send_note)
# real_send: "yes" | "degraded" | "no"
FEATURES: dict[str, tuple[bool, bool, str, bool, str]] = {
    "plain_text":            (True, True, "yes", True,
                              "three real paths: sdef send / pasteboard+UI / shortcuts run (headless)"),
    "rich_formatting":       (True, True, "degraded", True,
                              "sdef send takes text only; sent as plain, rich kept in local record"),
    "links":                 (True, True, "yes", True,
                              "sent as text; peer auto-detects"),
    "mentions":              (True, True, "no", True,
                              "group UI feature; receive decodes __kIMMentionAttributeName runs "
                              "+ has_unseen_mention flag"),
    "text_effects":          (True, True, "no", True,
                              "app UI only; receive decodes __kIMTextEffectAttributeName runs "
                              "+ expressive_send_style_id"),
    "attachments":           (True, True, "yes", True,
                              "sdef send accepts file; receive resolves Attachments/ paths"),
    "inline_attachments":    (True, True, "degraded", True,
                              "sent as separate file parts; receive decodes inline GUID runs"),
    "stickers":              (True, True, "degraded", True,
                              "sendable as image files; receive decodes is_sticker + 4000/4001"),
    "tapbacks":              (True, True, "no", True,
                              "associated_message_type 2000-3006 decoded on receive"),
    "replies":               (True, True, "no", True,
                              "thread_originator_guid decoded on receive"),
    "audio_messages":        (True, True, "yes", True,
                              "send as audio file; receive decodes is_audio_message/is_played"),
    "read_receipts":         (True, True, "n/a", True,
                              "date_read/date_delivered decoded for outgoing"),
    "typing_indicator":      (True, True, "no", False,
                              "ephemeral, never persisted; sim-only"),
    "edit":                  (True, True, "no", True,
                              "date_edited + summary_info history decoded on receive; "
                              "send-side is app UI (900s window, 5-frame history); "
                              "sim: edit_message tool"),
    "retract":               (True, True, "no", True,
                              "date_retracted decoded on receive; retract == edit "
                              "that retracts parts (120s window); sim: retract_message"),
    "notifications":         (True, True, "yes", True,
                              "madrid watch --notify / webhook / jsonl / MCP events"),
    "rich_links":            (True, True, "degraded", True,
                              "send a URL as text (peer builds the preview); receive decodes "
                              "URLBalloonProvider balloons"),
    "app_messages":          (True, True, "no", True,
                              "MSMessageExtension balloons (incl. Apple Pay); receive decodes payload_data bplist"),
    "digital_touch":         (True, True, "no", True,
                              "com.apple.DigitalTouchBalloonProvider; macOS renders (bundle on "
                              "disk); send is iOS UI only"),
    "handwriting":           (True, True, "no", True,
                              "com.apple.Handwriting.HandwritingProvider; payload is a "
                              "proprietary binary container"),
    "animoji_memoji":        (True, True, "degraded", True,
                              "IMEmojiSticker attachments (is_sticker + pack metadata); "
                              "send as image file loses sticker semantics"),
    "apple_pay":             (True, True, "no", True,
                              "PeerPayment MSMessageExtension balloon; "
                              "needs PassKit UI"),
    "scheduled_send":        (True, False, "no", True,
                              "schedule_type/schedule_state columns exist; send is app UI only"),
    "satellite":             (True, False, "no", True,
                              "is_stewie/is_sos columns"),
}


def environment() -> dict[str, object]:
    """Live capability probe of this machine."""
    av = applescript.available()
    probe = applescript.app_probe(timeout=10) if av["osascript"] else {"app": False}
    db = realbox.DEFAULT_DB
    chatdb = False
    if db and __import__("os").path.exists(db):
        try:
            realbox.RealInbox(db).close()
            chatdb = True
        except realbox.RealInboxError:
            chatdb = False
    return {
        "osascript": av["osascript"],
        "sdef": av["sdef"],
        "real_gate_env": av["gate"],
        "messages_app": probe,
        "chat_db_readable": chatdb,
        "clang": bool(shutil.which("clang")),
        "foundation_decoder": real_format.helper_available(),
    }


def matrix() -> list[dict[str, object]]:
    return [{"feature": k,
             "sim_send": s, "sim_receive": r,
             "real_send": rs, "real_receive": rr,
             "note": n}
            for k, (s, r, rs, rr, n) in FEATURES.items()]
