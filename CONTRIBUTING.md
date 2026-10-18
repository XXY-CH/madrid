# Contributing

## Setup

```bash
git clone https://github.com/XXY-CH/madrid.git
cd madrid
pip install -e .
```

No build step. No dependencies. Pure stdlib.

## Running tests

```bash
python3 tests/test_smoke.py
python3 integrations/hermes/test_adapter.py
```

On CI or in containers where `~/Library/Messages/chat.db` isn't available:

```bash
MADRID_SKIP_REAL_DB=1 python3 tests/test_smoke.py
```

## Linting

```bash
pip install ruff
ruff check madrid/
```

## Project layout

```
madrid/           Python package (the whole product)
  serve.py        HTTP + SSE server — this is the main agent interface
  rpc.py          JSON-RPC 2.0 over stdio
  cli.py          Command-line interface
  mcp_server.py   MCP stdio server
  engine.py       Message lifecycle, routing, real-send orchestration
  rich.py         HTML ⇄ Markdown ⇄ Apple attribute keys
  mddoc.py        Markdown document mode (tables/code → PNG cards)
  realbox.py      chat.db reader (read-only, full decode)
  watch.py        Event daemon (kqueue + polling fallback)
  fastwatch.py    kqueue file-change detection
  stats.py        Message/chat/contact statistics
  contacts.py     Handle → contact name resolution
  applescript.py  AppleScript/pasteboard/Shortcuts send channels
  uiedit.py       UI edit path (typewriter fallback)
  bridge.py       Debugger bridge (optional, needs SIP exemption)
  native/         ObjC helpers (self-compiling with clang)
integrations/hermes/  Hermes gateway plugin
sdk/                  Python, Go, Rust, TypeScript clients
tests/                Smoke tests
```

## Key rules

1. **`chat.db` is always read-only.** Never write to Apple's database.
2. **Real sends are double-gated.** `MADRID_ALLOW_REAL_SEND=1` + explicit `--real`.
3. **Zero dependencies.** Only Python stdlib in `madrid/`.
4. **Tests must pass.** `python3 tests/test_smoke.py` before every PR.

## Release process

```bash
# 1. Update CHANGELOG.md
# 2. Tag and push
git tag v1.X.0
git push origin main --tags
# 3. CI runs tests, then auto-publishes to PyPI via Trusted Publishing
```
