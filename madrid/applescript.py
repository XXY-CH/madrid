"""Real bridge to Messages.app through AppleScript.

The verbs come from the app's own scripting definition
(/System/Applications/Messages.app/Contents/Resources/Messages.sdef):

    command send  code=ichtsend  "Sends a message to a participant or to a chat."
        direct-parameter: text | file
        parameter "to"    : participant | chat
    command login  code=ichtlogi  "Login to all accounts."
    command logout code=ichtlogo  "Logout of all accounts."

    suite  "Messages Suite"  code=icht
    enums  service type (ssms/sims/srcs), direction (FTic/FTog),
           transfer status (FTsp/FTsw/FTsg/FTsz/FTsf/FTse),
           connection status (dcng/conn/cong/dcon)
    classes participant(pres)/account/chat/file transfer

Nothing here can reach Apple's servers on its own — every real send goes
through the actual Messages.app process, so behaviour is the app's behaviour.

Gating: a real send requires BOTH --real on the command line and the
environment variable MADRID_ALLOW_REAL_SEND=1. Sending to a real contact is
externally visible and irreversible; the double gate is deliberate.
"""
from __future__ import annotations
import os
import subprocess

OSA = "/usr/bin/osascript"
SDEF = "/System/Applications/Messages.app/Contents/Resources/Messages.sdef"
GATE_ENV = "MADRID_ALLOW_REAL_SEND"


class AppleScriptError(RuntimeError):
    pass


def available() -> dict[str, bool]:
    return {
        "osascript": os.path.exists(OSA),
        "sdef": os.path.exists(SDEF),
        "gate": os.environ.get(GATE_ENV) == "1",
    }


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _run(script: str, *, timeout: float = 60.0) -> str:
    if not available()["osascript"]:
        raise AppleScriptError("osascript not found")
    try:
        p = subprocess.run([OSA, "-e", script], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AppleScriptError(f"osascript timed out after {timeout}s") from exc
    if p.returncode != 0:
        raise AppleScriptError((p.stderr or p.stdout or "").strip() or
                               f"osascript exit {p.returncode}")
    return (p.stdout or "").strip()


def _assert_gated() -> None:
    if not available()["gate"]:
        raise AppleScriptError(
            f"real send blocked: set {GATE_ENV}=1 and pass --real to enable")


def send_text(text: str, *, to: str) -> str:
    """`send <text> to <participant>` — the app composes and sends it."""
    _assert_gated()
    if not text:
        raise AppleScriptError("refusing to send empty text")
    if not to:
        raise AppleScriptError("missing recipient handle")
    return _run(
        'tell application "Messages"\n'
        f'    send "{_esc(text)}" to "{_esc(to)}"\n'
        "end tell")


def send_file(path: str, *, to: str) -> str:
    _assert_gated()
    if not os.path.exists(path):
        raise AppleScriptError(f"file not found: {path}")
    return _run(
        'tell application "Messages"\n'
        f'    send file POSIX file "{_esc(os.path.abspath(path))}" '
        f'to "{_esc(to)}"\n'
        "end tell")


def send_text_to_chat(text: str, *, chat_name: str) -> str:
    """`send <text> to <chat>` — group chats addressed by name."""
    _assert_gated()
    return _run(
        'tell application "Messages"\n'
        f'    send "{_esc(text)}" to chat "{_esc(chat_name)}"\n'
        "end tell")


def send_text_to_chat_id(text: str, *, chat_id: str) -> str:
    """`send <text> to chat id "..."` — address by the chat's GUID."""
    _assert_gated()
    return _run(
        'tell application "Messages"\n'
        f'    send "{_esc(text)}" to chat id "{_esc(chat_id)}"\n'
        "end tell")


def send_file_to_chat_id(path: str, *, chat_id: str) -> str:
    """`send <file> to chat id "..."` — the sdef send verb takes file|text."""
    _assert_gated()
    if not os.path.exists(path):
        raise AppleScriptError(f"file not found: {path}")
    return _run(
        'tell application "Messages"\n'
        f'    send file POSIX file "{_esc(os.path.abspath(path))}" '
        f'to chat id "{_esc(chat_id)}"\n'
        "end tell")


def accounts() -> str:
    """Per-account report using only sdef-declared properties.

    Measured on Messages 26 / macOS 27 (2026-10-13): `name` is NOT a
    property of `account` (sdef has description/id/enabled/connection
    status/service type) — `name of every account` errors -1728, and
    `service type` fails -10000 on this build, so each property is fetched
    in its own try and failures degrade to "?".
    """
    return _run(
        'tell application "Messages"\n'
        "    set out to \"\"\n"
        "    repeat with a in accounts\n"
        "        set out to out & (id of a) & \"|\"\n"
        "        try\n"
        "            set out to out & (description of a) & \"|\"\n"
        "        on error\n"
        "            set out to out & \"?|\"\n"
        "        end try\n"
        "        try\n"
        "            set out to out & (enabled of a as string) & \"|\"\n"
        "        on error\n"
        "            set out to out & \"?|\"\n"
        "        end try\n"
        "        try\n"
        "            set out to out & (connection status of a as string) & \"|\"\n"
        "        on error\n"
        "            set out to out & \"?|\"\n"
        "        end try\n"
        "        try\n"
        "            set out to out & (service type of a as string)\n"
        "        on error\n"
        "            set out to out & \"?\"\n"
        "        end try\n"
        "        set out to out & linefeed\n"
        "    end repeat\n"
        "    return out\n"
        "end tell")


def account_report() -> list[dict[str, str]]:
    """accounts() parsed into dicts: id/description/enabled/connection."""
    raw = accounts()
    out = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        parts += ["?"] * (4 - len(parts))
        out.append({"id": parts[0], "description": parts[1],
                    "enabled": parts[2], "connection": parts[3]})
    return out


def app_probe(timeout: float = 15.0) -> dict[str, object]:
    """Quick liveness probe: the standard-suite `version` always answers,
    iChat-suite element queries hang when the app is half-initialized."""
    try:
        v = _run('tell application "Messages" to get version', timeout=timeout)
        return {"app": True, "version": v}
    except AppleScriptError:
        return {"app": False}


def chats() -> str:
    return _run(
        'tell application "Messages"\n'
        "    set out to \"\"\n"
        "    repeat with c in chats\n"
        "        set out to out & (id of c) & \"|\" & (name of c) & linefeed\n"
        "    end repeat\n"
        "    return out\n"
        "end tell")


def participants(chat_id: str) -> str:
    return _run(
        'tell application "Messages"\n'
        f'    repeat with c in chats where id of c is "{_esc(chat_id)}"\n'
        "        set out to \"\"\n"
        "        repeat with p in participants of c\n"
        "            set out to out & (handle of p) & \"|\" & "
        "(full name of p) & linefeed\n"
        "        end repeat\n"
        "        return out\n"
        "    end repeat\n"
        '    return ""\n'
        "end tell")


def login() -> str:
    return _run('tell application "Messages"\n    login\nend tell')


def logout() -> str:
    return _run('tell application "Messages"\n    logout\nend tell')


# ---- rich-text real send: pasteboard + UI scripting -------------------------
# Messages' composition field is an NSTextView: pasting an NSAttributedString
# carries every standard format attribute (bold/italic/underline/strikethrough/
# fonts/colors/links) — the same set its Format menu writes. Stage 1 (this
# file) stages the pasteboard via madrid/native/rich_pasteboard; stage 2 drives the
# app. Mentions/text-effects/bubble-effects are app-UI constructs and do not
# ride the pasteboard — reported as dropped, same as plain send.

RICH_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "native", "rich_pasteboard")


def rich_to_pasteboard(html: str, *, binary: str = "/tmp/rich_pasteboard") -> dict:
    """Build an NSAttributedString from our HTML subset and put it on the
    general pasteboard. Returns the helper's run report (verification)."""
    if not os.access(binary, os.X_OK):
        if not os.path.exists(RICH_HELPER + ".m"):
            raise AppleScriptError(f"helper source missing: {RICH_HELPER}.m")
        subprocess.run(["clang", "-fobjc-arc", "-framework", "Foundation",
                        "-framework", "AppKit", RICH_HELPER + ".m", "-o", binary],
                       capture_output=True, check=True)
    p = subprocess.run([binary, "--html", html], capture_output=True, text=True,
                       timeout=30)
    if p.returncode != 0:
        raise AppleScriptError(f"rich_pasteboard failed: {p.stderr.strip()[:200]}")
    lines = [l for l in p.stdout.splitlines() if l.strip().startswith("{")]
    import json as _json
    report = _json.loads(lines[0]) if lines else {}
    if not all(_json.loads(l).get("pasteboard") for l in lines[1:]):
        raise AppleScriptError("pasteboard write failed")
    return report


def send_rich_via_ui(html: str, *, handle: str, press_return: bool = False) -> dict:
    """Real-send rich text: stage the pasteboard, open the chat, paste, and
    (only with press_return) send. Double-gated like every real send; keep
    press_return=False for a dry run that leaves the text in the composer."""
    _assert_gated()
    report = rich_to_pasteboard(html)
    if not handle:
        raise AppleScriptError("missing recipient handle")
    subprocess.run(["open", "-g", f"imessage://{handle}"], capture_output=True, timeout=10)
    import time
    time.sleep(1.0)
    keys = 'keystroke "v" using command down'
    if press_return:
        keys += '\ndelay 0.4\nkeystroke return'
    script = (
        'tell application "Messages" to activate\n'
        "delay 0.5\n"
        'tell application "System Events"\n'
        '    tell process "Messages"\n'
        f"        {keys}\n"
        "    end tell\n"
        "end tell")
    result = _run(script, timeout=30)
    return {"pasted": True, "sent": bool(press_return), "runs": report.get("runs"),
            "osascript": result}


# ---- Shortcuts channel: headless send via an entitled extension process ---
# `shortcuts run` drives Apple's Shortcuts daemon/extension, which sends the
# message from its own privileged process — no GUI focus, no System Events.
# shortcut (built once in Shortcuts.app) should be:
#   action "Send Message": message <- Shortcut Input, recipient <- fixed
#                         (or first line of input, split in the shortcut)
def send_via_shortcuts(text: str, *, shortcut: str = "madrid send",
                       timeout: float = 120.0) -> dict:
    """Real-send via `shortcuts run` (Apple-sanctioned, headless).

    First run needs Shortcuts' automation permission grant; the shortcut
    itself must exist (see docs/agent-integration.md)."""
    _assert_gated()
    if not text:
        raise AppleScriptError("refusing to send empty text")
    try:
        p = subprocess.run(["shortcuts", "run", shortcut, "-i", text],
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise AppleScriptError("shortcuts CLI not found")
    except subprocess.TimeoutExpired:
        raise AppleScriptError(f"shortcuts run timed out after {timeout}s")
    if p.returncode != 0:
        raise AppleScriptError((p.stderr or p.stdout or "").strip()[:300]
                               or f"shortcuts exit {p.returncode}")
    return {"sent_via": "shortcuts", "shortcut": shortcut, "text": text}


def shortcuts_available() -> bool:
    return subprocess.run(["which", "shortcuts"], capture_output=True).returncode == 0


def mark_chat_read(handle: str) -> dict:
    """Mark a real conversation read by opening it (Messages marks a chat
    read when its conversation is presented). The peer sees a read receipt
    only if the user's "Send Read Receipts" setting is on. Gated: it is
    peer-visible state."""
    _assert_gated()
    if not handle:
        raise AppleScriptError("missing handle")
    subprocess.run(["open", "-g", f"imessage://{handle}"],
                   capture_output=True, timeout=10)
    return {"marked_read": True, "handle": handle,
            "note": "peer sees a receipt only if Send Read Receipts is enabled"}
