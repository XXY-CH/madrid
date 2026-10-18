"""Shell completion scripts for bash, zsh, and fish.

Usage:
    python3 -m madrid completions bash
    python3 -m madrid completions zsh
    python3 -m madrid completions fish

Or source directly:
    source <(python3 -m madrid completions zsh)
"""
from __future__ import annotations

COMMANDS = [
    ("doctor", "environment and capability check"),
    ("accounts", "list accounts"),
    ("chats", "list chats"),
    ("open", "open or create a chat"),
    ("send", "send a message"),
    ("receive", "inject an incoming message (sim)"),
    ("edit", "edit a sent message"),
    ("retract", "retract (unsend) a sent message"),
    ("edit-history", "list edit frames of a message"),
    ("pump", "advance pending lifecycle transitions"),
    ("wait", "pump until settled"),
    ("read", "mark a message read"),
    ("tapback", "add a tapback"),
    ("typing", "typing indicator"),
    ("transcript", "messages in a chat"),
    ("search", "search message text"),
    ("events", "event log"),
    ("archive", "archive/unarchive a chat"),
    ("mute", "mute/unmute a chat"),
    ("real-body", "read real attributedBody blobs (read-only)"),
    ("real-chats", "list real chats (read-only)"),
    ("real-transcript", "decode recent real messages (read-only)"),
    ("real-accounts", "real accounts via AppleScript"),
    ("stats", "statistics over the real chat.db"),
    ("contacts", "resolve handles to contact names"),
    ("create-chat", "open a chat with a new handle"),
    ("mark-read", "real: mark a conversation read"),
    ("typewriter", "real: stream frames into one message via edits"),
    ("bridge", "resident in-process bridge (debugger install-once)"),
    ("send-shortcut", "real: headless send via shortcuts run"),
    ("rich", "convert text between rich formats"),
    ("watch", "watch the real inbox and dispatch envelopes"),
    ("serve", "HTTP + SSE server for agents"),
    ("rpc", "JSON-RPC 2.0 over stdio"),
    ("completions", "generate shell completion script"),
    ("login", "real: login all accounts"),
    ("logout", "real: logout all accounts"),
]


def _cmd_names() -> list[str]:
    return [c[0] for c in COMMANDS]


def bash() -> str:
    cmds = " ".join(_cmd_names())
    return f"""_madrid() {{
    local cur prev opts
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"

    if [[ $COMP_CWORD -eq 1 ]]; then
        opts="{cmds}"
        COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
    elif [[ "$prev" == "--format" ]]; then
        COMPREPLY=( $(compgen -W "plain html markdown" -- "$cur") )
    elif [[ "$prev" == "--effect" ]]; then
        COMPREPLY=( $(compgen -W "none slam echo shake bloom invisibleInk bigLoud spotlight confetti" -- "$cur") )
    elif [[ "$prev" == "--service" ]]; then
        COMPREPLY=( $(compgen -W "sims ssms srcs" -- "$cur") )
    elif [[ "$prev" == "-f" || "$prev" == "--db" ]]; then
        COMPREPLY=( $(compgen -f -- "$cur") )
    elif [[ "$prev" == "--chat" || "$prev" == "--to" ]]; then
        COMPREPLY=( )
    else
        COMPREPLY=( $(compgen -W "--help --format --effect --mention --attach --reply-to --chat --to --db --real --json --limit --interval --notify --webhook --jsonl --source --once --backfill" -- "$cur") )
    fi
}}
complete -F _madrid madrid
"""


def zsh() -> str:
    lines = ["#compdef madrid", "_madrid() {"]
    lines.append("  local -a commands")
    lines.append("  commands=(")
    for name, desc in COMMANDS:
        lines.append(f"    '{name}[{desc}]'")
    lines.append("  )")
    lines.append("  _arguments -C '1: :->cmds' '*: :->args'")
    lines.append("  case $state in")
    lines.append("    cmds) _describe 'command' commands ;;")
    lines.append("    args)")
    lines.append("      case $words[1] in")
    lines.append("        send)")
    lines.append("          _arguments '--format[plain/html/markdown]' '--effect[animation]' '--mention[range=handle]' '--attach[file]' '--reply-to[guid]'")
    lines.append("          ;;")
    lines.append("        serve)")
    lines.append("          _arguments '--port[number]' '--host[addr]' '--watch[ingest real inbox]' '--real[real send mode]'")
    lines.append("          ;;")
    lines.append("        watch)")
    lines.append("          _arguments '--notify[macOS notifications]' '--webhook[URL]' '--jsonl[path]' '--interval[seconds]' '--once[poll once]'")
    lines.append("          ;;")
    lines.append("      esac")
    lines.append("      ;;")
    lines.append("  esac")
    lines.append("}")
    lines.append("_madrid \"$@\"")
    return "\n".join(lines) + "\n"


def fish() -> str:
    lines = []
    for name, desc in COMMANDS:
        lines.append(f"complete -c madrid -n '__fish_use_subcommand' -a '{name}' -d '{desc}'")
    lines.append("complete -c madrid -n '__fish_seen_subcommand_from send' -l format -a 'plain html markdown' -d 'rich text format'")
    lines.append("complete -c madrid -n '__fish_seen_subcommand_from send' -l effect -d 'text effect'")
    lines.append("complete -c madrid -n '__fish_seen_subcommand_from serve' -l port -d 'port number'")
    lines.append("complete -c madrid -n '__fish_seen_subcommand_from serve' -l watch -d 'ingest real inbox'")
    lines.append("complete -c madrid -n '__fish_seen_subcommand_from watch' -l notify -d 'macOS notifications'")
    return "\n".join(lines) + "\n"


def generate(shell: str) -> str:
    if shell == "bash":
        return bash()
    if shell == "zsh":
        return zsh()
    if shell == "fish":
        return fish()
    raise ValueError(f"unknown shell: {shell} (bash/zsh/fish)")


def main() -> None:
    import sys
    if len(sys.argv) < 2:
        print("usage: python3 -m madrid completions bash|zsh|fish", file=sys.stderr)
        sys.exit(2)
    print(generate(sys.argv[1]))


if __name__ == "__main__":
    main()
