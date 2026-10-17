# Agent 接入指南（Hermes / Qwen Code / 任意 MCP 客户端）

**目标**：让一个 Agent 用你的 Apple ID 收发 iMessage 富文本。发走 Messages.app
（AppleScript `send`），收走 `chat.db` 只读解码，通知走事件流 / macOS 通知 /
webhook / JSONL。

一条命令自检：

```bash
python3 -m madrid doctor        # environment: osascript/sdef/messages_app/chat_db_readable/clang
                             # capabilities: 每个特性 × {sim_send, sim_receive, real_send, real_receive}
```

---

## 0. 前置（一次性）

1. **Apple ID 登录**：系统设置 → 信息（Messages）→ iMessage，签入你要用的
   Apple ID。凭据输入本身不可脚本化（安全设计）；之后随时：

   ```bash
   python3 -m madrid real-accounts   # 查看账户与连接态（sdef account 对象）
   python3 -m madrid --real login    # 激活全部账户（幂等）
   ```

2. **权限**：首次真发/真查会弹两个 TCC 授权框——"自动化"（控制 Messages）与
   "完全磁盘访问"（读 `chat.db` 的终端进程）。点允许即可。

3. **解码器**（可选但推荐）：富文本 runs 需要 Foundation 解码器，首次使用时
   自动用 clang 编译（需要 Xcode CLT）；也可手动：

   ```bash
   clang -fobjc-arc -framework Foundation madrid/native/decode_attributed_body.m \
         -o /tmp/decode_attributed_body
   ```

---

## 1. MCP 接入（推荐）

`python3 -m madrid.mcp_server`，stdio JSON-RPC 2.0，28 个工具，无第三方依赖。

Hermes（或任何 MCP 客户端）注册：

```json
{
  "mcpServers": {
    "madrid": {
      "command": "python3",
      "args": ["-m", "madrid.mcp_server"],
      "cwd": "/Users/xiexingyu/Documents/Projects/message",
      "env": {"MADRID_DB": "~/.madrid/sim.db"}
    }
  }
}
```

### 发送

```
send {
  chat_id: "chat-+15551234567",          # 先 open_chat 拿到；或 real chat guid（real 模式）
  text:  "部署完成：<b>v1.2.0</b> 已上线，见 <a href='https://ci/run/42'>CI</a>",
  format: "html",                        # plain | html | markdown
  mentions: [{handle: "+15551234567", start: 0, length: 4}],   # 可选
  effect: "slam",                        # 可选（sim 全保真；真发会 dropped 上报）
  attachments: [{kind: "image", url: "/tmp/chart.png"}],       # 可选；真发=发文件
  reply_to: "<guid>"                     # 可选
}
```

Markdown 同理：`text: "**v1.2.0** 已上线 [CI](https://ci/run/42)", format: "markdown"`。

返回：`text`（纯文本投影）、`html`（标准富文本）、`attributedBody`（sim 容器）、
`part`（渲染类名）、real 模式下另有 `bridge: "osascript"` 与 `dropped: [...]`
（被降级的特性，如实上报）。

### 接收（两种姿势）

**A. 拉模型**：让 server 后台帮你看库，你定期取事件。

```
watch_start {notify: true, webhook: "http://127.0.0.1:8787/madrid"}   # 一次
events     {since_seq: 0}                                           # 轮询增量
watch_status / watch_stop                                           # 控制
```

**B. 推模型**：`watch_start` 的 `webhook` 参数把每个信封 POST 到你的服务
（Hermes 的 ingest 端点）；或用 CLI 常驻进程（见 §2）。

其余高频工具：`real_chats`（真实会话列表）、`real_transcript`（解码最近真实
消息）、`chats`/`transcript`/`search`（sim 库）、`tapback`、`set_typing`、
`mark_read`、`rich_convert`、`notify`（发 macOS 通知）、`doctor`。

### 真发门控

`real_send` 与 `--real send` 需要**同时**：进程以 real 模式启动 + 环境变量
`MADRID_ALLOW_REAL_SEND=1`。MCP 注册里想真发就把 env 加上；不加则只能 sim。

---

## 2. CLI 常驻进程（不经 MCP 也要收通知）

```bash
# 回放最近 10 条看看信封长什么样
python3 -m madrid watch --once --backfill 10

# 常驻：入库事件流 + macOS 通知 + JSONL + webhook + 命令钩子（任意组合）
python3 -m madrid watch \
    --notify \
    --jsonl ~/.madrid/inbox.jsonl \
    --webhook http://127.0.0.1:8787/madrid \
    --command 'curl -s -X POST http://127.0.0.1:8787/ingest -d @-' \
    --interval 2
```

`--command` 的子进程环境变量 `MADRID_EVENT` 是信封 JSON；stdin 无内容，
用 `$MADRID_EVENT` 传递。

---

## 3. 信封 schema（real.received / webhook / JSONL 的 payload）

```jsonc
{
  "event": "new",                       // new | updated
  "type": "message",                    // message | tapback | sticker | retracted
  "guid": "2E9B2131-4302-479C-A1B2-2BFC8973D082",
  "rowid": 12456,
  "chat": {"guid": "any;-;+1415…", "name": "…", "handles": ["+1415…"],
            "service": "sims", "style": 45, "archived": false},
  "from_me": false,
  "sender": "+1415…",                    // from_me 时为 "me"
  "service": "sims",                     // sims | ssms | srcs
  "date": 1760340000000,                 // Unix ms
  "delivered_at": null, "read_at": null, // 发出消息的回执
  "text": "纯文本投影",
  "html": "<b>粗体</b> 与 <a href=…>链接</a>",   // 标准富文本
  "attachments": [{"guid": "A-1", "kind": "image", "path": "~/Library/Messages/Attachments/…",
                    "mime": "image/png", "size": 1234, "transfer_status": "finished",
                    "sticker": false}],
  "reply_to": {"guid": "…", "part": "0:0:21"},   // 内联回复
  "tapback": {"kind": "like", "emoji": null, "raw_type": 2001,
               "target": "<被回应消息 guid>", "target_part": 0, "from": "+1415…"},
  "mention_unseen": false,
  "expressive_style": null,              // 气泡/全屏效果 id 原样透传
  "audio": {"message": true, "played": true, "played_at": 1760340001000},
  "edited_at": null, "retracted_at": null,
  "part_count": 1, "item_type": 0, "is_empty": false
}
// 仅 tapback/sticker 行带 tapback；仅回复行带 reply_to；编辑过的行带 summary_info_keys。
```

事件流（`events` 工具 / `madrid events`）里同一信封出现在 `payload` 字段，
`kind` 为 `real.received` / `real.tapback` / `real.retracted` / `real.updated`。

---

## 4. 标准富文本速查

| 特性 | HTML（canonical） | Markdown |
|---|---|---|
| 粗体 | `<b>x</b>` | `**x**` |
| 斜体 | `<i>x</i>` | `*x*` |
| 粗斜体 | `<b><i>x</i></b>` | `***x***` |
| 下划线 | `<u>x</u>` | `__x__` |
| 删除线 | `<s>x</s>` | `~~x~~` |
| 链接 | `<a href="https://…">x</a>` | `[x](https://…)` |
| 换行 | `<br>` | `\n` |
| 提及 | `<span data-mention="+1555…">@x</span>` | 用 `mentions` 参数（字符区间） |
| 文字效果 | `<span data-effect="slam">x</span>` | 用 `effect` 参数（整条） |
| 内联附件 | `<img data-attachment="guid" data-kind="image">` | 用 `attachments` 参数 |

属性键 = Apple 键名（`__kIMTextBoldAttributeName` 等）；
`rich_convert` 工具 / `madrid rich` 命令可任意互转并查看 runs。

效果全表：`doctor` 的 `text_effects`；轻点回应全表：`tapbacks`。

---

## 5. 能力与边界（速查）

- 真发：文本/附件/音频文件 ✅；富文本与效果**降级**（纯文本投影 + `dropped` 上报）；
  提及/轻点回应/回复真发不可（sdef 只有 `send <text|file>`）。
- 真收：除 typing indicator 外全部可解（含编辑/撤回/回执）。
- typing indicator：瞬态不落库 → sim 专属。
- 延迟：轮询间隔 `--interval`（默认 2s）；macOS 无第三方推送通道。

更多边界与取证：README"真实边界"、`docs/格式参考` §10。


---

## 6. Shortcuts 发送通道（headless 真发）

比 AppleScript 更干净的第三条真发路：`shortcuts run`——无窗口焦点、无
System Events，发送在 Apple 的 entitled 扩展进程里完成。

一次性搭建：

1. 打开 Shortcuts.app → 新建快捷指令，命名 **`madrid send`**。
2. 添加动作 **"Send Message"（发送消息）**：
   - 消息内容 = **Shortcut Input**（快捷指令输入）
   - 接收人 = 固定一个；或输入格式约定为 `第一行=接收人\n其余=正文`，
     在快捷指令里用 "Split Text" 拆出接收人。
3. 首次运行会请求自动化权限，点允许。

使用（双重门控同 AppleScript 路径）：

```bash
export MADRID_ALLOW_REAL_SEND=1
python3 -m madrid --real send-shortcut "报告正文…" --shortcut "madrid send"
```

限制：无富文本格式（富文本走粘贴板通道）；附件需在快捷指令里额外接
"Receive files from input"。


---

## 8. HTTP + SSE 接入（非 MCP 通道，含 TS SDK）

适合把 madrid 当作**IM 通道插件**嵌进任意框架（Hermes 等）：

```bash
python3 -m madrid serve --port 8787 --watch   # --watch 同时把真实收件箱汇入事件流
```

- REST：`/health` `/capabilities` `/chats` `POST /chats` `/chats/{id}/transcript`
  `POST /send`（`format: html|markdown|plain`）`POST /edit`（打字机帧）
  `POST /retract` `POST /tapback` `/real/chats` `/real/transcript`
- 实时：`GET /events`（SSE，`?since=<seq>` 断线续传）
- 自描述：`GET /openapi.json`（OpenAPI 3.1，可直接喂 openapi-generator
  生成任意语言 SDK）

TypeScript（零依赖客户端在 `sdk/typescript/madrid.ts`）：

```ts
import { Madrid } from "./madrid";
const im = new Madrid("http://127.0.0.1:8787");

// 收：新消息/编辑帧/轻点回应实时推送
im.events((e) => {
  if (e.kind === "real.received") console.log("<-", e.payload.text, e.payload.html);
});

// 发：富文本 + 流式打字机
const chat = await im.openChat(["+15551234567"]);
const m = await im.send(chat.id, "…", { format: "html" });
await im.edit(m.guid, "… 更长一点");      // 每次编辑 = 一帧，接收端气泡实时刷新
```

真发门控不变：`serve --real` 需同时设置 `MADRID_ALLOW_REAL_SEND=1`。


---

## 9. Markdown 文档模式（混合渲染）

Agent 直接发整篇 Markdown，桥自动映射到 iMessage 可渲染的形态：

| Markdown 块 | iMessage 形态 |
|---|---|
| 段落/行内格式（`**b** *i* ~~s~~ __u__ [l](url)`） | 真实富文本属性 |
| 标题 | 粗体文本 |
| 列表 / 引用 | `• ` / `❝ ` 前缀文本 |
| 分割线 | `─` 文本行 |
| **表格 / 代码块** | **AppKit 渲染 PNG 卡片，内嵌在对应位置**（图文混排） |

用法不变：`POST /send {"format":"markdown"}` 或
`python3 -m madrid send --format markdown`。真发（粘贴板通道）会把文本与
卡片合成一份富 HTML 一次粘贴。注意：**不要期望 iMessage 解析 Markdown**
——原生只认上面的映射；表格/代码以图片形式存在，不可被文本搜索。


---

## 10. 已读回执（双向）与其他注意事项

**看对端已读**（真实）：`watch` 现在对已发出的消息做 `date_read` /
`date_delivered` 快照 diff——对端"已读/已送达"会以 `receipt` 事件实时推送
（`change: ["read"]`、`read_at` 时间戳），HTTP/SSE 一样收。

**让我们已读**（真实，peer 可见）：`mark-read --handle +1555…` /
`POST /mark-read` / MCP `real_mark_read`——通过呈现会话触发 Messages 的
已读标记（门控）。**对端能否看到取决于用户设置里"发送已读回执"是否开启**
（这是系统行为，不是本桥的开关）。

**其他注意**（按踩坑概率排序）：

1. 打字机（编辑流）真发已就绪，**桥优先、UI 兜底**：`typewriter` 命令。
   首帧走粘贴板富文本通道；后续帧优先走**常驻桥**（原生速，见 §12），
   桥未就绪/编辑 op 未接线时自动回退 UI 编辑"最新气泡"。编辑窗 15 分钟；
   间隔建议 ≥0.8s；UI 兜底路径首次使用先 `--verify-only` 确认本地化菜单名。
2. Agent 流式输出期间**对端看不到"正在输入…"**——typing indicator 无法由
   第三方产生（瞬态、不落库、无通道）。
3. 编辑窗 15 分钟：长生成需要在窗口内完成或分片。
4. webhook sink 会把消息内容 POST 出去——只指向本地地址。
5. `serve --real` + `MADRID_ALLOW_REAL_SEND=1` 组合即对外可真发：端口只绑
   127.0.0.1（默认），勿暴露公网。
6. macOS 权限三件套：完全磁盘访问（读 chat.db）、自动化（Messages）、
   （粘贴板通道需要）System Events。


---

## 11. 多 Agent / 多身份

**一个 madrid 实例 = 一个 macOS 用户会话 = 一个 iMessage 账号**。群聊里
多个 Agent 的可选形态：

| 形态 | 说明 |
|---|---|
| 单账号 + 多会话 | 一个 Agent 账号在群里，多个 Agent 用不同群/私聊区分（最省事） |
| 多 macOS 用户账号 | 每个用户账号登不同 Apple ID，各自跑一个 madrid（快速切换/多开；推荐 2-3 个 Agent 时用） |
| 多台 Mac / 虚机 | 完全隔离；虚机需真实序列号配置，属灰色地带 |
| 虚拟号码 | **不可行**：iMessage 激活拦截 VoIP 号码，且新身份注册有设备级校验；第三方虚拟号（"Photon"类）无法注册独立 iMessage 身份。虚拟号只在"挂到已有 Apple ID 的可达地址"上有意义——那仍是同一个群成员 |

同一 Apple ID 的多个"可达地址"（邮箱/号码）不会变成群里的不同成员，
只是同一身份的多个入口。


---

## 12. 常驻桥（打字机首选路径）

调试器只当一次"安装器"：单次附加在 Messages 内装 Unix-socket 命令调度器
（`/tmp/madrid-bridge.sock`），随后 detach；此后每条命令是本机 socket 往返
（亚毫秒），编辑帧流 10+ fps 无压力。

前置（一次性，需恢复模式）：

```bash
csrutil enable --without debug     # 恢复模式
sudo DevToolsSecurity -enable      # 本机
python3 -m madrid bridge status    # ready_to_install: true
MADRID_ALLOW_REAL_SEND=1 python3 -m madrid bridge install
python3 -m madrid bridge ping      # {"pong": true}
```

桥的命令表从 `ping` 起步；`edit` 等 op 在活进程里确认签名后接线（接线
完成前 typewriter 自动走 UI 兜底）。Messages 退出/升级后重装一次即可。


---

## 13. Hermes gateway 平台适配器（iMessage 通道插件）

`integrations/hermes/` 是 [Hermes Agent](https://nousresearch.com) gateway
的 iMessage 平台插件——把 madrid 变成 Hermes 的原生消息通道（与
Telegram/Discord/Signal 同级），**纯接口（HTTP+SSE），零 UI 依赖**：

```
iMessage 对端 ⇄ Messages.app/chat.db ⇄ madrid serve ──SSE──▶ Hermes adapter ──▶ Agent
                                                ◀──HTTP /send──┘
```

安装（已复制到 `~/.hermes/hermes-agent/plugins/platforms/imessage/`）：

```bash
# 1. madrid 侧：serve + 真实收件箱
python3 -m madrid serve --port 8787 --watch

# 2. Hermes 侧：注册平台
hermes gateway setup           # 选 "iMessage (madrid)"
# 或直接设 env:
export MADRID_URL=http://127.0.0.1:8787
hermes gateway start
```

能力：markdown 文档模式出站（表格/代码→内嵌 PNG 卡片）、打字机流式
（`MADRID_TYPEWRITER=true`）、allowlist（`MADRID_ALLOWED_USERS`）、cron
投递（`MADRID_HOME_CHANNEL`）、每会话独立 Hermes session。适配器接口：
`send`（markdown→富文本）、`send_draft`（typewriter 帧）、SSE 入站带
echo 预防与授权检查。
