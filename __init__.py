"""Hermes Feishu Command Palette (feishu-command-palette) v1.2.0

Interactive single-card control panel to dispatch Hermes slash commands directly.

Disclosure:
Answers the Feishu /card text with a control card sent through the external
lark-cli binary; button clicks run a fixed table of hermes ... CLI subcommands
locally and post their output (logs, sessions, redacted config/auth listings)
into the chat, and can write model/provider and agent.* settings including
agent.yolo; reads auth.json for provider names.
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
# 1. Output Formatting, Sandboxed Storage & Cleanup
# ════════════════════════════════════════════════════════════════════════════

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _safe_saved_output_path(value: str) -> str | None:
    """Validate archive path: strictly within PLUGIN_DATA_DIR, no directory traversal."""
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


def _read_saved_output(path: str) -> tuple[str, str | None]:
    safe = _safe_saved_output_path(path)
    if not safe:
        return "", "Invalid archive path / 非法归档路径"
    try:
        p = Path(safe)
        if not p.exists():
            return "", "Archive file does not exist / 归档文件不存在"
        if not p.is_file():
            return "", "Target is not a regular file / 目标不是普通文件"
        return p.read_text(encoding="utf-8", errors="replace"), None
    except Exception as e:
        return "", f"Failed to read file / 读取失败: {e}"


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


def _beautify_unified(cmd: str, clean_text: str) -> list[dict]:
    elements: list[dict] = []
    if not clean_text:
        elements.append({
            "tag": "markdown",
            "content": "**📌 Output (执行结果)**\n• *(No output returned / 无输出)*",
        })
        return elements

    sections = re.split(r"(?m)^(?=◆ )", clean_text)
    if len(sections) <= 1:
        lines = clean_text.splitlines()
        formatted_lines = [_format_unified_line(l) for l in lines]
        formatted_lines = [l for l in formatted_lines if l]
        content_body = "\n".join(formatted_lines[:45])
        if len(formatted_lines) > 45:
            content_body += "\n• *(Output truncated in card / 已截断展示)*"
        elements.append({
            "tag": "markdown",
            "content": f"**📌 Details (查询详情)**\n{content_body}",
        })
        return elements

    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        sec_lines = sec.splitlines()
        header_line = sec_lines[0].strip()
        title = header_line.lstrip("◆").strip() or "Details (详情)"
        body_lines = sec_lines[1:]
        if not body_lines:
            continue
        formatted_body = [_format_unified_line(l) for l in body_lines]
        formatted_body = [l for l in formatted_body if l]
        if not formatted_body:
            continue
        content_body = "\n".join(formatted_body[:25])
        if len(formatted_body) > 25:
            content_body += "\n• *(Output truncated in card / 已截断展示)*"
        elements.append({
            "tag": "markdown",
            "content": f"**📌 {title}**\n{content_body}",
        })
    return elements


def parse_and_beautify_output(cmd: str, raw_text: str, max_chars_inline: int = 1500) -> tuple[list[dict], str]:
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

    line_count = len(clean_text.splitlines()) if clean_text else 0
    char_count = len(clean_text)
    fails = len(re.findall(r"^[ \t]*✗", clean_text, re.M))
    status_mark = "🔴 Exceptions Found (包含异常)" if fails > 0 else "🟢 Success (执行成功)"

    top_summary = {
        "tag": "markdown",
        "content": f"{status_mark}  |  📊 `{char_count}` chars  |  📄 `{line_count}` lines",
    }
    sub_elements = _beautify_unified(cmd, clean_text)
    bottom_archive = {
        "tag": "markdown",
        "content": f"📄 **Archive Path (归档路径)**\n`{saved_path}`",
    }

    elements = [top_summary]
    elements.extend(sub_elements)
    elements.append(bottom_archive)
    return elements, str(saved_path)


def _cleanup_old_outputs() -> None:
    """Clean up archived outputs older than 7 days inside PLUGIN_DATA_DIR only."""
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
# 2. Command Execution Engine & PTY Streaming
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
# 3. Command Registry & Bilingual Metadata (English First)
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
    # Session Queries
    "/status":   {"label": "Status (系统状态)", "cli": ["hermes", "status"]},
    "/context":  {"label": "Context (上下文分析)", "cli": ["hermes", "prompt-size"]},
    "/usage":    {"label": "Usage (配额用量)", "cli": ["hermes", "usage"]},
    "/sessions": {"label": "Sessions (历史会话)", "cli": ["hermes", "sessions", "list", "--limit", "10"]},
    # Tool & Diagnostics (with adaptive timeout tiers)
    "/diff":     {"label": "Diff (代码差异)", "cli": ["git", "-C", str(INSTALL_PATH), "diff", "--stat"], "timeout": 45},
    "/doctor":   {"label": "Doctor (健康诊断)", "cli": ["hermes", "doctor"], "timeout": 120},
    "/debug":    {"label": "Debug (调试摘要)", "cli": ["hermes", "debug", "share", "--local"], "timeout": 45},
    "/security": {"label": "Security (安全审计)", "cli": ["hermes", "security", "audit"], "timeout": 60},
    "/logs":     {"label": "Logs (网关日志)", "cli": ["hermes", "logs", "gateway", "-n", "40"]},
    "/cron":     {"label": "Cron (定时任务)", "cli": ["hermes", "cron", "list"]},
    "/plugins":  {"label": "Plugins (插件列表)", "cli": ["hermes", "plugins", "list"], "timeout": 45},
    "/skills":   {"label": "Skills (技能库)", "cli": ["hermes", "skills", "list"]},
    "/bundles":  {"label": "Bundles (技能包)", "cli": ["hermes", "bundles"]},
    "/memory":   {"label": "Memory (记忆系统)", "cli": ["hermes", "memory"]},
    # System Info
    "/version":  {"label": "Version (框架版本)", "cli": ["hermes", "--version"]},
    "/profile":  {"label": "Profile (配置详情)", "cli": ["hermes", "profile"]},
    "/config":   {"label": "Config (配置概览)", "cli": ["hermes", "config", "show"]},
    "/whoami":   {"label": "Credentials (身份凭证)", "cli": ["hermes", "auth", "list"]},
}

CONFIG_CMDS: dict[str, dict] = {
    "/model":         {"label": "Switch Model (模型切换)", "kind": "picker"},
    "/reasoning":     {"label": "Reasoning Effort (推理力度)", "options": ["none", "minimal", "low", "medium", "high"], "key": "agent.reasoning_effort"},
    "/personality":   {"label": "Personality (AI 人格)", "options": ["technical", "concise", "creative", "helpful", "hype", "noir", "philosopher", "teacher"], "key": "agent.personality"},
    "/verbose":       {"label": "Verbose Level (日志级别)", "options": ["off", "tools", "all"], "key": "agent.verbose"},
    "/yolo":          {"label": "YOLO Mode (极速免审)", "options": ["true", "false"], "key": "agent.yolo"},
    "/fast":          {"label": "Fast Mode (高速模式)", "options": ["true", "false"], "key": "agent.fast"},
    "/codex-runtime": {"label": "Codex Runtime (执行环境)", "options": ["auto", "app", "cli"], "key": "agent.codex_runtime"},
}

SESSION_ACTS: dict[str, dict] = {
    "/new":      {"label": "New Session (新建会话)", "danger": True},
    "/stop":     {"label": "Force Stop (强制停止)", "danger": True},
    "/undo":     {"label": "Undo Turn (撤销回复)", "danger": False},
    "/retry":    {"label": "Retry Turn (重试执行)", "danger": False},
    "/compress": {"label": "Compress Context (压缩会话)", "danger": False},
}

GUIDE_CMDS: dict[str, dict] = {
    "/background": {"label": "Background Task (后台任务)", "template": "/background <prompt / 目标>", "desc": "Run a long-running task in a detached subagent without blocking the current chat."},
    "/steer":      {"label": "Steer Command (动态干预)", "template": "/steer <instruction / 指令>", "desc": "Inject guidance mid-turn during agent execution to adjust course."},
    "/goal":       {"label": "Goal Setting (目标管理)", "template": "/goal set <long-term goal>", "desc": "Set persistent goals across multiple conversation turns."},
}

CATEGORIES = [
    ("session", "💬", "Session (会话管理)", "blue",
     ["/new", "/stop", "/status", "/context", "/usage", "/sessions", "/undo", "/retry", "/compress", "/background"]),
    ("config", "⚙️", "Config (系统配置)", "purple",
     ["/model", "/reasoning", "/personality", "/verbose", "/yolo", "/fast", "/codex-runtime"]),
    ("tools", "🔧", "Tools (工具研发)", "green",
     ["/diff", "/doctor", "/security", "/debug", "/logs", "/cron", "/plugins", "/skills", "/bundles", "/memory"]),
    ("info", "ℹ️", "Info (系统信息)", "grey",
     ["/version", "/profile", "/config", "/whoami"]),
]

_CMD_META: dict[str, dict] = {}
for c in [CLI_CMDS, CONFIG_CMDS, SESSION_ACTS, GUIDE_CMDS]:
    _CMD_META.update(c)

CMD_PARENT_CATEGORY: dict[str, str] = {}
for cat_key, _, _, _, cmds in CATEGORIES:
    for c in cmds:
        CMD_PARENT_CATEGORY[c] = f"/card/{cat_key}"

QUICK_ACTIONS = [
    ("🔄 **Switch Model** (模型切换)", "▶", "primary", {"action": "nav:/card/model"}),
    ("📊 **System Status** (系统状态)", "▶", "default", {"action": "cmd:/status"}),
    ("🆕 **New Session** (新建会话)", "▶", "default", {"action": "cmd:/new"}),
    ("⏹ **Force Stop** (强制停止)", "▶", "danger", {"action": "cmd:/stop"}),
]

# ════════════════════════════════════════════════════════════════════════════
# 4. State Management, RBAC & Native Config Set
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


def _get_admin_open_ids(adapter: Any = None) -> set[str]:
    admins = set()
    env_admins = os.environ.get("FEISHU_ADMINS", "").strip()
    if env_admins:
        admins.update(a.strip() for a in env_admins.split(",") if a.strip())
    if adapter is not None:
        adapter_admins = getattr(adapter, "_admins", None)
        if isinstance(adapter_admins, (list, set, tuple)):
            admins.update(str(a).strip() for a in adapter_admins if a)
    return admins


def _is_admin(open_id: str, adapter: Any = None) -> bool:
    """Strict fail-closed admin verification for write/dangerous actions."""
    normalized = str(open_id or "").strip()
    if not normalized:
        return False
    admins = _get_admin_open_ids(adapter)
    if not admins:
        return False
    return "*" in admins or normalized in admins


def _is_operator_allowed(open_id: str, adapter: Any = None) -> bool:
    """Check if operator is allowed to trigger read-only cards."""
    normalized = str(open_id or "").strip()
    if not normalized:
        return False
    admins = _get_admin_open_ids(adapter)
    allowed = set(admins)
    env_allowed = os.environ.get("FEISHU_ALLOWED_USERS", "").strip()
    if env_allowed:
        allowed.update(a.strip() for a in env_allowed.split(",") if a.strip())
    if adapter is not None:
        group_allowed = getattr(adapter, "_allowed_group_users", None)
        if isinstance(group_allowed, (list, set, tuple)):
            allowed.update(str(u).strip() for u in group_allowed if u)
    if not allowed:
        return True
    return "*" in allowed or normalized in allowed


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
    """Switch model via official `hermes config set` CLI without wiping comments."""
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
    r1 = run_subprocess(["hermes", "config", "set", "model.provider", prov_str, "--yes"], timeout=10)
    if not r1.ok:
        return False, f"Failed to set model.provider: {r1.stderr or r1.stdout}"
    r2 = run_subprocess(["hermes", "config", "set", "model.default", tgt_model, "--yes"], timeout=10)
    if not r2.ok:
        return False, f"Failed to set model.default: {r2.stderr or r2.stdout}"
    return True, ""


# ════════════════════════════════════════════════════════════════════════════
# 5. Feishu Schema 1.0 Card Builders (Strict Standards)
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


def _nav_row_for_subpage() -> dict:
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
                "elements": [_nav_btn("⬅ Back (返回)", "/card/root", "default")],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "center",
                "horizontal_align": "left",
                "elements": [_nav_btn("🏠 Home (首页)", "/card/root", "default")],
            },
        ],
    }


def _nav_row_for_deep_page(parent_target: str, extra_actions: list | None = None) -> dict:
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
        _nav_btn("⬅ Back (返回)", parent_target, "default"),
        _nav_btn("🏠 Home (首页)", "/card/root", "default"),
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


def _cc_command_grid(cmds: list[str]) -> list[dict]:
    elements = []
    for cmd in cmds:
        meta = _CMD_META.get(cmd, {})
        label = meta.get("label", cmd)
        emoji = CMD_EMOJI.get(cmd, "▶")
        btn_type = "danger" if meta.get("danger") else "default"
        elements.append(_cc_command_row(f"{emoji} **{label}** `{cmd}`", "Run / 执行", btn_type, {"action": f"cmd:{cmd}"}))
    return elements


def _root_card() -> dict:
    _, cur_p, cur_m, _, _ = get_hermes_catalog_and_status()
    short_model = cur_m.split("/")[-1] if "/" in cur_m else cur_m
    reasoning = "medium"
    try:
        if CONFIG_PATH.exists():
            c = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            reasoning = c.get("agent", {}).get("reasoning_effort", "medium")
    except Exception:
        pass

    cat_btns = []
    for k, icon, title, _, _ in CATEGORIES:
        cat_btns.append(_btn(f"{icon} {title}", "default", value={"action": f"nav:/card/{k}"}))

    elements = [
        {"tag": "markdown", "content": f"🟢 **{short_model}** · {reasoning}"},
    ]
    elements.extend(_cc_category_grid(cat_btns))
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": "⚡ **Quick Actions (快捷操作)**"})
    for label_md, btn_label, btn_typ, act_val in QUICK_ACTIONS:
        elements.append(_cc_command_row(label_md, btn_label, btn_typ, act_val))
    return _card("⌨️ Hermes Control Panel (控制面板)", "blue", elements)


def _cat_card(ck: str, icon: str, title: str, color: str, cmds: list) -> dict:
    elements = _cc_command_grid(cmds)
    elements.append({"tag": "hr"})
    elements.append(_nav_row_for_subpage())
    return _card(f"{icon} {title}", color, elements)


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
            "placeholder": _pt(f"Models 1-50 (current: {model.split('/')[-1]})"),
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
        {"tag": "markdown", "content": f"🎯 **Active (生效中)**: `{cur_m}`\n🔌 **Provider (部署源)**: `{cur_p}`"},
        {"tag": "hr"},
        {"tag": "markdown", "content": "👇 **Select Provider & Model (选择通道与模型)**"},
        {"tag": "action", "actions": action_elements},
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                _btn("✅ Confirm Switch (确认切换)", "primary", value={"action": f"confirm_switch:{provider}:{model}"}),
            ],
        },
    ]

    if status_msg:
        elements.insert(0, {"tag": "markdown", "content": status_msg})

    elements.append({"tag": "hr"})
    elements.append(_nav_row_for_deep_page("/card/config"))
    return _card("🔄 Model Switch (模型切换)", ("green" if ok else "red") if status_msg else "purple", elements)


def _build_options_card(cmd: str) -> dict:
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
        {"tag": "markdown", "content": f"Current value (当前值): `{cur}`"},
    ]
    for opt in opts:
        is_cur = (str(opt).lower() == cur.lower())
        btn_type = "primary" if is_cur else "default"
        btn_text = "✓ Active (当前)" if is_cur else "Select (选择)"
        display_text = f"● **{opt}**" if is_cur else f"**{opt}**"
        elements.append(_cc_command_row(
            display_text, btn_text, btn_type,
            {"action": f"set_cfg:{cmd}:{opt}"}
        ))

    elements.append({"tag": "hr"})
    elements.append(_nav_row_for_deep_page(CMD_PARENT_CATEGORY.get(cmd, "/card/config")))
    return _card(f"⚙️ Settings: {meta.get('label', cmd)}", "purple", elements)


def _build_guide_card(cmd: str) -> dict:
    meta = GUIDE_CMDS.get(cmd, {})
    template = meta.get("template", cmd)
    desc = meta.get("desc", "")
    elements = [
        {"tag": "markdown", "content": f"📖 **Description (说明)**\n{desc}"},
        {"tag": "markdown", "content": f"📋 **Template (使用格式)**\n```{template}```"},
        {"tag": "markdown", "content": "💡 *Copy the template above and send to chat to execute.*"},
        {"tag": "hr"},
        _nav_row_for_deep_page(CMD_PARENT_CATEGORY.get(cmd, "/card/session")),
    ]
    return _card(f"💡 Guide: {meta.get('label', cmd)}", "blue", elements)


def _build_exec_confirm_card(cmd: str, args: str, token: str) -> dict:
    meta = _CMD_META.get(cmd, {})
    full = f"{cmd} {args}".strip()
    elements = [
        {"tag": "markdown", "content": (
            f"⚠️ **High Risk Confirmation (高危确认)**: `{full}`\n"
            f"{meta.get('label', '')} — Token valid for 120s. Only clicking user authorized.")},
    ]
    confirm_btns = [
        _btn("✅ Confirm (确认执行)", "danger", value={"action": f"exec_confirm:{token}"}),
        _btn("❌ Cancel (取消)", "default", value={"action": "exec_cancel"}),
        _nav_btn("⬅ Back (返回)", CMD_PARENT_CATEGORY.get(cmd, "/card/session")),
        _nav_btn("🏠 Home (首页)", "/card/root"),
    ]
    elements.extend(_cc_category_grid(confirm_btns))
    return _card(f"⚠️ Confirm: {meta.get('label', cmd)}", "red", elements)


def _build_streaming_card(cmd: str, elapsed: float, recent_lines: list[str], is_heartbeat: bool = False) -> dict:
    meta = _CMD_META.get(cmd, {})
    label = meta.get("label", cmd)
    header_content = f"⚡ Running: {label} ({cmd})  |  ⏱️ {elapsed:.1f}s"
    if recent_lines:
        log_block = "```text\n" + "\n".join(recent_lines) + "\n```"
        subtitle = f"⏳ **Live Output Capture (终端捕获中)** · Elapsed `{elapsed:.1f}s`"
    elif is_heartbeat:
        log_block = "⏳ *Deep diagnostics in progress, waiting for next output...*"
        subtitle = f"⏳ **Deep Diagnostics (后台检测中)** · Elapsed `{elapsed:.1f}s` (Process Active)"
    else:
        log_block = "⏳ *Waiting for initial process output...*"
        subtitle = f"⏳ **Process Initializing (正在初始化)** · Elapsed `{elapsed:.1f}s`"
    elements = [
        {"tag": "markdown", "content": subtitle},
        {"tag": "markdown", "content": log_block},
        {"tag": "markdown", "content": "ℹ️ *Live streaming preview. Final report folds automatically on completion.*"},
        _nav_row_for_deep_page(CMD_PARENT_CATEGORY.get(cmd, "/card/root")),
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": header_content}, "template": "blue"},
        "elements": elements,
    }


def _build_running_card(cmd: str) -> dict:
    meta = _CMD_META.get(cmd, {})
    elements = [
        {"tag": "markdown",
         "content": f"⏳ **Executing** `{cmd}` ({meta.get('label', '')})... Card updates automatically on completion."},
        _nav_row_for_deep_page(CMD_PARENT_CATEGORY.get(cmd, "/card/root")),
    ]
    return _card(f"⚡ {meta.get('label', cmd)}", "yellow", elements)


def _build_copy_card(cmd: str, saved_path: str) -> dict:
    if not cmd and saved_path:
        cmd = _infer_cmd_from_saved_path(saved_path)
    meta = _CMD_META.get(cmd, {})
    label = meta.get("label", cmd or "Output")

    content, err = _read_saved_output(saved_path)
    if err:
        body = f"Failed to read archive: {err}"
    else:
        truncated = False
        display = content
        if len(display) > 18000:
            display = display[:18000]
            truncated = True
        safe_display = display.replace("```", r"\`\`\`")
        suffix = f"\n[Truncated in card preview. Full archive: {saved_path}]" if truncated else ""
        body = f"```text\n{safe_display}\n```{suffix}"
        _test_card = {"tag": "markdown", "content": body}
        while len(json.dumps(_test_card, ensure_ascii=False).encode("utf-8")) > 26000 and len(safe_display) > 200:
            safe_display = safe_display[:int(len(safe_display) * 0.85)]
            truncated = True
            body = f"```text\n{safe_display}\n```\n[Card size limit reached. Full archive: {saved_path}]"
            _test_card = {"tag": "markdown", "content": body}

    elements = [
        {"tag": "markdown", "content": f"📋 **Full Raw Output (完整内容)** · `{cmd}`"},
        {"tag": "markdown", "content": body},
        {"tag": "hr"},
        {"tag": "markdown", "content": f"📄 **Archive File (归档文件)**\n`{saved_path}`"},
        _nav_row_for_deep_page(
            CMD_PARENT_CATEGORY.get(cmd, "/card/root"),
            extra_actions=[
                _btn("↩ Back to Report (返回报告)", "default", value={"action": f"show_result:{saved_path}"}),
                _btn("🔁 Re-run (再次执行)", "primary", value={"action": f"cmd:{cmd}"}),
            ],
        ),
    ]
    return _card(f"📋 Copy: {label}", "grey", elements)


def _result_status_from_text(text: str) -> bool:
    clean = _strip_ansi(text or "")
    if re.search(r"^[ \t]*✗", clean, re.M):
        return False
    return True


def _build_beautiful_result_card(cmd: str, raw_output: str, elapsed: float = 0.0,
                                 ok: bool = True, saved_path: str = "",
                                 timed_out: bool = False,
                                 status_text: str = "") -> dict:
    meta = _CMD_META.get(cmd, {})
    label = meta.get("label", cmd)

    if timed_out:
        raw_output = f"⚠️ Command execution timed out (Partial output captured / 已捕获部分输出)\n\n{raw_output}"

    sub_elements, _saved = parse_and_beautify_output(cmd, raw_output)
    if not saved_path:
        saved_path = _saved

    is_no_data = False
    lower_out = (raw_output or "").lower()
    if "no account usage available" in lower_out or "no credential is configured" in lower_out:
        is_no_data = True

    if is_no_data:
        template = "grey"
        title_text = f"ℹ️ **{label} ({cmd}) No Data (暂无数据)**  |  ⏱️ {elapsed:.1f}s"
    elif timed_out:
        template = "orange"
        title_text = f"⚠️ **{label} ({cmd}) Timed Out (执行超时)**  |  ⏱️ {elapsed:.1f}s"
    elif ok:
        template = "green"
        status_disp = status_text or "Success (执行成功)"
        title_text = f"✅ **{label} ({cmd}) {status_disp}**  |  ⏱️ {elapsed:.1f}s"
    else:
        template = "red"
        status_disp = status_text or "Failed (执行失败)"
        title_text = f"❌ **{label} ({cmd}) {status_disp}**  |  ⏱️ {elapsed:.1f}s"

    elements = list(sub_elements)
    elements.append({"tag": "hr"})

    parent_cat = CMD_PARENT_CATEGORY.get(cmd, "/card/root")
    extra_actions = []
    if saved_path:
        extra_actions.append(_btn("📋 Copy (复制内容)", "default", value={"action": f"copy_output:{saved_path}"}))
    extra_actions.append(_btn("🔁 Re-run (再次执行)", "primary", value={"action": f"cmd:{cmd}"}))

    elements.append(_nav_row_for_deep_page(parent_cat, extra_actions=extra_actions))
    return _card(title_text, template, elements)


def _nav(action: str, skey: str = "") -> dict:
    sub = action[4:].strip()
    if sub in ("/card/root", "root", "/card"):
        return _root_card()
    if sub in ("/card/model", "model"):
        return _build_model_card(skey)
    for k, icon, title, color, cmds in CATEGORIES:
        if sub in (f"/card/{k}", k):
            return _cat_card(k, icon, title, color, cmds)
    return _root_card()


# ════════════════════════════════════════════════════════════════════════════
# 6. Messaging Egress via lark-cli & Asynchronous Dispatch
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


def _execute_cli_async(cmd: str, mid: str) -> None:
    meta = CLI_CMDS.get(cmd, {})
    argv = meta.get("cli") or ["hermes", cmd.lstrip("/")]
    cmd_timeout = meta.get("timeout", _DEFAULT_CMD_TIMEOUT)

    if not STREAMING_ENABLED or not mid:
        t0 = time.monotonic()
        res = run_subprocess(argv, timeout=cmd_timeout)
        elapsed = time.monotonic() - t0
        output_text = res.stdout if res.ok else (res.stderr or res.stdout or f"exit code {res.exit_code}")
        card = _build_beautiful_result_card(cmd, output_text, elapsed=elapsed, ok=res.ok, timed_out=res.timed_out)
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
        stream_card = _build_streaming_card(cmd, elapsed, lines, is_heartbeat=is_heartbeat)
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

    card = _build_beautiful_result_card(cmd, output_text, elapsed=elapsed, ok=res.ok, timed_out=res.timed_out)
    if mid:
        _patch_card(mid, card)


# ════════════════════════════════════════════════════════════════════════════
# 7. Unified Action Dispatcher (Shared by Synthetic Messages & Callbacks)
# ════════════════════════════════════════════════════════════════════════════

def dispatch_palette_action(action_value: dict, cid: str, mid: str, open_id: str,
                            adapter: Any = None) -> tuple[Optional[dict], str, str]:
    """Execute panel action. Returns (card, toast_text, toast_type)."""
    ac = action_value.get("action", "")
    opt = action_value.get("option", "")
    sk = _skey(cid, open_id)

    # 1. Dropdown Selection Staging
    if ac == "select_provider" and opt:
        with _state_lock:
            _selected_provider[sk] = opt
            _pending_model.pop(sk, None)
        card = _build_model_card(sk, provider=opt)
        return card, f"Provider selected: {opt}", "info"

    if ac == "select_model" and opt:
        prov = action_value.get("provider", _selected_provider.get(sk, ""))
        with _state_lock:
            _pending_model[sk] = opt
            if prov:
                _selected_provider[sk] = prov
        card = _build_model_card(sk, provider=prov, model=opt)
        return card, f"Model selected: {opt.split('/')[-1]}", "info"

    # 2. Confirm Model Switch (Write Action: Admin Gated)
    if ac.startswith("confirm_switch:"):
        if not _is_admin(open_id, adapter):
            return None, "⛔ Permission denied: Admin privileges required / 需要管理员权限", "error"

        parts = ac.split(":", 2)
        prov = parts[1] if len(parts) > 1 else ""
        tgt = parts[2] if len(parts) > 2 else _pending_model.get(sk, "")
        if not tgt:
            return None, "Please select a target model first / 请先选择目标模型", "warning"

        ok, err = _switch_hermes_model(tgt, prov)
        if ok:
            msg = f"🎉 **Model Switched Successfully (模型切换成功)**\n• Model: `{tgt}`\n• Provider: `{prov}`"
            toast = f"Switched to {tgt.split('/')[-1]}"
        else:
            msg = f"❌ **Model Switch Failed (模型切换失败)**\n{err}"
            toast = "Switch failed / 切换失败"

        card = _build_model_card(sk, provider=prov, model=tgt, status_msg=msg, ok=ok)
        return card, toast, "info" if ok else "error"

    # 3. Direct Config Setting (Write Action: Admin Gated + Whitelisted)
    if ac.startswith("set_cfg:"):
        if not _is_admin(open_id, adapter):
            return None, "⛔ Permission denied: Admin privileges required / 需要管理员权限", "error"

        parts = ac.split(":", 2)
        cmd = parts[1] if len(parts) > 1 else ""
        val = parts[2] if len(parts) > 2 else ""
        meta = CONFIG_CMDS.get(cmd, {})
        allowed_opts = meta.get("options") or []
        if val not in allowed_opts:
            return None, f"⛔ Invalid option: {val} not in {allowed_opts}", "error"

        cfg_key = meta.get("key", f"agent.{cmd.lstrip('/')}")
        res = run_subprocess(["hermes", "config", "set", cfg_key, val, "--yes"], timeout=10)
        card = _build_options_card(cmd)
        if res.ok:
            return card, f"Set {cmd} to {val} (已生效)", "info"
        return card, f"Failed to set config: {res.stderr or res.stdout}", "error"

    # 4. Dangerous Action Execution (/new, /stop: Admin Gated)
    if ac.startswith("exec_confirm:"):
        if not _is_admin(open_id, adapter):
            return None, "⛔ Permission denied: Admin privileges required / 需要管理员权限", "error"

        tok = ac.split(":", 1)[1]
        rec = _redeem_token(tok, open_id)
        if not rec:
            return None, "Token expired or operator mismatch / 令牌失效", "error"
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
        card = _card(f"✅ {cmd} Executed", "green", [
            {"tag": "markdown", "content": f"✅ `{cmd}` executed successfully via gateway."},
            {"tag": "hr"},
            _nav_row_for_deep_page(parent_cat),
        ])
        return card, f"{cmd} executed", "info"

    if ac == "exec_cancel":
        return _root_card(), "Action cancelled / 操作已取消", "info"

    # 5. Output View & Copy Actions
    if ac.startswith("copy_output:"):
        raw_path = ac[len("copy_output:"):].strip()
        safe = _safe_saved_output_path(raw_path)
        if not safe:
            return None, "Invalid archive path / 非法路径", "error"
        inferred_cmd = _infer_cmd_from_saved_path(safe)
        card = _build_copy_card(inferred_cmd, safe)
        return card, "Expanded full text, copy as needed / 请长按或选中复制", "info"

    if ac.startswith("show_result:"):
        raw_path = ac[len("show_result:"):].strip()
        safe = _safe_saved_output_path(raw_path)
        if not safe:
            return None, "Invalid archive path / 非法路径", "error"
        content, err = _read_saved_output(safe)
        if err:
            return None, f"Read error / 读取错误: {err}", "error"
        inferred_cmd = _infer_cmd_from_saved_path(safe)
        result_ok = _result_status_from_text(content)
        card = _build_beautiful_result_card(inferred_cmd, content, ok=result_ok,
                                             saved_path=safe, status_text="Archived Result (已归档结果)")
        return card, "Result loaded / 结果已加载", "info"

    # 6. Navigation Actions
    if ac.startswith("nav:"):
        card = _nav(ac, sk)
        return card, "", "info"

    # 7. Command Execution Routing
    if ac.startswith("cmd:"):
        cmd = ac[4:].strip()
        meta = _CMD_META.get(cmd)
        if not meta:
            return None, f"Unknown command {cmd} / 未知命令", "error"

        # Cooldown Check (3.0s per user per command)
        global _cooldown_check_counter
        cooldown_key = f"{cmd}:{open_id}"
        now_mono = time.monotonic()
        if now_mono - _cmd_cooldown.get(cooldown_key, 0) < 3.0:
            return None, "Please wait, command in cooldown / 命令冷却中", "warning"
        _cmd_cooldown[cooldown_key] = now_mono
        _cooldown_check_counter += 1
        if _cooldown_check_counter >= 100:
            _cooldown_check_counter = 0
            cutoff = now_mono - 300
            expired = [k for k, v in _cmd_cooldown.items() if v < cutoff]
            for k in expired:
                del _cmd_cooldown[k]

        if cmd == "/model":
            return _build_model_card(sk), "", "info"

        if meta.get("options"):
            return _build_options_card(cmd), "", "info"

        if cmd in GUIDE_CMDS:
            return _build_guide_card(cmd), "", "info"

        if meta.get("danger"):
            if not _is_admin(open_id, adapter):
                return None, "⛔ Permission denied: Admin privileges required / 需要管理员权限", "error"
            tok = _mint_token(cid, open_id, mid, cmd, "")
            return _build_exec_confirm_card(cmd, "", tok), "Confirmation required / 请二次确认", "warning"

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
            card = _card(f"✅ {meta.get('label', cmd)}", "green", [
                {"tag": "markdown", "content": f"✅ `{cmd}` sent directly to current session."},
                {"tag": "hr"},
                _nav_row_for_deep_page(parent_cat),
            ])
            return card, f"{cmd} dispatched", "info"

        # Regular CLI queries (/status, /doctor, /diff, etc.)
        running_card = _build_running_card(cmd)
        if mid:
            _async(_execute_cli_async, cmd, mid)
        return running_card, f"Running {cmd}... / 正在拉取数据", "info"

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
# 8. Hook: pre_gateway_dispatch (Official Stock Hermes Path)
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

    # Path A: User sends "/card" command
    if txt == "/card":
        if not _check_lark_cli():
            logger.error("[command-palette] lark-cli is not installed in PATH")
            return None
        if not _is_operator_allowed(sender_open_id):
            return {"action": "skip", "reason": "command-palette:unauthorized"}
        _send_root_card(cid, _root_card())
        return {"action": "skip", "reason": "command-palette:card"}

    # Path B: Stock Hermes synthetic message from card clicks: `/card button {...}`
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
    logger.info("Hermes feishu-command-palette v1.2.0 registered (Stock compatible, English default, sandboxed)")
