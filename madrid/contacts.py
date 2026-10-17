"""Contact name resolution: handle → display name.

Priority order:
  1. macOS Contacts (AddressBook via osascript, cached)
  2. chat.db handle table (last-known identifier)
  3. Raw handle (phone/email as-is)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from typing import Any

_CACHE_TTL = 300  # 5 min


class ContactResolver:
    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or os.path.expanduser("~/Library/Messages/chat.db")
        self._cache: dict[str, str] = {}
        self._cache_time = 0.0
        self._osascript_available = os.path.exists("/usr/bin/osascript")

    def resolve(self, handle: str) -> str:
        """Best-effort display name for a handle."""
        if not handle or handle == "me":
            return handle or "me"
        now = time.time()
        if now - self._cache_time > _CACHE_TTL:
            self._cache.clear()
            self._cache_time = now
        if handle in self._cache:
            return self._cache[handle]
        name = self._from_contacts(handle) or self._from_db(handle) or handle
        self._cache[handle] = name
        return name

    def resolve_many(self, handles: list[str]) -> dict[str, str]:
        return {h: self.resolve(h) for h in handles}

    # -- macOS Contacts (AppleScript) --------------------------------------

    def _from_contacts(self, handle: str) -> str | None:
        """Query Contacts.app for a matching phone/email → full name."""
        if not self._osascript_available:
            return None
        clean = handle.strip().replace("+", "")
        # Try phone search
        for q in (handle, clean, f"+{clean}"):
            name = self._contacts_query(q)
            if name:
                return name
        return None

    @staticmethod
    def _contacts_query(query: str) -> str | None:
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')
        # Phone search
        script = (
            'tell application "Contacts"\n'
            f'    set results to (every person whose phones value contains "{esc(query)}")\n'
            "    if (count of results) > 0 then\n"
            "        return name of item 1 of results\n"
            "    end if\n"
            "    set results to (every person whose emails value contains \"{esc(query)}\")\n"
            "    if (count of results) > 0 then\n"
            "        return name of item 1 of results\n"
            "    end if\n"
            '    return ""\n'
            "end tell")
        try:
            p = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=10)
            name = (p.stdout or "").strip()
            if name and p.returncode == 0 and name.lower() != "missing value":
                return name
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None

    # -- chat.db handle table -------------------------------------------------

    def _from_db(self, handle: str) -> str | None:
        """Check if chat.db has a canonical form or associated chat name."""
        if not os.path.exists(self._db_path):
            return None
        try:
            db = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            row = db.execute(
                "SELECT chat.display_name FROM handle"
                " JOIN chat_handle_join ON chat_handle_join.handle_id = handle.ROWID"
                " JOIN chat ON chat.ROWID = chat_handle_join.chat_id"
                " WHERE handle.id = ? AND chat.display_name IS NOT NULL"
                " ORDER BY chat.last_addressed_handle DESC LIMIT 1",
                (handle,)).fetchone()
            db.close()
            if row and row[0]:
                return row[0]
        except sqlite3.Error:
            pass
        return None


def format_handle(handle: str) -> str:
    """Pretty-print a phone number or email for display."""
    if re.match(r"^\+?\d{7,15}$", handle):
        # E.164 phone → (XXX) XXX-XXXX or +XX XXXX XXXXXXX
        digits = re.sub(r"\D", "", handle)
        if len(digits) == 11 and digits.startswith("1"):
            return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
        if len(digits) == 10:
            return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
        return handle
    return handle
