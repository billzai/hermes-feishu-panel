# Hermes Feishu Panel（飞书控制面板）

> Hermes Agent 的飞书交互式控制面板插件。在飞书会话中发送 `/card` 即可获得一个可交互的控制面板，直接在卡片内执行 Hermes CLI 命令、切换模型、调整配置，无需离开聊天窗口。

## ✨ 功能特性

- **🎛️ 单卡片全生命周期**：所有操作在一张卡片内原地完成，不刷屏
- **📋 31+ 真实 CLI 命令**：状态查询、健康诊断、日志查看、定时任务等
- **🔄 模型/Provider 切换**：可视化选择，原子写入 `config.yaml`，毫秒级生效
- **⚙️ 配置参数调整**：推理力度、AI 人格、日志级别、极速模式等
- **📊 结果美化**：Doctor 风格分区渲染，✓/⚠/✗ 状态标识
- **📋 复制内容**：查询结果支持展开完整纯文本，长按/选中复制
- **🔒 权限控制**：仅授权用户可操作，危险命令二次确认
- **🧹 自动清理**：`/tmp` 归档文件 7 天 TTL 自动清理

## 📦 安装

### 前置要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| **Python** | ≥ 3.10 | 推荐 3.11+ |
| **Hermes Agent** | ≥ 0.21.0 | 必须已安装并配置 |
| **lark-oapi** | ≥ 1.6.0 | 飞书开放平台 SDK |
| **lark-cli** | ≥ 1.0.0 | 飞书 CLI 工具 |
| **PyYAML** | ≥ 6.0 | 配置文件解析 |

### 极简安装（二选一）

**方式一：使用 Hermes 插件管理器一键安装（推荐）**

```bash
hermes plugins install billzai/hermes-feishu-panel
```

**方式二：手动克隆安装**

```bash
# 克隆到本地插件目录
git clone https://github.com/billzai/hermes-feishu-panel.git ~/.hermes/plugins/feishu-command-palette

# 重启 Hermes Gateway 生效
systemctl --user restart hermes-gateway
```

### 飞书机器人配置

1. 在 [飞书开放平台](https://open.feishu.cn/app) 创建自建应用
2. 开启「机器人」能力
3. 配置事件订阅：
   - 请求地址：`https://your-domain.com/webhook/feishu`（或使用长连接模式）
   - 订阅事件：`im.message.receive_v1`、`card.action.trigger`
4. 发布应用并获取 `App ID` 和 `App Secret`
5. 在 `~/.hermes/config.yaml` 中配置飞书平台参数

## 🚀 使用方法

### 打开控制面板

在飞书会话中发送：

```
/card
```

### 界面说明

```
┌─────────────────────────────┐
│  ⌨️ Hermes 控制面板          │
│  🟢 **gemini-3.8-flash-high** · max  │  ← 当前模型 + 推理力度
├──────────────┬──────────────┤
│ 💬 会话管理   │ ⚙️ 系统配置   │  ← 分类入口
├──────────────┼──────────────┤
│ 🔧 工具研发   │ ℹ️ 系统信息   │
├─────────────────────────────┤
│  ⚡ 快捷操作                  │
│  🔄 **模型切换** model    [▶] │
│  📊 **系统状态** status   [▶] │
│  🆕 **新建会话** new      [▶] │
│  ⏹ **强制停止** stop      [▶] │
└─────────────────────────────┘
```

### 命令分类

| 分类 | 命令 | 说明 |
|------|------|------|
| **💬 会话管理** | `/new` `/stop` `/status` `/context` `/usage` `/sessions` `/undo` `/retry` `/compress` `/background` | 会话生命周期管理 |
| **⚙️ 系统配置** | `/model` `/reasoning` `/personality` `/verbose` `/yolo` `/fast` `/codex-runtime` | 模型与参数配置 |
| **🔧 工具研发** | `/diff` `/doctor` `/security` `/debug` `/logs` `/cron` `/plugins` `/skills` `/bundles` `/memory` | 诊断与工具 |
| **ℹ️ 系统信息** | `/version` `/profile` `/config` `/whoami` | 系统信息查询 |

### 结果页面

```
┌─────────────────────────────┐
│  ✅ 系统状态 (/status)       │
│  执行成功 | ⏱️ 耗时 0.8s     │
├─────────────────────────────┤
│  🟢 执行成功 | 📊 3531字符   │
│                             │
│  📌 Environment             │
│  • **Project**: /path/to/hermes │
│  • **Python**: 3.11         │
│  • **Model**: gemini-...    │
│                             │
│  📌 API Keys                │
│  • OpenAI        ✓ [valid]  │
│  • Google/Gemini ✓ [valid]  │
│  ...                        │
│                             │
│  📄 **完整归档路径**         │
│  `/tmp/hermes_status_...txt`│
├─────────────────────────────┤
│ [📋 复制内容] [🔁 再次执行]  │
│ [⬅ 返回]      [🏠 首页]      │
└─────────────────────────────┘
```

## 🔧 技术架构

```
飞书用户点击按钮
       ↓
飞书开放平台 (card.action.trigger)
       ↓
Hermes Gateway (WebSocket / Webhook)
       ↓
command-palette 插件
       ↓
┌──────────────────┐
│  命令路由分发      │  ← handle_card_action_sync()
│  (cmd:/nav:/copy) │
└────────┬─────────┘
         ↓
┌──────────────────┐
│  本地 CLI 执行     │  ← run_subprocess()
│  (hermes status)  │
└────────┬─────────┘
         ↓
┌──────────────────┐
│  输出美化引擎      │  ← parse_and_beautify_output()
│  (Doctor 风格)    │
└────────┬─────────┘
         ↓
┌──────────────────┐
│  卡片原地重绘      │  ← _patch_card()
│  (lark-cli patch) │
└──────────────────┘
```

### 核心设计

| 设计 | 说明 |
|------|------|
| **同步回调** | 卡片按钮点击直接同步执行，不经过 LLM 对话链路 |
| **原地重绘** | 使用 `lark-cli im messages patch` 更新原卡片，不发新消息 |
| **原子配置写入** | 模型切换使用临时文件 + `os.replace()`，避免配置损坏 |
| **权限校验** | 每个操作前检查 `_is_interactive_operator_authorized()` |
| **危险确认** | `/new` `/stop` 等危险命令需二次确认（120 秒令牌） |
| **命令冷却** | 同一命令 3 秒冷却，防止快速连点 |
| **超时保护** | CLI 命令 50 秒超时，超时后显示橙色警告卡片 |

## 📁 项目结构

```
hermes-feishu-panel/
├── __init__.py               # 核心插件代码（遵循 Hermes 插件规范）
├── plugin.yaml               # 官方插件清单（通过 hermes plugins validate）
├── LICENSE                   # MIT 许可证
├── .gitignore
└── README.md                 # 完整说明文档
```

## ⚠️ 使用条件与限制

### 必须满足的条件

1. **Hermes Agent ≥ 0.21.0**：插件依赖 Hermes 的插件加载器和 `plugins.platforms.feishu.adapter.FeishuAdapter`
2. **飞书自建应用**：需要 `im.message.receive_v1` 和 `card.action.trigger` 事件订阅
3. **lark-cli ≥ 1.0.0**：用于发送和更新卡片消息
4. **Python ≥ 3.10**：使用了 `str | None` 类型注解语法

### 已知限制

| 限制 | 说明 |
|------|------|
| **飞书卡片大小** | 单张卡片 JSON ≤ 30KB，超长输出自动截断并归档到 `/tmp` |
| **按钮文字居中** | 飞书经典卡片按钮内文字强制居中，无法左对齐 |
| **行间距** | 经典卡片无 padding/margin 控制属性，间距由客户端固定 |
| **模型下拉上限** | 飞书 `select_static` 最多 100 个选项，超过 50 个模型时自动智能分段拆分为两个下拉框（A-M / N-Z） |
| **命令超时** | CLI 命令 50 秒超时，超时后显示警告卡片 |

### 版本兼容性

| 组件 | 测试版本 | 最低要求 | 说明 |
|------|---------|---------|------|
| Python | 3.11.15 | 3.10 | `str | None` 语法需要 3.10+ |
| Hermes Agent | 0.21.4 | 0.21.0 | 插件加载器 API |
| lark-oapi | 1.6.8 | 1.6.0 | 飞书 SDK 回调模型 |
| lark-cli | 1.0.96 | 1.0.0 | `im messages patch` 命令 |
| PyYAML | 6.0.3 | 6.0 | `yaml.safe_load` / `yaml.safe_dump` |

## 🐛 常见问题

### Q: 点击按钮没有反应？

**A:** 检查以下几点：
1. 网关是否运行：`systemctl --user status hermes-gateway`
2. 飞书 WebSocket 是否连接：`grep "Connected in websocket mode" ~/.hermes/logs/gateway.log`
3. 插件是否加载：`grep "command-palette" ~/.hermes/logs/gateway.log`
4. 用户是否有权限操作（`_is_interactive_operator_authorized`）

### Q: 卡片显示「命令正在冷却中」？

**A:** 同一命令 3 秒内只能执行一次，等待 3 秒后重试。

### Q: 模型切换后没有生效？

**A:** 检查 `~/.hermes/config.yaml` 是否已更新：
```bash
grep -A2 "model:" ~/.hermes/config.yaml
```
如果配置已更新但对话未切换，可能需要重启会话。

### Q: `/tmp` 目录下 `hermes_*.txt` 文件太多？

**A:** 插件会自动清理 7 天前的归档文件。如需手动清理：
```bash
find /tmp -name "hermes_*.txt" -mtime +7 -delete
```

### Q: 卡片显示「暂无数据」？

**A:** 某些命令（如 `/usage`）需要 Provider 支持 usage 端点。如果当前 Provider 不支持，会显示灰色「暂无数据」卡片而非错误。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request。

## 📄 许可证

[MIT License](LICENSE)

---

**维护者**: [@billzai](https://github.com/billzai)
