"""Standard rich-text interchange for iMessage: HTML subset ⇄ Markdown ⇄ runs.

"标准富文本" layer. The canonical in-memory carrier stays the run list
(text + per-range attribute dictionaries, exactly how ChatKit carries rich
text); this module maps it to/from two standard text formats:

  html      restricted subset: <b> <i> <u> <s> <a href> <br>
            <span data-mention="handle"> <span data-effect="slam">
            <img data-attachment src data-kind>  (inline attachment marker)
  markdown  inline subset: **bold** *italic* ***both*** __underline__
            ~~strike~~ [label](url)

Attribute keys are the REAL keys measured in ~/Library/Messages/chat.db
:

    __kIMTextBoldAttributeName          498 runs   (measured)
    __kIMTextItalicAttributeName         22 runs   (measured, whole-DB scan)
    __kIMTextStrikethroughAttributeName   8 runs   (measured)
    __kIMLinkAttributeName                          (measured)
    __kIMFileTransferGUIDAttributeName              (measured)

Additional keys in the same family: __kIMTextUnderlineAttributeName,
__kIMMentionAttributeName, __kIMTextEffectAttributeName. Both the sim-side and
real keys are understood on input; emission prefers the sim keys and the
real keys survive round-trips via runs.

Inline attachments occupy one U+FFFC (OBJECT REPLACEMENT CHARACTER) in the
text, mirroring how Apple marks them (tapback rows' text starts with
U+FFFC).
"""
from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser
from typing import Any

from . import model

# Apple attributedBody attribute keys.
ATTR_BOLD = "__kIMTextBoldAttributeName"
ATTR_ITALIC = "__kIMTextItalicAttributeName"
ATTR_STRIKE = "__kIMTextStrikethroughAttributeName"
ATTR_UNDERLINE = "__kIMTextUnderlineAttributeName"
ATTR_LINK = "__kIMLinkAttributeName"
ATTR_INLINE_GUID = "__kIMFileTransferGUIDAttributeName"
# Keys per Apple's naming family.
ATTR_MENTION_REAL = "__kIMMentionAttributeName"
ATTR_TEXT_EFFECT = "__kIMTextEffectAttributeName"

MENTION_KEYS = {model.ATTR_MENTION, ATTR_MENTION_REAL}
EFFECT_KEYS = {model.ATTR_EFFECT, ATTR_TEXT_EFFECT}

FORMATTING_TAGS = {
    "b": ATTR_BOLD, "strong": ATTR_BOLD,
    "i": ATTR_ITALIC, "em": ATTR_ITALIC,
    "u": ATTR_UNDERLINE,
    "s": ATTR_STRIKE, "strike": ATTR_STRIKE, "del": ATTR_STRIKE,
}

OBJECT_CHAR = "\ufffc"      # Apple's inline-attachment placeholder

VALID_FORMATS = ("plain", "html", "markdown")


# ---- parsing: html -> runs --------------------------------------------------

class _RichParser(HTMLParser):
    """Build runs from the supported HTML subset; unknown tags are dropped,
    their text kept (lenient by design — agents emit imperfect HTML)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.runs: list[model.Run] = []
        self._stack: list[dict[str, Any]] = []      # active attributes
        self._inline_attachments: list[dict[str, Any]] = []   # inline attachment markers

    def _attrs(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for frame in self._stack:
            merged.update(frame)
        return merged

    def _emit(self, text: str) -> None:
        if not text:
            return
        merged = self._attrs()
        if self.runs and self.runs[-1].attributes == merged:
            self.runs[-1].text += text
        else:
            self.runs.append(model.Run(text=text, attributes=dict(merged)))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in FORMATTING_TAGS:
            self._stack.append({FORMATTING_TAGS[tag]: 1})
        elif tag == "a":
            self._stack.append({ATTR_LINK: a.get("href") or ""})
        elif tag == "br":
            self._emit("\n")
        elif tag == "span":
            frame: dict[str, Any] = {}
            if a.get("data-mention") is not None:
                frame[model.ATTR_MENTION] = a["data-mention"]
                frame["CKMentionsRenderingAttributes"] = {
                    "handleID": a["data-mention"], "entityType": "person"}
            if a.get("data-effect") is not None:
                frame[model.ATTR_EFFECT] = a["data-effect"]
            self._stack.append(frame)
        elif tag in ("img", "audio", "video"):
            guid = a.get("data-attachment")
            self._inline_attachments.append({
                "kind": a.get("data-kind") or ("image" if tag == "img"
                                               else "audio" if tag == "audio"
                                               else "video"),
                "src": a.get("src") or "",
                "guid": guid or "",
            })
            self._emit(OBJECT_CHAR)

    def handle_endtag(self, tag: str) -> None:
        if tag in FORMATTING_TAGS or tag in ("a", "span"):
            if self._stack:
                self._stack.pop()

    def handle_data(self, data: str) -> None:
        self._emit(data)


def html_to_runs(html: str) -> tuple[list[model.Run], list[dict[str, Any]]]:
    """Parse the HTML subset. Returns (runs, inline_attachments) where each
    inline attachment dict is {kind, src, guid?, position} — position is the
    character index of its OBJECT_CHAR in the plain text."""
    p = _RichParser()
    p.feed(html)
    p.close()
    runs = [r for r in p.runs if r.text]
    attachments: list[dict[str, Any]] = []
    pos = -1
    seen = 0
    for r in runs:
        for ch in r.text:
            pos += 1
            if ch == OBJECT_CHAR:
                meta = dict(p._inline_attachments[seen]) if seen < len(p._inline_attachments) else {}
                meta["position"] = pos
                attachments.append(meta)
                seen += 1
    return runs, attachments


# ---- runs -> html -----------------------------------------------------------

_ESC_TEXT = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_ESC_ATTR = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}


def _esc(s: str, table: dict[str, str]) -> str:
    return "".join(table.get(c, c) for c in s)


def runs_to_html(runs: list[model.Run]) -> str:
    out: list[str] = []
    for r in runs:
        text = _esc(r.text, _ESC_TEXT).replace("\n", "<br>")
        guid = r.attributes.get(ATTR_INLINE_GUID)
        if guid and OBJECT_CHAR in r.text:
            kind = r.attributes.get("kind") or "image"
            text = text.replace(
                _esc(OBJECT_CHAR, _ESC_TEXT),
                f'<img data-attachment="{_esc(str(guid), _ESC_ATTR)}" '
                f'data-kind="{_esc(str(kind), _ESC_ATTR)}">')
        if r.attributes.get(ATTR_BOLD):
            text = f"<b>{text}</b>"
        if r.attributes.get(ATTR_ITALIC):
            text = f"<i>{text}</i>"
        if r.attributes.get(ATTR_UNDERLINE):
            text = f"<u>{text}</u>"
        if r.attributes.get(ATTR_STRIKE):
            text = f"<s>{text}</s>"
        link = r.attributes.get(ATTR_LINK)
        if link:
            text = f'<a href="{_esc(str(link), _ESC_ATTR)}">{text}</a>'
        mention = r.attributes.get(model.ATTR_MENTION)
        if mention is None:
            mention = r.attributes.get(ATTR_MENTION_REAL)
        if mention:
            text = f'<span data-mention="{_esc(str(mention), _ESC_ATTR)}">{text}</span>'
        effect = r.attributes.get(model.ATTR_EFFECT)
        if effect is None:
            effect = r.attributes.get(ATTR_TEXT_EFFECT)
        if effect:
            text = f'<span data-effect="{_esc(str(effect), _ESC_ATTR)}">{text}</span>'
        out.append(text)
    return "".join(out)


# ---- markdown -> runs -------------------------------------------------------

# Ordered: bolditalic first so ***x*** is not read as ** + *.
_MD_TOKENS = [
    ("***", {ATTR_BOLD: 1, ATTR_ITALIC: 1}),
    ("___", {ATTR_UNDERLINE: 1, ATTR_ITALIC: 1}),
    ("**", {ATTR_BOLD: 1}),
    ("__", {ATTR_UNDERLINE: 1}),
    ("~~", {ATTR_STRIKE: 1}),
    ("*", {ATTR_ITALIC: 1}),
]
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def md_to_runs(md: str) -> list[model.Run]:
    """Parse the inline-markdown subset. Links bind tighter than emphasis."""
    runs: list[model.Run] = []
    attrs: dict[str, Any] = {}
    i = 0

    def emit(text: str) -> None:
        if not text:
            return
        if runs and runs[-1].attributes == attrs:
            runs[-1].text += text
        else:
            runs.append(model.Run(text=text, attributes=dict(attrs)))

    while i < len(md):
        link = _MD_LINK_RE.match(md, i)
        if link:
            saved = dict(attrs)
            attrs[ATTR_LINK] = link.group(2)
            emit(link.group(1))
            attrs = saved
            i = link.end()
            continue
        matched = False
        for token, a in _MD_TOKENS:
            if md.startswith(token, i):
                if a.keys() <= attrs.keys():       # closing token
                    for k in a:
                        attrs.pop(k, None)
                else:                              # opening token
                    # a token only opens when its closer exists later
                    if md.find(token, i + len(token)) != -1:
                        attrs.update(a)
                i += len(token)
                matched = True
                break
        if not matched:
            j = i
            while j < len(md) and not any(
                    md.startswith(t, j) for t, _ in _MD_TOKENS) \
                    and not _MD_LINK_RE.match(md, j):
                j += 1
            emit(md[i:j])
            i = j
    return runs


def runs_to_md(runs: list[model.Run]) -> str:
    out: list[str] = []
    for r in runs:
        text = r.text
        if not text.strip():
            out.append(text)
            continue
        if r.attributes.get(ATTR_STRIKE):
            text = f"~~{text}~~"
        if r.attributes.get(ATTR_UNDERLINE):
            text = f"__{text}__"
        if r.attributes.get(ATTR_ITALIC):
            text = f"*{text}*"
        if r.attributes.get(ATTR_BOLD):
            text = f"**{text}**"
        link = r.attributes.get(ATTR_LINK)
        if link:
            text = f"[{text}]({link})"
        out.append(text)
    return "".join(out)


# ---- shared projections -----------------------------------------------------

def plain(runs: list[model.Run]) -> str:
    return "".join(r.text for r in runs)


def merge(runs: list[model.Run]) -> list[model.Run]:
    """Merge adjacent runs with identical attributes; drop empties."""
    out: list[model.Run] = []
    for r in runs:
        if not r.text:
            continue
        if out and out[-1].attributes == r.attributes:
            out[-1].text += r.text
        else:
            out.append(model.Run(text=r.text, attributes=dict(r.attributes)))
    return out


def runs_from(text: str, fmt: str = "plain") -> tuple[list[model.Run], list[dict[str, Any]]]:
    """Build runs from text in the given format. Returns (runs, inline_attachments)."""
    if fmt == "plain":
        return ([model.Run(text=text)] if text else []), []
    if fmt == "html":
        return html_to_runs(text)
    if fmt == "markdown":
        return md_to_runs(text), []
    raise ValueError(f"unknown format: {fmt!r} (expected one of {VALID_FORMATS})")


def html_of(runs: list[model.Run]) -> str:
    return runs_to_html(merge(runs))


def md_of(runs: list[model.Run]) -> str:
    return runs_to_md(merge(runs))
