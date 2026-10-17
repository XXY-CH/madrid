# Changelog

> Renamed from `imsg` to `madrid` (2026-10-14).

## 0.9.0 (2026-10-14)
- Hermes Agent gateway platform adapter (`integrations/hermes/`): iMessage as
  a first-class Hermes channel via madrid HTTP+SSE — markdown document mode
  outbound, SSE inbound, allowlist, typewriter streaming, cron delivery;
  installed to ~/.hermes/hermes-agent/plugins/platforms/imessage/

## 0.8.0 (2026-10-14)
- Bridge-first typewriter: the resident in-process bridge (madrid/bridge.py,
  product-ized) is the primary frame path; UI-edit is automatic fallback when
  the bridge is absent or its edit op is not yet wired; `bridge status|install|ping`
  CLI + MCP tools
- Receipt boot snapshot: watcher's first poll reports current read/delivered
  state of recent outgoing messages as receipt events

## 0.7.0 (2026-10-14)
- Real typewriter (experimental): frame 0 via pasteboard, later frames
  UI-edit the newest bubble (AXShowMenu -> localized Edit -> cmd+A -> type);
  `--verify-only` probe lists the context menu without editing; wired into
  CLI/HTTP/MCP
- Multi-agent / virtual-number guidance (docs §11)

## 0.6.0 (2026-10-14)
- Read receipts both ways: live `receipt` events when peers read/deliver our
  messages; `mark-read` surface (CLI/HTTP/MCP) to mark a chat read
- Real-mode `edit` now fails loudly instead of silently not reaching the wire

## 0.5.0 (2026-10-14)
- Markdown document mode: full-block parsing; tables & code blocks render to
  inline PNG cards (AppKit), everything else maps to real attributes/text;
  composite paste for the real-send path

## 0.4.0 (2026-10-14)
- `madrid serve`: HTTP + SSE agent surface (REST, OpenAPI 3.1 at /openapi.json,
  live /events stream with resumption); CLI `serve` verb
- TypeScript SDK (`sdk/typescript/madrid.ts`, zero-dependency)
- Store is now thread-safe (shared by HTTP workers and the watch daemon)

## 0.3.0 (2026-10-14)
- Rich-text real send: pasteboard bridge (bold/italic/underline/strikethrough/
  links/fonts/colors) + headless Shortcuts channel
- Typewriter via edits: sim send side (`edit`/`retract` with protocol windows)
  and frame-by-frame receive events (`real.edited`)
- Edit/retract receive decoding (summary_info: retracted parts, text ranges)
- Handwriting balloon payload detection in envelopes
- Fix: long-lived watcher could miss new writes (read-only connection now
  uses `mode=ro` so WAL content and external updates are visible)
- Projectization: pyproject/Makefile/CHANGELOG; local analysis artifacts
  consolidated and gitignored

## 0.2.0 (2026-10-13)
- Standard rich-text layer (HTML subset / Markdown / runs, Apple attribute keys)
- Real inbox decoder + watch daemon (event stream / notifications / webhook / JSONL)
- MCP server (28 tools), capability matrix (24 features), AppleScript fixes

## 0.1.0 (2026-10-13)
- Initial release: simulator + AppleScript real send (double-gated) +
  attributedBody reader
