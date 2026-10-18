"""madrid Python client — zero dependencies (stdlib urllib).

    from madrid import Madrid
    im = Madrid("http://127.0.0.1:8787")
    im.send("+15551234567", "hello **world**", format="markdown")
"""
import json
import urllib.request
import urllib.parse


class Madrid:
    def __init__(self, base: str = "http://127.0.0.1:8787"):
        self.base = base.rstrip("/")

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, method=method, data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def health(self):
        return self._call("GET", "/health")

    def capabilities(self):
        return self._call("GET", "/capabilities")

    def list_chats(self):
        return self._call("GET", "/chats")

    def open_chat(self, handles, name=None):
        return self._call("POST", "/chats", {"handles": handles, "name": name})

    def transcript(self, chat_id, limit=50):
        q = urllib.parse.urlencode({"limit": limit})
        return self._call("GET", f"/chats/{urllib.parse.quote(chat_id, safe='')}/transcript?{q}")

    def send(self, chat_id, text, format="plain", **kw):
        return self._call("POST", "/send", {
            "chat_id": chat_id, "text": text, "format": format, **kw})

    def edit(self, guid, text, format="plain"):
        return self._call("POST", "/edit", {"guid": guid, "text": text, "format": format})

    def retract(self, guid):
        return self._call("POST", "/retract", {"guid": guid})

    def tapback(self, guid, kind):
        return self._call("POST", "/tapback", {"guid": guid, "kind": kind})

    def typewriter(self, handle, frames, interval=1.0, format="plain"):
        return self._call("POST", "/typewriter", {
            "handle": handle, "frames": frames,
            "interval": interval, "format": format})

    def mark_read(self, handle):
        return self._call("POST", "/mark-read", {"handle": handle})

    def real_chats(self, limit=50):
        return self._call("GET", f"/real/chats?limit={limit}")

    def real_transcript(self, chat=None, limit=25):
        q = urllib.parse.urlencode({"limit": limit, **({"chat": chat} if chat else {})})
        return self._call("GET", f"/real/transcript?{q}")

    def stats(self, sub=None):
        return self._call("GET", f"/stats{f'?sub={sub}' if sub else ''}")

    def contacts(self, handles):
        q = urllib.parse.urlencode({"handles": ",".join(handles)})
        return self._call("GET", f"/contacts?{q}")

    def openapi(self):
        return self._call("GET", "/openapi.json")
