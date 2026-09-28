"""Hermes Feishu Command Palette (feishu-command-palette) v1.4.0

Interactive single-card control panel to dispatch Hermes slash commands directly.
Supports clean bilingual decoupling (Chinese by default, switchable to English in-place)
to ensure 100% zero-truncation on mobile screens.

Disclosure:
Answers the Feishu /card text with a control card sent through the external
lark-cli binary; button clicks run a fixed table of hermes ... CLI subcommands
locally and post their output (logs, sessions, redacted config/auth listings)
into the chat, and can write model/provider and agent.* settings including
agent.yolo via official hermes config set CLI; reads auth.json for provider names.
All temporary process outputs are sandboxed in HERMES_HOME/plugin-data/feishu-command-palette/ (mode 0700).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

logger = logging.getLogger("command-palette")

# Global Settings & Experiments
STREAMING_ENABLED = True
STREAM_PATCH_INTERVAL = 0.8  # Minimal seconds between streaming card patches (protects Lark QPS)
_DEFAULT_CMD_TIMEOUT = 35

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
CONFIG_PATH = HERMES_HOME / "config.yaml"
AUTH_PATH = HERMES_HOME / "auth.json"
INSTALL_PATH = HERMES_HOME / "hermes-agent"

# Dedicated, Sandboxed Data Directory (Strictly NO /tmp usage)
PLUGIN_DATA_DIR = HERMES_HOME / "plugin-data" / "feishu-command-palette"
try:
    PLUGIN_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLUGIN_DATA_DIR.chmod(0o700)
except Exception as e:
    logger.warning("[command-palette] failed to initialize plugin-data directory: %s", e)

# ════════════════════════════════════════════════════════════════════════════
# 1. I18N Framework & Preferences Persistence (Default Chinese, Zero Truncation)
# ════════════════════════════════════════════════════════════════════════════

_user_lang: dict[str, str] = {}


def _get_lang(skey: str = "") -> str:
    """获取用户/会话语言偏好，默认为 'zh' (中文)"""
    if skey and skey in _user_lang:
        return _user_lang[skey]
    pref_file = PLUGIN_DATA_DIR / "preferences.json"
    try:
        if pref_file.exists():
            data = json.loads(pref_file.read_text(encoding="utf-8"))
            if skey and skey in data.get("lang", {}):
                lang = data["lang"][skey]
                _user_lang[skey] = lang
                return lang
            default_l = data.get("default_lang", "zh")
            if skey:
                _user_lang[skey] = default_l
            return default_l
    except Exception:
        pass
    if skey:
        _user_lang[skey] = "zh"
    return "zh"


def _set_lang(skey: str, lang: str) -> None:
    """切换语言偏好并原子持久化"""
    norm_lang = "en" if lang.lower() == "en" else "zh"
    if skey:
        _user_lang[skey] = norm_lang
    pref_file = PLUGIN_DATA_DIR / "preferences.json"
    try:
        data = {}
        if pref_file.exists():
            data = json.loads(pref_file.read_text(encoding="utf-8"))
        if skey:
            data.setdefault("lang", {})[skey] = norm_lang
        data["default_lang"] = norm_lang
        pref_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        pref_file.chmod(0o600)
    except Exception as e:
        logger.warning("[command-palette] failed to save lang preference: %s", e)


I18N = {
    "zh": {
        "title": "Hermes 控制面板",
        "quick_actions": "⚡ 快捷操作",
        "switch_lang_btn": "🌐 English",
        "switch_lang_toast": "已切换为英文 / Switched to English",
        "back": "⬅ 返回",
        "home": "🏠 首页",
        "run": "执行",
        "copy": "📋 复制内容",
        "retry": "🔁 再次执行",
        "executing": "⏳ 正在执行",
        "executing_tip": "…完成后自动重绘结果卡。",
        "live_stream": "⏳ 终端输出实时捕获中",
        "deep_diag": "⏳ 后台深度检测中",
        "process_active": "进程活跃运行中",
        "init_process": "⏳ 正在初始化进程...",
        "stream_note": "ℹ️ 终端流式预览中，执行完毕将自动折叠为完整报告",
        "success": "执行成功",
        "failed": "执行失败",
        "timed_out": "执行超时",
        "no_data": "暂无数据",
        "exceptions_found": "包含异常",
        "archive_path": "完整归档路径",
        "details": "查询详情",
        "preview": "摘要前瞻",
        "full_details": "完整明细",
        "confirm_high_risk": "⚠️ 高危操作确认",
        "confirm_tip": "令牌 120 秒有效，仅所有者点击有效。",
        "confirm_btn": "✅ 确认执行",
        "cancel_btn": "❌ 取消",
        "action_cancelled": "操作已取消",
        "active_model": "当前生效模型",
        "active_prov": "当前部署通道",
        "select_prov_model": "选择通道与模型",
        "confirm_switch_btn": "✅ 确认切换",
        "switch_success": "🎉 模型切换成功！",
        "switch_failed": "❌ 模型切换写入失败",
        "cur_val": "当前值",
        "active_opt": "✓ 当前",
        "select_opt": "选择",
        "guide_desc": "说明",
        "guide_tmpl": "使用格式",
        "guide_tip": "💡 复制上方格式并发送至会话即可执行。",
        "only_owner": "⛔ 仅限实例所有者执行操作",
        "cooldown": "请稍等，命令正在冷却中",
        "expanded_full": "已展开完整内容，请长按或选中复制",
        "result_loaded": "结果已加载",
        "dispatched": "已直接下发至当前会话处理。",
    },
    "en": {
        "title": "Hermes Control Panel",
        "quick_actions": "⚡ Quick Actions",
        "switch_lang_btn": "🌐 简体中文",
        "switch_lang_toast": "已切换为简体中文 / Switched to Chinese",
        "back": "⬅ Back",
        "home": "🏠 Home",
        "run": "Run",
        "copy": "📋 Copy Output",
        "retry": "🔁 Re-run",
        "executing": "⏳ Executing",
        "executing_tip": "...Card updates automatically on completion.",
        "live_stream": "⏳ Live Output Capture",
        "deep_diag": "⏳ Deep Diagnostics in Progress",
        "process_active": "Process Active",
        "init_process": "⏳ Initializing process...",
        "stream_note": "ℹ️ Live streaming preview. Final report folds automatically on completion.",
        "success": "Success",
        "failed": "Failed",
        "timed_out": "Timed Out",
        "no_data": "No Data",
        "exceptions_found": "Exceptions Found",
        "archive_path": "Archive Path",
        "details": "Details",
        "preview": "Preview",
        "full_details": "Full Details",
        "confirm_high_risk": "⚠️ High Risk Confirmation",
        "confirm_tip": "Token valid for 120s. Restricted to instance owner.",
        "confirm_btn": "✅ Confirm",
        "cancel_btn": "❌ Cancel",
        "action_cancelled": "Action cancelled",
        "active_model": "Active Model",
        "active_prov": "Provider",
        "select_prov_model": "Select Provider & Model",
        "confirm_switch_btn": "✅ Confirm Switch",
        "switch_success": "🎉 Model Switched Successfully!",
        "switch_failed": "❌ Model Switch Failed",
        "cur_val": "Current value",
        "active_opt": "✓ Active",
        "select_opt": "Select",
        "guide_desc": "Description",
        "guide_tmpl": "Template",
        "guide_tip": "💡 Copy template above and send to chat to execute.",
        "only_owner": "⛔ Operation restricted to instance owner",
        "cooldown": "Please wait, command in cooldown",
        "expanded_full": "Expanded full text, copy as needed",
        "result_loaded": "Result loaded",
        "dispatched": "Dispatched directly to current session.",
    }
}

# ════════════════════════════════════════════════════════════════════════════
# 2. Output Formatting & Sandboxed Storage
# ════════════════════════════════════════════════════════════════════════════

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _safe_saved_output_path(value: str) -> str | None:
    if not value or not isinstance(value, str):
        return None
    val = value.strip()
    if ".." in val:
        return None
    try:
        p = Path(val).resolve()
        base = PLUGIN_DATA_DIR.resolve()
        if not str(p).startswith(str(base)) or not p.is_file():
            return None
        return str(p)
    except Exception:
        return None


def _infer_cmd_from_saved_path(saved_path: str) -> str:
    safe = _safe_saved_output_path(saved_path)
    if not safe:
        return ""
    basename = Path(safe).stem
    for cmd in CLI_CMDS:
        prefix = "hermes_" + cmd.strip("/").replace(" ", "_") + "_"
        if basename.startswith(prefix):
            return cmd
    return ""


def _read_saved_output(path: str, lang: str = "zh") -> tuple[str, str | None]:
    safe = _safe_saved_output_path(path)
    if not safe:
        return "", ("非法归档路径" if lang == "zh" else "Invalid archive path")
    try:
        p = Path(safe)
        if not p.exists():
            return "", ("归档文件不存在" if lang == "zh" else "Archive file does not exist")
        if not p.is_file():
            return "", ("目标不是普通文件" if lang == "zh" else "Target is not a regular file")
        return p.read_text(encoding="utf-8", errors="replace"), None
    except Exception as e:
        return "", (f"读取失败: {e}" if lang == "zh" else f"Failed to read file: {e}")


def _format_unified_line(line: str) -> str:
    s = line.strip()
    if not s:
        return ""
    if s.startswith("✓"):
        msg = s.lstrip("✓").strip()
        return f"🟢 {msg}"
    if s.startswith("⚠") or s.startswith("!"):
        msg = s.lstrip("⚠!").strip()
        return f"⚠️ {msg}"
    if s.startswith("✗") or s.startswith("×"):
        msg = s.lstrip("✗×").strip()
        return f"🔴 {msg}"
    if re.match(r"^[A-Za-z0-9_\-\.\[\]/]+\s*:\s+", s):
        parts = s.split(":", 1)
        k, v = parts[0].strip(), parts[1].strip()
        return f"• **{k}**: {v}"
    if s.startswith("- ") or s.startswith("* "):
        clean_s = s[2:].strip()
        return f"• {clean_s}" if clean_s else ""
    clean_s = s.lstrip("-•* ").strip()
    return f"• {clean_s}" if clean_s else ""


def _collapsible_panel(title: str, elements: list[dict], expanded: bool = False) -> dict:
    """标准飞书卡片可折叠面板组件（原生支持）"""
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "plain_text", "content": str(title)},
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "color": "grey",
                "size": "16px",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "8px"},
        "padding": "8px 12px 8px 12px",
        "elements": elements,
    }


def _beautify_unified(cmd: str, clean_text: str, lang: str = "zh") -> list[dict]:
    elements: list[dict] = []
    L = I18N[lang]
    if not clean_text:
        elements.append({
            "tag": "markdown",
            "content": f"**📌 {L['details']}**\n• *({'无输出返回' if lang == 'zh' else 'No output returned'})*",
        })
        return elements

    sections = re.split(r"(?m)^(?=◆ )", clean_text)
    if len(sections) <= 1:
        lines = clean_text.splitlines()
        formatted_lines = [_format_unified_line(l) for l in lines]
        formatted_lines = [l for l in formatted_lines if l]
        total_lines = len(formatted_lines)
        if total_lines <= 8:
            elements.append({
                "tag": "markdown",
                "content": f"**📌 {L['details']}**\n" + "\n".join(formatted_lines),
            })
        else:
            preview = "\n".join(formatted_lines[:3])
            full_body = "\n".join(formatted_lines[:50])
            if total_lines > 50:
                full_body += f"\n• *({'卡片展示已达50行上限，更多见归档' if lang == 'zh' else 'Card display capped at 50 lines. Full in archive'})*"
            elements.append({
                "tag": "markdown",
                "content": f"**📌 {L['preview']}**\n{preview}",
            })
            elements.append(_collapsible_panel(
                f"📄 {L['full_details']} ({total_lines} {'行' if lang == 'zh' else 'lines'})",
                [{"tag": "markdown", "content": full_body}],
                expanded=False,
            ))
        return elements

    first_panel = True
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        sec_lines = sec.splitlines()
        header_line = sec_lines[0].strip()
        title = header_line.lstrip("◆").strip() or L["details"]
        body_lines = sec_lines[1:]
        if not body_lines:
            continue
        formatted_body = [_format_unified_line(l) for l in body_lines]
        formatted_body = [l for l in formatted_body if l]
        if not formatted_body:
            continue

        has_err = any("🔴" in l or "✗" in l for l in formatted_body)
        has_warn = any("⚠️" in l or "⚠" in l for l in formatted_body)
        status_icon = "🔴" if has_err else ("⚠️" if has_warn else "🟢")

        should_expand = first_panel or has_err
        first_panel = False

        content_body = "\n".join(formatted_body[:35])
        if len(formatted_body) > 35:
            content_body += f"\n• *({'本段展示已达35项上限，完整内容见归档' if lang == 'zh' else 'Section display capped at 35 items. Full in archive'})*"

        elements.append(_collapsible_panel(
            f"{status_icon} {title} ({len(formatted_body)} {'项' if lang == 'zh' else 'items'})",
            [{"tag": "markdown", "content": content_body}],
            expanded=should_expand,
        ))
    return elements


def parse_and_beautify_output(cmd: str, raw_text: str, lang: str = "zh") -> tuple[list[dict], str]:
    clean = _strip_ansi(raw_text or "").replace("\r\n", "\n").replace("\r", "\n")
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
    saved_path = PLUGIN_DATA_DIR / f"hermes_{safe_cmd}_{ts_str}.txt"
    try:
        saved_path.write_text(clean_text, encoding="utf-8")
    except Exception as e:
        logger.warning("[command-palette] write saved output failed: %s", e)

    L = I18N[lang]
    line_count = len(clean_text.splitlines()) if clean_text else 0
    char_count = len(clean_text)
    fails = len(re.findall(r"^[ \t]*✗", clean_text, re.M))
    status_mark = f"🔴 {L['exceptions_found']}" if fails > 0 else f"🟢 {L['success']}"

    top_summary = {
        "tag": "markdown",
        "content": f"{status_mark}  |  📊 `{char_count}` {'字符' if lang == 'zh' else 'chars'}  |  📄 `{line_count}` {'行' if lang == 'zh' else 'lines'}",
    }
    sub_elements = _beautify_unified(cmd, clean_text, lang=lang)
    bottom_archive = {
        "tag": "markdown",
        "content": f"📄 **{L['archive_path']}**\n`{saved_path}`",
    }

    elements = [top_summary]
    elements.extend(sub_elements)
    elements.append(bottom_archive)
    return elements, str(saved_path)


def _cleanup_old_outputs() -> None:
    try:
        cutoff = time.time() - 7 * 86400
        if PLUGIN_DATA_DIR.exists():
            for p in PLUGIN_DATA_DIR.glob("hermes_*.txt"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════
# 3. Command Execution Engine & PTY Streaming
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
        return CmdResult(False, stderr=f"Executable not found: {argv[0]}", exit_code=127)
    except Exception as e:
        return CmdResult(False, stderr=f"Failed to start process: {e}", exit_code=126)
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
        is_ok = (proc.returncode == 0) or (argv[1:2] == ["doctor"] and bool(out.strip()))
        return CmdResult(is_ok, out or "", err or "", proc.returncode or 0, False, time.monotonic() - start)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        out, err = proc.communicate()
        return CmdResult(False, out or "", err or "", -9, True, time.monotonic() - start)


def run_subprocess_streaming(argv: list[str], *, timeout: int,
                              on_chunk: Optional[Callable[[str, float], None]] = None,
                              on_tick: Optional[Callable[[float], None]] = None) -> CmdResult:
    start = time.monotonic()
    try:
        import pty
        import select as _select
    except ImportError:
        return run_subprocess(argv, timeout=timeout)

    try:
        master, slave = pty.openpty()
    except Exception:
        return run_subprocess(argv, timeout=timeout)

    try:
        proc = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        os.close(master)
        os.close(slave)
        return CmdResult(False, stderr=f"Executable not found: {argv[0]}", exit_code=127)
    except Exception as e:
        os.close(master)
        os.close(slave)
        return CmdResult(False, stderr=f"Failed to start process: {e}", exit_code=126)

    os.close(slave)
    out_parts: list[str] = []
    last_tick: float = time.monotonic()

    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    os.close(master)
                except Exception:
                    pass
                proc.wait()
                return CmdResult(False, "".join(out_parts), "", -9, True, elapsed)

            try:
                rlist, _, _ = _select.select([master], [], [], 0.1)
            except Exception:
                break

            if rlist:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                out_parts.append(text)
                last_tick = time.monotonic()
                if on_chunk is not None:
                    try:
                        on_chunk(text, time.monotonic() - start)
                    except Exception:
                        pass
            else:
                if proc.poll() is not None:
                    try:
                        while True:
                            rlist2, _, _ = _select.select([master], [], [], 0.05)
                            if not rlist2:
                                break
                            data = os.read(master, 4096)
                            if not data:
                                break
                            text = data.decode("utf-8", errors="replace")
                            out_parts.append(text)
                            if on_chunk is not None:
                                try:
                                    on_chunk(text, time.monotonic() - start)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    break
                else:
                    now = time.monotonic()
                    if on_tick is not None and (now - last_tick >= 1.0):
                        last_tick = now
                        try:
                            on_tick(time.monotonic() - start)
                        except Exception:
                            pass
    finally:
        try:
            os.close(master)
        except Exception:
            pass

    proc.wait()
    total_elapsed = time.monotonic() - start
    raw_out = "".join(out_parts)
    is_ok = (proc.returncode == 0) or (argv[1:2] == ["doctor"] and bool(raw_out.strip()))
    return CmdResult(is_ok, raw_out, "", proc.returncode or 0, False, total_elapsed)


def run_lark(args: list[str], *, timeout: int = 20) -> CmdResult:
    return run_subprocess(["lark-cli", *args], timeout=timeout)


def _check_lark_cli() -> bool:
    return shutil.which("lark-cli") is not None


# ════════════════════════════════════════════════════════════════════════════
# 4. Decoupled Command Registry & Metadata (ZH & EN Separate)
# ════════════════════════════════════════════════════════════════════════════

CMD_EMOJI: dict[str, str] = {
    # session
    "/new":      "🆕",
    "/stop":     "⏹",
    "/status":   "📊",
    "/context":  "📐",
    "/usage":    "💳",
    "/sessions": "🗂",
    "/undo":     "↩️",
    "/retry":    "🔁",
    "/compress": "🗜",
    "/background":"🚀",
    # config
    "/model":        "🔄",
    "/reasoning":    "🧠",
    "/personality":  "🎭",
    "/verbose":      "🔊",
    "/yolo":         "⚡",
    "/fast":         "🚀",
    "/codex-runtime":"🖥",
    # tools
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
    # info
    "/version": "ℹ️",
    "/profile": "👤",
    "/config":  "⚙️",
    "/whoami":  "🔑",
}

CLI_CMDS: dict[str, dict] = {
    "/status":   {"label_zh": "系统状态", "label_en": "System Status", "cli": ["hermes", "status"]},
    "/context":  {"label_zh": "上下文分析", "label_en": "Context Size", "cli": ["hermes", "prompt-size"]},
    "/usage":    {"label_zh": "配额用量", "label_en": "Account Usage", "cli": ["hermes", "usage"]},
    "/sessions": {"label_zh": "历史会话", "label_en": "Sessions List", "cli": ["hermes", "sessions", "list", "--limit", "10"]},
    "/diff":     {"label_zh": "代码差异", "label_en": "Git Diff", "cli": ["git", "-C", str(INSTALL_PATH), "diff", "--stat"], "timeout": 45},
    "/doctor":   {"label_zh": "健康诊断", "label_en": "Doctor Diagnostics", "cli": ["hermes", "doctor"], "timeout": 120},
    "/debug":    {"label_zh": "调试摘要", "label_en": "Debug Share", "cli": ["hermes", "debug", "share", "--local"], "timeout": 45},
    "/security": {"label_zh": "安全审计", "label_en": "Security Audit", "cli": ["hermes", "security", "audit"], "timeout": 60},
    "/logs":     {"label_zh": "网关日志", "label_en": "Gateway Logs", "cli": ["hermes", "logs", "gateway", "-n", "40"]},
    "/cron":     {"label_zh": "定时任务", "label_en": "Cron Jobs", "cli": ["hermes", "cron", "list"]},
    "/plugins":  {"label_zh": "插件列表", "label_en": "Plugin List", "cli": ["hermes", "plugins", "list"], "timeout": 45},
    "/skills":   {"label_zh": "技能库",   "label_en": "Skills Catalog", "cli": ["hermes", "skills", "list"]},
    "/bundles":  {"label_zh": "技能包",   "label_en": "Skill Bundles", "cli": ["hermes", "bundles"]},
    "/memory":   {"label_zh": "记忆系统", "label_en": "Memory Status", "cli": ["hermes", "memory"]},
    "/version":  {"label_zh": "框架版本", "label_en": "Version Info", "cli": ["hermes", "--version"]},
    "/profile":  {"label_zh": "配置详情", "label_en": "Profile Info", "cli": ["hermes", "profile"]},
    "/config":   {"label_zh": "配置概览", "label_en": "Config Show", "cli": ["hermes", "config", "show"]},
    "/whoami":   {"label_zh": "身份凭证", "label_en": "Credentials", "cli": ["hermes", "auth", "list"]},
}

CONFIG_CMDS: dict[str, dict] = {
    "/model":         {"label_zh": "模型切换", "label_en": "Switch Model", "kind": "picker"},
    "/reasoning":     {"label_zh": "推理力度", "label_en": "Reasoning Effort", "options": ["none", "minimal", "low", "medium", "high"], "key": "agent.reasoning_effort"},
    "/personality":   {"label_zh": "AI人格",   "label_en": "Personality", "options": ["technical", "concise", "creative", "helpful", "hype", "noir", "philosopher", "teacher"], "key": "agent.personality"},
    "/verbose":       {"label_zh": "日志级别", "label_en": "Verbose Level", "options": ["off", "tools", "all"], "key": "agent.verbose"},
    "/yolo":          {"label_zh": "极速免审", "label_en": "YOLO Mode", "options": ["true", "false"], "key": "agent.yolo"},
    "/fast":          {"label_zh": "高速模式", "label_en": "Fast Mode", "options": ["true", "false"], "key": "agent.fast"},
    "/codex-runtime": {"label_zh": "执行环境", "label_en": "Codex Runtime", "options": ["auto", "app", "cli"], "key": "agent.codex_runtime"},
}

SESSION_ACTS: dict[str, dict] = {
    "/new":      {"label_zh": "新建会话", "label_en": "New Session", "danger": True},
    "/stop":     {"label_zh": "强制停止", "label_en": "Force Stop", "danger": True},
    "/undo":     {"label_zh": "撤销回复", "label_en": "Undo Turn", "danger": False},
    "/retry":    {"label_zh": "重试执行", "label_en": "Retry Turn", "danger": False},
    "/compress": {"label_zh": "压缩会话", "label_en": "Compress Context", "danger": False},
}

GUIDE_CMDS: dict[str, dict] = {
    "/background": {"label_zh": "后台任务", "label_en": "Background Task", "template": "/background <prompt / 目标>", "desc_zh": "在独立后台进程中运行长耗时任务，不占用当前对话流。", "desc_en": "Run long task in a detached subagent without blocking chat."},
    "/steer":      {"label_zh": "动态干预", "label_en": "Steer Command", "template": "/steer <instruction / 指令>", "desc_zh": "在 Agent 执行工具调用的间隙动态插入控制指令，修正方向。", "desc_en": "Inject guidance mid-turn during agent execution."},
    "/goal":       {"label_zh": "目标管理", "label_en": "Goal Setting", "template": "/goal set <long-term goal>", "desc_zh": "设定跨多轮次生效的长期执行目标。", "desc_en": "Set persistent goals across multiple turns."},
}

CATEGORIES_ZH = [
    ("session", "💬", "会话管理", "blue", ["/new", "/stop", "/status", "/context", "/usage", "/sessions", "/undo", "/retry", "/compress", "/background"]),
    ("config", "⚙️", "系统配置", "purple", ["/model", "/reasoning", "/personality", "/verbose", "/yolo", "/fast", "/codex-runtime"]),
    ("tools", "🔧", "工具研发", "green", ["/diff", "/doctor", "/security", "/debug", "/logs", "/cron", "/plugins", "/skills", "/bundles", "/memory"]),
    ("info", "ℹ️", "系统信息", "grey", ["/version", "/profile", "/config", "/whoami"]),
]

CATEGORIES_EN = [
    ("session", "💬", "Session", "blue", ["/new", "/stop", "/status", "/context", "/usage", "/sessions", "/undo", "/retry", "/compress", "/background"]),
    ("config", "⚙️", "Config", "purple", ["/model", "/reasoning", "/personality", "/verbose", "/yolo", "/fast", "/codex-runtime"]),
    ("tools", "🔧", "Tools", "green", ["/diff", "/doctor", "/security", "/debug", "/logs", "/cron", "/plugins", "/skills", "/bundles", "/memory"]),
    ("info", "ℹ️", "Info", "grey", ["/version", "/profile", "/config", "/whoami"]),
]

_CMD_META: dict[str, dict] = {}
for c in [CLI_CMDS, CONFIG_CMDS, SESSION_ACTS, GUIDE_CMDS]:
    _CMD_META.update(c)

CMD_PARENT_CATEGORY: dict[str, str] = {}
for cat_key, _, _, _, cmds in CATEGORIES_ZH:
    for c in cmds:
        CMD_PARENT_CATEGORY[c] = f"/card/{cat_key}"


def _cmd_label(cmd: str, lang: str = "zh") -> str:
    meta = _CMD_META.get(cmd, {})
    return meta.get(f"label_{lang}") or meta.get("label_zh") or cmd


# ════════════════════════════════════════════════════════════════════════════
# 5. State Management & Owner-Only RBAC Security
# ════════════════════════════════════════════════════════════════════════════

_state_lock = threading.Lock()
_selected_provider: dict[str, str] = {}
_pending_model: dict[str, str] = {}
_chat_card_map: dict[str, str] = {}
_exec_tokens: dict[str, dict] = {}
_cmd_cooldown: dict[str, float] = {}
_cooldown_check_counter: int = 0


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
        if not rec:
            return None
        if rec["exp"] < time.time():
            return None
        if rec["open_id"] and open_id and rec["open_id"] != open_id:
            return None
        return rec


def _get_owner_open_ids(adapter: Any = None) -> set[str]:
    owners = set()
    for env_key in ("FEISHU_ADMINS", "FEISHU_OWNER_ID"):
        val = os.environ.get(env_key, "").strip()
        if val:
            for item in val.split(","):
                clean = item.strip()
                if clean and clean != "*":  # 严禁通配符，防止误配置导致全员可执行
                    owners.add(clean)
    if adapter is not None:
        adapter_admins = getattr(adapter, "_admins", None)
        if isinstance(adapter_admins, (list, set, tuple)):
            for a in adapter_admins:
                clean = str(a).strip()
                if clean and clean != "*":
                    owners.add(clean)
    owner_file = PLUGIN_DATA_DIR / "owner.json"
    try:
        if owner_file.exists():
            data = json.loads(owner_file.read_text(encoding="utf-8"))
            saved = str(data.get("owner_id", "")).strip()
            if saved and saved != "*":
                owners.add(saved)
    except Exception:
        pass
    return owners


def _record_default_owner_if_empty(open_id: str, is_p2p: bool = False) -> None:
    """当未配置任何 Owner 时，如果用户在私聊会话中与机器人交互，自动认领为默认 Owner。群聊中绝不自动认领。"""
    if not open_id or not is_p2p:
        return
    owners = _get_owner_open_ids()
    if owners:
        return
    try:
        owner_file = PLUGIN_DATA_DIR / "owner.json"
        owner_file.write_text(json.dumps({
            "owner_id": open_id,
            "claimed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": "Automatically claimed in private P2P session on first interaction."
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        owner_file.chmod(0o600)
        logger.info("[command-palette] Default owner automatically claimed: %s", open_id)
    except Exception as e:
        logger.warning("[command-palette] failed to save default owner: %s", e)


def _is_owner(open_id: str, adapter: Any = None) -> bool:
    """严格 Fail-Closed 所有者鉴权：仅实例所有者可执行命令与写操作"""
    normalized = str(open_id or "").strip()
    if not normalized or normalized == "*":
        return False
    owners = _get_owner_open_ids(adapter)
    if not owners:
        return False
    return normalized in owners


_is_admin = _is_owner


_STATIC_OAUTH_MODELS: dict[str, list[str]] = {
    "openai-codex": [
        "openai-codex/gpt-5.3-codex",
        "openai-codex/gpt-5-codex",
        "openai-codex/gpt-5-codex-mini",
        "openai-codex/gpt-5.1-codex",
        "openai-codex/gpt-5.1-codex-mini",
        "openai-codex/gpt-5.2-codex",
    ],
    "xai-oauth": [
        "xai/grok-4",
        "xai/grok-4.5",
        "xai/grok-4.5-mini",
        "xai/grok-4.5-vision",
        "xai/grok-code",
    ],
    "copilot": [
        "copilot/claude-sonnet-4.6",
        "copilot/gpt-5.3-codex",
        "copilot/gpt-4o",
    ],
    "minimax-oauth": [
        "minimax/MiniMax-Text-01",
    ],
}


def get_hermes_catalog_and_status():
    cfg = {}
    auth_data = {}
    try:
        if CONFIG_PATH.exists():
            cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("[command-palette] read config.yaml failed: %s", e)

    try:
        if AUTH_PATH.exists():
            auth_data = json.loads(AUTH_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("[command-palette] read auth.json failed: %s", e)

    catalog: dict[str, list[str]] = {}
    for cp in (cfg.get("custom_providers") or []):
        name = cp.get("name")
        models = cp.get("models") or []
        if name and models:
            catalog[name] = list(models)

    providers_block = cfg.get("providers") or {}
    for pname, pinfo in providers_block.items():
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

    norm_cur = cur_provider.replace("custom:", "")
    if norm_cur in catalog:
        ordered[norm_cur] = catalog[norm_cur]
    for k in sorted(catalog.keys()):
        if k not in ordered:
            ordered[k] = catalog[k]

    return ordered, cur_provider, cur_model, base_url, profile


def _switch_hermes_model(tgt_model: str, prov: str) -> tuple[bool, str]:
    catalog, cur_p, cur_m, _, _ = get_hermes_catalog_and_status()
    if prov not in catalog:
        return False, f"Provider '{prov}' not in catalog"
    if tgt_model not in catalog[prov]:
        return False, f"Model '{tgt_model}' not found in provider '{prov}'"

    custom_names = set()
    try:
        if CONFIG_PATH.exists():
            cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            for cp in (cfg.get("custom_providers") or []):
                if cp.get("name"):
                    custom_names.add(cp["name"])
            for k in (cfg.get("providers") or {}).get("custom", {}).keys():
                custom_names.add(k)
    except Exception:
        pass

    prov_str = f"custom:{prov}" if (prov in custom_names or prov == "custom") else prov
    r1 = run_subprocess(["hermes", "config", "set", "model.provider", prov_str], timeout=10)
    if not r1.ok:
        return False, f"Failed to set model.provider: {r1.stderr or r1.stdout}"
    r2 = run_subprocess(["hermes", "config", "set", "model.default", tgt_model], timeout=10)
    if not r2.ok:
        return False, f"Failed to set model.default: {r2.stderr or r2.stdout}"
    return True, ""


# ════════════════════════════════════════════════════════════════════════════
# 6. Card Builders (Pure Chinese / Pure English with Zero Truncation)
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
        "header": {
            "title": _pt(title),
            "template": template,
        },
        "elements": elements,
    }


def _nav_row_for_subpage(lang: str = "zh") -> dict:
    L = I18N[lang]
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
                "elements": [_nav_btn(L["back"], "/card/root", "default")],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "horizontal_align": "left",
                "elements": [_nav_btn(L["home"], "/card/root", "default")],
            },
        ],
    }


def _nav_row_for_deep_page(parent_target: str, extra_actions: list | None = None, lang: str = "zh") -> dict:
    L = I18N[lang]

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
        _nav_btn(L["back"], parent_target, "default"),
        _nav_btn(L["home"], "/card/root", "default"),
    ]
    rows.append(_make_column_set(nav_btns))

    if len(rows) == 1:
        return rows[0]
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "center", "elements": rows},
        ],
    }


def _cc_category_grid(buttons: list[dict]) -> list[dict]:
    elements = []
    for i in range(0, len(buttons), 2):
        pair = buttons[i:i + 2]
        cols = []
        for b in pair:
            cols.append({
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "horizontal_align": "left",
                "elements": [b],
            })
        if len(cols) == 1:
            cols.append({
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "elements": [],
            })
        elements.append({
            "tag": "column_set",
            "flex_mode": "bisect",
            "columns": cols,
        })
    return elements


def _cc_command_row(text: str, button_text: str, button_type: str, value: dict) -> dict:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 5,
                "vertical_align": "center",
                "horizontal_align": "left",
                "elements": [{
                    "tag": "markdown",
                    "content": text,
                }],
            },
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "center",
                "horizontal_align": "right",
                "elements": [{
                    "tag": "button",
                    "text": _pt(button_text),
                    "type": button_type,
                    "value": value,
                }],
            },
        ],
    }


def _cc_command_grid(cmds: list[str], lang: str = "zh") -> list[dict]:
    elements = []
    L = I18N[lang]
    for cmd in cmds:
        meta = _CMD_META.get(cmd, {})
        label = _cmd_label(cmd, lang)
        emoji = CMD_EMOJI.get(cmd, "▶")
        btn_type = "danger" if meta.get("danger") else "default"
        elements.append(_cc_command_row(f"{emoji} **{label}** `{cmd}`", L["run"], btn_type, {"action": f"cmd:{cmd}"}))
    return elements


def _root_card(skey: str = "") -> dict:
    lang = _get_lang(skey)
    L = I18N[lang]
    _, cur_p, cur_m, _, _ = get_hermes_catalog_and_status()
    short_model = cur_m.split("/")[-1] if "/" in cur_m else cur_m
    reasoning = "medium"
    try:
        if CONFIG_PATH.exists():
            c = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            reasoning = c.get("agent", {}).get("reasoning_effort", "medium")
    except Exception:
        pass

    cat_list = CATEGORIES_ZH if lang == "zh" else CATEGORIES_EN
    cat_btns = []
    for k, icon, title, _, _ in cat_list:
        cat_btns.append(_btn(f"{icon} {title}", "default", value={"action": f"nav:/card/{k}"}))

    # 顶层状态栏与语言切换按钮排版（无缝集成）
    target_switch_lang = "en" if lang == "zh" else "zh"
    status_header_row = {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 5,
                "vertical_align": "center",
                "horizontal_align": "left",
                "elements": [
                    {"tag": "markdown", "content": f"🟢 **{short_model}** · {reasoning}"},
                ],
            },
            {
                "tag": "column",
                "width": "auto",
                "vertical_align": "center",
                "horizontal_align": "right",
                "elements": [
                    _btn(L["switch_lang_btn"], "default", value={"action": f"set_lang:{target_switch_lang}"}),
                ],
            },
        ],
    }

    elements = [status_header_row]
    elements.extend(_cc_category_grid(cat_btns))
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": L["quick_actions"]})

    quick_actions = [
        (f"🔄 **{L['title'][:2] if lang=='zh' else 'Model'}**" if False else (
            "🔄 **模型切换** model" if lang == "zh" else "🔄 **Switch Model**"
        ), "▶", "primary", {"action": "nav:/card/model"}),
        ("📊 **系统状态** status" if lang == "zh" else "📊 **System Status**", "▶", "default", {"action": "cmd:/status"}),
        ("🆕 **新建会话** new" if lang == "zh" else "🆕 **New Session**", "▶", "default", {"action": "cmd:/new"}),
        ("⏹ **强制停止** stop" if lang == "zh" else "⏹ **Force Stop**", "▶", "danger", {"action": "cmd:/stop"}),
    ]

    for label_md, btn_label, btn_typ, act_val in quick_actions:
        elements.append(_cc_command_row(label_md, btn_label, btn_typ, act_val))

    return _card(L["title"], "blue", elements)


def _cat_card(ck: str, icon: str, title: str, color: str, cmds: list, lang: str = "zh") -> dict:
    elements = _cc_command_grid(cmds, lang=lang)
    elements.append({"tag": "hr"})
    elements.append(_nav_row_for_subpage(lang=lang))
    return _card(f"{icon} {title}", color, elements)


def _build_model_card(skey: str, provider: str = "", model: str = "",
                      status_msg: str = "", ok: bool = True) -> dict:
    lang = _get_lang(skey)
    L = I18N[lang]
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

    action_elements = [
        {
            "tag": "select_static",
            "placeholder": _pt(f"Provider: {provider}"),
            "value": {"action": "select_provider"},
            "options": prov_opts,
            "initial_option": provider if provider in catalog else None,
        }
    ]

    if len(models) > 50:
        opts_part1 = [{"text": _pt(m), "value": m} for m in models[:50]]
        opts_part2 = [{"text": _pt(m), "value": m} for m in models[50:100]]
        action_elements.append({
            "tag": "select_static",
            "placeholder": _pt(f"Models 1-50 ({model.split('/')[-1]})"),
            "value": {"action": "select_model", "provider": provider},
            "options": opts_part1,
            "initial_option": model if model in [o["value"] for o in opts_part1] else None,
        })
        action_elements.append({
            "tag": "select_static",
            "placeholder": _pt(f"Models 51+ ({len(models)} total)"),
            "value": {"action": "select_model", "provider": provider},
            "options": opts_part2,
            "initial_option": model if model in [o["value"] for o in opts_part2] else None,
        })
    else:
        model_opts = [{"text": _pt(m), "value": m} for m in models[:100]]
        action_elements.append({
            "tag": "select_static",
            "placeholder": _pt(f"Model: {model.split('/')[-1]}"),
            "value": {"action": "select_model", "provider": provider},
            "options": model_opts,
            "initial_option": model if model in models else None,
        })

    elements = [
        {"tag": "markdown", "content": f"🎯 **{L['active_model']}**: `{cur_m}`\n🔌 **{L['active_prov']}**: `{cur_p}`"},
        {"tag": "hr"},
        {"tag": "markdown", "content": f"👇 **{L['select_prov_model']}**"},
        {"tag": "action", "actions": action_elements},
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                _btn(L["confirm_switch_btn"], "primary", value={"action": f"confirm_switch:{provider}:{model}"}),
            ],
        },
    ]

    if status_msg:
        elements.insert(0, {"tag": "markdown", "content": status_msg})

    elements.append({"tag": "hr"})
    elements.append(_nav_row_for_deep_page("/card/config", lang=lang))
    card_title = "🔄 模型切换" if lang == "zh" else "🔄 Switch Model"
    return _card(card_title, ("green" if ok else "red") if status_msg else "purple", elements)


def _build_options_card(cmd: str, skey: str = "") -> dict:
    lang = _get_lang(skey)
    L = I18N[lang]
    meta = CONFIG_CMDS.get(cmd, {})
    opts = meta.get("options") or []
    cfg_key = meta.get("key", f"agent.{cmd.lstrip('/')}")

    cur = "unknown"
    try:
        if CONFIG_PATH.exists():
            cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            val = cfg
            for part in cfg_key.split("."):
                val = val.get(part, {}) if isinstance(val, dict) else {}
            cur = str(val) if val != {} else "default"
    except Exception:
        pass

    elements = [
        {"tag": "markdown", "content": f"{L['cur_val']}: `{cur}`"},
    ]
    for opt in opts:
        is_cur = (str(opt).lower() == cur.lower())
        btn_type = "primary" if is_cur else "default"
        btn_text = L["active_opt"] if is_cur else L["select_opt"]
        display_text = f"● **{opt}**" if is_cur else f"**{opt}**"
        elements.append(_cc_command_row(
            display_text, btn_text, btn_type,
            {"action": f"set_cfg:{cmd}:{opt}"}
        ))

    elements.append({"tag": "hr"})
    elements.append(_nav_row_for_deep_page(CMD_PARENT_CATEGORY.get(cmd, "/card/config"), lang=lang))
    header_title = f"⚙️ {meta.get(f'label_{lang}', cmd)}"
    return _card(header_title, "purple", elements)


def _build_guide_card(cmd: str, skey: str = "") -> dict:
    lang = _get_lang(skey)
    L = I18N[lang]
    meta = GUIDE_CMDS.get(cmd, {})
    template = meta.get("template", cmd)
    desc = meta.get(f"desc_{lang}") or meta.get("desc_zh", "")
    elements = [
        {"tag": "markdown", "content": f"📖 **{L['guide_desc']}**\n{desc}"},
        {"tag": "markdown", "content": f"📋 **{L['guide_tmpl']}**\n```{template}```"},
        {"tag": "markdown", "content": L["guide_tip"]},
        {"tag": "hr"},
        _nav_row_for_deep_page(CMD_PARENT_CATEGORY.get(cmd, "/card/session"), lang=lang),
    ]
    header_title = f"💡 {meta.get(f'label_{lang}', cmd)}"
    return _card(header_title, "blue", elements)


def _build_exec_confirm_card(cmd: str, args: str, token: str, skey: str = "") -> dict:
    lang = _get_lang(skey)
    L = I18N[lang]
    meta = _CMD_META.get(cmd, {})
    full = f"{cmd} {args}".strip()
    elements = [
        {"tag": "markdown", "content": (
            f"⚠️ **{L['confirm_high_risk']}**: `{full}`\n"
            f"{_cmd_label(cmd, lang)} — {L['confirm_tip']}")},
    ]
    confirm_btns = [
        _btn(L["confirm_btn"], "danger", value={"action": f"exec_confirm:{token}"}),
        _btn(L["cancel_btn"], "default", value={"action": "exec_cancel"}),
        _nav_btn(L["back"], CMD_PARENT_CATEGORY.get(cmd, "/card/session")),
        _nav_btn(L["home"], "/card/root"),
    ]
    elements.extend(_cc_category_grid(confirm_btns))
    header_title = f"⚠️ {L['confirm_high_risk']}"
    return _card(header_title, "red", elements)


def _build_streaming_card(cmd: str, elapsed: float, recent_lines: list[str], is_heartbeat: bool = False, lang: str = "zh") -> dict:
    L = I18N[lang]
    label = _cmd_label(cmd, lang)
    header_content = f"⚡ {L['executing']}: {label} ({cmd})  |  ⏱️ {elapsed:.1f}s"
    if recent_lines:
        log_block = "```text\n" + "\n".join(recent_lines) + "\n```"
        subtitle = f"{L['live_stream']} · {'已耗时' if lang == 'zh' else 'Elapsed'} `{elapsed:.1f}s`"
    elif is_heartbeat:
        log_block = f"⏳ *{'后台深度检测中，暂无终端输出...' if lang == 'zh' else 'Deep diagnostics in progress, waiting for output...'}*"
        subtitle = f"{L['deep_diag']} · {'已耗时' if lang == 'zh' else 'Elapsed'} `{elapsed:.1f}s` ({L['process_active']})"
    else:
        log_block = f"⏳ *{'等待终端首包输出...' if lang == 'zh' else 'Waiting for initial output...'}*"
        subtitle = f"{L['init_process']} · {'已耗时' if lang == 'zh' else 'Elapsed'} `{elapsed:.1f}s`"
    elements = [
        {"tag": "markdown", "content": subtitle},
        {"tag": "markdown", "content": log_block},
        {"tag": "markdown", "content": L["stream_note"]},
        _nav_row_for_deep_page(CMD_PARENT_CATEGORY.get(cmd, "/card/root"), lang=lang),
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": header_content}, "template": "blue"},
        "elements": elements,
    }


def _build_running_card(cmd: str, skey: str = "") -> dict:
    lang = _get_lang(skey)
    L = I18N[lang]
    label = _cmd_label(cmd, lang)
    elements = [
        {"tag": "markdown",
         "content": f"{L['executing']} `{cmd}` ({label}){L['executing_tip']}"},
        _nav_row_for_deep_page(CMD_PARENT_CATEGORY.get(cmd, "/card/root"), lang=lang),
    ]
    return _card(f"⚡ {label}", "yellow", elements)


def _build_copy_card(cmd: str, saved_path: str, lang: str = "zh") -> dict:
    L = I18N[lang]
    if not cmd and saved_path:
        cmd = _infer_cmd_from_saved_path(saved_path)
    label = _cmd_label(cmd, lang) if cmd else ("输出明细" if lang == "zh" else "Output")

    content, err = _read_saved_output(saved_path, lang=lang)
    if err:
        body = f"无法读取归档内容 / Error reading archive: {err}"
    else:
        truncated = False
        display = content
        if len(display) > 18000:
            display = display[:18000]
            truncated = True
        safe_display = display.replace("```", r"\`\`\`")
        suffix = f"\n[{'卡片展示已截断，完整内容见归档' if lang == 'zh' else 'Truncated in preview. Full archive in file'}]: {saved_path}" if truncated else ""
        body = f"```text\n{safe_display}\n```{suffix}"
        _test_card = {"tag": "markdown", "content": body}
        while len(json.dumps(_test_card, ensure_ascii=False).encode("utf-8")) > 26000 and len(safe_display) > 200:
            safe_display = safe_display[:int(len(safe_display) * 0.85)]
            truncated = True
            body = f"```text\n{safe_display}\n```\n[{'卡片尺寸超限已收缩' if lang == 'zh' else 'Card size limit reached'}]: {saved_path}"
            _test_card = {"tag": "markdown", "content": body}

    elements = [
        {"tag": "markdown", "content": f"📋 **{L['full_details']}** · `{cmd}`"},
        {"tag": "markdown", "content": body},
        {"tag": "hr"},
        {"tag": "markdown", "content": f"📄 **{L['archive_path']}**\n`{saved_path}`"},
        _nav_row_for_deep_page(
            CMD_PARENT_CATEGORY.get(cmd, "/card/root"),
            extra_actions=[
                _btn("↩ 返回报告" if lang == "zh" else "↩ Report", "default", value={"action": f"show_result:{saved_path}"}),
                _btn(L["retry"], "primary", value={"action": f"cmd:{cmd}"}),
            ],
            lang=lang,
        ),
    ]
    return _card(f"📋 {label}", "grey", elements)


def _result_status_from_text(text: str) -> bool:
    clean = _strip_ansi(text or "")
    if re.search(r"^[ \t]*✗", clean, re.M):
        return False
    return True


def _build_beautiful_result_card(cmd: str, raw_output: str, elapsed: float = 0.0,
                                 ok: bool = True, saved_path: str = "",
                                 timed_out: bool = False,
                                 status_text: str = "",
                                 lang: str = "zh") -> dict:
    L = I18N[lang]
    label = _cmd_label(cmd, lang)

    if timed_out:
        timeout_hint = f"⚠️ {'命令执行超时（已捕获部分输出）' if lang == 'zh' else 'Execution timed out (Partial output captured)'}"
        raw_output = f"{timeout_hint}\n\n{raw_output}"

    sub_elements, _saved = parse_and_beautify_output(cmd, raw_output, lang=lang)
    if not saved_path:
        saved_path = _saved

    is_no_data = False
    lower_out = (raw_output or "").lower()
    if "no account usage available" in lower_out or "no credential is configured" in lower_out:
        is_no_data = True

    if is_no_data:
        template = "grey"
        title_text = f"ℹ️ **{label} ({cmd}) {L['no_data']}**  |  ⏱️ {elapsed:.1f}s"
    elif timed_out:
        template = "orange"
        title_text = f"⚠️ **{label} ({cmd}) {L['timed_out']}**  |  ⏱️ {elapsed:.1f}s"
    elif ok:
        template = "green"
        status_disp = status_text or L["success"]
        title_text = f"✅ **{label} ({cmd}) {status_disp}**  |  ⏱️ {elapsed:.1f}s"
    else:
        template = "red"
        status_disp = status_text or L["failed"]
        title_text = f"❌ **{label} ({cmd}) {status_disp}**  |  ⏱️ {elapsed:.1f}s"

    elements = list(sub_elements)
    elements.append({"tag": "hr"})

    parent_cat = CMD_PARENT_CATEGORY.get(cmd, "/card/root")
    extra_actions = []
    if saved_path:
        extra_actions.append(_btn(L["copy"], "default", value={"action": f"copy_output:{saved_path}"}))
    extra_actions.append(_btn(L["retry"], "primary", value={"action": f"cmd:{cmd}"}))

    elements.append(_nav_row_for_deep_page(parent_cat, extra_actions=extra_actions, lang=lang))
    return _card(title_text, template, elements)


def _nav(action: str, skey: str = "") -> dict:
    lang = _get_lang(skey)
    sub = action[4:].strip()
    if sub in ("/card/root", "root", "/card"):
        return _root_card(skey)
    if sub in ("/card/model", "model"):
        with _state_lock:
            _selected_provider.pop(skey, None)
            _pending_model.pop(skey, None)
        return _build_model_card(skey)
    cat_list = CATEGORIES_ZH if lang == "zh" else CATEGORIES_EN
    for k, icon, title, color, cmds in cat_list:
        if sub in (f"/card/{k}", k):
            return _cat_card(k, icon, title, color, cmds, lang=lang)
    return _root_card(skey)


# ════════════════════════════════════════════════════════════════════════════
# 7. Messaging Egress via lark-cli & Asynchronous Dispatch
# ════════════════════════════════════════════════════════════════════════════

def _patch_card(mid: str, card: dict) -> bool:
    if not mid:
        return False
    if not _check_lark_cli():
        logger.error("[command-palette] lark-cli not found in PATH")
        return False
    data = json.dumps({"content": json.dumps(card, ensure_ascii=False)}, ensure_ascii=False)
    res = run_lark(["im", "messages", "patch", "--as", "bot", "--message-id", mid,
                    "--data", data], timeout=25)
    if not res.ok:
        logger.error("[command-palette] patch failed: mid=%s exit=%s stderr=%s", mid, res.exit_code, res.stderr)
    return res.ok


def _send_root_card(chat_id: str, card: dict) -> bool:
    if not _check_lark_cli():
        logger.error("[command-palette] lark-cli not found in PATH")
        return False
    res = run_lark(["im", "+messages-send", "--as", "bot", "--chat-id", chat_id,
                    "--msg-type", "interactive", "--content", json.dumps(card, ensure_ascii=False)], timeout=25)
    if res.ok:
        try:
            mid = json.loads(res.stdout).get("data", {}).get("message_id")
            if mid:
                _chat_card_map[chat_id] = mid
                if len(_chat_card_map) > 500:
                    del _chat_card_map[next(iter(_chat_card_map))]
        except Exception:
            pass
    else:
        logger.error("[command-palette] send failed: exit=%s stderr=%s stdout=%s",
                     res.exit_code, (res.stderr or "").strip(), (res.stdout or "").strip())
    return res.ok


def _async(fn: Callable, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def _execute_cli_async(cmd: str, mid: str, skey: str = "") -> None:
    lang = _get_lang(skey)
    meta = CLI_CMDS.get(cmd, {})
    argv = meta.get("cli") or ["hermes", cmd.lstrip("/")]
    cmd_timeout = meta.get("timeout", _DEFAULT_CMD_TIMEOUT)

    if not STREAMING_ENABLED or not mid:
        t0 = time.monotonic()
        res = run_subprocess(argv, timeout=cmd_timeout)
        elapsed = time.monotonic() - t0
        output_text = res.stdout if res.ok else (res.stderr or res.stdout or f"exit code {res.exit_code}")
        card = _build_beautiful_result_card(cmd, output_text, elapsed=elapsed, ok=res.ok, timed_out=res.timed_out, lang=lang)
        if mid:
            _patch_card(mid, card)
        return

    t0 = time.monotonic()
    last_patch: list[float] = [0.0]
    chunks: list[str] = []
    has_output: list[bool] = [False]

    def _update_stream_card(elapsed: float, is_heartbeat: bool = False) -> None:
        now = time.monotonic()
        if now - last_patch[0] < STREAM_PATCH_INTERVAL:
            return
        last_patch[0] = now
        raw_all = "".join(chunks)
        clean = _strip_ansi(raw_all)
        lines = [line.strip() for line in clean.splitlines() if line.strip()][-8:]
        lines = [line[:120] for line in lines]
        stream_card = _build_streaming_card(cmd, elapsed, lines, is_heartbeat=is_heartbeat, lang=lang)
        try:
            _patch_card(mid, stream_card)
        except Exception as e:
            logger.debug("[command-palette] stream patch failed: %s", e)

    def on_chunk(text: str, elapsed: float) -> None:
        chunks.append(text)
        has_output[0] = True
        _update_stream_card(elapsed, is_heartbeat=False)

    def on_tick(elapsed: float) -> None:
        _update_stream_card(elapsed, is_heartbeat=not has_output[0])

    res = run_subprocess_streaming(argv, timeout=cmd_timeout, on_chunk=on_chunk, on_tick=on_tick)
    elapsed = time.monotonic() - t0
    output_text = res.stdout if res.ok else (res.stderr or res.stdout or f"exit code {res.exit_code}")

    if res.timed_out and output_text:
        safe_cmd = cmd.strip("/").replace(" ", "_") or "output"
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        partial_path = PLUGIN_DATA_DIR / f"hermes_{safe_cmd}_timeout_{ts_str}.txt"
        try:
            partial_path.write_text(_strip_ansi(output_text), encoding="utf-8")
        except Exception:
            pass

    card = _build_beautiful_result_card(cmd, output_text, elapsed=elapsed, ok=res.ok, timed_out=res.timed_out, lang=lang)
    if mid:
        _patch_card(mid, card)


# ════════════════════════════════════════════════════════════════════════════
# 8. Unified Action Dispatcher (Language-Aware & Owner-Gated)
# ════════════════════════════════════════════════════════════════════════════

def dispatch_palette_action(action_value: dict, cid: str, mid: str, open_id: str,
                            adapter: Any = None) -> tuple[Optional[dict], str, str]:
    ac = action_value.get("action", "")
    opt = action_value.get("option", "")
    sk = _skey(cid, open_id)
    lang = _get_lang(sk)
    L = I18N[lang]

    # ── 0. 语言切换动作 (全员放行，原地重绘) ──
    if ac.startswith("set_lang:"):
        target_lang = ac.split(":", 1)[1].strip()
        _set_lang(sk, target_lang)
        new_card = _root_card(sk)
        new_toast = I18N[target_lang]["switch_lang_toast"]
        return new_card, new_toast, "info"

    # ── 1. 下拉选择（暂存状态，全员放行） ──
    if ac == "select_provider" and opt:
        with _state_lock:
            _selected_provider[sk] = opt
            _pending_model.pop(sk, None)
        card = _build_model_card(sk, provider=opt)
        return card, f"{'已选通道' if lang=='zh' else 'Provider'}: {opt}", "info"

    if ac == "select_model" and opt:
        prov = action_value.get("provider", _selected_provider.get(sk, ""))
        with _state_lock:
            _pending_model[sk] = opt
            if prov:
                _selected_provider[sk] = prov
        card = _build_model_card(sk, provider=prov, model=opt)
        return card, f"{'已选模型' if lang=='zh' else 'Model'}: {opt.split('/')[-1]}", "info"

    # ── 2. 确认切换模型 (写操作：仅限所有者！) ──
    if ac.startswith("confirm_switch:"):
        if not _is_owner(open_id, adapter):
            return None, L["only_owner"], "error"

        parts = ac.split(":", 2)
        prov = parts[1] if len(parts) > 1 else ""
        tgt = parts[2] if len(parts) > 2 else _pending_model.get(sk, "")
        if not tgt:
            return None, ("请先选择目标模型" if lang == "zh" else "Please select target model first"), "warning"

        ok, err = _switch_hermes_model(tgt, prov)
        if ok:
            with _state_lock:
                _selected_provider.pop(sk, None)
                _pending_model.pop(sk, None)
            msg = f"{L['switch_success']}\n• {L['active_model']}: `{tgt}`\n• {L['active_prov']}: `{prov}`"
            toast = f"{'已切换至' if lang=='zh' else 'Switched to'} {tgt.split('/')[-1]}"
        else:
            msg = f"{L['switch_failed']}\n{err}"
            toast = L["failed"]

        card = _build_model_card(sk, provider=prov, model=tgt, status_msg=msg, ok=ok)
        return card, toast, "info" if ok else "error"

    # ── 3. 参数配置点选 (写操作：仅限所有者！严格白名单) ──
    if ac.startswith("set_cfg:"):
        if not _is_owner(open_id, adapter):
            return None, L["only_owner"], "error"

        parts = ac.split(":", 2)
        cmd = parts[1] if len(parts) > 1 else ""
        val = parts[2] if len(parts) > 2 else ""
        meta = CONFIG_CMDS.get(cmd, {})
        allowed_opts = meta.get("options") or []
        if val not in allowed_opts:
            return None, f"⛔ Invalid option: {val} not in {allowed_opts}", "error"

        cfg_key = meta.get("key", f"agent.{cmd.lstrip('/')}")
        res = run_subprocess(["hermes", "config", "set", cfg_key, val], timeout=10)
        card = _build_options_card(cmd, skey=sk)
        if res.ok:
            return card, f"{_cmd_label(cmd, lang)} -> {val} ({L['success']})", "info"
        return card, f"Failed: {res.stderr or res.stdout}", "error"

    # ── 4. 危险操作二次确认与执行 (仅限所有者！) ──
    if ac.startswith("exec_confirm:"):
        if not _is_owner(open_id, adapter):
            return None, L["only_owner"], "error"

        tok = ac.split(":", 1)[1]
        rec = _redeem_token(tok, open_id)
        if not rec:
            return None, ("令牌已失效或操作人变更" if lang == "zh" else "Token expired or operator mismatch"), "error"
        cmd = rec["cmd"]
        dispatch = getattr(adapter, "_dispatch_synthetic_event", None)
        if callable(dispatch):
            try:
                from gateway.platforms.event import MessageType
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
        card = _card(f"✅ {_cmd_label(cmd, lang)}", "green", [
            {"tag": "markdown", "content": f"✅ `{cmd}` {L['dispatched']}"},
            {"tag": "hr"},
            _nav_row_for_deep_page(parent_cat, lang=lang),
        ])
        return card, f"{cmd} {L['success']}", "info"

    if ac == "exec_cancel":
        return _root_card(sk), L["action_cancelled"], "info"

    # ── 5. 输出查看与复制 (仅限所有者！防数据泄露) ──
    if ac.startswith("copy_output:"):
        if not _is_owner(open_id, adapter):
            return None, L["only_owner"], "error"
        raw_path = ac[len("copy_output:"):].strip()
        safe = _safe_saved_output_path(raw_path)
        if not safe:
            return None, ("非法归档路径" if lang == "zh" else "Invalid archive path"), "error"
        inferred_cmd = _infer_cmd_from_saved_path(safe)
        card = _build_copy_card(inferred_cmd, safe, lang=lang)
        return card, L["expanded_full"], "info"

    if ac.startswith("show_result:"):
        if not _is_owner(open_id, adapter):
            return None, L["only_owner"], "error"
        raw_path = ac[len("show_result:"):].strip()
        safe = _safe_saved_output_path(raw_path)
        if not safe:
            return None, ("非法归档路径" if lang == "zh" else "Invalid archive path"), "error"
        content, err = _read_saved_output(safe, lang=lang)
        if err:
            return None, err, "error"
        inferred_cmd = _infer_cmd_from_saved_path(safe)
        result_ok = _result_status_from_text(content)
        card = _build_beautiful_result_card(inferred_cmd, content, ok=result_ok,
                                             saved_path=safe, status_text=("已归档结果" if lang == "zh" else "Archived Result"), lang=lang)
        return card, L["result_loaded"], "info"

    # ── 6. 导航动作 (全员放行浏览) ──
    if ac.startswith("nav:"):
        card = _nav(ac, sk)
        return card, "", "info"

    # ── 7. 命令触发路由 ──
    if ac.startswith("cmd:"):
        cmd = ac[4:].strip()
        meta = _CMD_META.get(cmd)
        if not meta:
            return None, f"Unknown command {cmd}", "error"

        # 7.1 菜单展示类：所有人均可查看
        if cmd == "/model":
            with _state_lock:
                _selected_provider.pop(sk, None)
                _pending_model.pop(sk, None)
            return _build_model_card(sk), "", "info"

        if meta.get("options"):
            return _build_options_card(cmd, skey=sk), "", "info"

        if cmd in GUIDE_CMDS:
            return _build_guide_card(cmd, skey=sk), "", "info"

        # 7.2 执行类与控制类命令：严格仅限所有者！
        if not _is_owner(open_id, adapter):
            return None, L["only_owner"], "error"

        # 命令冷却检查（3.0s）
        global _cooldown_check_counter
        cooldown_key = f"{cmd}:{open_id}"
        now_mono = time.monotonic()
        if now_mono - _cmd_cooldown.get(cooldown_key, 0) < 3.0:
            return None, L["cooldown"], "warning"
        _cmd_cooldown[cooldown_key] = now_mono
        _cooldown_check_counter += 1
        if _cooldown_check_counter >= 100:
            _cooldown_check_counter = 0
            cutoff = now_mono - 300
            expired = [k for k, v in _cmd_cooldown.items() if v < cutoff]
            for k in expired:
                del _cmd_cooldown[k]

        if meta.get("danger"):
            tok = _mint_token(cid, open_id, mid, cmd, "")
            return _build_exec_confirm_card(cmd, "", tok, skey=sk), L["confirm_high_risk"], "warning"

        if cmd in SESSION_ACTS:
            dispatch = getattr(adapter, "_dispatch_synthetic_event", None)
            if callable(dispatch):
                try:
                    from gateway.platforms.event import MessageType
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
            card = _card(f"✅ {_cmd_label(cmd, lang)}", "green", [
                {"tag": "markdown", "content": f"✅ `{cmd}` {L['dispatched']}"},
                {"tag": "hr"},
                _nav_row_for_deep_page(parent_cat, lang=lang),
            ])
            return card, f"{cmd} {L['success']}", "info"

        # 常规 CLI 查询
        running_card = _build_running_card(cmd, skey=sk)
        if mid:
            _async(_execute_cli_async, cmd, mid, sk)
        return running_card, f"{L['executing']} {cmd}...", "info"

    return None, "", "info"


def _resp(card: dict = None, toast: str = "", toast_type: str = "info"):
    try:
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
    except Exception:
        return None


def handle_card_action_sync(adapter: Any, data: Any) -> Optional[Any]:
    event = getattr(data, "event", None)
    act = getattr(event, "action", None) if not isinstance(event, dict) else (event or {}).get("action")
    av = (getattr(act, "value", {}) or {}) if act else {}
    if not isinstance(av, dict):
        av = {}

    opt = getattr(act, "option", "") or ""
    if opt and "option" not in av:
        av["option"] = opt

    ctx = getattr(event, "context", None) if not isinstance(event, dict) else (event or {}).get("context")
    cid = getattr(ctx, "open_chat_id", "") if ctx else ""
    mid = getattr(ctx, "open_message_id", "") if ctx else ""
    operator = getattr(event, "operator", None) if not isinstance(event, dict) else (event or {}).get("operator")
    open_id = getattr(operator, "open_id", "") if operator else ""

    if mid and cid:
        _chat_card_map[cid] = mid
        if len(_chat_card_map) > 500:
            del _chat_card_map[next(iter(_chat_card_map))]

    card, toast, toast_type = dispatch_palette_action(av, cid, mid, open_id, adapter)
    return _resp(card, toast, toast_type)


# ════════════════════════════════════════════════════════════════════════════
# 9. Hook: pre_gateway_dispatch (Official Stock Hermes Path)
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
    sender_open_id = getattr(src, "user_id", "") or ""

    if txt == "/card":
        if not _check_lark_cli():
            logger.error("[command-palette] lark-cli is not installed in PATH")
            return None
        chat_type = getattr(src, "chat_type", "") or ""
        _record_default_owner_if_empty(sender_open_id, is_p2p=(chat_type in ("p2p", "dm")))
        sk = _skey(cid, sender_open_id)
        _send_root_card(cid, _root_card(sk))
        return {"action": "skip", "reason": "command-palette:card"}

    if txt.startswith("/card "):
        rest = txt[6:].strip()
        av = {}
        if " " in rest:
            _, json_part = rest.split(" ", 1)
            try:
                av = json.loads(json_part)
            except Exception:
                pass
        else:
            try:
                av = json.loads(rest)
            except Exception:
                pass

        raw_msg = getattr(event, "raw_message", None)
        raw_event = getattr(raw_msg, "event", None) if raw_msg else None
        raw_ctx = getattr(raw_event, "context", None) if raw_event else None
        mid = getattr(raw_ctx, "open_message_id", "") if raw_ctx else ""
        if not mid:
            mid = _chat_card_map.get(cid, "")

        raw_op = getattr(raw_event, "operator", None) if raw_event else None
        open_id = getattr(raw_op, "open_id", "") if raw_op else sender_open_id

        adapter = None
        if gateway and hasattr(gateway, "adapters"):
            from gateway.config import Platform
            adapter = gateway.adapters.get(Platform.FEISHU)

        card, toast, toast_type = dispatch_palette_action(av, cid, mid, open_id, adapter)
        if card and mid:
            _patch_card(mid, card)
        return {"action": "skip", "reason": "command-palette:action_consumed"}

    return None


def register(ctx) -> None:
    _cleanup_old_outputs()
    ctx.register_hook("pre_gateway_dispatch", _on_msg)
    logger.info("Hermes feishu-command-palette v1.4.0 registered (I18N zh/en decoupling, Owner-only security)")
