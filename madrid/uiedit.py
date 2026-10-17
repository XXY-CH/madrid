"""Real typewriter: UI-driven editing of the newest outgoing bubble.

Why this works reliably: right after the bridge sends frame N, that message
is the LAST bubble in the open transcript — so "edit the newest bubble" is a
stable invariant, no sender detection needed.

Flow (System Events / Accessibility):
  1. activate Messages (target conversation already open via imessage://)
  2. collect AXStaticText elements of the transcript, take the last
  3. perform AXShowMenu (right-click) on it
  4. click the localized Edit menu item
  5. cmd+A, type the new frame, Return

Two run modes:
  verify()   — opens the context menu, records the item names, presses Esc.
               No message is edited or sent. Run this once on a SELF-CHAT to
               confirm permissions and the localized Edit label.
  edit_last(text) — performs the edit. Gated like every real send.

Experimental: UI structure varies by OS release; verify() first, always.
The osascript runner is injectable for tests (run_script=...).
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Callable

EDIT_MENU_CANDIDATES = ["编辑…", "编辑", "Edit…", "Edit", "Editar", "Bearbeiten"]

OSA = "/usr/bin/osascript"


class UIEditError(RuntimeError):
    pass


def _default_run(script: str, timeout: float = 30.0) -> str:
    p = subprocess.run([OSA, "-e", script], capture_output=True, text=True,
                       timeout=timeout)
    if p.returncode != 0:
        raise UIEditError((p.stderr or p.stdout or "").strip()[:300]
                          or f"osascript exit {p.returncode}")
    return p.stdout.strip()


def _gate() -> None:
    if os.environ.get("MADRID_ALLOW_REAL_SEND") != "1":
        raise UIEditError("gated: set MADRID_ALLOW_REAL_SEND=1 "
                          "(this edits a real message)")


_COLLECT = """
on collectTexts(e, depth, out)
    if depth > 12 then return out
    tell application "System Events"
        try
            set r to role of e
        on error
            set r to ""
        end try
        if r is "AXStaticText" then
            try
                set end of out to (value of e) as text
            end try
        end if
        try
            repeat with c in UI elements of e
                collectTexts(c, depth + 1, out)
            end repeat
        end try
    end tell
    return out
end collectTexts

tell application "Messages" to activate
delay 0.5
tell application "System Events"
    tell process "Messages"
        set out to {}
        collectTexts(front window, 0, out)
        set {txtCount, lastTxt} to {count of out, ""}
        if txtCount > 0 then set lastTxt to item txtCount of out
        return txtCount & linefeed & lastTxt
    end tell
end tell
"""


def last_bubble_text(run_script: Callable[..., str] = _default_run) -> tuple[int, str]:
    out = run_script(_COLLECT).splitlines()
    return int(out[0]), (out[1] if len(out) > 1 else "")


def verify(run_script: Callable[..., str] = _default_run) -> dict:
    """Open the context menu on the newest bubble, list items, Esc out."""
    script = """
on deepLast(e, depth, best)
    if depth > 12 then return best
    tell application "System Events"
        try
            if role of e is "AXStaticText" then set best to e
        end try
        try
            repeat with c in UI elements of e
                set best to my deepLast(c, depth + 1, best)
            end repeat
        end try
    end tell
    return best
end deepLast

tell application "Messages" to activate
delay 0.4
tell application "System Events"
    tell process "Messages"
        set bubble to my deepLast(front window, 0, missing value)
        if bubble is missing value then error "no bubble found"
        perform action "AXShowMenu" of bubble
        delay 0.5
        set names to {}
        try
            repeat with mi in menu items of menu 1 of process "Messages"
                try
                    set end of names to name of mi
                end try
            end repeat
        end try
        -- dismiss the menu without choosing anything
        key code 53
        return (count of names) & linefeed & my join(names, "|")
    end tell
end tell

on join(lst, sep)
    set out to ""
    repeat with x in lst
        set out to out & (x as text) & sep
    end repeat
    if out is not "" then set out to text 1 thru -2 of out
    return out
end join
"""
    out = run_script(script).splitlines()
    names = out[1].split("|") if len(out) > 1 and out[1] else []
    edit_item = next((n for n in names
                      if any(n.startswith(c.rstrip("…")) for c in EDIT_MENU_CANDIDATES)), None)
    return {"menu_items": names, "edit_item": edit_item,
            "editable": edit_item is not None}


def edit_last(new_text: str, *, run_script: Callable[..., str] = _default_run,
              menu_item: str | None = None) -> dict:
    """Edit the newest bubble to new_text (the typewriter frame step)."""
    _gate()
    info = verify(run_script)          # also localizes the Edit label safely
    label = menu_item or info.get("edit_item")
    if not label:
        raise UIEditError(f"no Edit item in context menu: {info.get('menu_items')}")

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = f"""
on deepLast(e, depth, best)
    if depth > 12 then return best
    tell application "System Events"
        try
            if role of e is "AXStaticText" then set best to e
        end try
        try
            repeat with c in UI elements of e
                set best to my deepLast(c, depth + 1, best)
            end repeat
        end try
    end tell
    return best
end deepLast

tell application "Messages" to activate
delay 0.3
tell application "System Events"
    tell process "Messages"
        set bubble to my deepLast(front window, 0, missing value)
        if bubble is missing value then error "no bubble found"
        perform action "AXShowMenu" of bubble
        delay 0.4
        click (first menu item of menu 1 of process "Messages" whose name begins with "{esc(label.rstrip('…'))}")
        delay 0.5
        keystroke "a" using command down
        delay 0.1
        keystroke "{esc(new_text)}"
        delay 0.2
        keystroke return
    end tell
end tell
"""
    run_script(script, timeout=40.0)
    return {"edited_to": new_text, "menu_item": label}
