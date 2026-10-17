"""madrid — iMessage rich-text send/receive for agents.

sim mode (default): local, deterministic, no network.
real mode (--real + MADRID_ALLOW_REAL_SEND=1): drives the actual
Messages.app via its own AppleScript definition (send text|file, login,
logout) and decodes the real store read-only (realbox / watch).

Standard rich text: HTML subset / markdown ⇄ attributed runs, with Apple's
Apple __kIM* attribute keys (see madrid/rich.py).

Public surface for agents is the MCP server (madrid.mcp_server), the CLI
(python3 -m madrid) and the watch daemon (python3 -m madrid watch).
"""
from .model import (Attachment, Mention, Message, Run, State, TAPBACKS,
                    TEXT_EFFECTS)
from .engine import Engine

__all__ = ["Engine", "Message", "Run", "Mention", "Attachment", "State",
           "TAPBACKS", "TEXT_EFFECTS"]
__version__ = "1.0.0"
