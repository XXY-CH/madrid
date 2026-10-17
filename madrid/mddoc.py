"""Markdown document mode: map full agent markdown onto what iMessage can
render, and turn what it cannot (tables, code blocks) into inline PNG cards.

Strategy (per block):
  paragraphs / lists / quotes  -> real text (inline md -> attribute runs)
  headings                     -> bold text runs
  hr                           -> a line of ─ characters (text)
  fenced code / tables         -> AppKit-rendered PNG card, attached INLINE at
                                  a U+FFFC placeholder position (native
                                  mixed text+attachment flow)

Rendering uses madrid/native/render_html_png.m (clang + AppKit). When the
helper cannot be built, mixed mode degrades gracefully: cards become ordinary
image attachments without files, and the message text keeps placeholders.
"""
from __future__ import annotations

import html as _html
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

from . import model, rich

RENDERER_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "native", "render_html_png.m")
RENDERER_BIN = os.environ.get("MADRID_RENDERER", "/tmp/madrid_render_html_png")
CARD_CSS = ("font-family:-apple-system;font-size:13px;color:#111;"
            "table{border-collapse:collapse;font-size:12px}"
            "th,td{border:1px solid #bbb;padding:3px 8px;text-align:left}"
            "th{background:#f2f2f2}")


# ---- block parsing -----------------------------------------------------------

@dataclass
class Block:
    kind: str                      # text|heading|hr|list|quote|code|table
    text: str = ""
    level: int = 0
    ordered: bool = False
    items: list[str] = field(default_factory=list)
    lang: str = ""
    rows: list[list[str]] = field(default_factory=list)


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and set(s) <= set("|-: ") and "-" in s and "|" in s


def md_blocks(md: str) -> list[Block]:
    lines = md.splitlines()
    out: list[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            lang = s[3:].strip()
            i += 1
            body = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            out.append(Block("code", lang=lang, text="\n".join(body)))
        elif s.startswith("#"):
            level = len(s) - len(s.lstrip("#"))
            out.append(Block("heading", level=min(level, 4), text=s.lstrip("#").strip()))
            i += 1
        elif set(s) <= set("-_* ") and len(s) >= 3:
            out.append(Block("hr"))
            i += 1
        elif s.startswith("|") and i + 1 < len(lines) and _is_table_sep(lines[i + 1]):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not _is_table_sep(lines[i]):
                    rows.append(cells)
                i += 1
            out.append(Block("table", rows=rows))
        elif s.startswith(">"):
            body = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                body.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(Block("quote", text=" ".join(x for x in body if x)))
        elif (s.startswith(("-", "*", "+")) and len(s) > 2) or \
             (len(s) > 2 and s.split(".", 1)[0].isdigit() and s[1] == "."):
            ordered = s[0].isdigit()
            items = []
            while i < len(lines):
                t = lines[i].strip()
                if ordered and t and t.split(".", 1)[0].isdigit() and t[1] == ".":
                    items.append(t.split(".", 1)[1].strip())
                elif not ordered and t[:1] in ("-", "*", "+"):
                    items.append(t[1:].strip())
                elif t and items and not t.startswith(("#", "|", ">", "```")):
                    items[-1] += " " + t          # lazy continuation
                else:
                    break
                i += 1
            out.append(Block("list", ordered=ordered, items=items))
        else:
            body = [s]
            i += 1
            while i < len(lines):
                t = lines[i].strip()
                if (not t or t.startswith(("#", "|", ">", "```", "- ", "* ", "+ "))
                        or (t and t[0].isdigit() and t[1:2] == ".")):
                    break
                body.append(t)
                i += 1
            out.append(Block("text", text=" ".join(body)))
    return out


# ---- mixed assembly ----------------------------------------------------------

def md_to_mixed(md: str) -> tuple[list[model.Run], list[dict[str, Any]]]:
    """Returns (runs, cards). cards = [{html, title, position}] where position
    is the U+FFFC placeholder index in the runs' plain text."""
    runs: list[model.Run] = []
    cards: list[dict[str, Any]] = []

    def emit_text(md_inline: str, prefix_attrs: dict | None = None,
                  prefix_text: str = "") -> None:
        piece = md_to_runs_local(prefix_text + md_inline)
        if prefix_attrs and piece:
            merged = dict(prefix_attrs)
            merged.update(piece[0].attributes)
            piece[0] = model.Run(text=piece[0].text, attributes=merged)
        runs.extend(piece)

    def newline() -> None:
        runs.append(model.Run(text="\n"))

    first = True
    for b in md_blocks(md):
        if not first:
            newline()
        first = False
        if b.kind == "text":
            emit_text(b.text)
        elif b.kind == "heading":
            emit_text(b.text or f"h{b.level}",
                      prefix_attrs={rich.ATTR_BOLD: 1})
        elif b.kind == "hr":
            runs.append(model.Run(text="\u2500" * 12))
        elif b.kind == "quote":
            emit_text(b.text, prefix_text="\u275d ")
        elif b.kind == "list":
            for n, item in enumerate(b.items, 1):
                if n > 1:
                    newline()
                bullet = f"{n}. " if b.ordered else "\u2022 "
                emit_text(item, prefix_text=bullet)
        elif b.kind == "code":
            cards.append({"html": code_html(b.text, b.lang),
                          "title": f"code{('.' + b.lang) if b.lang else ''}",
                          "position": None})
            runs.append(model.Run(text=rich.OBJECT_CHAR))
        elif b.kind == "table":
            cards.append({"html": table_html(b.rows), "title": "table",
                          "position": None})
            runs.append(model.Run(text=rich.OBJECT_CHAR))
    # annotate card positions from the final text
    text = rich.plain(runs)
    seen = 0
    for c in cards:
        seen = text.find(rich.OBJECT_CHAR, seen)
        c["position"] = seen
        seen += 1
    return runs, cards


def md_to_runs_local(s: str) -> list[model.Run]:
    return rich.md_to_runs(s) if s else []


def code_html(code: str, lang: str = "") -> str:
    body = _html.escape(code)
    label = f'<div style="color:#888;font-size:10px;margin-bottom:2px">{_html.escape(lang)}</div>' if lang else ""
    return (f'<div>{label}<pre style="font-family:Menlo,monospace;font-size:12px;'
            f'background:#f6f8fa;padding:6px;border-radius:6px;white-space:pre-wrap;'
            f'margin:0">{body}</pre></div>')


def table_html(rows: list[list[str]]) -> str:
    if not rows:
        return "<div></div>"
    head = rows[0]

    def tr(cells: list[str], tag: str) -> str:
        return "<tr>" + "".join(f"<{tag}>{_html.escape(c)}</{tag}>" for c in cells) + "</tr>"

    body = "".join(tr(r, "td") for r in rows[1:])
    return (f'<table>{tr(head, "th")}{body}</table>')


def card_document(card_html: str) -> str:
    return f'<html><head><style>{CARD_CSS}</style></head><body>{card_html}</body></html>'


# ---- PNG rendering -----------------------------------------------------------

def _ensure_renderer() -> bool:
    if os.access(RENDERER_BIN, os.X_OK):
        return True
    if not os.path.exists(RENDERER_SRC):
        return False
    p = subprocess.run(["clang", "-fobjc-arc", "-framework", "Foundation",
                        "-framework", "AppKit", RENDERER_SRC, "-o", RENDERER_BIN],
                       capture_output=True)
    return p.returncode == 0


def render_cards(cards: list[dict[str, Any]], *, out_dir: str | None = None,
                 max_width: int = 720) -> list[dict[str, Any]]:
    """Materialize cards as PNG files; returns attachment specs (url/kind/
    filename/position). Unrenderable cards degrade to positionless specs."""
    out_dir = out_dir or tempfile.mkdtemp(prefix="madrid-md-")
    specs: list[dict[str, Any]] = []
    renderer_ok = _ensure_renderer()
    for idx, c in enumerate(cards):
        path = os.path.join(out_dir, f"card-{idx:02d}-{c['title']}.png")
        ok = False
        if renderer_ok:
            p = subprocess.run([RENDERER_BIN, "-", path, str(max_width)],
                               input=card_document(c["html"]).encode(),
                               capture_output=True, timeout=30)
            ok = p.returncode == 0 and os.path.exists(path)
        specs.append({"kind": "image",
                      "url": f"file://{path}" if ok else None,
                      "filename": os.path.basename(path),
                      "position": c["position"],
                      "rendered": ok})
    return specs


def composite_html(runs: list[model.Run],
                   specs: list[dict[str, Any]]) -> str:
    """One HTML document mixing the text portion with <img> at placeholder
    positions — the pasteboard form of a mixed message."""
    parts: list[str] = []
    for r in runs:
        if r.text == rich.OBJECT_CHAR:
            specs_match = next((s for s in specs
                                if s.get("position") is not None), None)
            # positions are baked into specs; use in-order consumption
            specs_match = specs.pop(0) if specs else None
            if specs_match and specs_match.get("url"):
                parts.append(f'<img src="{_html.escape(specs_match["url"])}">')
            else:
                parts.append("\ufffc")
        else:
            t = _html.escape(r.text).replace("\n", "<br>")
            if r.attributes.get(rich.ATTR_BOLD):
                t = f"<b>{t}</b>"
            if r.attributes.get(rich.ATTR_ITALIC):
                t = f"<i>{t}</i>"
            if r.attributes.get(rich.ATTR_UNDERLINE):
                t = f"<u>{t}</u>"
            if r.attributes.get(rich.ATTR_STRIKE):
                t = f"<s>{t}</s>"
            link = r.attributes.get(rich.ATTR_LINK)
            if link:
                t = f'<a href="{_html.escape(str(link))}">{t}</a>'
            parts.append(t)
    return "".join(parts)
