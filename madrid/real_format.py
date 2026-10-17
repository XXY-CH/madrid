"""Reader for the *real* iMessage ``attributedBody`` format.

The format, for reference:

* storage   an ``NSArchiver`` archive (typedstream) of an ``NSAttributedString``
            -- magic ``\\x04\\x0bstreamtyped`` + version 1000, not gzip, not
            protobuf, not ``NSKeyedArchiver`` (bplist).
* wire      Apple's ``super`` XML-ish markup layer.
* parts     every run of the attributed string carries
            ``__kIMMessagePartAttributeName`` (an ``NSNumber``), and inline
            attachments carry ``__kIMFileTransferGUIDAttributeName`` whose value
            resolves to a row in the ``attachment`` table.

Typedstream framing for payload text and ``NSRLEArray`` run encoding are not
reimplemented here; :func:`foundation_decode` delegates to
``madrid/native/decode_attributed_body.m`` (clang, Foundation). ``class_names``
and ``attribute_keys`` are structural projections needing no unarchiving.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

MAGIC = b"\x04\x0bstreamtyped"
BPLIST_MAGIC = b"bplist00"
GZIP_MAGIC = b"\x1f\x8b"
VERSION_EXPECTED = 1000

# `84 84 [84] <len-byte> <ascii name> 00` per class-table entry. Verified
# against a synthetic NSArchiver archive with known classes.
_CLASS_RE = re.compile(rb"\x84\x84\x84?([\x01-\x7f])([ -~]+)\x00")

# Attribute keys appear literally in the archive (verified on real blobs).
_KEY_RE = re.compile(rb"(?:__kIM|_IMM?|AX)[A-Za-z]{2,40}(?:AttributeName|IsReplied|Options?)")

ROOT = os.path.dirname(os.path.abspath(__file__))
HELPER_SOURCE = os.path.join(ROOT, "native", "decode_attributed_body.m")
HELPER_BINARY = os.environ.get("MADRID_DECODER", "/tmp/decode_attributed_body")


class RealFormatError(ValueError):
    """The blob is not a real attributedBody, or cannot be decoded."""


# ---- identification --------------------------------------------------------

def detect(blob: bytes) -> str:
    """Classify a blob without decoding it.

    Returns one of ``empty``, ``typedstream``, ``bplist``, ``gzip``, ``unknown``.
    """
    if not blob:
        return "empty"
    if blob.startswith(MAGIC):
        return "typedstream"
    if blob.startswith(BPLIST_MAGIC):
        return "bplist"
    if blob.startswith(GZIP_MAGIC):
        return "gzip"
    return "unknown"


def header(blob: bytes) -> dict[str, Any]:
    """Magic/version facts for a blob; ``version`` is None when unreadable."""
    fmt = detect(blob)
    version = None
    if fmt == "typedstream" and len(blob) >= 16:
        code, lo, hi = blob[13], blob[14], blob[15]
        if code == 0x81:
            version = lo | (hi << 8)
    return {
        "format": fmt,
        "magic_hex": blob[:16].hex(),
        "version": version,
        "is_real_attributed_body": fmt == "typedstream",
    }


def require_typedstream(blob: bytes) -> None:
    fmt = detect(blob)
    if fmt != "typedstream":
        raise RealFormatError(
            f"not an NSArchiver typedstream blob: detected {fmt!r} "
            f"(magic {blob[:8].hex()})")


# ---- structural projections (no unarchiving) ------------------------------

def class_names(blob: bytes) -> list[str]:
    """Class table of the archive: the classes the object graph can contain."""
    require_typedstream(blob)
    out: list[str] = []
    for m in _CLASS_RE.finditer(blob):
        name = m.group(2)
        if m.group(1)[0] == len(name):
            out.append(name.decode("ascii"))
    return sorted(set(out))


def attribute_keys(blob: bytes) -> list[str]:
    """Attribute-name literals carried by the archive."""
    require_typedstream(blob)
    return sorted({m.group(0).decode("ascii") for m in _KEY_RE.finditer(blob)})


def summary(blob: bytes) -> dict[str, Any]:
    """Cheap, safe structural report -- no unarchiving, no message text."""
    h = header(blob)
    if h["format"] != "typedstream":
        return h
    keys = attribute_keys(blob)
    return {
        **h,
        "class_table": class_names(blob),
        "attribute_keys": keys,
        "part_bound": "__kIMMessagePartAttributeName" in keys,
        "inline_attachments": "__kIMFileTransferGUIDAttributeName" in keys,
    }


# ---- full decode via Foundation -------------------------------------------

def helper_available() -> bool:
    return os.access(HELPER_BINARY, os.X_OK)


def build_helper(dest: str | None = None) -> str:
    """Compile the Foundation decoder; returns the binary path."""
    out = dest or HELPER_BINARY
    cmd = ["clang", "-fobjc-arc", "-framework", "Foundation", HELPER_SOURCE, "-o", out]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RealFormatError(f"clang failed: {proc.stderr.strip()[:400]}")
    return out


def foundation_decode(blob: bytes, *, with_text: bool = False,
                      binary: str | None = None) -> dict[str, Any]:
    """Decode with the same Foundation that wrote the archive.

    ``NSUnarchiver`` instantiates archived classes, which is why Apple
    deprecated it: only run this on blobs you produced or received yourself.
    """
    require_typedstream(blob)
    binpath = binary or HELPER_BINARY
    if not os.access(binpath, os.X_OK):
        binpath = build_helper(binpath)
    import tempfile
    fd, path = tempfile.mkstemp(prefix="madrid-body-", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        cmd = [binpath, path] + (["--with-text"] if with_text else [])
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 and not proc.stdout.strip():
            raise RealFormatError(f"decoder failed: {proc.stderr.strip()[:400]}")
        return json.loads(proc.stdout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---- the `super` markup ----------------------------------------------------

# Syntax evidence: the serializer's pooled literals are `<%@`,
# ` %@="%@"`, `>`, `</%@>` -> <element attr="value">…</element>. Names are
# runtime arguments, so no tag vocabulary is claimed here.
_ENTITY = {"&lt;": "<", "&gt;": ">", "&amp;": "&"}
_ENTITY_REV = {v: k for k, v in _ENTITY.items()} | {"&": "&amp;"}
_TAG_RE = re.compile(r"<(/?)([A-Za-z_][\w.:-]*)((?:\s+[\w.:-]+\s*=\s*\"[^\"]*\")*)\s*(/?)>")
_ATTR_RE = re.compile(r"([\w.:-]+)\s*=\s*\"([^\"]*)\"")


def super_unescape(text: str) -> str:
    for ent, ch in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        text = text.replace(ent, ch)
    return text


def super_escape(text: str) -> str:
    """Escape for super. `&` first, or `&lt;` would become `&amp;lt;`."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def super_scan(text: str) -> list[dict[str, Any]]:
    """List tags in a super document as {name, close, attrs, self_closing}.

    Read-side helper: it reports the markup that is actually present instead of
    assuming a vocabulary.
    """
    tags = []
    for m in _TAG_RE.finditer(text):
        close, name, raw_attrs, self_close = m.groups()
        tags.append({
            "name": name,
            "close": bool(close),
            "self_closing": bool(self_close),
            "attrs": {k: super_unescape(v) for k, v in _ATTR_RE.findall(raw_attrs or "")},
            "offset": m.start(),
        })
    return tags


def super_to_text(text: str) -> str:
    """Plain-text projection: drop tags, unescape entities.

    Mirrors the role of `IMSuperToPlainParserContext`, whose output is what
    lands in the `message.text` column.
    """
    return super_unescape(_TAG_RE.sub("", text))


# ---- reading a real chat.db (read-only) ------------------------------------

def read_real_db(source: str, *, guid: str | None = None, limit: int = 5,
                 decode: bool = False, with_text: bool = False) -> dict[str, Any]:
    """Read real ``message.attributedBody`` blobs, never writing to the database.

    The connection uses SQLite URI ``immutable=1``: no journal, no locks, no
    writes. Message text is omitted unless ``with_text`` is set, because the
    text column and the decoded string are the user's own content.
    """
    import sqlite3

    if not os.path.exists(source):
        raise RealFormatError(f"no such database: {source}")
    try:
        db = sqlite3.connect(f"file:{source}?immutable=1", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - permissions dependent
        raise RealFormatError(f"cannot open {source} read-only: {exc}") from exc

    query = ("SELECT guid, part_count, text, length(attributedBody), attributedBody "
             "FROM message WHERE attributedBody IS NOT NULL")
    args: list[Any] = []
    if guid:
        query += " AND guid=?"
        args.append(guid)
    query += " ORDER BY date DESC LIMIT ?"
    args.append(int(limit))

    rows = []
    try:
        for g, part_count, text, nbytes, blob in db.execute(query, args):
            blob = bytes(blob)
            row: dict[str, Any] = {
                "guid": g,
                "bytes": nbytes,
                "part_count": part_count,
                **summary(blob),
            }
            if with_text:
                row["text_column"] = text
            if decode:
                try:
                    row["decoded"] = foundation_decode(blob, with_text=with_text)
                except RealFormatError as exc:
                    row["decode_error"] = str(exc)
            rows.append(row)
    finally:
        db.close()
    return {"source": source, "rows": len(rows), "blobs": rows}
