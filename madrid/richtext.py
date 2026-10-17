"""attributedBody encode/decode.

Observed in the extracted binaries:
  IMCore      sel=attributedBodyData / attributedBodyText
  IMDPersistence  IMDMessageRecord.attributedBodyText,
                  IMDIndexableMessageRecord.attributedBody
  IMDPersistence  IMDAttachmentSyndicationUtilities
                  _attachmentGUIDIsSyndicatable:...attributedBody:encodedSyndicationRanges:isCMM:preflightStage:verbose:

This module is the *simulator's own* container: gzip'd JSON that keeps the
same field names and semantic shape (text + attribute runs + syndication
ranges) so the sim is round-trippable and inspectable.

It is NOT Apple's format. The real, measured format is an NSArchiver
typedstream archive (magic \x04\x0bstreamtyped + version 1000) of an
NSAttributedString, whose wire form is the `super` XML markup -- see
Read real blobs with `madrid.real_format`, not this module.
"""
from __future__ import annotations
import base64
import gzip
import json
from typing import Any

from . import model

CONTAINER_VERSION = 1


def _run_to_dict(r: model.Run) -> dict[str, Any]:
    return {"text": r.text, "attributes": dict(r.attributes)}


def _dict_to_run(d: dict[str, Any]) -> model.Run:
    return model.Run(text=d.get("text", ""), attributes=d.get("attributes") or {})


def encode(message: model.Message) -> bytes:
    """Return the attributedBody blob for a message."""
    payload: dict[str, Any] = {
        "v": CONTAINER_VERSION,
        "guid": message.guid,
        "text": message.text,
        "runs": [_run_to_dict(r) for r in message.runs],
        "effect": message.effect,
        "mentions": [
            {"handle": m.handle, "start": m.range_start,
             "length": m.range_length, "type": m.entity_type}
            for m in message.mentions
        ],
        "attachments": [
            {"guid": a.guid, "kind": a.kind, "uti": a.kind,
             "bundleIdentifier": a.bundle_id, "filename": a.filename,
             "size": a.size, "transferStatus": TRANSFER_CODE.get(a.transfer_status, a.transfer_status),
             "encodedSyndicationRanges": [list(r) for r in a.syndication_ranges]}
            for a in message.attachments
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return gzip.compress(raw, 6)


def decode(blob: bytes) -> dict[str, Any]:
    if not blob:
        raise ValueError("empty attributedBody")
    try:
        raw = gzip.decompress(blob)
    except OSError as exc:
        raise ValueError(f"attributedBody is not gzip: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"attributedBody payload is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("attributedBody payload must be an object")
    return payload


def decode_runs(blob: bytes) -> list[model.Run]:
    payload = decode(blob)
    return [_dict_to_run(d) for d in payload.get("runs", [])]


def decode_text(blob: bytes) -> str:
    return decode(blob).get("text", "")


def b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


def from_b64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def attributed_string_plan(text: str,
                           mentions: list[model.Mention],
                           effect: str = "none") -> list[model.Run]:
    """Split text into runs, attaching mention attributes to the ranges
    they cover. Mirrors how CKMentionsController decorates a string."""
    runs = [model.Run(text=text)] if text else []
    return decorate(runs, mentions, effect)


def decorate(runs: list[model.Run], mentions: list[model.Mention],
             effect: str = "none") -> list[model.Run]:
    """Apply mention/effect attributes onto existing runs, splitting at the
    mention boundaries — the rich-format carrier plus decorations path."""
    text = "".join(r.text for r in runs)
    if not text:
        return list(runs)
    ordered: list[tuple[int, int, dict[str, Any]]] = []
    for m in mentions:
        start = max(0, m.range_start)
        end = min(len(text), m.range_start + m.range_length)
        if end > start:
            ordered.append((start, end, {
                model.ATTR_MENTION: m.handle,
                "CKMentionsRenderingAttributes": {
                    "handleID": m.handle, "entityType": m.entity_type},
            }))
    if effect != "none":
        ordered.append((0, len(text), {model.ATTR_EFFECT: effect}))

    # map each character offset to its source run slice
    out: list[model.Run] = []
    pos = 0
    spans: list[tuple[int, int, dict[str, Any]]] = []   # (start, end, base attrs)
    for r in runs:
        spans.append((pos, pos + len(r.text), dict(r.attributes)))
        pos += len(r.text)
    boundaries = sorted({0, len(text)} | {s for s, _, _ in ordered} | {e for _, e, _ in ordered}
                        | {s for s, e, _ in spans} | {e for _, e, _ in spans})
    for i in range(len(boundaries) - 1):
        lo, hi = boundaries[i], boundaries[i + 1]
        if hi <= lo:
            continue
        base: dict[str, Any] = {}
        for s, e, a in spans:
            if s <= lo and hi <= e:
                base.update(a)
        for s, e, a in ordered:
            if s <= lo and hi <= e:
                base.update(a)
        chunk = text[lo:hi]
        if out and out[-1].attributes == base:
            out[-1].text += chunk
        else:
            out.append(model.Run(text=chunk, attributes=base))
    return out


def plain(runs: list[model.Run]) -> str:
    return "".join(r.text for r in runs)


TRANSFER_CODE = model.TRANSFER_STATUSES
