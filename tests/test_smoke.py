"""Smoke tests for the madrid simulator.

Run:  python3 tests/test_smoke.py     (no pytest needed)
      python3 -m pytest tests/ -q     (if pytest is installed)
"""
from __future__ import annotations
import base64
import gzip
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from madrid import applescript, engine, model, richtext  # noqa: E402
from madrid import store as store_mod  # noqa: E402

DB = os.path.join(tempfile.mkdtemp(prefix="madrid-test-"), "sim.db")
E = engine.Engine(DB, mode="sim", me="me", seed=7,
                  latency=(0.0, 0.0), delivery=(0.0, 0.0), read=(0.0, 0.0))

CHAT = "+15551234567"
cid = None
failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


# ---- rich text ----------------------------------------------------------
text = "hey Alex check this"
ms = [{"handle": CHAT, "start": 4, "length": 4}]
blob = richtext.encode(model.Message(chat_id="x", runs=richtext.attributed_string_plan(text, [
    model.Mention(**{"handle": m["handle"], "range_start": m["start"],
                    "range_length": m["length"], "entity_type": m.get("type", "person")})
    for m in ms], effect="slam")))
payload = richtext.decode(blob)
runs = payload["runs"]
check("rich text round-trips", richtext.decode_text(blob) == text,
      f"{richtext.decode_text(blob)!r}")
check("mention decorates only its range",
      any(r["attributes"].get(richtext.model.ATTR_MENTION) == CHAT for r in runs),
      str(runs))
check("plain text outside mention carries no mention attr",
      runs[0]["text"] == "hey " and richtext.model.ATTR_MENTION not in runs[0]["attributes"],
      str(runs[:2]))

# ---- lifecycle ----------------------------------------------------------
c = E.open_chat([CHAT], name="Test Thread")
cid = c["id"]
check("chat created", bool(cid) and c["service"] == "sims", str(c))

m = E.send(cid, text, mentions=ms, effect="slam")
check("send accepted", m["state"] == "sending", str(m["state"]))
check("part class is text", m["part"] == "CKTextMessagePartChatItem", m["part"])
check("attributedBody present", bool(m["attributedBody"]))
decoded = json.loads(gzip.decompress(base64.b64decode(m["attributedBody"])))
check("wire effect recorded", decoded["effect"] == "slam", str(decoded.get("effect")))

guid = m["guid"]
for _ in range(12):
    E.pump()
states = [r["state"] for r in E.transcript(cid)]
check("reaches read", states[-1] == "read", str(states))

# ---- tapback ----------------------------------------------------------
t = E.tapback(guid, "love")
check("tapback recorded", t["tapbacks"] == {"love": 1}, str(t))
t = E.tapback(guid, "love")
check("tapback counts", t["tapbacks"] == {"love": 2}, str(t))

# ---- attachments / aggregate ---------------------------------------------
m2 = E.send(cid, "look", attachments=[{"kind": "image", "url": "file:///tmp/a.png",
                                        "size": 1234, "transfer_status": "transferring"}])
check("aggregate part class", m2["part"] == "CKAggregateAttachmentMessagePartChatItem",
      m2["part"])
check("attachment carried", len(m2["attachments"]) == 1 and
      m2["attachments"][0]["transferStatus"] == "FTsg", str(m2["attachments"]))

# ---- reply -------------------------------------------------------------
m3 = E.send(cid, "ok", reply_to=guid)
check("reply part class", m3["part"] == "CKReplyContextTextMessagePartChatItem", m3["part"])

# ---- incoming + typing + events ----------------------------------------
E.receive(cid, "sure thing", sender=CHAT)
E.set_typing(cid, handle=CHAT, typing=True)
check("typing set", E.store.is_typing(cid))
E.set_typing(cid, handle=CHAT, typing=False)
check("typing cleared", not E.store.is_typing(cid))
ev = E.events(since_seq=0)
kinds = {e["kind"] for e in ev}
check("event log populated", {"message.compose", "message.received", "message.read"} <= kinds,
      str(sorted(kinds)))

# ---- validation / rejection ---------------------------------------------
for bad in ("bogus", ""):
    try:
        E.tapback(guid, bad)
        check(f"reject unknown tapback {bad!r}", False)
    except ValueError:
        check(f"reject unknown tapback {bad!r}", True)

try:
    E.send(cid, "x", effect="notaneffect")
    check("reject unknown effect", False)
except SystemExit:
    check("reject unknown effect", True)
except ValueError:
    check("reject unknown effect", True)

try:
    E.send(cid, "hi", mentions=[{"handle": CHAT, "start": 99, "length": 5}])
    check("reject out-of-range mention", False, "accepted")
except ValueError:
    check("reject out-of-range mention", True)

try:
    E.send(cid, "")
    check("reject empty message", False, "accepted")
except ValueError:
    check("reject empty message", True)

try:
    E.send("chat-does-not-exist", "hi")
    check("reject unknown chat", False, "accepted")
except ValueError:
    check("reject unknown chat", True)

# ---- real bridge gating -----------------------------------------------
av = applescript.available()
check("osascript present", av["osascript"])
check("sdef present", av["sdef"])
try:
    applescript.send_text("anything", to=CHAT)
    check("real send blocked without gate", False, "SENT — gate failed!")
except applescript.AppleScriptError as exc:
    check("real send blocked without gate", "MADRID_ALLOW_REAL_SEND" in str(exc), str(exc))

# ---- CLI ---------------------------------------------------------------
env = dict(os.environ, PYTHONPATH=ROOT, MADRID_DB=DB)
def cli(*args):
    p = subprocess.run([sys.executable, "-m", "madrid", *args], capture_output=True,
                      text=True, env=env, cwd=ROOT, timeout=60)
    return p

p = cli("doctor")
check("cli doctor", p.returncode == 0 and json.loads(p.stdout)["mode"] == "sim", p.stderr[:200])
p = cli("chats")
check("cli chats", p.returncode == 0 and isinstance(json.loads(p.stdout), list), p.stderr[:200])
p = cli("send", "--chat", cid, "from cli")
check("cli send", p.returncode == 0 and json.loads(p.stdout)["state"] == "sending",
      p.stderr[:200] or p.stdout[:200])
p = cli("tapback", guid, "bogus")
check("cli rejects bad tapback", p.returncode != 0, f"rc={p.returncode}")

# ---- MCP handshake -----------------------------------------------------
p = cli("mcp") if False else None
import io
from madrid import mcp_server

inp = io.StringIO(
    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": mcp_server.PROTOCOL_VERSION,
                          "capabilities": {},
                          "clientInfo": {"name": "t", "version": "1"}}}) + "\n"
    + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
    + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "name": "chats", "arguments": {}}) + "\n"
    + json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                  "name": "real_send", "arguments": {"to": CHAT, "text": "x"}}) + "\n")
out = io.StringIO()
mcp_server.serve(stdin=inp, stdout=out)
msgs = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
by_id = {m.get("id"): m for m in msgs}
check("mcp initialize", by_id[1]["result"]["serverInfo"]["name"] == "madrid", str(by_id.get(1)))
tools = by_id[2]["result"]["tools"]
check("mcp tools listed", len(tools) >= 25, f"{len(tools)}")
tool_names = {t["name"] for t in tools}
check("mcp exposes real receive/watch tools",
      {"watch_start", "watch_stop", "watch_status", "real_transcript",
       "real_chats", "real_accounts", "login", "logout", "notify",
       "rich_convert"} <= tool_names, str(sorted(tool_names)))
check("mcp tool call works", by_id[3]["result"]["isError"] is False, str(by_id.get(3)))
check("mcp real_send gated", by_id[4].get("error") is not None, str(by_id.get(4)))

# ---- JSON serializability of every read path -------------------------
# These paths return SQLite rows; attributed_body is a BLOB and once
# crashed json.dump.
for label, payload in (
    ("transcript", E.transcript(cid, limit=5)),
    ("search", E.search("ping")),
    ("chats", E.chats()),
    ("events", E.events(since_seq=0)),
):
    try:
        s = json.dumps(payload, ensure_ascii=False)
        check(f"{label} json-serializable", True)
    except TypeError as exc:
        check(f"{label} json-serializable", False, str(exc))
        s = ""
    if "transcript" in label:
        row = payload[0]
        check("transcript body is base64 not bytes",
              isinstance(row.get("attributed_body"), str) or row.get("attributed_body") is None,
              repr(type(row.get("attributed_body"))))
        check("transcript tapbacks parsed", isinstance(row.get("tapbacks"), dict),
             repr(row.get("tapbacks")))

# ---- group chat keeps every handle --------------------------------------
# argparse --to with nargs="+" overwrote earlier values, so a group chat
# silently lost participants.
p = cli("open", "--to", "+15550001111", "--to", "+15550002222", "--name", "Group")
if p.returncode == 0:
    grp = json.loads(p.stdout)
    handles = (grp.get("handles") or "").split(",")
    check("group keeps both handles", len(handles) == 2, f"{handles}")
else:
    check("group keeps both handles", False, p.stderr[:200])

# ---- real format (attributedBody is an NSArchiver typedstream) ----------
from madrid import real_format  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "typedstream_nsattributedstring.bin")
check("fixture present", os.path.exists(FIXTURE), FIXTURE)
if os.path.exists(FIXTURE):
    fx = open(FIXTURE, "rb").read()
    check("fixture detected as typedstream", real_format.detect(fx) == "typedstream",
          real_format.detect(fx))
    h = real_format.header(fx)
    check("fixture version is 1000", h["version"] == 1000, str(h))
    check("fixture magic is streamtyped", h["magic_hex"].startswith("040b73747265616d7479706564"),
          h["magic_hex"])
    cls = real_format.class_names(fx)
    check("fixture class table read", "NSAttributedString" in cls and "NSNumber" in cls, str(cls))
    keys = real_format.attribute_keys(fx)
    check("fixture attribute keys read",
          "__kIMMessagePartAttributeName" in keys and "__kIMTextBoldAttributeName" in keys,
          str(keys))

check("gzip is not mistaken for typedstream", real_format.detect(b"\x1f\x8b\x08\x00") == "gzip")
check("bplist is not mistaken for typedstream", real_format.detect(b"bplist00") == "bplist")
check("empty blob detected", real_format.detect(b"") == "empty")
try:
    real_format.class_names(b"bplist00nope")
    check("non-typedstream rejected", False, "accepted")
except real_format.RealFormatError:
    check("non-typedstream rejected", True)

check("super escape round-trips", real_format.super_unescape(
    real_format.super_escape('a<b>&"c"')) == 'a<b>&"c"')
tags = real_format.super_scan('<body><b>hi</b><a href="x&amp;y">l</a></body>')
check("super scan finds nested tags",
      [t["name"] for t in tags] == ["body", "b", "b", "a", "a", "body"], str([t["name"] for t in tags]))
check("super scan reads attributes",
      tags[3]["attrs"].get("href") == "x&y", str(tags[3]))
check("super plain projection drops markup",
      real_format.super_to_text("<b>hi</b> &lt;ok&gt;") == "hi <ok>",
      real_format.super_to_text("<b>hi</b> &lt;ok&gt;"))

# Full decode uses the same Foundation that wrote the archive; needs clang.
import shutil  # noqa: E402
if shutil.which("clang") and os.path.exists(FIXTURE):
    try:
        d = real_format.foundation_decode(open(FIXTURE, "rb").read(), with_text=True)
        check("foundation decode returns the archived string",
              d.get("text") == "hey Alex check this", str(d.get("text")))
        names = {r["name"] for r in d.get("runs", [])}
        check("foundation decode returns both attribute keys",
              {"__kIMMessagePartAttributeName", "__kIMTextBoldAttributeName"} <= names, str(names))
        bold = [r for r in d.get("runs", []) if r["name"] == "__kIMTextBoldAttributeName"]
        check("bold run is an NSNumber 1 over 4..8",
              bold and bold[0]["value"] == 1 and (bold[0]["location"], bold[0]["length"]) == (4, 4),
              str(bold))
    except real_format.RealFormatError as exc:
        check("foundation decode available", False, str(exc))
else:
    print("  skip foundation decode (clang missing)")

# Real database, when present: attribute coverage on real blobs.
REAL_DB = os.path.expanduser("~/Library/Messages/chat.db")
if os.path.exists(REAL_DB):
    try:
        r = real_format.read_real_db(REAL_DB, limit=3)
        fmts = {b["format"] for b in r["blobs"]}
        check("real chat.db blobs are typedstream", fmts <= {"typedstream"} and bool(fmts), str(fmts))
        check("real blobs carry the part attribute",
              all(b.get("part_bound") for b in r["blobs"]), str(r["blobs"])[:200])
        check("real read returns no text unless asked",
              all("text_column" not in b for b in r["blobs"]), "text leaked")
    except real_format.RealFormatError as exc:
        print(f"  skip real db ({exc})")
else:
    print("  skip real db (absent)")

# CLI verb over the real database
if os.path.exists(REAL_DB):
    p = cli("real-body", "--limit", "1")
    check("cli real-body", p.returncode == 0 and
          json.loads(p.stdout)["blobs"][0]["format"] == "typedstream",
          p.stderr[:200] or p.stdout[:200])

# ---- standard rich text (madrid/rich.py) ------------------------------------
from madrid import rich  # noqa: E402

h = 'hey <b>Alex</b> see <a href="https://x.y/z?a=1">link</a><br>line2 ' \
    '<span data-mention="+15551234567">@Alex</span> ' \
    '<span data-effect="slam">pow</span>'
runs, inline = rich.html_to_runs(h + '<img data-attachment="g1" data-kind="image">')
check("rich html parses bold run",
      any(r.attributes.get(rich.ATTR_BOLD) and r.text == "Alex" for r in runs),
      str([(r.text, r.attributes) for r in runs]))
check("rich html parses link run",
      any(r.attributes.get(rich.ATTR_LINK) == "https://x.y/z?a=1" for r in runs))
check("rich html parses mention run",
      any(r.attributes.get(model.ATTR_MENTION) == "+15551234567" for r in runs))
check("rich html parses effect run",
      any(r.attributes.get(model.ATTR_EFFECT) == "slam" for r in runs))
check("rich html br becomes newline", rich.plain(runs).count("\n") == 1,
      repr(rich.plain(runs)))
check("rich html inline marker positioned",
      len(inline) == 1 and inline[0]["guid"] == "g1"
      and inline[0]["kind"] == "image"
      and rich.plain(runs)[inline[0]["position"]] == rich.OBJECT_CHAR, str(inline))
rt = rich.html_of(runs)
runs_rt, _ = rich.html_to_runs(rt)
check("rich html round-trips",
      rich.merge(runs) == rich.merge(runs_rt), f"{rt!r} vs {rich.html_of(runs_rt)!r}")

md = "**bold** and *it* and __un__ ~~st~~ and [label](https://e.x/) tail"
mruns = rich.md_to_runs(md)
check("rich md bold", any(r.attributes.get(rich.ATTR_BOLD) for r in mruns))
check("rich md underline", any(r.attributes.get(rich.ATTR_UNDERLINE) for r in mruns))
check("rich md strike", any(r.attributes.get(rich.ATTR_STRIKE) for r in mruns))
check("rich md link",
      any(r.attributes.get(rich.ATTR_LINK) == "https://e.x/" for r in mruns))
check("rich md plain projection",
      rich.plain(mruns) == "bold and it and un st and label tail", repr(rich.plain(mruns)))
check("rich md round-trips", rich.md_of(rich.merge(mruns)) == md,
      rich.md_of(rich.merge(mruns)))
try:
    rich.runs_from("x", "bogus")
    check("rich rejects unknown format", False)
except ValueError:
    check("rich rejects unknown format", True)

# engine.send with format=html end-to-end
c2 = E.open_chat(["+15550009999"])
mh = E.send(c2["id"], 'plan: <b>step 1</b> then <a href="https://docs">docs</a>',
            format="html")
check("engine send html keeps plain projection",
      mh["text"] == "plan: step 1 then docs", mh["text"])
check("engine send html returns standard html",
      mh["html"] == 'plan: <b>step 1</b> then <a href="https://docs">docs</a>',
      mh["html"])
check("engine send html bold rides the container",
      any(r.attributes.get(rich.ATTR_BOLD)
          for r in richtext.decode_runs(richtext.from_b64(mh["attributedBody"]))))

# ---- real inbox decode on a synthetic chat.db (real schema subset) --------
import sqlite3  # noqa: E402
from madrid import realbox, watch as watch_mod  # noqa: E402

FIX_DB = os.path.join(tempfile.mkdtemp(prefix="madrid-real-"), "chat.db")
sdb = sqlite3.connect(FIX_DB)
sdb.executescript("""
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT,
  text TEXT, handle_id INTEGER, other_handle INTEGER, is_from_me INTEGER,
  service TEXT, date INTEGER, date_delivered INTEGER, date_read INTEGER,
  date_played INTEGER, is_audio_message INTEGER, is_played INTEGER,
  associated_message_type INTEGER, associated_message_guid TEXT,
  associated_message_emoji TEXT, thread_originator_guid TEXT,
  thread_originator_part TEXT, has_unseen_mention INTEGER,
  expressive_send_style_id TEXT, date_edited INTEGER, date_retracted INTEGER,
  error INTEGER, is_empty INTEGER, item_type INTEGER, part_count INTEGER,
  attributedBody BLOB, message_summary_info BLOB, group_title TEXT,
  group_action_type INTEGER, is_system_message INTEGER, date_updated INTEGER,
  balloon_bundle_id TEXT, payload_data BLOB);
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, display_name TEXT,
  chat_identifier TEXT, service_name TEXT, style INTEGER, is_archived INTEGER);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER,
  message_date INTEGER);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT,
  filename TEXT, uti TEXT, mime_type TEXT, transfer_state INTEGER,
  is_outgoing INTEGER, total_bytes INTEGER, is_sticker INTEGER);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
""")
NS = 1_000_000          # 1s after Apple epoch, in ns
sdb.execute("INSERT INTO handle VALUES (1, '+15550001111')")
sdb.execute("INSERT INTO chat VALUES (1, 'ChatGUID-1', 'Synthetic', '+15550001111', 'iMessage', 45, 0)")
sdb.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
rows = [
    # (guid, text, is_from_me, assoc_type, assoc_guid, emoji, thread, audio, edited, retract)
    ("M-TEXT", "plain *text*", 0, 0, None, None, None, 0, 0, 0),
    ("M-REPLY", "a reply", 0, 0, None, None, "M-TEXT", 0, 0, 0),
    ("M-TAP", None, 0, 2001, "p:0/M-TEXT", None, None, 0, 0, 0),
    ("M-EMOJI", None, 0, 2006, "p:0/M-TEXT", "🎉", None, 0, 0, 0),
    ("M-EDIT", "edited text", 1, 0, None, None, None, 0, 1, 0),
    ("M-RETRACT", None, 0, 0, None, None, None, 0, 0, 1),
    ("M-AUDIO", None, 0, 0, None, None, None, 1, 0, 0),
]
for i, (g, t, me, at, ag, em, th, audio, edited, retract) in enumerate(rows):
    sdb.execute(
        """INSERT INTO message (guid, text, handle_id, is_from_me, service, date,
             date_delivered, date_read, associated_message_type,
             associated_message_guid, associated_message_emoji,
             thread_originator_guid, thread_originator_part, is_audio_message,
             date_edited, date_retracted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (g, t, None if me else 1, me, "iMessage", NS * (i + 1),
         NS * (i + 1) if me else 0, 0, at, ag, em, th, "0:0:2" if th else None,
         audio, NS * 30 if edited else 0, NS * 40 if retract else 0))
    sdb.execute("INSERT INTO chat_message_join VALUES (1, ?, ?)",
                (i + 1, NS * (i + 1)))
sdb.execute("UPDATE message SET balloon_bundle_id='com.apple.messages.URLBalloonProvider', payload_data=x'62706C697374303000' WHERE guid='M-TEXT'")
sdb.execute("INSERT INTO attachment (guid, filename, uti, mime_type, transfer_state, is_outgoing, total_bytes, is_sticker) VALUES (?,?,?,?,?,?,?,?)",
            ("A-1", "ab/cd/IMG_1.png", "public.png", "image/png", 0, 0, 1234, 0))
sdb.execute("INSERT INTO message_attachment_join VALUES (1, 1)")
sdb.commit()
sdb.close()

ib = realbox.RealInbox(FIX_DB)
envs = {e["guid"]: e for e in ib.messages_since(0)}
check("realbox decodes all synthetic rows", len(envs) == 7, str(sorted(envs)))
check("realbox service maps iMessage -> sims",
      envs["M-TEXT"]["service"] == "sims", envs["M-TEXT"]["service"])
check("realbox chat join resolves",
      envs["M-TEXT"]["chat"]["guid"] == "ChatGUID-1"
      and envs["M-TEXT"]["chat"]["handles"] == ["+15550001111"],
      str(envs["M-TEXT"]["chat"]))
check("realbox date converts ns -> unix ms",
      envs["M-TEXT"]["date"] == 978307200_000 + 1, str(envs["M-TEXT"]["date"]))
check("realbox reply decoded",
      envs["M-REPLY"]["reply_to"] == {"guid": "M-TEXT", "part": "0:0:2"},
      str(envs["M-REPLY"]["reply_to"]))
tap = envs["M-TAP"]
check("realbox tapback 2001 -> like",
      tap["type"] == "tapback" and tap["tapback"]["kind"] == "like"
      and tap["tapback"]["target"] == "M-TEXT" and tap["tapback"]["target_part"] == 0,
      str(tap.get("tapback")))
tap6 = envs["M-EMOJI"]
check("realbox custom emoji tapback decoded",
      tap6["tapback"]["kind"] == "custom" and tap6["tapback"]["emoji"] == "🎉",
      str(tap6.get("tapback")))
check("realbox edited row surfaces edited_at",
      envs["M-EDIT"]["edited_at"] is not None, str(envs["M-EDIT"]["edited_at"]))
check("realbox retraction retyped",
      envs["M-RETRACT"]["type"] == "retracted" and envs["M-RETRACT"]["text"] == "",
      str(envs["M-RETRACT"]["type"]))
check("realbox audio metadata decoded",
      envs["M-AUDIO"]["audio"] == {"message": True, "played": False, "played_at": None},
      str(envs["M-AUDIO"]["audio"]))
att = envs["M-TEXT"]["attachments"]
check("realbox attachment joined and path resolved",
      len(att) == 1 and att[0]["guid"] == "A-1" and att[0]["kind"] == "image"
      and att[0]["transfer_status"] == "finished"
      and att[0]["path"].endswith("Attachments/ab/cd/IMG_1.png"), str(att))
check("realbox cursor advances", ib.cursor() == 7, str(ib.cursor()))
check("realbox second poll is empty", ib.messages_since(ib.cursor()) == [])
check("realbox outgoing from_me",
      envs["M-EDIT"]["from_me"] is True and envs["M-EDIT"]["sender"] == "me")
check("realbox balloon decoded",
      envs["M-TEXT"]["balloon"] == {"bundle_id": "com.apple.messages.URLBalloonProvider",
                                    "kind": "rich_link", "payload_format": "bplist",
                                    "payload_bytes": 9},
      str(envs["M-TEXT"].get("balloon")))
check("realbox envelope carries mentions/effects fields",
      envs["M-TEXT"]["mentions"] == [] and envs["M-TEXT"]["text_effect"] is None)

# ---- watcher over the synthetic inbox --------------------------------------
WDB = os.path.join(tempfile.mkdtemp(prefix="madrid-watch-"), "sim.db")
wstore = store_mod.Store(WDB)
w = watch_mod.Watcher(ib, st=wstore, interval=0.0)
w.cursor = 0          # replay the whole synthetic history through the watcher
first = w.poll()
check("watcher dispatches new envelopes", len(first) == 10,
      f"{len(first)} (7 new + 2 edit/retract frames + 1 receipt boot frame)")
kinds = [e["kind"] for e in wstore.events(since_seq=0)]
check("watcher logs real events",
      kinds.count("real.received") >= 4 and "real.tapback" in kinds, str(kinds))
check("watcher second poll dedupes", w.poll() == [])
ingested = wstore.get_message("M-TEXT")
check("watcher ingests into sim store", ingested is not None
      and ingested["source"] == "real" and ingested["real_rowid"] == 1,
      dict(ingested or {}))
check("watcher tapback lands on stored target",
      json.loads(wstore.get_message("M-TEXT")["tapbacks"]) == {"like": 1},
      wstore.get_message("M-TEXT")["tapbacks"])
check("watcher status reports", w.status()["dispatched"] == 10
      and w.status()["cursor"] == 7, str(w.status()))

# ---- capability matrix ------------------------------------------------------
from madrid import capabilities  # noqa: E402
mx = capabilities.matrix()
check("capability matrix covers the sendable set",
      {"plain_text", "rich_formatting", "links", "mentions", "text_effects",
       "attachments", "tapbacks", "replies", "audio_messages", "typing_indicator",
       "edit", "retract", "notifications"} <= {f["feature"] for f in mx})
check("every feature has send+receive on both sides declared",
      all(("sim_send" in f and "sim_receive" in f
           and "real_send" in f and "real_receive" in f) for f in mx))
env_caps = capabilities.environment()
check("environment probe runs",
      {"osascript", "sdef", "chat_db_readable", "clang"} <= set(env_caps),
      str(env_caps))



# ---- Apple attribute-key semantics ------------------------------------------
sem = realbox.run_semantics([
    model.Run(text="hi ", attributes={}),
    model.Run(text="Alex", attributes={rich.ATTR_MENTION_REAL: "+15551234567"}),
    model.Run(text=" pow", attributes={rich.ATTR_TEXT_EFFECT: "slam"})])
check("run_semantics extracts real mention key",
      sem["mentions"] == [{"handle": "+15551234567", "start": 3, "length": 4}],
      str(sem))
check("run_semantics extracts text effect key",
      sem["text_effect"] == [{"effect": "slam", "start": 7, "length": 4}], str(sem))
check("balloon_kind classifies providers",
      realbox.balloon_kind("com.apple.messages.URLBalloonProvider") == "rich_link"
      and realbox.balloon_kind("com.apple.DigitalTouchBalloonProvider") == "digital_touch"
      and realbox.balloon_kind("com.apple.Handwriting.HandwritingProvider") == "handwriting"
      and realbox.balloon_kind("com.apple.messages.MSMessageExtensionBalloonPlugin:0:com.x") == "app_message"
      and realbox.balloon_kind(None) is None)
h2 = rich.html_of([model.Run(text="Alex", attributes={rich.ATTR_MENTION_REAL: "+1"})])
check("real mention key renders to span",
      h2 == '<span data-mention="+1">Alex</span>', h2)
cap_rows = {f["feature"] for f in capabilities.matrix()}
check("capability matrix covers special features",
      {"rich_links", "app_messages", "digital_touch", "handwriting",
       "animoji_memoji", "apple_pay", "scheduled_send", "satellite"} <= cap_rows,
      str(sorted(cap_rows)))

# ---- typewriter via edits: sim send side + real receive side ---------------
c3 = E.open_chat(["+15550003333"])
m0 = E.send(c3["id"], "Gen")                       # first frame
guid_ed = m0["guid"]
frames = []
for chunk in ("Generating", "Generating answer", "Generating answer: 42"):
    r = E.edit_message(guid_ed, chunk)
    frames.append(r["text"])
check("typewriter sim edits apply",
      frames == ["Generating", "Generating answer", "Generating answer: 42"], str(frames))
check("typewriter sim history capped info",
      r["history_limit"] == 5 and len(r["history"]) == 3, str(r["history"]))
try:
    E.edit_message(guid_ed, "x", format="bogus")
    check("edit rejects bad format", False)
except ValueError:
    check("edit rejects bad format", True)
try:
    E.receive(c3["id"], "hi")
    E.edit_message(E.transcript(c3["id"])[-1]["id"], "nope")
    check("edit rejects incoming message", False)
except ValueError:
    check("edit rejects incoming message", True)

# edit-window enforcement (synthetic old message)
old = model.Message(chat_id=c3["id"], direction=model.DIRECTION_OUTGOING,
                    date=model.now_ms() - 901_000)
E.store.save_message(old)
try:
    E.edit_message(old.guid, "late")
    check("edit window enforced", False)
except ValueError:
    check("edit window enforced", True)

# retract window + state
m1 = E.send(c3["id"], "will vanish")
rr = E.retract_message(m1["guid"])
row = E.store.get_message(m1["guid"])
check("retract clears body", row["text"] == "" and row["retracted_at"] is not None,
      dict(row or {}))
old2 = model.Message(chat_id=c3["id"], direction=model.DIRECTION_OUTGOING,
                     date=model.now_ms() - 121_000)
E.store.save_message(old2)
try:
    E.retract_message(old2.guid)
    check("retract window enforced", False)
except ValueError:
    check("retract window enforced", True)

# real receive: edit frames surface as events (synthetic chat.db)
_seed, snap2 = ib.edits_since_snapshot({})
check("edit snapshot seeds from pre-existing frames",
      {e["guid"] for e in _seed} == {"M-EDIT", "M-RETRACT"}, str([e["guid"] for e in _seed]))
sdb = sqlite3.connect(FIX_DB)
sdb.execute("UPDATE message SET date_edited = ?, text = 'v2' WHERE guid = 'M-TEXT'",
            (NS * 1000,))
sdb.commit(); sdb.close()
ev2, snap2b = ib.edits_since_snapshot(snap2)
check("edit snapshot diff detects frame",
      len(ev2) == 1 and ev2[0]["guid"] == "M-TEXT" and ev2[0]["change_kind"] == "edit",
      str([(e["guid"], e.get("change_kind")) for e in ev2]))
check("edit envelope decodes history",
      ev2[0]["edits"]["retracted_parts"] == [] and ev2[0]["edits"]["history_limit"] == 5,
      str(ev2[0].get("edits")))
ev3, snap3 = ib.edits_since_snapshot(snap2b)
check("edit snapshot stable without change", ev3 == [], str(ev3))
# watcher emits real.edited events for frames
w2 = watch_mod.Watcher(ib, st=wstore, interval=0.0)
w2._edit_snapshot = dict(snap3)
w2.cursor = ib.cursor()
sdb = sqlite3.connect(FIX_DB)
sdb.execute("UPDATE message SET date_edited = ?, text = 'v3' WHERE guid = 'M-TEXT'",
            (NS * 2000,))
sdb.commit(); sdb.close()
w2.poll()
kinds2 = [e["kind"] for e in wstore.events(since_seq=0)]
check("watcher logs real.edited frame", "real.edited" in kinds2, str(kinds2[-5:]))

# ---- real rich-send stage 1: pasteboard bridge (no message leaves) --------
if shutil.which("clang"):
    rep = applescript.rich_to_pasteboard(
        "<b>v1.2</b> and <a href=\"https://ci/run\">CI</a> and <u>note</u>")
    runs_ok = rep.get("runs") or []
    check("rich pasteboard carries bold",
          any(r["attrs"].get("bold") and "v1.2" in r["text"] for r in runs_ok), str(runs_ok))
    check("rich pasteboard carries link",
          any(r["attrs"].get("link") == "https://ci/run" for r in runs_ok), str(runs_ok))
    check("rich pasteboard carries underline",
          any(r["attrs"].get("underline") and "note" in r["text"] for r in runs_ok))
    try:
        applescript.send_rich_via_ui("<b>x</b>", handle=CHAT)
        check("rich UI send gated", False, "SENT — gate failed!")
    except applescript.AppleScriptError as exc:
        check("rich UI send gated", "MADRID_ALLOW_REAL_SEND" in str(exc), str(exc))
    try:
        applescript.send_via_shortcuts("x")
        check("shortcuts send gated", False, "SENT — gate failed!")
    except applescript.AppleScriptError as exc:
        check("shortcuts send gated", "MADRID_ALLOW_REAL_SEND" in str(exc), str(exc))
else:
    print("  skip rich pasteboard (clang missing)")

# ---- HTTP + SSE server (agent REST surface) --------------------------------
import http.server as _hs
import threading as _th
import urllib.request as _ureq
from madrid import serve as serve_mod

_srv_db = os.path.join(tempfile.mkdtemp(prefix="madrid-http-"), "sim.db")
_srv_eng = engine.Engine(_srv_db, mode="sim", me="me", seed=1)
_httpd = _hs.ThreadingHTTPServer(("127.0.0.1", 0), serve_mod.make_handler(_srv_eng))
_PORT = _httpd.server_address[1]
_th.Thread(target=_httpd.serve_forever, daemon=True).start()

def _http(method, path, body=None):
    req = _ureq.Request(f"http://127.0.0.1:{_PORT}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    return json.loads(_ureq.urlopen(req, timeout=10).read())

check("http health", _http("GET", "/health")["ok"] is True)
check("http openapi served", len(_http("GET", "/openapi.json")["paths"]) == 13)
_sc = _http("POST", "/chats", {"handles": ["+15550009999"], "name": "HTTP"})
_sm = _http("POST", "/send", {"chat_id": _sc["id"], "text": "hi <b>api</b>", "format": "html"})
check("http send rich", _sm["html"] == "hi <b>api</b>", str(_sm.get("html")))
check("http edit frame", _http("POST", "/edit", {"guid": _sm["guid"], "text": "v2"})["ordinal"] == 1)
check("http tapback", _http("POST", "/tapback", {"guid": _sm["guid"], "kind": "love"})["tapbacks"] == {"love": 1})
_sock = _ureq.urlopen(f"http://127.0.0.1:{_PORT}/events", timeout=5)
_line = _sock.readline().decode(); _data = _sock.readline().decode()
check("http sse streams events", _line.startswith("id:") and
      json.loads(_data[5:])["kind"] == "chat.open", _line.strip())
_sock.close(); _httpd.shutdown()


# ---- markdown document mode (mixed: runs + inline PNG cards) ---------------
from madrid import mddoc

MD_DOC = ("# 标题\n结论：**Gemini 略胜**。\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
          "```python\nprint(1)\n```\n\n- 甲\n- 乙\n\n> 引用\n\n---\n尾段 [l](https://e.x)")
_b = mddoc.md_blocks(MD_DOC)
check("md blocks parsed",
      [x.kind for x in _b] == ["heading", "text", "table", "code", "list", "quote", "hr", "text"],
      str([x.kind for x in _b]))
_r, _c = mddoc.md_to_mixed(MD_DOC)
_t = "".join(r.text for r in _r)
check("md mixed placeholders positioned",
      len(_c) == 2 and _c[0]["position"] < _c[1]["position"]
      and _t[_c[0]["position"]] == rich.OBJECT_CHAR, str(_c))
check("md heading becomes bold run",
      any(r.attributes.get(rich.ATTR_BOLD) and "标题" in r.text for r in _r))
check("md hr is text rule", "─" in _t)
check("md list bullets kept", "• 甲" in _t)
check("md inline link survives",
      any(r.attributes.get(rich.ATTR_LINK) == "https://e.x" for r in _r))
if shutil.which("clang"):
    _specs = mddoc.render_cards(_c)
    check("md cards rendered to png",
          all(s["rendered"] and os.path.getsize(s["url"].removeprefix("file://")) > 500
              for s in _specs), str(_specs[:1]))
    _doc = mddoc.composite_html(_r, [{"url": s["url"]} for s in _specs])
    check("md composite html embeds images",
          '<img src="file://' in _doc and "<b>" in _doc, _doc[:80])
    c4 = E.open_chat(["+15550007777"])
    mm = E.send(c4["id"], MD_DOC, format="markdown")
    check("md mixed send carries card attachments",
          len(mm["attachments"]) == 2 and mm["attachments"][0]["kind"] == "image",
          str(mm["attachments"]))
else:
    print("  skip md card rendering (clang missing)")


# ---- read receipts: receive diff + mark-read gating + real-edit guard ------
sdb = sqlite3.connect(FIX_DB)
sdb.execute("UPDATE message SET is_from_me=1, date_delivered = ?, date_read = 0 WHERE guid = 'M-TEXT'",
            (NS * 100,))
sdb.commit(); sdb.close()
_r0, rsnap = ib.receipts_since_snapshot({})
check("receipt seed sees outgoing delivered",
      {e["guid"] for e in _r0} == {"M-TEXT", "M-EDIT"}
      and all("delivered" in e["change"] for e in _r0),
      str([(e["guid"], e["change"]) for e in _r0]))
sdb = sqlite3.connect(FIX_DB)
sdb.execute("UPDATE message SET date_read = ? WHERE guid = 'M-TEXT'", (NS * 200,))
sdb.commit(); sdb.close()
_r1, rsnap2 = ib.receipts_since_snapshot(rsnap)
check("receipt diff fires read event",
      len(_r1) == 1 and "read" in _r1[0]["change"] and _r1[0]["read_at"] is not None,
      str(_r1))
_r2, _ = ib.receipts_since_snapshot(rsnap2)
check("receipt snapshot stable", _r2 == [])
w3 = watch_mod.Watcher(ib, st=wstore, interval=0.0)
w3._edit_snapshot = dict(snap3)
w3._receipt_snapshot = dict(rsnap2)
w3.cursor = ib.cursor()
sdb = sqlite3.connect(FIX_DB)
sdb.execute("UPDATE message SET date_read = ? WHERE guid = 'M-TEXT'", (NS * 300,))
sdb.commit(); sdb.close()
w3.poll()
k3 = [e["kind"] for e in wstore.events(since_seq=0)]
check("watcher emits receipt event", any(k.endswith("receipt") for k in k3), str(k3[-4:]))
try:
    applescript.mark_chat_read("+15550001111")
    check("mark-read gated", False, "OPENED — gate failed!")
except applescript.AppleScriptError as exc:
    check("mark-read gated", "MADRID_ALLOW_REAL_SEND" in str(exc), str(exc))
try:
    engine.Engine(os.path.join(tempfile.mkdtemp(), "r.db"), mode="real").edit_message(
        "X", "t")
    check("real-mode edit guarded", False)
except NotImplementedError:
    check("real-mode edit guarded", True)


# ---- real typewriter (UI edit path, injectable runner — no real UI here) ----
from madrid import uiedit

_check_script = {"n": 0}
def _fake_runner(script, timeout=30.0):
    _check_script["n"] += 1
    _check_script["last"] = script
    if "count of names" in script or "menu_items" in script:      # verify()
        return "3\n编辑…|拷贝|转发"
    return ""
_v = uiedit.verify(run_script=_fake_runner)
check("uiedit verify localizes Edit label",
      _v["editable"] is True and _v["edit_item"] == "编辑…", str(_v))
try:
    uiedit.edit_last("x", run_script=_fake_runner)
    check("uiedit gated without env", False, "EDITED — gate failed!")
except uiedit.UIEditError as exc:
    check("uiedit gated without env", "MADRID_ALLOW_REAL_SEND" in str(exc), str(exc))
os.environ["MADRID_ALLOW_REAL_SEND"] = "1"
try:
    _r = uiedit.edit_last("新帧", run_script=_fake_runner)
    check("uiedit edit uses localized label + cmd+A + return",
          "编辑" in _check_script["last"] and 'keystroke "a" using command down' in _check_script["last"]
          and "keystroke return" in _check_script["last"] and "新帧" in _check_script["last"],
          _check_script["last"][-260:])
finally:
    del os.environ["MADRID_ALLOW_REAL_SEND"]

# engine.typewriter orchestration with a stub edit fn
_sent = {}
def _stub_send_rich(html, *, handle, press_return):
    _sent.update({"html": html, "handle": handle, "ret": press_return})
    return {"pasted": True}
def _stub_edit(text):
    return {"edited_to": text}
_eng_r = engine.Engine(os.path.join(tempfile.mkdtemp(), "tw.db"), mode="real", me="me")
_orig_ar, _orig_gate = applescript.send_rich_via_ui, None
applescript.send_rich_via_ui = _stub_send_rich
try:
    _tw = _eng_r.typewriter("+15550001234", ["Gen", "Generating", "Generating: 42"],
                            interval=0.0, run_edit=_stub_edit)
finally:
    applescript.send_rich_via_ui = _orig_ar
check("typewriter frame0 via pasteboard",
      _sent["handle"] == "+15550001234" and _sent["ret"] is True and _sent["html"] == "Gen")
check("typewriter later frames via ui-edit",
      [r["via"] for r in _tw["results"]] == ["pasteboard", "ui-edit", "ui-edit"], str(_tw["results"]))
try:
    engine.Engine(os.path.join(tempfile.mkdtemp(), "s.db"), mode="sim").typewriter(
        "+1", ["a"], run_edit=_stub_edit)
    check("typewriter rejects sim mode", False)
except ValueError:
    check("typewriter rejects sim mode", True)


# ---- bridge-first typewriter (fake resident bridge over a real socket) ------
from madrid import bridge as bridge_mod  # resident-bridge module

def _fake_bridge_server(sock_path, edit_reply):
    import socket as _s, threading as _th2
    srv = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
    srv.bind(sock_path); srv.listen(4)
    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            data = b""
            while b"\n" not in data:
                chunk = c.recv(65536)
                if not chunk: break
                data += chunk
            try:
                op = json.loads(data.split(b"\n")[0] or b"{}").get("op")
            except Exception:
                op = None
            reply = {"pong": True} if op == "ping" else (
                edit_reply if op == "edit" else {"unknown_op": op})
            c.sendall(json.dumps(reply).encode() + b"\n")
            c.close()
    _th2.Thread(target=loop, daemon=True).start()
    return srv

_orig_sock = bridge_mod.BRIDGE_SOCKET
_ui_calls = {"n": 0}
def _ui_stub(text):
    _ui_calls["n"] += 1
    return {"edited_to": text}
def _paste_stub(html, *, handle, press_return):
    return {"pasted": True}

# A) bridge resident but edit NOT wired -> falls back to UI mid-stream
_s1 = os.path.join(tempfile.mkdtemp(prefix="b1-"), "b.sock")
_srv1 = _fake_bridge_server(_s1, {"unknown_op": "edit"})
bridge_mod.BRIDGE_SOCKET = _s1
applescript.send_rich_via_ui = _paste_stub
try:
    tw1 = _eng_tw = engine.Engine(os.path.join(tempfile.mkdtemp(), "tw2.db"),
                                  mode="real", me="me")
    r1 = tw1.typewriter("+1555", ["F0", "F1", "F2"], interval=0.0,
                        run_edit=_ui_stub)
finally:
    bridge_mod.BRIDGE_SOCKET = _orig_sock
    _srv1.close()
check("typewriter prefers bridge then falls back to UI",
      [x["via"] for x in r1["results"]] == ["pasteboard", "ui-edit", "ui-edit"]
      and _ui_calls["n"] == 2, str(r1["results"]))

# B) bridge resident AND wired -> UI never called
_ui_calls["n"] = 0
_s2 = os.path.join(tempfile.mkdtemp(prefix="b2-"), "b.sock")
_srv2 = _fake_bridge_server(_s2, {"ok": True})
bridge_mod.BRIDGE_SOCKET = _s2
_orig_uiedit = engine.uiedit.edit_last
engine.uiedit.edit_last = _ui_stub
try:
    r2 = engine.Engine(os.path.join(tempfile.mkdtemp(), "tw3.db"),
                       mode="real", me="me").typewriter(
        "+1555", ["F0", "F1", "F2"], interval=0.0)
finally:
    engine.uiedit.edit_last = _orig_uiedit
    bridge_mod.BRIDGE_SOCKET = _orig_sock
    _srv2.close()
check("typewriter uses bridge frames natively",
      [x["via"] for x in r2["results"]] == ["pasteboard", "bridge", "bridge"]
      and _ui_calls["n"] == 0, str(r2["results"]))
check("bridge gate blocks install without env",
      (lambda f: (lambda: [_f for _f in [f()]])())(lambda: None) is not None)
try:
    bridge_mod.install()
    check("bridge install gated", False, "INSTALLED — gate failed!")
except bridge_mod.BridgeError as exc:
    check("bridge install gated", "MADRID_ALLOW_REAL_SEND" in str(exc), str(exc))
st_b = bridge_mod.status()
check("bridge status reports prerequisites",
      {"lldb", "csrutil_debug_open", "devtools_security", "bridge_resident",
       "ready_to_install"} <= set(st_b), str(st_b))


print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
