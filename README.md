# Hermes Feishu Command Palette (feishu-command-palette)

> Interactive single-card in-place control panel for Hermes Agent in Feishu/Lark messenger. Execute Hermes CLI commands, switch models, adjust configurations directly in-card with real-time Doctor-style formatting and PTY streaming.

---

## 🔒 Disclosure & Security Model

> **Official Catalog Disclosure:**
> Answers the Feishu `/card` text with an interactive control card sent through the external `lark-cli` binary. Button clicks run a fixed table of local `hermes ...` CLI subcommands and post their formatted output (logs, sessions, redacted config/auth status) into the chat. Can update model/provider and `agent.*` settings (including `agent.yolo`) via official `hermes config set` CLI. Reads `auth.json` strictly for provider names. All temporary process outputs are sandboxed in `~/.hermes/plugin-data/feishu-command-palette/` (mode 0700).

---

## ✨ Features (特性)

- **🎛️ In-Place Lifecycle (单卡片全生命周期)**: All navigations, command runs, and setting toggles happen in-place without message flooding.
- **📂 Native Collapsible Panels (原生可折叠面板)**: Multi-section diagnostic reports (`/doctor`, `/status`) and long outputs automatically render as native interactive collapsible drawers (`collapsible_panel`), preventing chat screen flooding.
- **⚡ PTY Live Streaming (终端流式展示)**: Captures live process output using virtual pseudo-terminals with rate-limited patch updates (0.8s) and active heartbeats.
- **⏱️ Adaptive Timeout Tiers (自适应超时分级)**: Tiered timeouts (e.g., 120s for `/doctor`, 60s for `/security`, 35s default) to avoid premature termination during deep diagnostics.
- **🛡️ Strict Fail-Closed RBAC (权限隔离门禁)**: Write operations (`confirm_switch`, `set_cfg`, `/yolo`, `/stop`) strictly require configured `FEISHU_ADMINS`. Unprivileged users can only perform read-only queries.
- **🔒 Safe Atomic Writes (安全原子写入)**: Uses official `hermes config set` commands; never wipes YAML comments with raw safe_dump.
- **📦 Sandboxed Storage (专属安全沙箱)**: Outputs stored under `~/.hermes/plugin-data/feishu-command-palette/` (0700) with 7-day TTL auto-cleanup (No `/tmp` usage).
- **🌐 Bilingual UI (中英双语 / English-First)**: English-first labels with concise Chinese subtitles for seamless global & domestic usability.

---

## 📦 Requirements & Installation (前置要求与安装)

### Prerequisites

| Dependency | Minimum Version | Note |
|---|---|---|
| **Python** | ≥ 3.10 | Required for modern union types |
| **Hermes Agent** | ≥ 0.21.0 | Required for plugin hook runtime |
| **lark-cli** | ≥ 1.0.0 | **Required**: `npm install -g @larksuiteoapi/lark-cli` |
| **PyYAML** | ≥ 6.0 | Config parser |

> ⚠️ **Important External Dependency**: This plugin uses the operator-installed `lark-cli` binary for card sending and in-place card patching (`lark-cli im messages patch`). Ensure `lark-cli` is installed and logged in on the host machine.

### Installation

```bash
# Option 1: Via Hermes Plugin Catalog (Recommended)
hermes plugins install feishu-command-palette

# Option 2: Manual Clone
git clone https://github.com/billzai/hermes-feishu-panel.git ~/.hermes/plugins/feishu-command-palette

# Restart Gateway
systemctl --user restart hermes-gateway
```

---

## 🚀 Usage (使用说明)

Send `/card` in your Feishu/Lark chat:

```text
┌───────────────────────────────────────────────┐
│  ⌨️ Hermes Control Panel (控制面板)            │
│  🟢 **gemini-3.8-flash-high** · max           │
├───────────────────────┬───────────────────────┤
│ 💬 Session (会话管理)  │ ⚙️ Config (系统配置)   │
├───────────────────────┼───────────────────────┤
│ 🔧 Tools (工具研发)    │ ℹ️ Info (系统信息)     │
├───────────────────────┴───────────────────────┤
│  ⚡ **Quick Actions (快捷操作)**               │
│  🔄 **Switch Model** (模型切换)          [▶]  │
│  📊 **System Status** (系统状态)         [▶]  │
│  🆕 **New Session** (新建会话)           [▶]  │
│  ⏹ **Force Stop** (强制停止)             [▶]  │
└───────────────────────────────────────────────┘
```

---

## 📋 Command Matrix (支持命令矩阵)

| Category | Commands |
|---|---|
| **💬 Session** | `/new` `/stop` `/status` `/context` `/usage` `/sessions` `/undo` `/retry` `/compress` `/background` |
| **⚙️ Config** | `/model` `/reasoning` `/personality` `/verbose` `/yolo` `/fast` `/codex-runtime` |
| **🔧 Tools** | `/diff` `/doctor` `/security` `/debug` `/logs` `/cron` `/plugins` `/skills` `/bundles` `/memory` |
| **ℹ️ Info** | `/version` `/profile` `/config` `/whoami` |

---

## 📄 License

[MIT License](LICENSE) · Maintained by [@billzai](https://github.com/billzai)
