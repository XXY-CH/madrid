# madrid — iMessage Rich-Text Bridge for Agents

**English** | [中文](README_zh.md)

> `madrid` is Apple's internal codename for the iMessage service (`com.apple.madrid`).
> Formerly `imsg`, renamed and greatly expanded in Oct 2026.

Turn your Apple ID into an agent-ready iMessage channel (Hermes / Qwen Code / any MCP client):

- **Send**: Rich text (HTML subset / Markdown / plain) + attachments; Markdown document mode auto-renders tables/code blocks as inline PNG cards
- **Receive**: Read-only `chat.db` polling with full decode — rich text runs, attachments, tapbacks, stickers, replies, audio, edits (frame-by-frame), retractions, read receipts, system events, balloon payloads
- **Notifications**: SSE stream / macOS notifications / webhooks / JSONL / command hooks
- **Typewriter**: Edit-protocol streaming (15-min window), bridge-first/UI-fallback, HTTP interface ready
- **Slash commands**: Native support via Hermes gateway integration (`/help` `/status` etc., inherited from the adapter base class)
- **Hermes gateway native channel**: iMessage as a first-class Hermes Agent platform (on par with Telegram/Discord)
- **Zero third-party dependencies** (pure stdlib), macOS + Xcode CLT only

```bash
make test          # see CHANGELOG for count
make doctor        # environment + capability matrix
python3 -m madrid --help
```

## madrid vs [openclaw/imsg](https://github.com/openclaw/imsg)

[imsg](https://github.com/openclaw/imsg) (1.4k⭐, Swift, MIT) is the mature CLI for iMessage automation.
madrid takes a different angle — **rich text + agent-first interfaces**. Here's the honest comparison:

| Feature | imsg | madrid |
|---|---|---|
| **Language** | Swift 6 (compiled, fast) | Python (stdlib only, zero deps) |
| **Platform** | macOS 14+, Linux read-only | macOS only |
| **Receive: text** | ✅ from `text` column + attributedBody recovery | ✅ same |
| **Receive: rich text decode** | Plain text only | ✅ Full Apple attribute keys (bold/italic/strike/underline/links/mentions/effects) |
| **Receive: tapbacks** | ✅ | ✅ |
| **Receive: edits/retract** | — | ✅ Frame-by-frame + retraction events |
| **Receive: read receipts** | — | ✅ Live `receipt` events |
| **Receive: stickers/replies/audio** | Stickers ✅ | ✅ All + played status |
| **Receive: polls** | ✅ Native decode | — (payload detection only) |
| **Send: text + files** | ✅ AppleScript (auto-staged) | ✅ AppleScript |
| **Send: rich text** | Bridge only (SIP off + dylib) | ✅ Pasteboard channel (bold/italic/links/fonts) |
| **Send: attachments** | ✅ | ✅ |
| **Send: tapbacks** | ✅ AppleScript | ✅ Sim; bridge for real |
| **Markdown → rich text** | — | ✅ Tables/code → inline PNG cards |
| **Typewriter streaming** | — | ✅ Edit-protocol (bridge-first/UI fallback) |
| **Watch mechanism** | FSEvents + polling fallback | Polling (`mode=ro`, WAL-aware) |
| **CLI interface** | ✅ Rich (completions, LLM help) | ✅ JSON output |
| **JSON-RPC stdio** | ✅ | — |
| **HTTP/SSE server** | — | ✅ REST + OpenAPI 3.1 + SSE events |
| **MCP server** | — | ✅ 30+ tools |
| **TypeScript SDK** | — | ✅ Zero-dependency |
| **Hermes gateway adapter** | — | ✅ Native platform (slash commands/streaming/allowlist) |
| **Statistics** | ✅ Message/media counts | — |
| **Chat creation** | ✅ New handles via IDS check | — |
| **Advanced (SIP off)** | Dylib injection (rich send/read/typing/stickers) | Debugger bridge (typewriter/full send) |
| **License** | MIT | TBD |

**TL;DR**: Use **imsg** for a polished native CLI, Linux reads, statistics, and poll decoding.
Use **madrid** when you need **rich text** (decode + send), **agent-first APIs** (HTTP/MCP/SDK),
**Markdown document mode**, or **typewriter streaming**.

## Quick Start

```bash
pip install -e .

export MADRID_DB=/tmp/madrid.db
python3 -m madrid doctor                       # env check + 24-feature matrix
python3 -m madrid open --to "+15551234567" --name "Test"
python3 -m madrid send --chat chat-+15551234567 \
    'hey <b>Alex</b> check <a href="https://docs">this</a>' --format html \
    --mention 4:4=+15551234567 --effect slam
python3 -m madrid edit <guid> "Generating answer: 42"      # typewriter frame
python3 -m madrid watch --notify --jsonl ~/.madrid/inbox.jsonl   # daemon
python3 -m madrid real-chats --limit 10        # real chats (read-only)
python3 -m madrid serve --port 8787 --watch    # HTTP+SSE (agent channel plugin)
```

## Rich Text

| Feature | HTML (canonical) | Markdown | Attribute key |
|---|---|---|---|
| Bold/Italic/Strikethrough | `<b> <i> <s>` | `**x** *x* ~~x~~` | `__kIMText{Bold,Italic,Strikethrough}AttributeName` |
| Underline | `<u>` | `__x__` | `__kIMTextUnderlineAttributeName` |
| Link | `<a href>` | `[x](url)` | `__kIMLinkAttributeName` |
| Mention | `<span data-mention>` | `mentions` param | `__kIMMentionAttributeName` |
| Text effect | `<span data-effect>` | `effect` param | `__kIMTextEffectAttributeName` |
| Inline attachment | `<img data-attachment>` | `attachments` param | `__kIMFileTransferGUIDAttributeName` |
| Table/code block | — | auto → PNG card | AppKit render |

## Hermes Agent Integration

```bash
# 1. madrid side
python3 -m madrid serve --port 8787 --watch

# 2. Hermes side (adapter installed at ~/.hermes/hermes-agent/plugins/platforms/imessage/)
export MADRID_URL=http://127.0.0.1:8787
hermes gateway setup       # pick "💬 iMessage (madrid)"
hermes gateway start
```

iMessage becomes a native Hermes channel: anyone messages your Apple ID, the agent automatically responds (with rich text). Supports slash commands (`/help` `/status`), typewriter streaming (`MADRID_TYPEWRITER=true`), allowlist (`MADRID_ALLOWED_USERS`), cron delivery (`MADRID_HOME_CHANNEL`).

## Send Channels

| Channel | Capability | Status |
|---|---|---|
| AppleScript (`send`) | Text + files | ✅ Double-gated |
| Pasteboard + paste | Full rich text (fonts/colors) | ✅ Needs Automation perm |
| Shortcuts (`shortcuts run`) | Headless text + attachments | ✅ One-time setup |
| Resident bridge (debugger install-once) | Theoretical full capability | 🔧 Tool ready, see security note |

## Security

### `csrutil enable --without debug` (resident bridge prerequisite)

This is a **official developer exemption** in macOS SIP. What it does:

| Protection kept ON | Exemption |
|---|---|
| Filesystem protection (/System sealed) | Allow `task_for_pid()` on Apple platform binaries |
| NVRAM protection | Allow debugger read/write on attached processes |
| Runtime integrity (library validation) | — |
| Kext signing requirement | — |

**Practical risk**: Any process with your user privileges can attach a debugger to system processes. Malware already running could read process memory — but it could equally keylog/screenshot with SIP fully enabled.

**Mitigations**: Only on trusted machines; reversible (`csrutil enable`); madrid's bridge installs only a localhost Unix-socket dispatcher (no network exposure, no system file modification).

### General

- `chat.db` is **always opened read-only** (`mode=ro`; never writes to Apple's DB)
- Real sends are **double-gated**: `MADRID_ALLOW_REAL_SEND=1` + explicit real mode
- `serve` binds `127.0.0.1` only — do not expose publicly
- macOS permissions: Full Disk Access (chat.db), Automation (Messages), System Events (pasteboard)

## Project Structure

```
madrid/                    Python package (pure stdlib)
  cli.py  mcp_server.py    CLI (JSON output) & MCP stdio server
  serve.py                 HTTP + SSE server (REST + OpenAPI 3.1 + SSE /events)
  engine.py                Lifecycle, send(format=), edit/retract, typewriter, real-send routing
  rich.py                  Rich text layer: HTML ⇄ Markdown ⇄ runs
  mddoc.py                 Markdown document mode: block parsing → runs + PNG cards
  realbox.py               Real inbox: chat.db read-only → standard envelopes
  watch.py                 Daemon: polling → event stream/notifications/webhook/JSONL
  capabilities.py          Capability matrix + environment probing
  store.py  model.py       Local SQLite & data model (thread-safe)
  applescript.py           AppleScript/pasteboard/Shortcuts channels (double-gated)
  uiedit.py                UI edit path (typewriter fallback)
  bridge.py                Resident bridge (debugger install-once, native speed)
  real_format.py  richtext.py   attributedBody reading & sim container
  native/                  Runtime native tools (clang)
    decode_attributed_body.m  typedstream Foundation decoder
    rich_pasteboard.m         Rich text → NSAttributedString → clipboard
    render_html_png.m         HTML block → PNG card (AppKit)
integrations/hermes/       Hermes gateway platform adapter (iMessage channel plugin)
sdk/typescript/            Zero-dependency TS client (HTTP+SSE)
docs/agent-integration.md  Integration guide (the only doc shipped in-repo)
tests/                     Smoke tests (no pytest dependency)
```

## Documentation

| File | When to read |
|---|---|
| [agent-integration.md](docs/agent-integration.md) | Agent deployment / envelope schema / Hermes setup / read receipts / multi-agent / security |

## License

TBD (private repository).
