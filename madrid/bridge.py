"""Resident in-process bridge (debugger install-once, native-speed thereafter).

The debugger is used ONCE as an installer: a single expression installs a
Unix-socket command dispatcher on Messages' main runloop; afterwards the
process is detached and every command is a plain local socket round-trip
(sub-millisecond). No images are loaded into the app; the dispatcher is
written by the debugger's expression evaluator.

Prerequisites (one-time, user-side):
  recoveryOS: csrutil enable --without debug
  local:      sudo DevToolsSecurity -enable

Lifecycle: the dispatcher lives until Messages quits. Re-install after
relaunch/OS update. The command table starts with `ping` only — feature ops
(`edit`…) are wired in-place once their runtime signatures are confirmed in
the live process (see `verify_op`).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess

GATE_ENV = "MADRID_ALLOW_REAL_SEND"
BRIDGE_SOCKET = "/tmp/madrid-bridge.sock"

INSTALL_EXPR = (
    '^{'
    'NSFileManager *fm=[NSFileManager defaultManager];'
    f' [fm removeItemAtPath:@"{BRIDGE_SOCKET}" error:nil];'
    ' int fd=socket(AF_UNIX,SOCK_STREAM,0);'
    ' struct sockaddr_un a; a.sun_family=AF_UNIX;'
    f' strcpy(a.sun_path,"{BRIDGE_SOCKET}");'
    ' bind(fd,(struct sockaddr*)&a,sizeof(a)); listen(fd,16);'
    ' int flags=fcntl(fd,F_GETFL); fcntl(fd,F_SETFL,flags|O_NONBLOCK);'
    ' NSFileHandle *h=[[NSFileHandle alloc] initWithFileDescriptor:fd'
    '                                                closeOnDealloc:NO];'
    ' dispatch_source_t src=dispatch_source_create(DISPATCH_SOURCE_TYPE_READ,'
    '                                              (uintptr_t)fd,0,'
    '                                              dispatch_get_main_queue());'
    ' dispatch_source_set_event_handler(src, ^{'
    '   int c=accept(fd,NULL,NULL); if(c<0) return;'
    '   char buf[65536]; ssize_t n=read(c,buf,sizeof(buf)-1);'
    '   if(n<=0){ close(c); return; } buf[n]=0;'
    '   NSData *d=[NSData dataWithBytes:buf length:n];'
    '   NSError *e=nil; NSDictionary *cmd=[NSJSONSerialization JSONObjectWithData:d'
    '                                     options:0 error:&e];'
    '   NSString *op=e?nil:cmd[@"op"]; NSString *out;'
    '   if(!op){ out=@"{\\"error\\":\\"bad command\\"}"; }'
    '   else if([op isEqualToString:@"ping"]){ out=@"{\\"pong\\":true}"; }'
    '   else{ out=[NSString stringWithFormat:@"{{\\\\\\"unknown_op\\\\\\":\\\\\\"%@\\\\\\"}}",op]; }'
    '   const char *o=out.UTF8String;'
    '   write(c,o,strlen(o)); close(c);'
    ' });'
    ' dispatch_resume(src);'
    ' objc_setAssociatedObject([NSObject new],(void*)"madrid_bridge",src,'
    '                          OBJC_ASSOCIATION_RETAIN);'
    ' return @"bridge-installed"; }()')


class BridgeError(RuntimeError):
    pass


class BridgeOpNotWired(BridgeError):
    """The dispatcher is resident but this op is not wired yet."""


# ---- client -------------------------------------------------------------------

def call(payload: dict, timeout: float = 5.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(BRIDGE_SOCKET)
        s.sendall(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except (OSError, socket.timeout) as exc:
        raise BridgeError(f"bridge unreachable: {exc}") from exc
    finally:
        s.close()
    line = buf.split(b"\n")[0].decode()
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"raw": line}


def ping() -> bool:
    try:
        return bool(call({"op": "ping"}, timeout=2.0).get("pong"))
    except BridgeError:
        return False


def available() -> bool:
    return ping()


def edit(frame: str) -> dict:
    """Typewriter frame step. Raises BridgeOpNotWired until the edit op is
    confirmed and wired in the live process."""
    r = call({"op": "edit", "text": frame})
    if "unknown_op" in r:
        raise BridgeOpNotWired("edit op not wired yet — dispatcher resident, "
                               "command table pending live-process wiring")
    return r


# ---- installer ------------------------------------------------------------------

def status() -> dict:
    st = {"lldb": bool(shutil.which("lldb"))}
    try:
        p = subprocess.run(["csrutil", "status"], capture_output=True, text=True)
        st["csrutil_debug_open"] = "Debugging Restrictions: disabled" in p.stdout
    except OSError:
        st["csrutil_debug_open"] = False
    try:
        p = subprocess.run(["DevToolsSecurity", "-status"],
                           capture_output=True, text=True)
        st["devtools_security"] = "enabled" in p.stdout.lower()
    except OSError:
        st["devtools_security"] = False
    st["messages_running"] = subprocess.run(["pgrep", "-x", "Messages"],
                                            capture_output=True).returncode == 0
    st["bridge_resident"] = ping()
    st["ready_to_install"] = all((st["lldb"], st["csrutil_debug_open"],
                                  st["devtools_security"], st["messages_running"]))
    return st


def install() -> str:
    if os.environ.get(GATE_ENV) != "1":
        raise BridgeError(f"gated: set {GATE_ENV}=1 (this attaches a debugger "
                          "to Messages and installs a resident dispatcher)")
    cmds = ["process attach -n Messages",
            'script print("<MARK:bridge>")',
            f"expr -l objc++ -O -- (id){INSTALL_EXPR}"]
    args = ["lldb", "--batch"]
    for c in cmds:
        args += ["-o", c]
    args += ["-o", "detach", "-o", "quit"]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise BridgeError("lldb not found")
    out = p.stdout
    if "bridge-installed" in out:
        return "bridge-installed"
    raise BridgeError((p.stderr or out or "").strip()[-400:] or "install failed")
