"""Statistics over the real chat.db (read-only).

Mirrors imsg's `stats` surface: message counts, media counts, per-chat
activity, top contacts, daily volume.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from .realbox import RealInbox, RealInboxError, ns_to_ms


def _connect(source: str | None = None) -> sqlite3.Connection:
    p = source or os.path.expanduser("~/Library/Messages/chat.db")
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def overview(source: str | None = None) -> dict[str, Any]:
    """Global counts: messages, chats, attachments, handles, media."""
    db = _connect(source)
    try:
        out: dict[str, Any] = {}
        out["total_messages"] = db.execute(
            "SELECT count(*) FROM message").fetchone()[0]
        out["total_chats"] = db.execute(
            "SELECT count(*) FROM chat").fetchone()[0]
        out["total_attachments"] = db.execute(
            "SELECT count(*) FROM attachment").fetchone()[0]
        out["total_media"] = db.execute(
            "SELECT count(*) FROM attachment WHERE mime_type LIKE 'image/%'"
            " OR mime_type LIKE 'video/%' OR mime_type LIKE 'audio/%'").fetchone()[0]
        out["total_stickers"] = db.execute(
            "SELECT count(*) FROM attachment WHERE is_sticker=1").fetchone()[0]
        out["total_tapbacks"] = db.execute(
            "SELECT count(*) FROM message WHERE associated_message_type"
            " BETWEEN 2000 AND 2999").fetchone()[0]
        out["total_edits"] = db.execute(
            "SELECT count(*) FROM message WHERE date_edited != 0").fetchone()[0]
        out["total_retractions"] = db.execute(
            "SELECT count(*) FROM message WHERE date_retracted != 0").fetchone()[0]
        # service split
        out["by_service"] = {
            r[0]: r[1] for r in db.execute(
                "SELECT service, count(*) FROM message GROUP BY service")}
        # date range
        r = db.execute("SELECT MIN(date), MAX(date) FROM message"
                       " WHERE date > 0").fetchone()
        out["oldest"] = ns_to_ms(r[0]) if r[0] else None
        out["newest"] = ns_to_ms(r[1]) if r[1] else None
        return out
    finally:
        db.close()


def by_chat(source: str | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
    """Per-chat message counts + last activity."""
    db = _connect(source)
    try:
        rows = db.execute("""
            SELECT c.guid, c.display_name, c.chat_identifier, c.service_name,
                   count(m.ROWID) as msg_count,
                   MAX(m.date) as last_date,
                   SUM(CASE WHEN m.is_from_me=1 THEN 1 ELSE 0 END) as sent,
                   SUM(CASE WHEN m.is_from_me=0 THEN 1 ELSE 0 END) as recv
            FROM chat c
            JOIN chat_message_join j ON j.chat_id = c.ROWID
            JOIN message m ON m.ROWID = j.message_id
            GROUP BY c.ROWID
            ORDER BY msg_count DESC
            LIMIT ?""", (limit,)).fetchall()
        return [{
            "chat_guid": r["guid"],
            "name": r["display_name"] or r["chat_identifier"],
            "service": r["service_name"],
            "messages": r["msg_count"],
            "sent": r["sent"] or 0,
            "received": r["recv"] or 0,
            "last_activity": ns_to_ms(r["last_date"]),
        } for r in rows]
    finally:
        db.close()


def top_contacts(source: str | None = None, *, limit: int = 10) -> list[dict[str, Any]]:
    """Most-messaged handles (1:1 chats only)."""
    db = _connect(source)
    try:
        rows = db.execute("""
            SELECT h.id as handle, count(m.ROWID) as msg_count
            FROM handle h
            JOIN message m ON m.handle_id = h.ROWID
            WHERE m.is_from_me = 0
            GROUP BY h.ROWID
            ORDER BY msg_count DESC LIMIT ?""", (limit,)).fetchall()
        return [{"handle": r["handle"], "messages_from": r["msg_count"]}
                for r in rows]
    finally:
        db.close()


def daily_volume(source: str | None = None, *, days: int = 7) -> list[dict[str, Any]]:
    """Message count per day for the last N days."""
    db = _connect(source)
    try:
        rows = db.execute("""
            SELECT date(m.date/1000000000, 'unixepoch', 'localtime') as day,
                   count(*) as cnt,
                   SUM(CASE WHEN m.is_from_me=1 THEN 1 ELSE 0 END) as sent
            FROM message m
            WHERE m.date > (strftime('%s','now') - ?) * 1000000000
            GROUP BY day ORDER BY day DESC""", (days * 86400,)).fetchall()
        return [{"date": r["day"], "messages": r["cnt"], "sent": r["sent"] or 0}
                for r in rows]
    finally:
        db.close()


def media_by_type(source: str | None = None) -> dict[str, int]:
    db = _connect(source)
    try:
        rows = db.execute("""
            SELECT COALESCE(mime_type, 'unknown') as mt, count(*) as cnt
            FROM attachment GROUP BY mt ORDER BY cnt DESC""").fetchall()
        return {r["mt"]: r["cnt"] for r in rows}
    finally:
        db.close()
