"""Smoke checks for the madrid iMessage Hermes adapter (no gateway needed).
Run:  python3 integrations/hermes/test_adapter.py
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []

def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)

src = open(os.path.join(HERE, "adapter.py")).read()
tree = ast.parse(src)

classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
cls = next(c for c in classes if c.name == "ImessageAdapter")
methods = {m.name for m in cls.body if isinstance(m, ast.AsyncFunctionDef)} | \
          {m.name for m in cls.body if isinstance(m, ast.FunctionDef)}
check("adapter has BasePlatformAdapter parent",
      any("BasePlatformAdapter" in ast.dump(b) for b in cls.bases))
for required in ("connect", "disconnect", "send", "send_draft", "get_chat_info",
                 "_sse_loop", "_dispatch_event"):
    check(f"adapter implements {required}", required in methods)

check("outbound uses /send endpoint", "/send" in src)
check("inbound uses /events SSE", "/events" in src)
check("typewriter path exists", "/typewriter" in src)
check("markdown doc mode default", 'MADRID_SEND_FORMAT", "markdown"' in src)
check("allowlist wired", "MADRID_ALLOWED_USERS" in src and "_is_sender_authorized" in src)
check("echo prevention (from_me skip)", "from_me" in src)
check("plugin register() exists", "def register(ctx)" in src)
check("plugin.yaml present", os.path.exists(os.path.join(HERE, "plugin.yaml")))
yml = open(os.path.join(HERE, "plugin.yaml")).read()
check("plugin kind=platform", "kind: platform" in yml)
check("required env declares MADRID_URL", "MADRID_URL" in yml)

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all adapter checks passed")
