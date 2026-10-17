# madrid

**English** | [中文](README_zh.md)

Send and receive iMessage from your agent. Rich text in, rich text out.

madrid reads your local Messages database (read-only, never writes to it) and sends through Messages.app. Your agent gets a clean HTTP API, an MCP server, or a Hermes gateway plugin — whichever fits.

```bash
pip install -e .
python3 -m madrid serve --port 8787 --watch
```

That's it. Your agent can now:

- Receive iMessages as they arrive (sub-second, kqueue on the database)
- Reply with formatting — bold, italic, links, tables, code blocks
- Stream responses word-by-word like a person typing
- See when someone reads a message
- React to messages, edit them, unsend them

## Why not [openclaw/imsg](https://github.com/openclaw/imsg)?

imsg is a great Swift CLI. We built madrid because we needed things imsg doesn't do:

| | imsg | madrid |
|---|---|---|
| Rich text on receive | Plain text only | Full decode (bold/italic/links/mentions/effects) |
| Rich text on send | Requires SIP off + dylib injection | Pasteboard channel, works out of the box |
| Markdown → iMessage | No | Tables and code blocks become inline images |
| Typewriter streaming | No | Yes, via the edit protocol |
| HTTP API | No | REST + SSE + OpenAPI spec |
| MCP server | No | Yes |
| Hermes integration | No | Native gateway plugin |
| Edit/retract events | No | Frame-by-frame |
| Read receipts | No | Live events |

imsg is better if you want a native compiled binary, Linux read-only support, or its more polished CLI. Both tools are honest about what they can and can't do.

## What it looks like

**Your agent sends Markdown:**
```markdown
## Deploy complete

| Service | Status |
|---------|--------|
| API     | ✅     |
| Worker  | ✅     |

```python
print("hello")
```
```

**The recipient sees in iMessage:**
- "Deploy complete" in bold
- A formatted table as a clean card image
- The code block as a syntax-highlighted card image

**They reply, your agent gets:**
```json
{
  "type": "message",
  "text": "nice, ship it",
  "sender": "+14155551212",
  "chat": {"name": "Alex", "handles": ["+14155551212"]},
  "html": "nice, ship it"
}
```

## Getting started

```bash
# Grant Full Disk Access to your terminal first (System Settings → Privacy)
pip install -e .

# Check everything works
python3 -m madrid doctor

# Start the HTTP+SSE server (this is what agents connect to)
python3 -m madrid serve --port 8787 --watch

# Or use the CLI directly
python3 -m madrid real-chats
python3 -m madrid stats
python3 -m madrid send --to "+14155551212" --text "hello" --real
```

## Hermes setup

The adapter is already at `~/.hermes/hermes-agent/plugins/platforms/imessage/`.

```bash
export MADRID_URL=http://127.0.0.1:8787
hermes gateway setup    # pick "iMessage (madrid)"
hermes gateway start
```

Now anyone who iMessages you gets your agent. It responds with rich text. You can set `MADRID_ALLOWED_USERS=+14155551212` to restrict who can talk to it, or `MADRID_TYPEWRITER=true` to stream responses.

## Sending channels

| How | What | Notes |
|---|---|---|
| AppleScript | Text + files | Works everywhere |
| Pasteboard | Rich text (bold/links/fonts) | Needs Automation permission |
| Shortcuts | Headless text + attachments | Set up once in Shortcuts.app |
| Debugger bridge | Everything | Needs `csrutil --without debug` (see below) |

## Typewriter

```bash
python3 -m madrid typewriter --to "+14155551212" \
    --frames "Generating;;Generating answer;;Generating answer: 42"
```

The first frame sends normally. Each subsequent frame edits the message, so the recipient watches the text grow. Uses the bridge if available, falls back to UI scripting otherwise. Fifteen-minute window, roughly one edit per second.

## Security

**The database is read-only.** madrid never writes to `chat.db`. It opens with `mode=ro` and uses kqueue to detect changes.

**Real sends are gated.** You need `MADRID_ALLOW_REAL_SEND=1` *and* an explicit `--real` flag. No accidental messages.

**The server binds localhost.** `serve` only listens on `127.0.0.1`. Don't expose it.

### About `csrutil enable --without debug`

This is needed only for the debugger bridge (advanced typewriter/send). It's an official Apple developer exemption that allows attaching a debugger to system processes. Everything else in madrid works without it.

What stays on: filesystem protection, NVRAM, runtime integrity, kext signing. What's exempted: `task_for_pid()` on Apple binaries.

The risk is that malware on your machine could read process memory. But if malware is already running, it can keylog regardless. Use on trusted machines only. Revert anytime with `csrutil enable` in recovery mode.

## Layout

```
madrid/           Python package (no dependencies)
  serve.py        HTTP + SSE server
  engine.py       Message lifecycle and routing
  rich.py         HTML ⇄ Markdown ⇄ Apple attribute keys
  mddoc.py        Markdown → rich text + PNG cards
  realbox.py      chat.db reader (read-only)
  watch.py        Event daemon (kqueue + polling fallback)
  stats.py        Message/chat/contact statistics
  contacts.py     Handle → contact name resolution
  fastwatch.py    kqueue file-change detection
  bridge.py       Debugger bridge (optional, needs SIP exemption)
  native/         ObjC helpers (compiled with clang on first use)
integrations/hermes/  Hermes gateway plugin
sdk/typescript/       TypeScript client
```

## License

TBD.
