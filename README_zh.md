# madrid — iMessage 富文本收发桥（Agent 接入）

**[English](README.md)** | 中文

> `madrid` 是 Apple 内部对 iMessage 服务的代号（`com.apple.madrid`）。
> 原名 `imsg`，2026-10 更名并大幅扩展。

让 Agent（Hermes / Qwen Code / 任何 MCP 客户端）用你的 Apple ID 收发 iMessage：

- **发**：标准富文本（HTML 子集 / Markdown / plain）+ 附件；Markdown
  文档模式自动混合渲染（表格/代码块 → 内嵌 PNG 卡片）
- **收**：只读轮询 `chat.db`，解码消息全量——富文本 runs、附件、轻点回应、
  贴纸、回复、音频、编辑（逐帧）、撤回、已读回执、系统事件、balloon 载荷
- **通知**：事件流（SSE）/ macOS 通知 / webhook / JSONL / 命令钩子
- **流式打字机**：编辑协议实现（15 分钟窗口），桥优先/UI 兜底，HTTP 接口就绪
- **Slash 命令**：Hermes gateway 集成后原生支持（`/help` `/status` 等由
  gateway 统一处理，适配器自动继承）
- **Hermes gateway 原生通道**：iMessage 成为 Hermes Agent 的一等消息平台
  （与 Telegram/Discord 同级）
- **零第三方依赖**（纯 stdlib），macOS + Xcode CLT 即可运行

```bash
make test          # 项数见 CHANGELOG
make doctor        # 环境与能力矩阵
python3 -m madrid --help
```

## madrid vs [openclaw/imsg](https://github.com/openclaw/imsg)

[imsg](https://github.com/openclaw/imsg)（1.4k⭐，Swift，MIT）是成熟的 iMessage 自动化 CLI。
madrid 走了不同的路——**富文本优先 + Agent 原生接口**。诚实对比：

| 能力 | imsg | madrid |
|---|---|---|
| **语言** | Swift 6（编译，快） | Python（纯 stdlib，零依赖） |
| **平台** | macOS 14+，Linux 只读 | 仅 macOS |
| **收：文本** | ✅ text 列 + attributedBody 恢复 | ✅ 同 |
| **收：富文本解码** | 仅纯文本恢复 | ✅ 完整 Apple 属性键（粗/斜/删/下划/链接/提及/效果） |
| **收：轻点回应** | ✅ | ✅ |
| **收：编辑/撤回** | — | ✅ 逐帧 + 撤回事件 |
| **收：已读回执** | — | ✅ 实时 receipt 事件 |
| **收：贴纸/回复/音频** | 贴纸 ✅ | ✅ 全部 + 播放状态 |
| **联系人解析** | ✅ AddressBook + chat.db | ✅ AddressBook + chat.db（缓存） |
| **收：投票** | ✅ 原生解码 | ✅ 问题/选项解码 |
| **发：文本+文件** | ✅ AppleScript（自动暂存） | ✅ AppleScript |
| **发：富文本** | 仅 Bridge（需关 SIP + dylib） | ✅ 粘贴板通道（粗/斜/链接/字体） |
| **发：附件** | ✅ | ✅ |
| **发：轻点回应** | ✅ AppleScript | ✅ Sim；真实走桥 |
| **Markdown → 富文本** | — | ✅ 表格/代码 → 内嵌 PNG 卡片 |
| **打字机流式** | — | ✅ 编辑协议（桥优先/UI 兜底） |
| **Watch 机制** | FSEvents + 轮询兜底 | kqueue 监听 WAL + 轮询兜底 |
| **CLI** | ✅ 丰富（补全/LLM 帮助） | ✅ JSON 输出 |
| **JSON-RPC stdio** | ✅ | — |
| **HTTP/SSE** | — | ✅ REST + OpenAPI 3.1 + SSE 事件流 |
| **MCP** | — | ✅ 30+ 工具 |
| **TypeScript SDK** | — | ✅ 零依赖 |
| **Hermes 适配器** | — | ✅ 原生平台（斜杠命令/流式/白名单） |
| **统计** | ✅ 消息/媒体计数 | ✅ 总览/每聊天/联系人/每日 |
| **创建聊天** | ✅ 新号码经 IDS 检查 | ✅ `create-chat`（imessage:// URL） |
| **高级功能（需 SIP）** | Dylib 注入（富发/已读/输入中/贴纸） | 调试器桥（打字机/全发） |
| **License** | MIT | 未定 |

**一句话**：要**原生 CLI + Linux + 统计 + 投票解码**，用 **imsg**。
要**富文本**（解码+发送）、**Agent 原生 API**（HTTP/MCP/SDK）、
**Markdown 文档模式**、**打字机流式**，用 **madrid**。

## 安装与快速开始

```bash
pip install -e .

export MADRID_DB=/tmp/madrid.db
python3 -m madrid doctor                       # 环境体检 + 24 项能力矩阵
python3 -m madrid open --to "+15551234567" --name "Test"
python3 -m madrid send --chat chat-+15551234567 \
    'hey <b>Alex</b> check <a href="https://docs">this</a>' --format html \
    --mention 4:4=+15551234567 --effect slam
python3 -m madrid edit <guid> "Generating answer: 42"      # 打字机帧
python3 -m madrid watch --notify --jsonl ~/.madrid/inbox.jsonl   # 常驻收通知
python3 -m madrid real-chats --limit 10        # 真实会话（只读）
python3 -m madrid serve --port 8787 --watch    # HTTP+SSE（Agent 通道插件形态）
```

## 标准富文本

| 特性 | HTML（canonical） | Markdown | 属性键 |
|---|---|---|---|
| 粗体/斜体/删除线 | `<b> <i> <s>` | `**x** *x* ~~x~~` | `__kIMText{Bold,Italic,Strikethrough}AttributeName` |
| 下划线 | `<u>` | `__x__` | `__kIMTextUnderlineAttributeName` |
| 链接 | `<a href>` | `[x](url)` | `__kIMLinkAttributeName` |
| 提及 | `<span data-mention>` | `mentions` 参数 | `__kIMMentionAttributeName` |
| 文字效果 | `<span data-effect>` | `effect` 参数 | `__kIMTextEffectAttributeName` |
| 内联附件 | `<img data-attachment>` | `attachments` 参数 | `__kIMFileTransferGUIDAttributeName` |
| 表格/代码块 | — | 自动 → PNG 卡片内嵌 | AppKit 渲染 |

## Hermes Agent 集成

```bash
# 1. madrid 侧
python3 -m madrid serve --port 8787 --watch

# 2. Hermes 侧（适配器已安装到 ~/.hermes/hermes-agent/plugins/platforms/imessage/）
export MADRID_URL=http://127.0.0.1:8787
hermes gateway setup       # 选 "💬 iMessage (madrid)"
hermes gateway start
```

iMessage 变成 Hermes 的原生消息通道：任何人给你的 Apple ID 发消息，
Agent 自动收到→调工具→回复（自动变富文本）。支持 slash 命令
（`/help` `/status` 等）、流式打字机（`MADRID_TYPEWRITER=true`）、
allowlist（`MADRID_ALLOWED_USERS`）、cron 投递（`MADRID_HOME_CHANNEL`）。

## 真实通道（发送）

| 通道 | 能力 | 状态 |
|---|---|---|
| AppleScript（`send`） | 文本 + 文件 | ✅ 双重门控 |
| 粘贴板 + 粘贴 | 富文本全格式（含字体/颜色） | ✅ 需自动化权限 |
| Shortcuts（`shortcuts run`） | headless 文本 + 附件 | ✅ 需一次性建快捷指令 |
| 常驻桥（debugger install-once） | 理论全功能（同 UI） | 🔧 工具就绪，见下方安全说明 |

收侧无特殊限制（`chat.db` 只读；typing indicator 除外——瞬态不落库）。

## 安全说明

### `csrutil enable --without debug`（常驻桥前提）

这是 macOS SIP 的**正式开发者豁免项**（Apple 提供给需要调试系统进程的
开发者）。它做了什么：

| 保持开启的保护 | 被豁免的 |
|---|---|
| 文件系统保护（/System 只读快照） | 允许 `task_for_pid()` 附加到 Apple 平台二进制 |
| NVRAM 保护 | 允许调试器读取/写入已附加进程的内存 |
| 运行时保护（运行时完整性） | — |
| 内核扩展签名要求 | — |
| DTrace 限制 | — |

**实际风险**：任何拥有你用户账户权限的进程都可以附加调试器到系统进程
（如 Messages.app、Finder）。这意味着恶意软件理论上可以读取这些进程的
内存。**但**恶意软件需要已经在你机器上运行才能利用这一点——SIP 全开时
它同样可以通过其他途径（键盘记录、屏幕截图）窃取信息。

**缓解措施**：
- 只在你信任的机器上开启
- 不在不信任的网络/公共场合使用
- 随时可逆：恢复模式 `csrutil enable` 恢复全开
- madrid 的常驻桥本身只装一个 Unix-socket 调度器（127.0.0.1 本地通信，
  无网络暴露），不修改任何系统文件

### 常规安全

- `chat.db` **一律只读**（`mode=ro`；绝不写 Apple 数据库）
- 真发**双重门控**：`MADRID_ALLOW_REAL_SEND=1` + 显式 real 模式
- `serve` 默认只绑 `127.0.0.1`——勿暴露公网
- webhook 只指向本地地址（消息内容会被 POST 出去）
- macOS 权限三件套：完全磁盘访问（chat.db）、自动化（Messages）、
  System Events（粘贴板通道）

## 项目结构

```
madrid/                    Python 包（纯 stdlib）
  cli.py  mcp_server.py    CLI（JSON 输出）与 MCP stdio server
  serve.py                 HTTP + SSE server（REST + OpenAPI 3.1 + SSE /events）
  engine.py                生命周期、send(format=)、edit/retract、typewriter、真发路由
  rich.py                  标准富文本层：HTML ⇄ Markdown ⇄ runs
  mddoc.py                 Markdown 文档模式：块级解析 → runs + PNG 卡片
  realbox.py               真实收件箱：chat.db 只读 → 标准信封（全特性解码）
  watch.py                 守护：轮询 → 事件流/通知/webhook/JSONL/命令
  capabilities.py          能力矩阵 + 环境探测
  store.py  model.py       本地 SQLite 与数据模型（线程安全）
  applescript.py           AppleScript/粘贴板/Shortcuts 通道（双重门控）
  uiedit.py                UI 编辑路径（打字机兜底）
  bridge.py                常驻桥（debugger install-once，原生速）
  real_format.py  richtext.py   attributedBody 读取与 sim 容器
  native/                  运行时原生小工具（clang 编译）
    decode_attributed_body.m  typedstream Foundation 解码器
    rich_pasteboard.m         富文本 → NSAttributedString → 剪贴板
    render_html_png.m         HTML 块 → PNG 卡片（AppKit）
integrations/hermes/       Hermes gateway 平台适配器（iMessage 通道插件）
sdk/typescript/            零依赖 TS 客户端（HTTP+SSE）
docs/agent-integration.md  接入指南（唯一随库文档）
tests/                     冒烟测试（无 pytest 依赖）
```

## 文档

| 文件 | 何时读 |
|---|---|
| [agent-integration.md](docs/agent-integration.md) | 接 Agent / 部署 / 信封 schema / Hermes 接入 / 已读回执 / 多 Agent / 安全 |

## License

未定（私有仓库）。
