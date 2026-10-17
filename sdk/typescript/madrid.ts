/**
 * madrid TS client — zero-dependency (fetch + EventSource).
 * Server: `python3 -m madrid serve --port 8787`
 */
export class Madrid {
  constructor(private base: string = "http://127.0.0.1:8787") {}

  private async call<T>(path: string, init?: RequestInit): Promise<T> {
    const r = await fetch(this.base + path, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
    if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
    return r.json() as Promise<T>;
  }

  health() { return this.call<{ ok: boolean; mode: string }>("/health"); }
  capabilities() { return this.call<any>("/capabilities"); }
  listChats() { return this.call<any[]>("/chats"); }
  openChat(handles: string[], name?: string) {
    return this.call<any>("/chats", { method: "POST", body: JSON.stringify({ handles, name }) });
  }
  transcript(chatId: string, limit = 50) {
    return this.call<any[]>(`/chats/${encodeURIComponent(chatId)}/transcript?limit=${limit}`);
  }
  /** Rich text: format "html" | "markdown" | "plain" (default plain). */
  send(chatId: string, text: string, opts: {
    format?: "html" | "markdown" | "plain";
    effect?: string; replyTo?: string; mentions?: any[]; attachments?: any[];
  } = {}) {
    return this.call<any>("/send", { method: "POST", body: JSON.stringify({
      chat_id: chatId, text, format: opts.format ?? "plain",
      effect: opts.effect ?? "none", reply_to: opts.replyTo,
      mentions: opts.mentions, attachments: opts.attachments,
    }) });
  }
  /** Typewriter frame: edit an already-sent message (15-min window). */
  edit(guid: string, text: string, format: "html" | "markdown" | "plain" = "plain") {
    return this.call<any>("/edit", { method: "POST", body: JSON.stringify({ guid, text, format }) });
  }
  retract(guid: string) {
    return this.call<any>("/retract", { method: "POST", body: JSON.stringify({ guid }) });
  }
  tapback(guid: string, kind: string) {
    return this.call<any>("/tapback", { method: "POST", body: JSON.stringify({ guid, kind }) });
  }
  realChats(limit = 50) { return this.call<any[]>(`/real/chats?limit=${limit}`); }
  realTranscript(chat?: string, limit = 25) {
    const q = new URLSearchParams({ limit: String(limit), ...(chat ? { chat } : {}) });
    return this.call<any[]>(`/real/transcript?${q}`);
  }

  /** Live event stream (incoming messages, edits, tapbacks…). */
  events(onEvent: (e: any) => void, since = 0): EventSource {
    const es = new EventSource(`${this.base}/events?since=${since}`);
    es.onmessage = (m) => onEvent(JSON.parse(m.data));
    return es;
  }
}
