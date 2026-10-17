"""SQLite persistence shaped after IMDPersistence.

Column names follow Apple's IMDPersistence record vocabulary
(IMDMessageRecord.attributedBodyText, IMDIndexableMessageRecord.attributedBody,
attachment rows carrying guid/UTI/syndication ranges). This is a
*shaped-after* schema, not a copy of Apple's live database (which lives in
~/Library/Messages/chat.db and is never written by this package).
"""
from __future__ import annotations
import base64
import json
import os
import sqlite3
import threading
from typing import Iterable

from . import model, richtext

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (
    id              TEXT PRIMARY KEY,
    account_guid    TEXT NOT NULL,
    service         TEXT NOT NULL DEFAULT 'sims',
    style           INTEGER NOT NULL DEFAULT 2,
    name            TEXT,
    is_archived     INTEGER NOT NULL DEFAULT 0,
    is_muted        INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    last_message_at INTEGER
);

CREATE TABLE IF NOT EXISTS handle (
    id       TEXT PRIMARY KEY,
    service  TEXT NOT NULL DEFAULT 'sims',
    name     TEXT,
    first_name TEXT,
    last_name  TEXT
);

CREATE TABLE IF NOT EXISTS chat_handle (
    chat_id   TEXT NOT NULL,
    handle_id TEXT NOT NULL,
    PRIMARY KEY (chat_id, handle_id)
);

CREATE TABLE IF NOT EXISTS message (
    id                 TEXT PRIMARY KEY,
    chat_id           TEXT NOT NULL,
    direction         TEXT NOT NULL,
    service           TEXT NOT NULL DEFAULT 'sims',
    state             TEXT NOT NULL DEFAULT 'pending',
    text              TEXT NOT NULL DEFAULT '',
    attributed_body   BLOB,
    attributed_text   TEXT NOT NULL DEFAULT '',
    effect            TEXT NOT NULL DEFAULT 'none',
    tapbacks          TEXT NOT NULL DEFAULT '{}',
    reply_to_guid    TEXT,
    sender           TEXT,
    error            TEXT,
    is_read          INTEGER NOT NULL DEFAULT 0,
    date             INTEGER NOT NULL,
    delivered_at     INTEGER,
    read_at          INTEGER,
    content_digest   TEXT
);
CREATE INDEX IF NOT EXISTS message_chat_date ON message (chat_id, date);

CREATE TABLE IF NOT EXISTS attachment (
    guid        TEXT PRIMARY KEY,
    message_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    uti         TEXT,
    bundle_id   TEXT,
    filename    TEXT,
    url         TEXT,
    size        INTEGER NOT NULL DEFAULT 0,
    transfer_status TEXT NOT NULL DEFAULT 'finished',
    syndication_ranges TEXT NOT NULL DEFAULT '[]',
    ordinal     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS attachment_message ON attachment (message_id, ordinal);

CREATE TABLE IF NOT EXISTS message_edit (
    guid        TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    html        TEXT,
    at          INTEGER NOT NULL,
    PRIMARY KEY (guid, ordinal)
);
CREATE INDEX IF NOT EXISTS message_edit_guid ON message_edit (guid, ordinal);

CREATE TABLE IF NOT EXISTS typing (
    chat_id    TEXT PRIMARY KEY,
    handle_id  TEXT,
    is_typing  INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    kind    TEXT NOT NULL,
    guid    TEXT,
    chat_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}'
);
"""


class _ThreadSafeDB:
    """sqlite3 objects are thread-bound by default; the HTTP server and the
    watch daemon call the store from worker threads. Serialise everything."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, *a, **kw):
        with self._lock:
            return self._conn.execute(*a, **kw)

    def executescript(self, *a, **kw):
        with self._lock:
            return self._conn.executescript(*a, **kw)

    def commit(self):
        with self._lock:
            return self._conn.commit()

    def close(self):
        with self._lock:
            return self._conn.close()

    def __enter__(self):
        self._lock.acquire()
        return self._conn.__enter__()

    def __exit__(self, *a):
        try:
            return self._conn.__exit__(*a)
        finally:
            self._lock.release()

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, v):
        self._conn.row_factory = v


def clean_row(row) -> dict:
    """Rows carry attributed_body as a BLOB; JSON cannot take bytes."""
    d = dict(row)
    blob = d.get("attributed_body")
    if isinstance(blob, (bytes, bytearray)):
        d["attributed_body"] = base64.b64encode(blob).decode("ascii")
    for col in ("tapbacks", "syndication_ranges"):
        if isinstance(d.get(col), str):
            try:
                d[col] = json.loads(d[col] or "null")
            except json.JSONDecodeError:
                d[col] = None
    return d


class Store:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._db = _ThreadSafeDB(sqlite3.connect(self.path, check_same_thread=False))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        """Idempotent column adds for schemas created before ingest existed."""
        have = {r[1] for r in self._db.execute("PRAGMA table_info(message)")}
        for col, ddl in (("source", "TEXT NOT NULL DEFAULT 'sim'"),
                         ("real_rowid", "INTEGER"),
                         ("edited_at", "INTEGER"),
                         ("retracted_at", "INTEGER")):
            if col not in have:
                self._db.execute(f"ALTER TABLE message ADD COLUMN {col} {ddl}")
                if col == "source":
                    self._db.execute("UPDATE message SET source='sim' WHERE source IS NULL")

    def close(self) -> None:
        self._db.close()

    # ---- chats ----------------------------------------------------------
    def upsert_chat(self, chat_id: str, *, account_guid: str = "local",
                    service: str = model.SERVICE_IMESSAGE, name: str | None = None,
                    style: int = 2, handles: Iterable[str] = ()) -> None:
        now = model.now_ms()
        with self._db:
            self._db.execute(
                """INSERT INTO chat (id, account_guid, service, style, name, created_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     service=excluded.service,
                     name=COALESCE(excluded.name, chat.name)""",
                (chat_id, account_guid, service, style, name, now))
            for h in handles:
                self._db.execute(
                    "INSERT OR IGNORE INTO handle (id, service) VALUES (?,?)", (h, service))
                self._db.execute(
                    "INSERT OR IGNORE INTO chat_handle (chat_id, handle_id) VALUES (?,?)",
                    (chat_id, h))

    def chats(self) -> list[dict]:
        rows = self._db.execute(
            """SELECT c.*, (SELECT group_concat(h.id, ',') FROM chat_handle ch
                              JOIN handle h ON h.id = ch.handle_id WHERE ch.chat_id = c.id) AS handles
               FROM chat c ORDER BY COALESCE(c.last_message_at, c.created_at) DESC""").fetchall()
        return [dict(r) for r in rows]

    def touch_chat(self, chat_id: str, at: int) -> None:
        with self._db:
            self._db.execute("UPDATE chat SET last_message_at=? WHERE id=?", (at, chat_id))

    def set_chat_flag(self, chat_id: str, column: str, value: bool) -> None:
        if column not in ("is_archived", "is_muted"):
            raise ValueError(f"unsupported chat flag: {column}")
        with self._db:
            self._db.execute(f"UPDATE chat SET {column}=? WHERE id=?", (int(value), chat_id))

    # ---- messages -------------------------------------------------------
    def save_message(self, m: model.Message) -> None:
        blob = richtext.encode(m) if (m.runs or m.attachments or m.effect != "none") else None
        with self._db:
            self._db.execute(
                """INSERT INTO message (id, chat_id, direction, service, state, text,
                     attributed_body, attributed_text, effect, tapbacks, reply_to_guid,
                     sender, error, is_read, date, delivered_at, read_at, content_digest,
                     source, real_rowid)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     state=excluded.state,
                     text=excluded.text,
                     attributed_body=excluded.attributed_body,
                     attributed_text=excluded.attributed_text,
                     effect=excluded.effect,
                     tapbacks=excluded.tapbacks,
                     error=excluded.error,
                     is_read=excluded.is_read,
                     delivered_at=excluded.delivered_at,
                     read_at=excluded.read_at,
                     source=excluded.source,
                     real_rowid=excluded.real_rowid""",
                (m.guid, m.chat_id, m.direction, m.service, m.state.value, m.text,
                 blob, m.text, m.effect, json.dumps(m.tapbacks, sort_keys=True),
                 m.reply_to_guid, m.sender, m.error,
                 int(m.state is model.State.READ), m.date, m.delivered_date, m.read_date,
                 m.content_digest, m.source, m.real_rowid))
            for i, a in enumerate(m.attachments):
                self._db.execute(
                    """INSERT OR REPLACE INTO attachment (guid, message_id, kind, uti, bundle_id,
                         filename, url, size, transfer_status, syndication_ranges, ordinal)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (a.guid, m.guid, a.kind, a.kind, a.bundle_id, a.filename, a.url,
                     a.size, a.transfer_status,
                     json.dumps([list(r) for r in a.syndication_ranges]), i))
            self.touch_chat(m.chat_id, m.date)

    def get_message(self, guid: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM message WHERE id=?", (guid,)).fetchone()

    def messages(self, chat_id: str, *, limit: int = 50,
                 before: int | None = None) -> list[dict]:
        q = "SELECT * FROM message WHERE chat_id=?"
        args: list[object] = [chat_id]
        if before is not None:
            q += " AND date < ?"
            args.append(before)
        q += " ORDER BY date DESC LIMIT ?"
        args.append(limit)
        rows = self._db.execute(q, args).fetchall()
        out = []
        for r in reversed(rows):
            d = clean_row(r)
            d["attachments"] = [clean_row(a) for a in self._db.execute(
                "SELECT * FROM attachment WHERE message_id=? ORDER BY ordinal", (r["id"],))]
            out.append(d)
        return out

    def update_state(self, guid: str, state: model.State, **dates: int) -> None:
        sets, args = ["state=?"], [state.value]
        for k, v in dates.items():
            if k not in ("delivered_at", "read_at", "date"):
                raise ValueError(f"unsupported date column: {k}")
            sets.append(f"{k}=?")
            args.append(v)
        if state is model.State.READ:
            sets.append("is_read=1")
        args.append(guid)
        with self._db:
            self._db.execute(f"UPDATE message SET {', '.join(sets)} WHERE id=?", args)

    def add_tapback(self, guid: str, kind: str) -> dict | None:
        if kind not in model.TAPBACKS:
            raise ValueError(f"unknown tapback: {kind}")
        row = self.get_message(guid)
        if row is None:
            return None
        taps = json.loads(row["tapbacks"] or "{}")
        taps[kind] = taps.get(kind, 0) + 1
        with self._db:
            self._db.execute("UPDATE message SET tapbacks=? WHERE id=?",
                             (json.dumps(taps, sort_keys=True), guid))
        return taps

    # ---- edits (mirrors the protocol: retraction == edit that drops parts) --
    EDIT_HISTORY_LIMIT = 5      # protocol edit-history limit

    def record_edit(self, guid: str, *, text: str, html: str | None) -> int:
        """Append an edit frame; prune beyond the protocol history limit."""
        with self._db:
            n = self._db.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM message_edit WHERE guid=?",
                (guid,)).fetchone()[0]
            self._db.execute(
                "INSERT INTO message_edit (guid, ordinal, text, html, at) VALUES (?,?,?,?,?)",
                (guid, n, text, html, model.now_ms()))
            self._db.execute(
                "DELETE FROM message_edit WHERE guid=? AND ordinal <= ?",
                (guid, n - self.EDIT_HISTORY_LIMIT))
        return n

    def edit_history(self, guid: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT ordinal, text, html, at FROM message_edit WHERE guid=? ORDER BY ordinal",
            (guid,)).fetchall()
        return [dict(r) for r in rows]

    def set_edited(self, guid: str, *, edited_at: int | None = None,
                   retracted_at: int | None = None) -> None:
        """chat.db semantics: date_edited marks the newest edit; a retraction
        clears the body (is_empty) — modelled here with explicit columns."""
        sets, args = [], []
        if edited_at is not None:
            sets.append("edited_at=?")
            args.append(edited_at)
        if retracted_at is not None:
            sets.append("retracted_at=?")
            args.append(retracted_at)
        if not sets:
            return
        args.append(guid)
        with self._db:
            self._db.execute(f"UPDATE message SET {', '.join(sets)} WHERE id=?", args)

    # ---- typing ---------------------------------------------------------
    def set_typing(self, chat_id: str, handle_id: str | None, typing: bool) -> None:
        with self._db:
            self._db.execute(
                """INSERT INTO typing (chat_id, handle_id, is_typing, updated_at)
                   VALUES (?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                     handle_id=excluded.handle_id, is_typing=excluded.is_typing,
                     updated_at=excluded.updated_at""",
                (chat_id, handle_id, int(typing), model.now_ms()))

    def is_typing(self, chat_id: str) -> bool:
        r = self._db.execute("SELECT is_typing FROM typing WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(r and r["is_typing"])

    # ---- events --------------------------------------------------------
    def log_event(self, kind: str, *, guid: str | None = None,
                  chat_id: str | None = None, payload: dict | None = None) -> int:
        with self._db:
            cur = self._db.execute(
                "INSERT INTO event (ts, kind, guid, chat_id, payload) VALUES (?,?,?,?,?)",
                (model.now_ms(), kind, guid, chat_id,
                 json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)))
            return int(cur.lastrowid)

    def events(self, *, since_seq: int = 0, limit: int = 200) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM event WHERE seq > ? ORDER BY seq LIMIT ?",
            (since_seq, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"] or "{}")
            out.append(d)
        return out
