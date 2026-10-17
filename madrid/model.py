"""Data model for the iMessage simulator.

  carrier      NSAttributedString -> runs with attribute dictionaries
  storage      IMDMessageRecord.attributedBodyText / attributedBody
  wire         IMCore attributedBodyData / attributedBodyText
  mentions     CKMentionEntityNode, CKMentionsRenderingAttributes
  effects      CKTextEffectCoordinator, CKInvisibleInkTextEffectView,
               CKTextEffectExplodeConfiguration
  tapbacks     111 selectors under 'tapback'
  parts        CKMessagePartChatItem, CKAttachmentMessagePartChatItem,
               CKAggregateAttachmentMessagePartChatItem
"""
from __future__ import annotations
import enum
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# Apple's own enums, from Messages.sdef (verbatim codes).
SERVICE_SMS = "ssms"
SERVICE_IMESSAGE = "sims"
SERVICE_RCS = "srcs"

DIRECTION_INCOMING = "FTic"
DIRECTION_OUTGOING = "FTog"

TRANSFER_STATUSES = {
    "preparing": "FTsp", "waiting": "FTsw", "transferring": "FTsg",
    "finalizing": "FTsz", "finished": "FTsf", "failed": "FTse",
}

CONNECTION_STATUSES = {
    "disconnecting": "dcng", "connected": "conn",
    "connecting": "cong", "disconnected": "dcon",
}

# Messages.app's own tapback set.
TAPBACKS = {
    "love", "like", "dislike", "laugh", "emphasize", "question",
    "heart", "bubbled", "slapped", "pound", "airplane", "fire",
    "halfHeart", "doubleHeart",
}

# Text effects exposed by the effect picker (CKEffectPickerLargeTextView).
TEXT_EFFECTS = {
    "none", "solid", "outline", "glow", "slam", "echo", "shake",
    "bloom", "newWave", "pop", "rising", "unfurl", "invisibleInk",
    "spotlight", "conductor", "centerRight", "bigLoud", "bubble",
    "largeTitle", "underline", "stars", "hearts", "bubbles",
    "confetti", "sparkles", "strikethrough", "bold", "italics",
    "underlineStyle",
}


class State(str, enum.Enum):
    """Message lifecycle, matching the observable Messages.app states."""
    PENDING = "pending"        # composing / not yet handed to the daemon
    SENDING = "sending"        # CKDraftManager -> IMDaemonController
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    RECEIVED = "received"      # incoming
    PLAYED = "played"          # audio / video completed


TERMINAL_OK = {State.SENT, State.DELIVERED, State.READ, State.RECEIVED, State.PLAYED}


def now_ms() -> int:
    return int(time.time() * 1000)


def new_guid() -> str:
    return str(uuid.uuid4()).upper()


@dataclass
class Run:
    """One run of an attributed string: characters plus attributes.

    Mirrors how ChatKit carries rich text — the carrier is a plain
    NSAttributedString, so formatting lives in per-range attribute
    dictionaries rather than in a dedicated rich-text model class.
    """
    text: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.attributes, dict):
            raise TypeError("attributes must be a dict")

    @property
    def length(self) -> int:
        return len(self.text)

    def is_empty(self) -> bool:
        return not self.text


# Attribute keys ChatKit uses, inferred from the selector names collected.
ATTR_FONT = "NSFont"
ATTR_COLOR = "NSColor"
ATTR_MENTION = "CKMentionAttributeName"
ATTR_LINK = "NSLink"
ATTR_INLINE_ATTACHMENT = "CKInlineAttachmentAttributeName"
ATTR_EFFECT = "CKTextEffectAttributeName"
ATTR_STRIKE = "NSStrikethrough"
ATTR_PARAGRAPH = "NSParagraphStyle"


@dataclass
class Mention:
    handle: str
    range_start: int
    range_length: int
    entity_type: str = "person"

    @property
    def range(self) -> tuple[int, int]:
        return (self.range_start, self.range_start + self.range_length)


@dataclass
class Attachment:
    kind: str = "file"          # file | image | video | audio | sticker | appStrip | location
    url: str | None = None
    guid: str = field(default_factory=new_guid)
    bundle_id: str | None = None   # App Bundle Identifier, from the sdef
    filename: str | None = None
    size: int = 0
    transfer_status: str = "finished"
    syndication_ranges: list[tuple[int, int]] = field(default_factory=list)

    def __post_init__(self):
        if self.transfer_status not in TRANSFER_STATUSES:
            raise ValueError(f"unknown transfer status: {self.transfer_status}")


@dataclass
class Message:
    guid: str = field(default_factory=new_guid)
    chat_id: str = ""
    direction: str = DIRECTION_OUTGOING
    service: str = SERVICE_IMESSAGE
    state: State = State.PENDING
    runs: list[Run] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    effect: str = "none"
    tapbacks: dict[str, int] = field(default_factory=dict)   # tapback -> count
    reply_to_guid: str | None = None
    sender: str | None = None
    date: int = field(default_factory=now_ms)
    delivered_date: int | None = None
    read_date: int | None = None
    error: str | None = None
    source: str = "sim"          # sim | real (ingested from chat.db by watch)
    real_rowid: int | None = None

    def __post_init__(self):
        if self.direction not in (DIRECTION_INCOMING, DIRECTION_OUTGOING):
            raise ValueError(f"unknown direction: {self.direction}")
        if self.service not in (SERVICE_SMS, SERVICE_IMESSAGE, SERVICE_RCS):
            raise ValueError(f"unknown service: {self.service}")
        if self.effect not in TEXT_EFFECTS:
            raise ValueError(f"unknown text effect: {self.effect}")
        for t in self.tapbacks:
            if t not in TAPBACKS:
                raise ValueError(f"unknown tapback: {t}")

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    @property
    def is_text(self) -> bool:
        return bool(self.text) and not self.attachments

    @property
    def is_part(self) -> bool:
        """A message part — maps to CK*MessagePartChatItem."""
        return bool(self.attachments) and not self.text

    @property
    def is_aggregate(self) -> bool:
        return bool(self.text) and bool(self.attachments)

    @property
    def part_class(self) -> str:
        if self.reply_to_guid:
            return "CKReplyContextTextMessagePartChatItem" if self.is_text else \
                   "CKReplyContextAggregateAttachmentMessagePartChatItem"
        if self.attachments and not self.text:
            return ("CKAnimatedEmojiMessagePartChatItem" if self.effect != "none"
                    else "CKAttachmentMessagePartChatItem")
        if self.attachments and self.text:
            return "CKAggregateAttachmentMessagePartChatItem"
        return "CKTextMessagePartChatItem"

    def tapback(self, kind: str, count: int = 1) -> None:
        self.tapbacks[kind] = self.tapbacks.get(kind, 0) + count

    def has_tapback(self, kind: str) -> bool:
        return self.tapbacks.get(kind, 0) > 0

    def advance(self, state: State, *, at: int | None = None) -> None:
        at = at or now_ms()
        self.state = state
        if state is State.DELIVERED or state is State.READ:
            self.delivered_date = self.delivered_date or at
        if state is State.READ:
            self.read_date = at

    @property
    def content_digest(self) -> str:
        h = hashlib.sha256()
        h.update(self.text.encode("utf-8"))
        for a in self.attachments:
            h.update((a.guid + (a.url or "")).encode("utf-8"))
        return h.hexdigest()[:16]

    @property
    def sdef_id(self) -> str:
        """The sdef participant-style id shape: GUID:+handle."""
        return f"{self.guid}:{self.sender or self.chat_id}"
