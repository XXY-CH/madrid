# madrid

[English](README.md) | **中文**

让 Agent 收发 iMessage。富文本进，富文本出。

madrid 读取本地 Messages 数据库（只读，不写入），通过 Messages.app 发送。你的 Agent 拿到的是一个干净的 HTTP API、MCP server、或 Hermes 网关插件——哪个顺手用哪个。

```bash
pip install -e .
python3 -m madrid serve --port 8787 --watch
```

就这样。你的 Agent 现在可以：

- 实时收 iMessage（kqueue 监听数据库，亚秒级）
- 带格式回复——粗体、斜体、链接、表格、代码块
- 逐字流式输出，像真人在打字
- 看到对方什么时候已读
- 轻点回应、编辑消息、撤回

## 为什么不用 [openclaw/imsg](https://github.com/openclaw/imsg)？

imsg 是一个不错的 Swift CLI。我们写 madrid 是因为有些事 imsg 做不了：

| | imsg | madrid |
|---|---|---|
| 收：富文本解码 | 只恢复纯文本 | 完整解码（粗/斜/链接/提及/效果） |
| 发：富文本 | 要关 SIP + 注入 dylib | 粘贴板通道，开箱即用 |
| Markdown → iMessage | 不支持 | 表格和代码块变内嵌图片 |
| 打字机流式 | 不支持 | 编辑协议实现 |
| HTTP API | 没有 | REST + SSE + OpenAPI |
| MCP server | 没有 | 有 |
| Hermes 集成 | 没有 | 原生网关插件 |
| 编辑/撤回事件 | 没有 | 逐帧 |
| 已读回执 | 没有 | 实时事件 |

如果你需要原生编译二进制、Linux 只读、或者更成熟的 CLI，用 imsg。两个工具都对自己的能力边界诚实。

### 先说清楚：`csrutil enable --without debug`

部分功能（打字机的高速桥、高级发送）需要你在恢复模式跑一次这个命令。这是 Apple 的正式开发者豁免，允许调试器附加到系统进程。

**madrid 的其他所有功能不需要它**——富文本、HTTP API、MCP、Hermes、UI 兜底的打字机，不开这个照样用。

保持开启的：文件系统保护、NVRAM、运行时完整性、内核扩展签名。被豁免的：对 Apple 二进制的 `task_for_pid()`。

风险：机器上已有的恶意软件可以读进程内存——但如果恶意软件已经跑起来了，不开这个它也能键盘记录。只在信任的机器上用。恢复模式 `csrutil enable` 随时恢复。

---

## 效果

**Agent 发 Markdown：**
````markdown
## 部署完成

| 服务   | 状态 |
|--------|------|
| API    | ✅   |
| Worker | ✅   |

```python
print("hello")
```
````

**iMessage 里看到：**
- "部署完成" 显示为粗体
- 表格变成一张干净的卡片图片
- 代码块变成带语法高亮的卡片图片

**对方回复，Agent 收到：**
```json
{
  "type": "message",
  "text": "不错，上线吧",
  "sender": "+14155551212",
  "chat": {"name": "Alex", "handles": ["+14155551212"]}
}
```

## 快速开始

```bash
# 先给终端开完全磁盘访问（系统设置 → 隐私与安全）
pip install -e .

# 检查环境
python3 -m madrid doctor

# 启动 HTTP+SSE 服务（Agent 连这个）
python3 -m madrid serve --port 8787 --watch

# 或者直接用 CLI
python3 -m madrid real-chats
python3 -m madrid stats
python3 -m madrid send --to "+14155551212" --text "hello" --real
```

## Hermes 接入

适配器已经装在 `~/.hermes/hermes-agent/plugins/platforms/imessage/`。

```bash
export MADRID_URL=http://127.0.0.1:8787
hermes gateway setup    # 选 "iMessage (madrid)"
hermes gateway start
```

现在任何人给你的 Apple ID 发 iMessage 都会由 Agent 回复。可以设
`MADRID_ALLOWED_USERS=+14155551212` 限制谁能跟 bot 说话，
`MADRID_TYPEWRITER=true` 开启流式输出。

## 发送通道

| 方式 | 能发什么 | 备注 |
|---|---|---|
| AppleScript | 文本 + 文件 | 到处能用 |
| 粘贴板 | 富文本（粗/链接/字体） | 需要自动化权限 |
| Shortcuts | headless 文本 + 附件 | 在快捷指令里建一次 |
| 调试器桥 | 全部 | 需要 `csrutil --without debug`（见下） |

## 打字机

```bash
python3 -m madrid typewriter --to "+14155551212" \
    --frames "Generating;;Generating answer;;Generating answer: 42"
```

第一帧正常发送，后续每帧编辑同一条消息——对方看到文字逐渐变长。
桥可用走桥（原生速度），不可用走 UI 编辑。15 分钟窗口，大约每秒一帧。

## 隐私

- `chat.db` 只读，madrid 永远不写。
- 真发需要 `MADRID_ALLOW_REAL_SEND=1` 加显式 `--real`。
- `serve` 只绑 `127.0.0.1`。

## 目录

```
madrid/           Python 包（零依赖）
  serve.py        HTTP + SSE 服务
  engine.py       消息生命周期与路由
  rich.py         HTML ⇄ Markdown ⇄ Apple 属性键
  mddoc.py        Markdown → 富文本 + PNG 卡片
  realbox.py      chat.db 读取（只读）
  watch.py        事件守护（kqueue + 轮询兜底）
  stats.py        消息/聊天/联系人统计
  contacts.py     handle → 联系人姓名
  fastwatch.py    kqueue 文件变化检测
  bridge.py       调试器桥（可选，需 SIP 豁免）
  native/         ObjC 辅助工具（首次使用时 clang 编译）
integrations/hermes/  Hermes 网关插件
sdk/typescript/       TypeScript 客户端
```

## License

未定。
