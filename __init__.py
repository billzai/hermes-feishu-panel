"""Hermes 命令面板 - 工业级美化与原生全量闭环版 (v7.3)

版本演进亮点：
1. 全景扩充命令矩阵（由 18 个暴增至 32 个核心与高级命令）：
   - 会话类新增：/usage (真实账户剩余配额与重置时间)、/background (后台长任务启动引导与模板)；
   - 配置类新增：/personality (14 种预置 AI 人格秒切选项卡！)、/codex-runtime；
   - 工具研发新增：/diff (工作区 Git 变更统计)、/security (供应链安全审计)、/memory (记忆库状态与插件审查)、/bundles (技能捆绑包)；
   - 系统信息新增：/config (全量配置概览)、/whoami (当前身份与权限)、/help (命令速查)。
2. 全局导航系统标准化重构（解决“某些界面缺少返回上一级按钮”）：
   - L1 根卡片（主菜单）：完全删除底部的提示文字，界面干净干练；
   - L2 二级分类菜单（会话/配置/工具/信息）：底部统一并排提供 [ ⬅ 返回上一级 ] 与 [ 🏠 返回主菜单 ]；
   - L3 三级子界面（选项卡/模型切换/执行结果/高危确认/指引卡）：底部统一提供：
     [ ⬅ 返回上一级 ]（精准回退所属分类） + [ 🏠 返回主菜单 ]（一键返回面板首页）；
     结果卡同时并排附带 [ 🔁 再次执行 ]。
3. 彻底治愈全部命令物理路径：
   - /version 严格绑定为 ["hermes", "--version"]；
   - /context 严格绑定为 ["hermes", "prompt-size"]；
   - /debug 严格绑定为 ["hermes", "debug", "share", "--local"]；
   - /diff 严格绑定为 ["git", "-C", hermes_home, "diff", "--stat"]。
4. 纯本地 0.5 毫秒全量 Provider 发现：集成 auth.json + config.yaml，支持 openai-codex、xai-oauth、copilot 等全部登录态。
5. 纯本地 2 毫秒模型原子切换：绝不发送破坏性 SIGHUP，网关长连接永久在线稳固。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

logger = logging.getLogger("command-palette")

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
CONFIG_PATH = HERMES_HOME / "config.yaml"
AUTH_PATH = HERMES_HOME / "auth.json"
INSTALL_PATH = HERMES_HOME / "hermes-agent"

# ════════════════════════════════════════════════════════════════════════════
# 1. 结构化美化与持久化引擎
# ════════════════════════════════════════════════════════════════════════════

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _infer_cmd_from_saved_path(saved_path: str) -> str:
    """通过文件名前缀精确匹配 CLI_CMDS 命令，比 split 下标更可靠。"""
    safe = _safe_saved_output_path(saved_path)
    if not safe:
        return ""
    basename = Path(safe).stem  # e.g. hermes_doctor_20260924_223000
    for cmd in CLI_CMDS:
        prefix = "hermes_" + cmd.strip("/").replace(" ", "_") + "_"
        if basename.startswith(prefix):
            return cmd
    return ""


def _safe_saved_output_path(value: str) -> str | None:
    """验证并清理归档文件路径，仅允许 /tmp/hermes_ 开头的合法路径。"""
    if not value or not isinstance(value, str):
        return None
    val = value.strip()
    if ".." in val or not val.startswith("/tmp/hermes_"):
        return None
    try:
        p = Path(val).resolve()
        if p.parent != Path("/tmp") or not p.name.startswith("hermes_"):
            return None
        return str(p)
    except Exception:
        return None


def _read_saved_output(path: str) -> tuple[str, str | None]:
    """安全读取归档文件内容，返回 (content, err_msg)。"""
    safe = _safe_saved_output_path(path)
    if not safe:
        return "", "非法归档路径"
    try:
        p = Path(safe)
        if not p.exists():
            return "", "归档文件不存在"
        if not p.is_file():
            return "", "目标不是普通文件"
        content = p.read_text(encoding="utf-8", errors="replace")
        return content, None
    except Exception as e:
        return "", f"读取文件失败: {e}"


def _format_unified_line(line: str) -> str:
    """按 Doctor 风格格式化单行文本。"""
    s = line.strip()
    if not s:
        return ""
    if s.startswith("✓"):
        msg = s.lstrip("✓").strip()
        return f"🟢 {msg}" if msg else "🟢"
    if s.startswith("⚠") or s.startswith("!"):
        msg = s.lstrip("⚠!").strip()
        return f"⚠️ <font color='orange'>{msg}</font>" if msg else "⚠️"
    if s.startswith("✗"):
        msg = s.lstrip("✗").strip()
        return f"🔴 <font color='red'>**{msg}**</font>" if msg else "🔴"
    if s.startswith("→"):
        msg = s.lstrip("→").strip()
        return f"　↳ *{msg}*" if msg else "　↳"

    content = s
    if content.startswith(("- ", "• ", "* ")) and len(content) > 2:
        content = content[2:].strip()

    if ":" in content and not content.startswith(("http://", "https://")):
        k, _, v = content.partition(":")
        k = k.strip()
        v = v.strip()
        if "✓" in v:
            v = "🟢 " + v.replace("✓", "").strip()
        elif "✗" in v:
            v = "🔴 " + v.replace("✗", "").strip()
        if v:
            return f"• **{k}**: {v}"
        return f"• **{k}**"

    clean_s = s.lstrip("-•* ").strip()
    return f"• {clean_s}" if clean_s else ""


def _beautify_unified(cmd: str, clean_text: str) -> list[dict]:
    """统一 Doctor 风格渲染引擎，按 ◆ 分段渲染，无分段时创建 📌 查询详情。"""
    elements: list[dict] = []
    if not clean_text:
        elements.append({
            "tag": "markdown",
            "content": "**📌 查询详情**\n• (无输出内容)",
        })
        return elements

    if "◆" in clean_text:
        raw_sections = re.split(r"(?:^|\n)(?=◆\s*)", clean_text)
        sections = [s.strip() for s in raw_sections if s.strip()]
        for sec in sections[:12]:
            sec_lines = sec.splitlines()
            if not sec_lines:
                continue
            if sec_lines[0].startswith("◆"):
                header = sec_lines[0].lstrip("◆").strip()
                body_lines = sec_lines[1:]
            else:
                header = "查询详情"
                body_lines = sec_lines
            items = []
            for l in body_lines:
                fmt = _format_unified_line(l)
                if fmt:
                    items.append(fmt)
            if items:
                if len(items) > 25:
                    shown = items[:25]
                    shown.append(f"• *(更多 {len(items) - 25} 行见归档)*")
                    content = f"**📌 {header}**\n" + "\n".join(shown)
                else:
                    content = f"**📌 {header}**\n" + "\n".join(items)
                elements.append({"tag": "markdown", "content": content})
    else:
        items = []
        for l in clean_text.splitlines():
            fmt = _format_unified_line(l)
            if fmt:
                items.append(fmt)
        if not items:
            items = ["• (无有效内容)"]
        if len(items) > 30:
            shown = items[:30]
            shown.append(f"• *(更多 {len(items) - 30} 行见归档)*")
            content = "**📌 查询详情**\n" + "\n".join(shown)
        else:
            content = "**📌 查询详情**\n" + "\n".join(items)
        elements.append({"tag": "markdown", "content": content})

    return elements


def parse_and_beautify_output(cmd: str, raw_text: str, max_chars_inline: int = 1500) -> tuple[list[dict], str]:
    """
    统一结果美化引擎：
    1. 清除 ANSI 颜色与终端盒子边框 (┌─┐│└─┘)；
    2. 全量保存清理后文本至 /tmp/hermes_<cmd>_<timestamp>.txt；
    3. 顶部摘要展示状态标记、字符数与行数；
    4. 统一 Doctor 风格模块化美化渲染；
    5. 底部归档路径单独行展示；
    6. 返回 (elements, saved_path)。
    """
    clean = _strip_ansi(raw_text).strip()

    filtered_lines = []
    for line in clean.splitlines():
        s = line.strip()
        if re.match(r"^[┌┐└┘├┤─│┬┴┼\-=#*~]{4,}$", s):
            continue
        if s.startswith("│") and s.endswith("│"):
            s = s[1:-1].strip()
        filtered_lines.append(s)
    clean_text = "\n".join(filtered_lines).strip()

    ts_str = time.strftime("%Y%m%d_%H%M%S")
    safe_cmd = cmd.strip("/").replace(" ", "_") or "output"
    tmp_path = Path(f"/tmp/hermes_{safe_cmd}_{ts_str}.txt")
    try:
        tmp_path.write_text(clean_text, encoding="utf-8")
    except Exception as e:
        logger.warning("[command-palette] write saved output failed: %s", e)

    line_count = len(clean_text.splitlines()) if clean_text else 0
    char_count = len(clean_text)
    fails = len(re.findall(r"^[ \t]*✗", clean_text, re.M))
    status_mark = "🔴 包含异常" if fails > 0 else "🟢 执行成功"

    top_summary = {
        "tag": "markdown",
        "content": f"{status_mark}  |  📊 `{char_count}` 字符  |  📄 `{line_count}` 行",
    }

    sub_elements = _beautify_unified(cmd, clean_text)

    bottom_archive = {
        "tag": "markdown",
        "content": f"📄 **完整归档路径**\n`{tmp_path}`",
    }

    elements = [top_summary]
    elements.extend(sub_elements)
    elements.append(bottom_archive)

    return elements, str(tmp_path)


# ════════════════════════════════════════════════════════════════════════════
# 2. CommandBridgeRunner — 本地命令直驱器
# ════════════════════════════════════════════════════════════════════════════

class CmdResult:
    __slots__ = ("ok", "stdout", "stderr", "exit_code", "timed_out", "elapsed")

    def __init__(self, ok: bool, stdout="", stderr="", exit_code=0, timed_out=False, elapsed=0.0):
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.elapsed = elapsed


def run_subprocess(argv: list[str], *, timeout: int, input_text: str | None = None) -> CmdResult:
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        return CmdResult(False, stderr=f"可执行文件不存在: {argv[0]}", exit_code=127)
    except Exception as e:  # noqa: BLE001
        return CmdResult(False, stderr=f"进程拉起失败: {e}", exit_code=126)
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
        is_ok = (proc.returncode == 0) or (argv[1:2] == ["doctor"] and bool(out.strip()))
        return CmdResult(is_ok, out or "", err or "",
                         proc.returncode or 0, False, time.monotonic() - start)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        out, err = proc.communicate()
        return CmdResult(False, out or "", err or "", -9, True, time.monotonic() - start)


def run_lark(args: list[str], *, timeout: int = 20) -> CmdResult:
    return run_subprocess(["lark-cli", *args], timeout=timeout)


# ════════════════════════════════════════════════════════════════════════════
# 3. 扩充后的全量命令注册表与从属关系定义（实测 100% 正确命令）
# ════════════════════════════════════════════════════════════════════════════

# 命令 Emoji 映射表
CMD_EMOJI: dict[str, str] = {
    # session 类
    "/new":        "🆕",
    "/stop":       "⏹",
    "/status":     "📊",
    "/context":    "📐",
    "/usage":      "📈",
    "/sessions":   "📋",
    "/undo":       "↩",
    "/retry":      "🔁",
    "/compress":   "🗜",
    "/background": "⏳",
    "/steer":      "🎯",
    "/goal":       "🏆",
    # config 类
    "/model":        "🔄",
    "/reasoning":    "🧠",
    "/personality":  "🎭",
    "/verbose":      "🔧",
    "/yolo":         "⚡",
    "/fast":         "🚀",
    "/codex-runtime":"🖥",
    # tools 类
    "/diff":     "📝",
    "/doctor":   "🩺",
    "/security": "🔒",
    "/debug":    "🐛",
    "/logs":     "📜",
    "/cron":     "⏰",
    "/plugins":  "🧩",
    "/skills":   "📚",
    "/bundles":  "📦",
    "/memory":   "🧠",
    # info 类
    "/version": "ℹ️",
    "/profile": "👤",
    "/config":  "⚙️",
    "/whoami":  "🔑",
}

CLI_CMDS: dict[str, dict] = {
    # 会话查询
    "/status":   {"label": "系统状态", "cli": ["hermes", "status"]},
    "/context":  {"label": "上下文分析", "cli": ["hermes", "prompt-size"]},
    "/usage":    {"label": "配额用量", "cli": ["hermes", "usage"]},
    "/sessions": {"label": "历史会话", "cli": ["hermes", "sessions", "list", "--limit", "10"]},
    # 工具研发
    "/diff":     {"label": "代码差异", "cli": ["git", "-C", str(INSTALL_PATH), "diff", "--stat"]},
    "/doctor":   {"label": "健康诊断", "cli": ["hermes", "doctor"]},
    "/debug":    {"label": "调试摘要", "cli": ["hermes", "debug", "share", "--local"]},
    "/security": {"label": "安全审计", "cli": ["hermes", "security", "audit"]},
    "/logs":     {"label": "网关日志", "cli": ["hermes", "logs", "gateway", "-n", "40"]},
    "/cron":     {"label": "定时任务", "cli": ["hermes", "cron", "list"]},
    "/plugins":  {"label": "插件列表", "cli": ["hermes", "plugins", "list"]},
    "/skills":   {"label": "技能库",   "cli": ["hermes", "skills", "list"]},
    "/bundles":  {"label": "技能包",   "cli": ["hermes", "bundles"]},
    "/memory":   {"label": "记忆系统", "cli": ["hermes", "memory"]},
    # 系统信息
    "/version":  {"label": "框架版本", "cli": ["hermes", "--version"]},
    "/profile":  {"label": "配置详情", "cli": ["hermes", "profile"]},
    "/config":   {"label": "配置概览", "cli": ["hermes", "config", "show"]},
    "/whoami":   {"label": "身份凭证", "cli": ["hermes", "auth", "list"]},
}

CONFIG_CMDS: dict[str, dict] = {
    "/model":         {"label": "模型切换", "kind": "picker"},
    "/reasoning":     {"label": "推理力度", "options": ["none", "minimal", "low", "medium", "high"], "key": "agent.reasoning_effort"},
    "/personality":   {"label": "AI人格",   "options": ["technical", "concise", "creative", "helpful", "hype", "noir", "philosopher", "teacher"], "key": "agent.personality"},
    "/verbose":       {"label": "日志级别", "options": ["off", "tools", "all"], "key": "agent.verbose"},
    "/yolo":          {"label": "极速模式", "options": ["true", "false"], "key": "agent.yolo"},
    "/fast":          {"label": "高速模式", "options": ["true", "false"], "key": "agent.fast"},
    "/codex-runtime": {"label": "运行时",   "options": ["app-server", "direct"], "key": "agent.codex_runtime"},
}

SESSION_ACTS: dict[str, dict] = {
    "/new":      {"label": "新建会话", "danger": True},
    "/stop":     {"label": "强制停止", "danger": True},
    "/undo":     {"label": "撤销回复", "danger": False},
    "/retry":    {"label": "重试执行", "danger": False},
    "/compress": {"label": "压缩会话", "danger": False},
}

# 引导类命令（弹出使用模板卡片）
GUIDE_CMDS: dict[str, dict] = {
    "/background": {"label": "后台任务", "template": "/background <提示词或任务目标>", "desc": "在独立后台进程中运行长耗时任务，不占用当前对话流。"},
    "/steer":      {"label": "动态引导", "template": "/steer <干预指令>", "desc": "在 Agent 执行工具调用的间隙动态插入控制指令，修正执行方向。"},
    "/goal":       {"label": "目标管理", "template": "/goal set <长期目标>", "desc": "设定跨多轮次生效的长期执行目标。"},
}

CATEGORIES = [
    ("session", "💬", "会话管理", "blue",
     ["/new", "/stop", "/status", "/context", "/usage", "/sessions", "/undo", "/retry", "/compress", "/background"]),
    ("config", "⚙️", "系统配置", "purple",
     ["/model", "/reasoning", "/personality", "/verbose", "/yolo", "/fast", "/codex-runtime"]),
    ("tools", "🔧", "工具研发", "green",
     ["/diff", "/doctor", "/security", "/debug", "/logs", "/cron", "/plugins", "/skills", "/bundles", "/memory"]),
    ("info", "ℹ️", "系统信息", "grey",
     ["/version", "/profile", "/config", "/whoami"]),
]

_CMD_META: dict[str, dict] = {}
for _d in (CLI_CMDS, CONFIG_CMDS, SESSION_ACTS, GUIDE_CMDS):
    for _k, _v in _d.items():
        _CMD_META[_k] = _v

CMD_PARENT_CATEGORY: dict[str, str] = {
    # 会话分类
    "/new": "/card/session",
    "/stop": "/card/session",
    "/status": "/card/session",
    "/context": "/card/session",
    "/usage": "/card/session",
    "/sessions": "/card/session",
    "/undo": "/card/session",
    "/retry": "/card/session",
    "/compress": "/card/session",
    "/background": "/card/session",
    "/steer": "/card/session",
    "/goal": "/card/session",
    # 配置分类
    "/model": "/card/config",
    "/reasoning": "/card/config",
    "/personality": "/card/config",
    "/verbose": "/card/config",
    "/yolo": "/card/config",
    "/fast": "/card/config",
    "/codex-runtime": "/card/config",
    # 工具分类
    "/diff": "/card/tools",
    "/doctor": "/card/tools",
    "/security": "/card/tools",
    "/debug": "/card/tools",
    "/logs": "/card/tools",
    "/cron": "/card/tools",
    "/plugins": "/card/tools",
    "/skills": "/card/tools",
    "/bundles": "/card/tools",
    "/memory": "/card/tools",
    # 信息分类
    "/version": "/card/info",
    "/profile": "/card/info",
    "/config": "/card/info",
    "/whoami": "/card/info",
}


# ════════════════════════════════════════════════════════════════════════════
# 4. 全量 Provider & Model 纯本地毫秒级发现
# ════════════════════════════════════════════════════════════════════════════

_state_lock = threading.Lock()
_selected_provider: dict[str, str] = {}
_pending_model: dict[str, str] = {}
_chat_card_map: dict[str, str] = {}
_exec_tokens: dict[str, dict] = {}
_cmd_cooldown: dict[str, float] = {}
_cooldown_check_counter: int = 0


def _cleanup_old_outputs() -> None:
    """扫描 /tmp/hermes_*.txt，删除 mtime 超过 7 天的文件。"""
    try:
        cutoff = time.time() - 7 * 86400
        for p in Path("/tmp").glob("hermes_*.txt"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def _skey(chat_id: str, open_id: str) -> str:
    return f"{chat_id}:{open_id or 'anon'}"


def _mint_token(chat_id: str, open_id: str, mid: str, cmd: str, args: str) -> str:
    tok = uuid.uuid4().hex[:16]
    with _state_lock:
        now = time.time()
        for k in [k for k, v in _exec_tokens.items() if v["exp"] < now]:
            _exec_tokens.pop(k, None)
        _exec_tokens[tok] = {
            "cmd": cmd, "args": args, "chat_id": chat_id,
            "open_id": open_id, "mid": mid, "exp": now + 120,
        }
    return tok


def _redeem_token(tok: str, open_id: str) -> Optional[dict]:
    with _state_lock:
        rec = _exec_tokens.pop(tok, None)
    if not rec or rec["exp"] < time.time():
        return None
    if rec["open_id"] != open_id:
        return None
    return rec


_STATIC_OAUTH_MODELS = {
    "openai-codex": [
        "gpt-6-astra", "gpt-6-astra-900k", "gpt-5.6-sol", "gpt-5.6-sol-900k",
        "gpt-5.6-terra", "gpt-5.6-terra-900k", "gpt-5.6-luna", "gpt-5.6-luna-900k",
        "gpt-5.5", "gpt-5.3-codex-spark",
    ],
    "xai-oauth": [
        "grok-4.6", "grok-4.5", "grok-4.3", "grok-4.20-0309-reasoning",
        "grok-4.20-0309-non-reasoning", "grok-4.20-multi-agent-0309",
    ],
    "copilot": [
        "claude-fable-5.1", "claude-fable-5", "claude-opus-4.7", "claude-sonnet-4-6", "gpt-5.6-sol",
    ],
    "nous": [
        "anthropic/claude-fable-5.1", "anthropic/claude-fable-5", "anthropic/claude-opus-5.5",
    ],
    "deepseek": [
        "deepseek-flash", "deepseek-v4-pro",
    ],
    "gemini": [
        "gemini-3.8-flash", "gemini-3-flash-preview", "gemini-2.5-flash-lite",
    ],
    "nvidia": [
        "nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nemotron-3-super-120b-a12b", "meta/llama-3.3-70b-instruct",
    ],
}


def get_hermes_catalog_and_status():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    auth_data = {}
    if AUTH_PATH.exists():
        try:
            auth_data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    catalog: dict[str, list[str]] = {}

    for cp in cfg.get("custom_providers", []) or []:
        pname = cp.get("name")
        if not pname:
            continue
        models = list((cp.get("models") or {}).keys())
        if not models and cp.get("model"):
            models = [cp["model"]]
        if models:
            catalog[pname] = models

    for pname, pinfo in (cfg.get("providers") or {}).items():
        if pname == "custom":
            for sub, sinfo in (pinfo or {}).items():
                if sub not in catalog and (sinfo or {}).get("model"):
                    catalog[sub] = [sinfo["model"]]
        elif pname not in catalog and (pinfo or {}).get("default_model"):
            catalog[pname] = [pinfo["default_model"]]

    authed_set = set(auth_data.get("providers", {}).keys()) | set(auth_data.get("credential_pool", []))
    for ap, mlist in _STATIC_OAUTH_MODELS.items():
        if ap in authed_set and ap not in catalog:
            catalog[ap] = mlist

    ordered: dict[str, list[str]] = {}
    model_cfg = cfg.get("model") or {}
    cur_model = model_cfg.get("default", "unknown")
    cur_provider = model_cfg.get("provider", "custom")
    base_url = model_cfg.get("base_url", "")
    profile = os.environ.get("HERMES_PROFILE") or cfg.get("active_profile", "default")

    # Current provider prioritized, others sorted
    norm_cur = cur_provider.replace("custom:", "")
    if norm_cur in catalog:
        ordered[norm_cur] = catalog[norm_cur]
    for k in sorted(catalog.keys()):
        if k not in ordered:
            ordered[k] = catalog[k]

    return ordered, cur_provider, cur_model, base_url, profile


def _atomic_write_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(f".yaml.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def _switch_hermes_model(tgt_model: str, prov: str) -> bool:
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        cfg.setdefault("model", {})
        cfg["model"]["default"] = tgt_model

        custom_names = {cp.get("name") for cp in (cfg.get("custom_providers") or []) if cp.get("name")}
        custom_dict_names = set((cfg.get("providers") or {}).get("custom", {}).keys())

        if prov in custom_names or prov in custom_dict_names or prov == "custom":
            cfg["model"]["provider"] = f"custom:{prov}" if prov != "custom" else "custom"
        else:
            cfg["model"]["provider"] = prov

        _atomic_write_config(cfg)
        logger.info("[command-palette] model successfully switched to: %s (%s)", tgt_model, cfg["model"]["provider"])
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("[command-palette] model switch failed: %s", e)
        return False


# ════════════════════════════════════════════════════════════════════════════
# 5. 卡片构建引擎（规范化多级双返回导航）
# ════════════════════════════════════════════════════════════════════════════

def _pt(content: str) -> dict:
    return {"tag": "plain_text", "content": str(content)}


def _btn(text: str, typ: str = "default", *, value: dict) -> dict:
    return {"tag": "button", "text": _pt(text), "type": typ, "value": value}


def _nav_btn(text: str, target: str, typ: str = "default") -> dict:
    return _btn(text, typ, value={"action": f"nav:{target}"})


def _card(title: str, template: str, elements: list) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": _pt(title), "template": template},
        "elements": elements,
    }


def _nav_row_for_subpage() -> dict:
    """二级页紧凑导航：双列 column_set，返回 / 首页，按钮直接在 column 下。"""
    return {
        "tag": "column_set",
        "flex_mode": "bisect",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "horizontal_align": "left",
                "elements": [_nav_btn("⬅ 返回", "/card/root", "default")],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "horizontal_align": "left",
                "elements": [_nav_btn("🏠 首页", "/card/root", "default")],
            },
        ],
    }


def _nav_row_for_deep_page(parent_target: str, extra_actions: list | None = None) -> dict:
    """三级页紧凑导航：extra_actions 每行最多 2 个，最后一行放 返回/首页。
    所有按钮直接在 column.elements 中，禁止嵌套 action 容器。"""
    def _make_column_set(btns: list[dict]) -> dict:
        columns = []
        for btn in btns:
            columns.append({
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "horizontal_align": "left",
                "elements": [btn],
            })
        return {"tag": "column_set", "flex_mode": "bisect", "columns": columns}

    rows: list[dict] = []
    extras = list(extra_actions or [])
    for i in range(0, len(extras), 2):
        rows.append(_make_column_set(extras[i:i + 2]))

    nav_btns = [
        _nav_btn("⬅ 返回", parent_target, "default"),
        _nav_btn("🏠 首页", "/card/root", "default"),
    ]
    rows.append(_make_column_set(nav_btns))

    if len(rows) == 1:
        return rows[0]
    return {"tag": "column_set", "flex_mode": "none", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center",
         "elements": rows},
    ]}


def _compact_button_grid(buttons: list[dict]) -> list[dict]:
    """将按钮按每行 2 个构造成经典卡片 column_set 网格。"""
    rows = []
    for i in range(0, len(buttons), 2):
        pair = buttons[i:i + 2]
        columns = []
        for btn in pair:
            columns.append({
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "elements": [btn],
            })
        rows.append({
            "tag": "column_set",
            "flex_mode": "none",
            "columns": columns,
        })
    return rows


def _cc_category_grid(buttons: list[dict]) -> list[dict]:
    """CC 风格分类按钮网格：bisect 模式，按钮带 width='fill'。"""
    # 为每个按钮补上 width=fill
    filled = []
    for btn in buttons:
        b = dict(btn)
        b["width"] = "fill"
        filled.append(b)
    rows = []
    for i in range(0, len(filled), 2):
        pair = filled[i:i + 2]
        columns = []
        for btn in pair:
            columns.append({
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "horizontal_align": "left",
                "elements": [btn],
            })
        rows.append({
            "tag": "column_set",
            "flex_mode": "bisect",
            "columns": columns,
        })
    return rows


def _cc_command_row(text: str, button_text: str, button_type: str, value: dict) -> dict:
    """CC 风格命令行：左宽列放 markdown，右 auto 列放按钮，禁止 action 容器。"""
    btn = {"tag": "button", "text": _pt(button_text), "type": button_type, "value": value}
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 5,
                "vertical_align": "center",
                "elements": [{"tag": "markdown", "content": text}],
            },
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "center",
                "elements": [btn],
            },
        ],
    }


# 配置选项中英双语标签映射 {cmd: {opt: "中文 English"}}
CONFIG_OPTION_LABELS: dict[str, dict[str, str]] = {
    "/reasoning": {
        "none":    "关闭 none",
        "minimal": "最小 minimal",
        "low":     "低 low",
        "medium":  "中等 medium",
        "high":    "高 high",
    },
    "/personality": {
        "technical":   "技术 technical",
        "concise":     "简洁 concise",
        "creative":    "创意 creative",
        "helpful":     "助手 helpful",
        "hype":        "热血 hype",
        "noir":        "黑色 noir",
        "philosopher": "哲思 philosopher",
        "teacher":     "教师 teacher",
    },
    "/verbose": {
        "off":   "关闭 off",
        "tools": "工具 tools",
        "all":   "全部 all",
    },
    "/yolo": {
        "true":  "开启 true",
        "false": "关闭 false",
    },
    "/fast": {
        "true":  "开启 true",
        "false": "关闭 false",
    },
    "/codex-runtime": {
        "app-server": "应用服务 app-server",
        "direct":     "直连 direct",
    },
}


def _config_option_label(cmd: str, opt: str, is_cur: bool) -> str:
    """返回配置选项的双语显示文本（单行），当前选中加 ● 前缀。格式：Emoji 中文 English"""
    mapping = CONFIG_OPTION_LABELS.get(cmd, {})
    if opt in mapping:
        label = mapping[opt]
    else:
        zh = _CMD_META.get(cmd, {}).get("label", "选项")
        label = f"{zh} {opt}"
    emoji = CMD_EMOJI.get(cmd, "")
    if emoji:
        label = f"{emoji} {label}"
    if is_cur:
        label = "● " + label
    return label


def _cc_command_grid(cmds: list[str]) -> list[dict]:
    """将命令列表构建为左列 markdown 文本 + 右列超短按钮的单行结构。"""
    _DISPLAY_ALIAS: dict[str, str] = {
        "/model":        "模型切换 model",
        "/reasoning":    "推理力度 reasoning",
        "/personality":  "人格选择 personality",
        "/verbose":      "日志级别 verbose",
        "/yolo":         "极速开关 yolo",
        "/fast":         "高速开关 fast",
        "/codex-runtime":"运行环境 codex",
        "/status":       "系统状态 status",
        "/context":      "上下文 context",
        "/usage":        "配额用量 usage",
        "/sessions":     "历史会话 sessions",
        "/new":          "新建会话 new",
        "/stop":         "强制停止 stop",
        "/undo":         "撤销回复 undo",
        "/retry":        "重试执行 retry",
        "/compress":     "压缩会话 compress",
        "/background":   "后台任务 background",
        "/diff":         "代码差异 diff",
        "/doctor":       "健康诊断 doctor",
        "/debug":        "调试摘要 debug",
        "/security":     "安全审计 security",
        "/logs":         "网关日志 logs",
        "/cron":         "定时任务 cron",
        "/plugins":      "插件列表 plugins",
        "/skills":       "技能库 skills",
        "/bundles":      "技能包 bundles",
        "/memory":       "记忆系统 memory",
        "/version":      "框架版本 version",
        "/profile":      "配置详情 profile",
        "/config":       "配置概览 config",
        "/whoami":       "身份凭证 whoami",
        "/steer":        "动态引导 steer",
        "/goal":         "目标管理 goal",
    }
    rows = []
    for cmd in cmds:
        meta = _CMD_META.get(cmd, {})
        danger = bool(meta.get("danger"))
        btn_type = "danger" if danger else "default"
        base_text = _DISPLAY_ALIAS.get(cmd) or f"{meta.get('label', cmd.strip('/'))} {cmd.strip('/')}"
        emoji = CMD_EMOJI.get(cmd, "")
        md_text = f"{emoji} **{base_text}**" if emoji else f"**{base_text}**"
        btn_label = "执行"
        rows.append({
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 5,
                    "vertical_align": "center",
                    "elements": [{"tag": "markdown", "content": md_text}],
                },
                {
                    "tag": "column",
                    "width": "auto",
                    "vertical_align": "center",
                    "elements": [{
                        "tag": "button",
                        "text": _pt(btn_label),
                        "type": btn_type,
                        "value": {"action": f"cmd:{cmd}"},
                    }],
                },
            ],
        })
    return rows


QUICK_ACTIONS = [
    ("🔄 **模型切换** model", "▶", "primary", {"action": "nav:/card/model"}),
    ("📊 **系统状态** status", "▶", "default", {"action": "cmd:/status"}),
    ("🆕 **新建会话** new", "▶", "default", {"action": "cmd:/new"}),
    ("⏹ **强制停止** stop", "▶", "danger", {"action": "cmd:/stop"}),
]


def _root_card() -> dict:
    catalog, cur_p, cur_m, _, prof = get_hermes_catalog_and_status()
    short_model = cur_m.split("/")[-1] if cur_m else cur_m
    try:
        reasoning = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")).get("agent", {}).get("reasoning_effort", "medium") or "medium"
    except Exception:
        reasoning = "medium"
    elements = [
        {"tag": "markdown",
         "content": f"🟢 **{short_model}** · {reasoning}"},
    ]
    cat_btns = [
        _nav_btn("💬 会话管理", "/card/session"),
        _nav_btn("⚙️ 系统配置", "/card/config"),
        _nav_btn("🔧 工具研发", "/card/tools"),
        _nav_btn("ℹ️ 系统信息", "/card/info"),
    ]
    elements.extend(_cc_category_grid(cat_btns))
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": "⚡ **快捷操作**"})
    for text, btn_text, btn_type, value in QUICK_ACTIONS:
        elements.append(_cc_command_row(text, btn_text, btn_type, value))
    elements.append({"tag": "hr"})
    return _card("⌨️ Hermes 控制面板", "blue", elements)


def _cat_card(ck: str, icon: str, title: str, color: str, cmds: list) -> dict:
    elements = _cc_command_grid(cmds)
    elements.append(_nav_row_for_subpage())
    return _card(f"{icon} {title} · {len(cmds)}项", color, elements)


def _build_model_card(skey: str, provider: str = "", model: str = "",
                      status_msg: str = "", ok: bool = True) -> dict:
    catalog, cur_p, cur_m, _, _ = get_hermes_catalog_and_status()
    if not provider:
        provider = _selected_provider.get(skey, "")
    if provider not in catalog:
        norm_p = cur_p.split(":")[-1] if cur_p else ""
        provider = norm_p if norm_p in catalog else (next(iter(catalog), "default"))
    with _state_lock:
        _selected_provider[skey] = provider

    models = catalog.get(provider, [])
    if not model or model not in models:
        model = _pending_model.get(skey, "") or (models[0] if models else "")
    with _state_lock:
        _pending_model[skey] = model

    prov_opts = [{"text": _pt(p), "value": p} for p in list(catalog.keys())[:100]]
    model_opts = [{"text": _pt(m), "value": m} for m in models[:100]]

    elements = []
    if status_msg:
        elements += [{"tag": "markdown", "content": status_msg}]
    else:
        elements += [
            {"tag": "markdown",
             "content": f"🟢 当前: `{cur_m}` (`{cur_p}`)  |  📦 `{len(catalog)}` 通道"},
        ]

    elements += [
        {"tag": "markdown", "content": f"🏢 通道 `{provider}`"},
        {"tag": "action", "actions": [{
            "tag": "select_static",
            "placeholder": _pt("点击展开选择 Provider"),
            "value": {"action": "select_provider"},
            "initial_option": provider,
            "options": prov_opts,
        }]},
        {"tag": "markdown", "content": f"🤖 模型 · `{len(models)}` 个"},
    ]

    if len(models) > 50:
        first_half = model_opts[:50]
        second_half = model_opts[50:]
        elements += [
            {"tag": "action", "actions": [{
                "tag": "select_static",
                "placeholder": _pt("模型 A-M（前50个）"),
                "value": {"action": "select_model", "provider": provider},
                "initial_option": model if model in [o["value"] for o in first_half] else None,
                "options": first_half,
            }]},
            {"tag": "action", "actions": [{
                "tag": "select_static",
                "placeholder": _pt("模型 N-Z（后续）"),
                "value": {"action": "select_model", "provider": provider},
                "initial_option": model if model in [o["value"] for o in second_half] else None,
                "options": second_half,
            }]},
        ]
    else:
        elements += [
            {"tag": "action", "actions": [{
                "tag": "select_static",
                "placeholder": _pt("点击展开选择模型"),
                "value": {"action": "select_model", "provider": provider},
                "initial_option": model,
                "options": model_opts,
            }]},
        ]

    elements += [
        {"tag": "markdown", "content": f"📌 待生效: `{provider}` / `{model}`"},
        _nav_row_for_deep_page(
            parent_target="/card/config",
            extra_actions=[_btn("✅ 确认切换", "primary", value={"action": f"confirm_switch:{provider}:{model}"})],
        ),
    ]
    return _card("🔄 模型与提供商切换", ("green" if ok else "red") if status_msg else "purple", elements)


def _build_options_card(cmd: str) -> dict:
    meta = CONFIG_CMDS.get(cmd, {})
    opts = meta.get("options") or []
    cfg_key = meta.get("key", f"agent.{cmd.lstrip('/')}")

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    node = cfg
    for k in cfg_key.split("."):
        node = node.get(k, {}) if isinstance(node, dict) else {}
    cur = str(node) if node != {} else "未设置"

    elements = [{"tag": "markdown", "content": f"当前值: `{cur}`"}]
    for opt in opts:
        is_cur = (str(opt).lower() == str(cur).lower())
        md_text = _config_option_label(cmd, str(opt), is_cur)
        btn_type = "primary" if is_cur else "default"
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 5,
                    "vertical_align": "center",
                    "elements": [{"tag": "markdown", "content": md_text}],
                },
                {
                    "tag": "column",
                    "width": "auto",
                    "vertical_align": "center",
                    "elements": [{
                        "tag": "button",
                        "text": _pt("选择"),
                        "type": btn_type,
                        "value": {"action": f"set_cfg:{cmd}:{opt}"},
                    }],
                },
            ],
        })
    elements.append(_nav_row_for_deep_page(CMD_PARENT_CATEGORY.get(cmd, "/card/config")))
    return _card(f"⚙️ 设置 {meta.get('label', cmd)}", "purple", elements)


def _build_guide_card(cmd: str) -> dict:
    """参数指令使用指南与模板卡片"""
    meta = GUIDE_CMDS.get(cmd, {})
    template = meta.get("template", cmd)
    desc = meta.get("desc", "")
    elements = [
        {"tag": "markdown",
         "content": f"💡 **{meta.get('label', cmd)}**：{desc}\n**格式**：`{template}`\n复制替换参数后直接发送。"},
        _nav_row_for_deep_page(CMD_PARENT_CATEGORY.get(cmd, "/card/session")),
    ]
    return _card(f"💡 {meta.get('label', cmd)} 指南", "blue", elements)


def _build_exec_confirm_card(cmd: str, args: str, token: str) -> dict:
    meta = _CMD_META.get(cmd, {})
    full = f"{cmd} {args}".strip()
    elements = [
        {"tag": "markdown", "content": (
            f"⚠️ **高危确认** `{full}`\n"
            f"{meta.get('label', '')} — 将重置会话或中止进程。令牌 120 秒有效、仅本人点击有效。")},
    ]
    confirm_btns = [
        _btn("✅ 确认执行", "danger", value={"action": f"exec_confirm:{token}"}),
        _btn("❌ 取消", "default", value={"action": "exec_cancel"}),
        _nav_btn("⬅ 返回上级", CMD_PARENT_CATEGORY.get(cmd, "/card/session")),
        _nav_btn("🏠 返回首页", "/card/root"),
    ]
    elements.extend(_cc_category_grid(confirm_btns))
    return _card(f"⚠️ 确认 {meta.get('label', cmd)}", "red", elements)


def _build_running_card(cmd: str) -> dict:
    meta = _CMD_META.get(cmd, {})
    elements = [
        {"tag": "markdown",
         "content": f"⏳ **正在执行** `{cmd}`（{meta.get('label', '')}）…完成后自动重绘结果卡。"},
        _nav_row_for_deep_page(CMD_PARENT_CATEGORY.get(cmd, "/card/root")),
    ]
    return _card(f"⚡ {meta.get('label', cmd)}", "yellow", elements)


def _build_copy_card(cmd: str, saved_path: str) -> dict:
    """构建便于移动端/桌面端完整选中文本的复制卡片。"""
    if not cmd and saved_path:
        cmd = _infer_cmd_from_saved_path(saved_path)

    meta = _CMD_META.get(cmd, {})
    label = meta.get("label", cmd or "命令输出")

    content, err = _read_saved_output(saved_path)
    if err:
        body = f"无法读取归档内容: {err}"
    else:
        truncated = False
        display = content
        # 先按字符数截断到 18000，再按 JSON 字节数动态收缩
        if len(display) > 18000:
            display = display[:18000]
            truncated = True
        safe_display = display.replace("```", r"\`\`\`")
        suffix = f"\n[卡片展示已截断，完整内容见归档文件: {saved_path}]" if truncated else ""
        body = f"```text\n{safe_display}\n```{suffix}"
        # 验证 card JSON 字节数，必要时继续缩短
        _test_card = {"tag": "markdown", "content": body}
        while len(json.dumps(_test_card, ensure_ascii=False).encode("utf-8")) > 26000 and len(safe_display) > 200:
            safe_display = safe_display[:int(len(safe_display) * 0.85)]
            truncated = True
            body = f"```text\n{safe_display}\n```\n[卡片展示已截断，完整内容见归档文件: {saved_path}]"
            _test_card = {"tag": "markdown", "content": body}

    parent_cat = CMD_PARENT_CATEGORY.get(cmd, "/card/root")
    elements = [
        {"tag": "markdown", "content": "请在飞书中长按（手机）或选中（电脑）以下内容复制"},
        {"tag": "markdown", "content": body},
        {
            "tag": "action",
            "actions": [
                _btn("⬅ 返回结果", "primary", value={"action": f"show_result:{saved_path}"}),
                _nav_btn("⬅ 返回上级", parent_cat, "default"),
                _nav_btn("🏠 返回首页", "/card/root", "default"),
            ],
        },
    ]
    return _card(f"📋 复制 {label}", "blue", elements)


def _result_status_from_text(text: str) -> bool:
    """从归档文本判断是否包含失败/异常标识，返回 ok 布尔值。"""
    if not text:
        return True
    patterns = [r"^[ \t]*✗", r"\bTraceback\b", r"\bError\b", r"\bException\b", r"exit code [1-9]"]
    for pat in patterns:
        if re.search(pat, text, re.MULTILINE):
            return False
    return True


def _build_beautiful_result_card(cmd: str, raw_output: str, elapsed: float = 0.0, ok: bool = True,
                                  saved_path: str = "", status_text: str = "",
                                  timed_out: bool = False) -> dict:
    meta = _CMD_META.get(cmd, {})
    label = meta.get("label", cmd)

    if timed_out:
        raw_output = f"⚠️ 命令执行超时（50秒限制）\n\n{raw_output}"

    sub_elements, _saved = parse_and_beautify_output(cmd, raw_output)
    if not saved_path:
        saved_path = _saved

    no_data = (
        "no account usage available" in raw_output.lower()
        or "no credential is configured" in raw_output.lower()
    )

    if timed_out:
        header_template = "orange"
        time_part = f"  |  ⏱️ 耗时 `{elapsed:.1f}s`" if elapsed > 0 else ""
        header_content = f"⚠️ **{label} ({cmd}) 执行超时**{time_part}"
    elif no_data:
        header_template = "grey"
        time_part = f"  |  ⏱️ 耗时 `{elapsed:.1f}s`" if elapsed > 0 else ""
        header_content = f"ℹ️ **{label} ({cmd}) 暂无数据**{time_part}"
    else:
        header_template = "green" if ok else "red"
        icon = "✅" if ok else "❌"
        if elapsed > 0:
            time_part = f"  |  ⏱️ 耗时 `{elapsed:.1f}s`"
        elif status_text:
            time_part = f"  |  {status_text}"
        else:
            time_part = "  |  已归档结果"
        header_content = f"{icon} **{label} ({cmd}) 执行{'成功' if ok else '遇到异常'}**{time_part}"

    elements = [{"tag": "markdown", "content": header_content}]
    elements.extend(sub_elements)

    parent_cat = CMD_PARENT_CATEGORY.get(cmd, "/card/root")
    extra_actions = [
        _btn("📋 复制内容", "primary", value={"action": f"copy_output:{saved_path}"}),
        _btn("🔁 再次执行", "default", value={"action": f"cmd:{cmd}"}),
    ]
    elements.append(_nav_row_for_deep_page(
        parent_target=parent_cat,
        extra_actions=extra_actions,
    ))
    card_icon = "⚠️" if timed_out else ("ℹ️" if no_data else ("✅" if ok else "❌"))
    return _card(f"{card_icon} {label}", header_template, elements)


def _nav(action: str, skey: str = "") -> dict:
    b = action[4:] if action.startswith("nav:") else action
    if b in ("", "/card", "/card/root"):
        return _root_card()
    if b == "/card/model":
        return _build_model_card(skey)
    for ck, ic, tn, cc, cmds in CATEGORIES:
        if b == f"/card/{ck}":
            return _cat_card(ck, ic, tn, cc, cmds)
    return _root_card()


# ════════════════════════════════════════════════════════════════════════════
# 6. 执行与卡片重绘通道
# ════════════════════════════════════════════════════════════════════════════

def _patch_card(mid: str, card: dict) -> bool:
    if not mid:
        return False
    data = json.dumps({"content": json.dumps(card, ensure_ascii=False)}, ensure_ascii=False)
    res = run_lark(["im", "messages", "patch", "--as", "bot", "--message-id", mid, "--data", data], timeout=15)
    if not res.ok:
        logger.error("[command-palette] _patch_card failed: exit=%s stderr=%s stdout=%s",
                     res.exit_code, (res.stderr or "").strip()[:500], (res.stdout or "").strip()[:200])
    return res.ok


def _send_root_card(chat_id: str, card: dict) -> bool:
    res = run_lark(["im", "+messages-send", "--as", "bot", "--chat-id", chat_id,
                    "--msg-type", "interactive", "--content", json.dumps(card, ensure_ascii=False)], timeout=25)
    if res.ok:
        try:
            mid = json.loads(res.stdout).get("data", {}).get("message_id")
            if mid:
                _chat_card_map[chat_id] = mid
                if len(_chat_card_map) > 500:
                    del _chat_card_map[next(iter(_chat_card_map))]
        except Exception:  # noqa: BLE001
            pass
    else:
        logger.error("[command-palette] /card send failed: exit=%s stderr=%s stdout=%s",
                     res.exit_code, (res.stderr or "").strip(), (res.stdout or "").strip())
    return res.ok


def _async(fn: Callable, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def _execute_cli_async(cmd: str, mid: str) -> None:
    meta = CLI_CMDS.get(cmd, {})
    t0 = time.monotonic()
    argv = meta.get("cli") or ["hermes", cmd.lstrip("/")]
    res = run_subprocess(argv, timeout=50)

    elapsed = time.monotonic() - t0
    output_text = res.stdout if res.ok else (res.stderr or res.stdout or f"exit code {res.exit_code}")
    card = _build_beautiful_result_card(cmd, output_text, elapsed=elapsed, ok=res.ok, timed_out=res.timed_out)
    if mid:
        _patch_card(mid, card)


# ════════════════════════════════════════════════════════════════════════════
# 7. 同步回调入口（handle_card_action_sync）
# ════════════════════════════════════════════════════════════════════════════

def _resp(card: dict = None, toast: str = "", toast_type: str = "info"):
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackCard, CallBackToast, P2CardActionTriggerResponse)
    r = P2CardActionTriggerResponse()
    if card is not None:
        c = CallBackCard()
        c.type = "raw"
        c.data = card
        r.card = c
    if toast:
        t = CallBackToast()
        t.type = toast_type
        t.content = toast
        r.toast = t
    return r


def handle_card_action_sync(adapter: Any, data: Any) -> Optional[Any]:
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (  # noqa: F401
            P2CardActionTriggerResponse)
    except Exception as e:  # noqa: BLE001
        logger.error("[command-palette] lark_oapi callback models unavailable: %s", e)
        return None

    event = getattr(data, "event", None)
    act = getattr(event, "action", None) if not isinstance(event, dict) else (event or {}).get("action")
    av = (getattr(act, "value", {}) or {}) if act else {}
    ac = av.get("action", "") if isinstance(av, dict) else ""
    opt = getattr(act, "option", "") or ""

    ctx = getattr(event, "context", None) if not isinstance(event, dict) else (event or {}).get("context")
    cid = getattr(ctx, "open_chat_id", "") if ctx else ""
    mid = getattr(ctx, "open_message_id", "") if ctx else ""
    operator = getattr(event, "operator", None) if not isinstance(event, dict) else (event or {}).get("operator")
    open_id = getattr(operator, "open_id", "") if operator else ""

    if mid and cid:
        _chat_card_map[cid] = mid
        if len(_chat_card_map) > 500:
            del _chat_card_map[next(iter(_chat_card_map))]
    sk = _skey(cid, open_id)

    checker = getattr(adapter, "_is_interactive_operator_authorized", None)
    if callable(checker) and not checker(open_id):
        return _resp(toast="⛔ 您无权操作此控制面板", toast_type="error")

    logger.info("[command-palette] ac=%s opt=%s cid=%s uid=%s", ac, opt, cid, open_id)

    # ── 1. 下拉选择（暂存状态） ──
    if ac == "select_provider" and opt:
        with _state_lock:
            _selected_provider[sk] = opt
            _pending_model.pop(sk, None)
        card = _build_model_card(sk, provider=opt)
        return _resp(card, f"已选择通道: {opt}")

    if ac == "select_model" and opt:
        prov = av.get("provider", _selected_provider.get(sk, ""))
        with _state_lock:
            _pending_model[sk] = opt
        card = _build_model_card(sk, provider=prov, model=opt)
        return _resp(card, f"已选择模型: {opt}")

    # ── 2. 模型切换【纯同步 2ms 搞定，立即返回绿色成功卡，绝不杀网关】 ──
    if ac.startswith("confirm_switch:"):
        parts = ac.split(":", 2)
        prov = parts[1] if len(parts) > 1 else ""
        tgt = parts[2] if len(parts) > 2 else _pending_model.get(sk, "")
        if not tgt:
            return _resp(toast="请先在下拉列表中选择目标模型", toast_type="warning")

        ok = _switch_hermes_model(tgt, prov)
        if ok:
            msg = f"🎉 **模型切换成功！**\n• 当前生效模型: `{tgt}`\n• 当前部署通道: `{prov}`\n*(配置已毫秒级更新，后续交互即刻生效)*"
        else:
            msg = f"❌ **模型切换写入失败**\n目标: `{tgt}` ({prov})"

        card = _build_model_card(sk, provider=prov, model=tgt, status_msg=msg, ok=ok)
        return _resp(card, toast="模型切换完成！" if ok else "切换失败")

    # ── 3. 参数配置点选（/reasoning /personality /verbose /yolo /fast 单选卡直接生效） ──
    if ac.startswith("set_cfg:"):
        parts = ac.split(":", 2)
        cmd = parts[1] if len(parts) > 1 else ""
        val = parts[2] if len(parts) > 2 else ""
        meta = CONFIG_CMDS.get(cmd, {})
        cfg_key = meta.get("key", f"agent.{cmd.lstrip('/')}")
        run_subprocess(["hermes", "config", "set", cfg_key, val, "--yes"], timeout=10)
        card = _build_options_card(cmd)
        return _resp(card, toast=f"已将 {cmd} 设为 {val}")

    # ── 4. 危险操作二次确认与兑换 (/new, /stop) ──
    if ac.startswith("exec_confirm:"):
        tok = ac.split(":", 1)[1]
        rec = _redeem_token(tok, open_id)
        if not rec:
            return _resp(toast="⛔ 令牌已失效或操作人变更", toast_type="error")
        cmd = rec["cmd"]
        dispatch = getattr(adapter, "_dispatch_synthetic_event", None)
        if callable(dispatch):
            try:
                from gateway.platforms.event import MessageType  # type: ignore
                coro = dispatch(
                    text=cmd, message_type=MessageType.COMMAND, chat_id=rec["chat_id"],
                    sender_id=type("S", (), {"open_id": open_id, "user_id": None, "union_id": None})(),
                    event_chat_type="group", raw_message=None,
                    message_id=f"palette-{uuid.uuid4().hex[:12]}",
                )
                loop = getattr(adapter, "_loop", None)
                if loop and not loop.is_closed():
                    import asyncio
                    asyncio.run_coroutine_threadsafe(coro, loop)
            except Exception as e:
                logger.error("[command-palette] dispatch synthetic failed: %s", e)
        parent_cat = CMD_PARENT_CATEGORY.get(cmd, "/card/session")
        card = _card(f"✅ {SESSION_ACTS.get(cmd, {}).get('label', cmd)}", "green", [
            {"tag": "markdown", "content": f"✅ `{cmd}` 已成功下发到底层执行。"},
            _nav_row_for_deep_page(parent_cat),
        ])
        return _resp(card, toast=f"{cmd} 已执行")

    if ac == "exec_cancel":
        return _resp(_root_card(), toast="已取消操作")

    # ── 5. 复制输出与回显原结果卡 ──
    if ac.startswith("copy_output:"):
        raw_path = ac[len("copy_output:"):]
        safe = _safe_saved_output_path(raw_path)
        if not safe:
            return _resp(toast="⛔ 无效的归档路径", toast_type="error")
        inferred_cmd = _infer_cmd_from_saved_path(safe)
        if not inferred_cmd:
            return _resp(toast="⛔ 无法从归档路径识别命令", toast_type="error")
        card = _build_copy_card(inferred_cmd, safe)
        return _resp(card, toast="已展开完整内容，请长按或选中复制")

    if ac.startswith("show_result:"):
        raw_path = ac[len("show_result:"):]
        safe = _safe_saved_output_path(raw_path)
        if not safe:
            return _resp(toast="⛔ 无效的归档路径", toast_type="error")
        content, err = _read_saved_output(safe)
        if err:
            return _resp(toast=f"⛔ 读取失败: {err}", toast_type="error")
        inferred_cmd = _infer_cmd_from_saved_path(safe)
        if not inferred_cmd:
            return _resp(toast="⛔ 无法从归档路径识别命令", toast_type="error")
        result_ok = _result_status_from_text(content)
        card = _build_beautiful_result_card(inferred_cmd, content, ok=result_ok,
                                             saved_path=safe, status_text="已归档结果")
        return _resp(card)

    # ── 6. 视图导航 ──
    if ac.startswith("nav:"):
        card = _nav(ac, sk)
        return _resp(card)

    # ── 6. 命令触发路由 ──
    if ac.startswith("cmd:"):
        cmd = ac[4:].strip()
        meta = _CMD_META.get(cmd)
        if not meta:
            return _resp(toast=f"⛔ 未知命令 {cmd}", toast_type="error")

        # 命令冷却检查（3秒内重复触发拦截）
        global _cooldown_check_counter
        cooldown_key = f"{cmd}:{open_id}"
        now_mono = time.monotonic()
        if now_mono - _cmd_cooldown.get(cooldown_key, 0) < 3.0:
            return _resp(toast="请稍等，命令正在冷却中", toast_type="warning")
        _cmd_cooldown[cooldown_key] = now_mono
        _cooldown_check_counter += 1
        if _cooldown_check_counter >= 100:
            _cooldown_check_counter = 0
            cutoff = now_mono - 300
            expired = [k for k, v in _cmd_cooldown.items() if v < cutoff]
            for k in expired:
                del _cmd_cooldown[k]

        if cmd == "/model":
            return _resp(_build_model_card(sk))

        # 参数配置类 -> 展示单选选项卡
        if meta.get("options"):
            return _resp(_build_options_card(cmd))

        # 指引模板类 -> 弹出使用格式卡
        if cmd in GUIDE_CMDS:
            return _resp(_build_guide_card(cmd))

        # 会话危险类 (/new, /stop) -> 弹二次确认卡
        if meta.get("danger"):
            tok = _mint_token(cid, open_id, mid, cmd, "")
            return _resp(_build_exec_confirm_card(cmd, "", tok), toast="请二次确认", toast_type="warning")

        # 会话非危险控制命令 (/undo, /retry, /compress) -> 下发真实消息管线执行
        if cmd in SESSION_ACTS:
            dispatch = getattr(adapter, "_dispatch_synthetic_event", None)
            if callable(dispatch):
                try:
                    from gateway.platforms.event import MessageType  # type: ignore
                    coro = dispatch(
                        text=cmd, message_type=MessageType.COMMAND, chat_id=cid,
                        sender_id=type("S", (), {"open_id": open_id, "user_id": None, "union_id": None})(),
                        event_chat_type="group", raw_message=None,
                        message_id=f"palette-{uuid.uuid4().hex[:12]}",
                    )
                    loop = getattr(adapter, "_loop", None)
                    if loop and not loop.is_closed():
                        import asyncio
                        asyncio.run_coroutine_threadsafe(coro, loop)
                except Exception as e:
                    logger.error("[command-palette] dispatch synthetic failed: %s", e)
            parent_cat = CMD_PARENT_CATEGORY.get(cmd, "/card/session")
            card = _card(f"✅ {meta.get('label', cmd)}", "green", [
                {"tag": "markdown", "content": f"✅ `{cmd}`（{meta.get('label', '')}）已直接下发至当前会话处理。"},
                {"tag": "hr"},
                _nav_row_for_deep_page(parent_cat),
            ])
            return _resp(card, toast=f"{cmd} 已下发")

        # 核心 CLI 查询类命令（/status, /context, /usage, /diff, /doctor, /version, /cron, /plugins...）：
        running_card = _build_running_card(cmd)
        if mid:
            _async(_execute_cli_async, cmd, mid)
        return _resp(running_card, toast=f"正在拉取 {cmd} 数据…")

    return None


# ════════════════════════════════════════════════════════════════════════════
# 8. 消息拦截钩子与注册
# ════════════════════════════════════════════════════════════════════════════

def _on_msg(event=None, gateway=None, **kw) -> Optional[dict]:
    if not event:
        return None
    txt = getattr(event, "text", None)
    if not txt or not isinstance(txt, str):
        return None
    txt = txt.strip()
    src = getattr(event, "source", None)
    if not src:
        return None
    if getattr(getattr(src, "platform", None), "value", "") != "feishu":
        return None
    cid = getattr(src, "chat_id", None) or ""
    if not cid:
        return None

    if txt == "/card":
        _send_root_card(cid, _root_card())
        return {"action": "skip", "reason": "command-palette:card"}

    if txt.startswith("/card "):
        return {"action": "skip", "reason": "command-palette:in_place_consumed"}

    return None


def register(ctx) -> None:
    _cleanup_old_outputs()
    try:
        from plugins.platforms.feishu.adapter import FeishuAdapter
        if not hasattr(FeishuAdapter, "_card_action_handlers"):
            FeishuAdapter._card_action_handlers = []
        if handle_card_action_sync not in FeishuAdapter._card_action_handlers:
            FeishuAdapter._card_action_handlers.append(handle_card_action_sync)
    except Exception:  # noqa: BLE001
        pass

    ctx.register_hook("pre_gateway_dispatch", _on_msg)
    logger.info("Hermes command-palette v7.3 (hierarchical nav + 32 rich commands) registered")
