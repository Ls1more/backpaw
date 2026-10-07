"""Backpaw: see what your AI coding agent deleted or moved, and put it back.

  backpaw                 open the restore window
  backpaw trash PATH...   send paths to the Recycle Bin / Trash (logged)
  backpaw list            print the log
  backpaw scan            print deletes/moves found in past Claude sessions
  backpaw check [DIR]     warn if DIR (default: cwd) is not backed up
  backpaw agents          list supported AI agents and whether the guard is on
  backpaw install [AGENT...]    turn the guard on (default: every agent found)
  backpaw uninstall [AGENT...]  turn it off (default: everywhere it is on)
  backpaw --version
  backpaw hook AGENT      (called by the agent) pre-command / session-start handler

Agents: claude, codex, gemini, cursor, copilot, windsurf, qwen, kimi, opencode
"""
import glob
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

__version__ = "0.4.0"
REPO_URL = "https://github.com/Ls1more/backpaw"

HOME = Path.home()
DATA = HOME / ".backpaw"
LOG = DATA / "log.jsonl"
CLAUDE = HOME / ".claude"
SETTINGS = CLAUDE / "settings.json"
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
SHELL_TOOLS = {"Bash", "PowerShell"}
TEMP = os.path.realpath(tempfile.gettempdir())

DELETE_VERBS = {"rm", "rmdir", "rd", "del", "erase", "unlink", "shred", "remove-item", "ri"}
MOVE_VERBS = {"mv", "move", "move-item", "mi"}
# Deletes we can't attribute to specific paths: always blocked.
ALWAYS_BLOCK = re.compile(
    r"\s-delete\b|rmtree|os\.(?:remove|unlink)|unlinkSync|fs\.rm|::Delete\(|\bgit\s+(?:clean\b|reset\s+--hard)",
    re.I,
)
LOOSE_DELETE = re.compile(r"\b(?:" + "|".join(DELETE_VERBS) + r")\b", re.I)
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;\n|]")
# ponytail: a command that starts with ssh is treated as remote as a whole, so
# "ssh host x; rm local" slips through. Split segments if that ever matters.
REMOTE_RE = re.compile(r"^\s*(ssh|plink)\b", re.I)


# ---------- command parsing ----------

def segments(command):
    """Yield (text, tokens) per simple command; tokens is None if it won't parse."""
    for seg in SEGMENT_SPLIT.split(command):
        try:
            toks = shlex.split(seg.replace("\\", "/") if IS_WIN else seg)
        except ValueError:
            yield seg, None
            continue
        while toks and toks[0].lower() in ("sudo", "cmd", "cmd.exe", "/c", "&"):
            toks = toks[1:]
        yield seg, toks


def delete_targets(command, cwd):
    """[] = no delete; list = absolute paths being deleted; None = a delete we can't pin down."""
    if REMOTE_RE.match(command):
        return []
    if ALWAYS_BLOCK.search(command):
        return None
    targets = []
    for seg, toks in segments(command):
        if toks is None:
            if LOOSE_DELETE.search(seg):
                return None
            continue
        if toks and toks[0].lower() in DELETE_VERBS:
            # On Windows a leading "/" is a cmd switch (del /q) or a Git Bash path we can't map: skip it,
            # which leaves no target and so blocks.
            args = [t for t in toks[1:] if not t.startswith("-") and not (IS_WIN and t.startswith("/"))]
            if not args or any("$" in a for a in args):
                return None
            targets += [os.path.abspath(os.path.join(cwd, a)) for a in args]
    return targets


def is_delete(command, cwd="."):
    return delete_targets(command, cwd) != []


def in_temp(path):
    p = os.path.realpath(path)
    try:
        return os.path.commonpath([p, TEMP]) == TEMP and p != TEMP
    except ValueError:  # different drives
        return False


def parse_moves(command, cwd):
    """Yield (src, dst) absolute pairs for mv / Move-Item segments in a command."""
    for _, toks in segments(command):
        if not toks or toks[0].lower() not in MOVE_VERBS:
            continue
        pos, named, it = [], {}, iter(toks[1:])
        for t in it:
            low = t.lower()
            if low in ("-path", "-literalpath", "-destination"):
                named[low] = next(it, None)
            elif not t.startswith("-"):
                pos.append(t)
        src, dst = named.get("-path") or named.get("-literalpath"), named.get("-destination")
        if src:
            srcs, dst = [src], dst or (pos[0] if pos else None)
        elif dst:
            srcs = pos
        else:
            srcs, dst = pos[:-1], pos[-1] if len(pos) >= 2 else None
        if not dst:
            continue
        dst = os.path.abspath(os.path.join(cwd, dst))
        for s in filter(None, srcs):
            s = os.path.abspath(os.path.join(cwd, s))
            yield s, os.path.join(dst, os.path.basename(s)) if os.path.isdir(dst) else dst


# ---------- log ----------

def log(entry):
    DATA.mkdir(exist_ok=True)
    entry = {"id": uuid.uuid4().hex[:12], "time": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_log():
    if not LOG.exists():
        return []
    entries = [json.loads(l) for l in LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    restored = {e["of"] for e in entries if e["op"] == "restored"}
    out = []
    for e in entries:
        if e["op"] == "restored":
            continue
        e["status"] = "restored" if e["id"] in restored else _status(e)
        out.append(e)
    return out


def _status(e):
    held = e.get("trashed") if e["op"] == "trash" else e.get("dst")
    if not held:
        return "unknown"
    return "restorable" if os.path.lexists(held) else "missing"


# ---------- trash ----------

def trash(path):
    p = os.path.abspath(path)
    if not os.path.lexists(p):
        raise FileNotFoundError(p)
    if IS_WIN:
        trashed = _trash_windows(p)
    elif IS_MAC:
        trashed = _trash_mac(p)
    else:
        raise OSError("Backpaw supports Windows and macOS")
    return log({"op": "trash", "path": p, "trashed": trashed})


def _trash_windows(p):
    import ctypes
    from ctypes import wintypes

    drive = os.path.splitdrive(p)[0]
    if not os.path.isdir(drive + "\\$Recycle.Bin"):
        # Without a Recycle Bin the shell would delete permanently.
        raise OSError(f"{drive} has no Recycle Bin; refusing to delete {p}")

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE = 3
    # silent, no confirm, recycle, no error UI -- but WANTNUKEWARNING still asks
    # before anything too big for the bin gets deleted permanently.
    flags = 0x4 | 0x10 | 0x40 | 0x400 | 0x4000
    started = time.time()
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=p + "\0\0", fFlags=flags)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted:
        raise OSError(f"Recycle Bin move failed for {p} (code {rc})")
    found = _find_in_recycle_bin(p, since=started - 2)
    if not found:
        raise OSError(f"{p} was removed but not found in the Recycle Bin")
    return found


def _read_info_file(ipath):
    """Parse a $I file -> (original path, deleted unix time)."""
    raw = Path(ipath).read_bytes()
    version, _size, filetime = struct.unpack_from("<qqq", raw)
    if version >= 2:
        (n,) = struct.unpack_from("<i", raw, 24)
        name = raw[28:28 + n * 2].decode("utf-16-le")
    else:
        name = raw[24:24 + 520].decode("utf-16-le")
    return name.split("\0", 1)[0], filetime / 1e7 - 11644473600


def _find_in_recycle_bin(p, since):
    drive = os.path.splitdrive(p)[0]
    best = None
    for ipath in glob.glob(drive + "\\$Recycle.Bin\\*\\$I*"):
        try:
            orig, when = _read_info_file(ipath)
        except (OSError, struct.error, UnicodeDecodeError):
            continue
        if orig.lower() == p.lower() and when >= since and (best is None or when > best[0]):
            best = (when, ipath)
    if best:
        d, name = os.path.split(best[1])
        return os.path.join(d, "$R" + name[2:])
    return None


def _trash_mac(p):
    script = f'tell application "Finder" to POSIX path of ((delete POSIX file {json.dumps(p)}) as alias)'
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if out.returncode:
        raise OSError(out.stderr.strip())
    return out.stdout.strip().rstrip("/")


# ---------- restore ----------

def restore(entry):
    if entry["op"] == "trash":
        held, home = entry["trashed"], entry["path"]
    elif entry["op"] == "move":
        held, home = entry["dst"], entry["src"]
    else:
        raise ValueError(f"can't restore a {entry['op']!r} entry")
    if not os.path.lexists(held):
        raise FileNotFoundError(f"{held} is missing (Recycle Bin emptied, or the move never happened)")
    if os.path.lexists(home):
        raise FileExistsError(f"{home} already exists; not overwriting")
    os.makedirs(os.path.dirname(home), exist_ok=True)
    shutil.move(held, home)
    if IS_WIN and entry["op"] == "trash":
        d, name = os.path.split(held)
        info = os.path.join(d, "$I" + name[2:])
        if os.path.exists(info):
            os.remove(info)
    log({"op": "restored", "of": entry["id"]})


# ---------- backup check ----------

def backup_warnings(folder):
    folder = os.path.abspath(folder)
    warnings = []
    if IS_WIN:
        roots = [os.environ.get(k) for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
        if not any(r and folder.lower().startswith(os.path.abspath(r).lower()) for r in roots):
            warnings.append(f"{folder} is not inside OneDrive, so it has no cloud backup or version history.")
    elif IS_MAC:
        icloud = str(HOME / "Library" / "Mobile Documents")
        tm = subprocess.run(["tmutil", "destinationinfo"], capture_output=True, text=True).stdout
        if not folder.startswith(icloud) and "Name" not in tm:
            warnings.append(f"{folder} is not in iCloud Drive and Time Machine has no backup destination.")
    return warnings


# ---------- guard ----------

BLOCK_DELETE = ("Backpaw blocked a command that permanently deletes files or discards uncommitted work.\n"
                "To delete files, send them to the Recycle Bin/Trash so they can be restored:\n  {trash}\n"
                "For git clean / reset --hard: commit or stash first, or ask the user.\n"
                "Run any remaining (non-delete) parts of the command separately.")
BLOCK_CLOBBER = "Backpaw blocked a move that would overwrite {files}.\nSend the existing file to the Recycle Bin first:\n  {trash}"


def guard(command, cwd):
    """Agent-independent decision: None to allow, or the reason to block. Logs allowed moves."""
    trash_cmd = f'python "{Path(__file__).resolve().as_posix()}" trash <path> [<path> ...]'
    targets = delete_targets(command, cwd)
    if targets is None or (targets and not all(map(in_temp, targets))):
        return BLOCK_DELETE.format(trash=trash_cmd)
    try:
        moves = list(parse_moves(command, cwd))
    except Exception:  # never let a parse bug crash the hook; the delete check already ran
        return None
    clobbered = [d for _, d in moves if os.path.isfile(d)]
    if clobbered:
        return BLOCK_CLOBBER.format(files=", ".join(clobbered), trash=trash_cmd)
    for src, dst in moves:
        log({"op": "move", "src": src, "dst": dst, "command": command})
    return None


# ---------- agents ----------
# Each agent: how to read its hook payload, how to answer, and where its hook config lives.

def _extract(agent, data):
    """-> (command, cwd) from an agent's hook payload; command is None if it isn't a shell call."""
    if agent == "cursor":
        return data.get("command"), data.get("cwd") or (data.get("workspace_roots") or [None])[0]
    if agent == "windsurf":
        info = data.get("tool_info") or {}
        return info.get("command_line"), info.get("cwd")
    if agent == "copilot":
        if data.get("toolName") not in ("bash", "powershell", "shell"):
            return None, None
        args = data.get("toolArgs") or {}
        args = json.loads(args) if isinstance(args, str) else args
        return args.get("command"), data.get("cwd")
    # claude, codex, gemini share the tool_name / tool_input shape
    return (data.get("tool_input") or {}).get("command"), data.get("cwd")


def hook(agent="claude"):
    data = json.load(sys.stdin)
    if data.get("hook_event_name") == "SessionStart":
        w = backup_warnings(data.get("cwd") or os.getcwd())
        if w:
            print(json.dumps({"systemMessage": "Backpaw: " + " ".join(w)}))
        return 0
    command, cwd = _extract(agent, data)
    if isinstance(command, list):
        command = " ".join(map(str, command))
    if not command:
        return 0
    reason = guard(command, cwd or os.getcwd())
    if not reason:
        return 0
    if agent == "cursor":
        print(json.dumps({"permission": "deny", "user_message": "Backpaw blocked a permanent delete.",
                          "agent_message": reason}))
        return 0
    if agent == "copilot":
        print(json.dumps({"permissionDecision": "deny", "permissionDecisionReason": reason}))
        return 0
    print(reason, file=sys.stderr)  # claude, codex, gemini, windsurf: exit 2 + stderr blocks
    return 2


def _hook_cmd(agent, powershell=False):
    """Hook command line, quoted only where needed so one string works in bash and cmd."""
    parts = [Path(sys.executable).as_posix(), Path(__file__).resolve().as_posix(), "hook", agent]
    line = " ".join(f'"{p}"' if " " in p else p for p in parts)
    return "& " + line if powershell and " " in parts[0] else line


def _agents():
    def cc(agent, matcher):  # Claude-style matcher group
        return {"matcher": matcher, "hooks": [{"type": "command", "command": _hook_cmd(agent)}]}

    return {
        "claude": dict(name="Claude Code", home=CLAUDE, config=SETTINGS, base={},
                       events={"PreToolUse": cc("claude", "Bash|PowerShell"), "SessionStart": cc("claude", "")}),
        "codex": dict(name="Codex", home=HOME / ".codex", config=HOME / ".codex" / "hooks.json", base={},
                      events={"PreToolUse": cc("codex", "Bash"), "SessionStart": cc("codex", "")}),
        "gemini": dict(name="Gemini CLI", home=HOME / ".gemini", config=HOME / ".gemini" / "settings.json", base={},
                       events={"BeforeTool": cc("gemini", "run_shell_command"),
                               "SessionStart": cc("gemini", "startup")}),
        "cursor": dict(name="Cursor", home=HOME / ".cursor", config=HOME / ".cursor" / "hooks.json",
                       base={"version": 1}, events={"beforeShellExecution": {"command": _hook_cmd("cursor")}}),
        "copilot": dict(name="GitHub Copilot CLI", home=HOME / ".copilot",
                        config=HOME / ".copilot" / "hooks" / "backpaw.json", base={"version": 1},
                        events={"preToolUse": {"type": "command", "bash": _hook_cmd("copilot"),
                                               "powershell": _hook_cmd("copilot", True), "timeoutSec": 30}}),
        "windsurf": dict(name="Windsurf", home=HOME / ".codeium" / "windsurf",
                         config=HOME / ".codeium" / "windsurf" / "hooks.json", base={},
                         events={"pre_run_command": {"command": _hook_cmd("windsurf"),
                                                     "powershell": _hook_cmd("windsurf", True), "show_output": True}}),
        "qwen": dict(name="Qwen Code", home=HOME / ".qwen", config=HOME / ".qwen" / "settings.json", base={},
                     events={"PreToolUse": cc("qwen", "run_shell_command")}),
        "kimi": dict(name="Kimi Code CLI", home=HOME / ".kimi-code", config=HOME / ".kimi-code" / "config.toml",
                     kind="toml"),
        "opencode": dict(name="OpenCode", home=HOME / ".config" / "opencode",
                         config=HOME / ".config" / "opencode" / "plugins" / "backpaw.js", kind="plugin"),
    }


# Kimi's config is TOML; Backpaw owns only the marked block.
TOML_BLOCK = """# >>> backpaw
[[hooks]]
event = "PreToolUse"
matcher = "Bash"
command = '{cmd}'
timeout = 30
# <<< backpaw
"""
TOML_BLOCK_RE = re.compile(r"\n*# >>> backpaw\n.*?# <<< backpaw\n?", re.S)

# OpenCode has no command hooks, only JS plugins: this one forwards bash calls to `backpaw hook`.
OPENCODE_PLUGIN = """// Backpaw guard for OpenCode. Written by `backpaw install opencode`; remove with `backpaw uninstall opencode`.
import { spawnSync } from "node:child_process"

export const Backpaw = async ({ directory }) => ({
  "tool.execute.before": async (input, output) => {
    if (input.tool !== "bash") return
    const payload = JSON.stringify({ tool_name: "Bash", tool_input: { command: output.args.command },
                                     cwd: output.args.workdir || directory })
    const r = spawnSync(__PYTHON__, [__SCRIPT__, "hook", "opencode"], { input: payload, encoding: "utf8" })
    if (r.status === 2) throw new Error(r.stderr)
  },
})
"""


def _read_text(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _read_json(path):
    return json.loads(_read_text(path) or "{}")


def _write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".backpaw-bak"))
    path.write_text(text, encoding="utf-8")


def _write_json(path, cfg):
    _write_text(path, json.dumps(cfg, indent=2))


def _without_backpaw(items):
    return [i for i in items if "backpaw" not in json.dumps(i)]


def detected_agents():
    return [k for k, a in _agents().items() if a["home"].is_dir()]


def installed(agent):
    a = _agents()[agent]
    try:
        if a.get("kind") == "plugin":
            return a["config"].exists()
        if a.get("kind") == "toml":
            return "# >>> backpaw" in _read_text(a["config"])
        return "backpaw" in json.dumps(_read_json(a["config"]).get("hooks", {}))
    except (OSError, ValueError):
        return False


def install(agent):
    a = _agents()[agent]
    if a.get("kind") == "plugin":
        js = OPENCODE_PLUGIN.replace("__PYTHON__", json.dumps(Path(sys.executable).as_posix()))
        _write_text(a["config"], js.replace("__SCRIPT__", json.dumps(Path(__file__).resolve().as_posix())))
        return f"{a['name']}: guard plugin written to {a['config']}. Restart it to activate."
    if a.get("kind") == "toml":
        text = TOML_BLOCK_RE.sub("\n", _read_text(a["config"])).rstrip("\n")
        _write_text(a["config"], (text + "\n\n" if text else "") + TOML_BLOCK.format(cmd=_hook_cmd(agent)))
        return f"{a['name']}: guard installed in {a['config']}. Restart it to activate."
    cfg = _read_json(a["config"])
    for k, v in a["base"].items():
        cfg.setdefault(k, v)
    hooks = cfg.setdefault("hooks", {})
    for event, item in a["events"].items():
        hooks[event] = _without_backpaw(hooks.get(event, [])) + [item]
    _write_json(a["config"], cfg)
    return f"{a['name']}: guard installed in {a['config']}. Restart it to activate."


def uninstall(agent):
    a = _agents()[agent]
    if not a["config"].exists():
        return f"{a['name']}: not installed."
    if a.get("kind") == "plugin":
        a["config"].unlink()
        return f"{a['name']}: guard plugin removed. Restart it."
    if a.get("kind") == "toml":
        _write_text(a["config"], TOML_BLOCK_RE.sub("\n", _read_text(a["config"])).strip("\n") + "\n")
        return f"{a['name']}: guard removed. Restart it."
    cfg = _read_json(a["config"])
    hooks = cfg.get("hooks", {})
    for event in list(hooks):
        hooks[event] = _without_backpaw(hooks[event])
        if not hooks[event]:
            del hooks[event]
    _write_json(a["config"], cfg)
    return f"{a['name']}: guard removed. Restart it."


# ---------- transcript scan ----------

def scan():
    """Deletes/moves Claude ran in past sessions, newest first."""
    found = []
    for f in (CLAUDE / "projects").glob("*/*.jsonl"):
        for line in f.open(encoding="utf-8", errors="replace"):
            if '"tool_use"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            for c in (rec.get("message") or {}).get("content") or []:
                if not isinstance(c, dict) or c.get("type") != "tool_use" or c.get("name") not in SHELL_TOOLS:
                    continue
                cmd = (c.get("input") or {}).get("command", "")
                cwd = rec.get("cwd", "")
                kind = "delete" if is_delete(cmd, cwd or ".") else "move" if any(
                    t and t[0].lower() in MOVE_VERBS for _, t in segments(cmd)) else None
                if kind:
                    found.append({"time": rec.get("timestamp", "")[:19].replace("T", " "), "kind": kind,
                                  "cwd": cwd, "command": cmd})
    return sorted(found, key=lambda r: r["time"], reverse=True)


# ---------- GUI ----------

LIGHT = dict(bg="#f5f6f8", card="#ffffff", fg="#1f2328", muted="#6b7280", border="#e5e7eb",
             accent="#d97757", accent_fg="#ffffff", stripe="#fafafa", select="#fde7dc",
             ok="#15803d", bad="#b91c1c", warn_bg="#fef3c7", warn_fg="#92400e")
DARK = dict(bg="#1b1c1f", card="#25262a", fg="#e8e8ea", muted="#9ca3af", border="#34353a",
            accent="#d97757", accent_fg="#ffffff", stripe="#2a2b30", select="#4a2f25",
            ok="#4ade80", bad="#f87171", warn_bg="#3d3115", warn_fg="#fcd34d")


def _dark_mode():
    try:
        if IS_WIN:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
        if IS_MAC:
            return "Dark" in subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                            capture_output=True, text=True).stdout
    except OSError:
        pass
    return False


def open_path(target):
    if IS_WIN:
        os.startfile(target)
    else:
        subprocess.Popen(["open", target])


def gui():
    import tkinter as tk
    import webbrowser
    from tkinter import font, messagebox, ttk

    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    C = DARK if _dark_mode() else LIGHT
    root = tk.Tk()
    root.title("Backpaw")
    k = root.winfo_fpixels("1i") / 96
    root.geometry(f"{int(1080 * k)}x{int(640 * k)}")
    root.minsize(int(760 * k), int(420 * k))
    root.configure(bg=C["bg"])

    base = font.nametofont("TkDefaultFont")
    base.configure(size=10)
    title_font = base.copy()
    title_font.configure(size=18, weight="bold")
    bold = base.copy()
    bold.configure(weight="bold")

    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=C["bg"], foreground=C["fg"], font=base, bordercolor=C["border"])
    s.configure("Card.TFrame", background=C["card"])
    s.configure("Muted.TLabel", foreground=C["muted"])
    s.configure("Title.TLabel", font=title_font)
    s.configure("TButton", padding=(14, 7), background=C["card"], foreground=C["fg"], borderwidth=1,
                focusthickness=0, relief="flat")
    s.map("TButton", background=[("active", C["border"])])
    s.configure("Accent.TButton", background=C["accent"], foreground=C["accent_fg"], borderwidth=0)
    s.map("Accent.TButton", background=[("active", "#c4623f")])
    s.configure("Treeview", background=C["card"], fieldbackground=C["card"], foreground=C["fg"],
                rowheight=30, borderwidth=0)
    s.map("Treeview", background=[("selected", C["select"])], foreground=[("selected", C["fg"])])
    s.configure("Treeview.Heading", background=C["bg"], foreground=C["muted"], font=bold,
                relief="flat", padding=(8, 6))
    s.map("Treeview.Heading", background=[("active", C["bg"])])
    s.configure("TNotebook", background=C["bg"], borderwidth=0, tabmargins=0)
    s.configure("TNotebook.Tab", background=C["bg"], foreground=C["muted"], padding=(16, 8), borderwidth=0)
    s.map("TNotebook.Tab", background=[("selected", C["card"])], foreground=[("selected", C["fg"])])
    s.configure("Vertical.TScrollbar", background=C["bg"], troughcolor=C["card"], borderwidth=0, arrowsize=0)

    # --- header ---
    header = ttk.Frame(root, padding=(20, 16, 20, 8))
    header.pack(fill="x")
    ttk.Label(header, text="🐾 Backpaw", style="Title.TLabel").pack(side="left")
    ttk.Label(header, text="  Get back what your AI agent deleted or moved", style="Muted.TLabel").pack(side="left", pady=(6, 0))

    def about():
        win = tk.Toplevel(root, bg=C["bg"], padx=28, pady=22)
        win.title("About Backpaw")
        win.resizable(False, False)
        win.transient(root)
        ttk.Label(win, text="🐾 Backpaw", style="Title.TLabel").pack(anchor="w")
        ttk.Label(win, text=f"Version {__version__}", style="Muted.TLabel").pack(anchor="w")
        ttk.Label(win, text="Get back what AI coding agents deleted or moved.\nRecycle Bin guard, move log, and restore.",
                  justify="left").pack(anchor="w", pady=(12, 12))
        on = [a["name"] for k, a in _agents().items() if installed(k)]
        for label, value in (("Guard", ", ".join(on) or "off"), ("Log", str(LOG)),
                             ("Python", sys.version.split()[0]), ("Platform", sys.platform)):
            row = ttk.Frame(win)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label, width=10, style="Muted.TLabel").pack(side="left")
            ttk.Label(row, text=value).pack(side="left")
        link = ttk.Label(win, text=REPO_URL, foreground=C["accent"], cursor="hand2")
        link.pack(anchor="w", pady=(12, 12))
        link.bind("<Button-1>", lambda _: webbrowser.open(REPO_URL))
        ttk.Button(win, text="Close", command=win.destroy).pack(anchor="e")

    ttk.Button(header, text="About", command=about).pack(side="right")
    guard_btn = ttk.Button(header)
    guard_btn.pack(side="right", padx=8)
    guard_lbl = tk.Label(header, font=bold, bg=C["bg"], padx=10, pady=4)
    guard_lbl.pack(side="right")

    def refresh_guard():
        on = [a["name"] for k, a in _agents().items() if installed(k)]
        guard_lbl.configure(text="● Guard on · " + ", ".join(on) if on else "● Guard off",
                            fg=C["ok"] if on else C["bad"])
        guard_btn.configure(text="Agents…", style="TButton" if on else "Accent.TButton", command=agents_dialog)

    def agents_dialog():
        win = tk.Toplevel(root, bg=C["bg"], padx=24, pady=20)
        win.title("Backpaw - agents")
        win.transient(root)
        win.resizable(False, False)
        ttk.Label(win, text="AI coding agents", font=bold).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        found = detected_agents()

        def add_row(r, key, a):
            on = installed(key)
            state = "guard on" if on else "guard off" if key in found else "not installed"
            ttk.Label(win, text=a["name"], width=22).grid(row=r, column=0, sticky="w", pady=4)
            tk.Label(win, text="● " + state, bg=C["bg"], fg=C["ok"] if on else C["muted"]).grid(
                row=r, column=1, sticky="w", padx=14)

            def toggle():
                if on and not messagebox.askyesno("Backpaw", f"Turn off the guard for {a['name']}? "
                                                  "Its deletes will be permanent again.", parent=win):
                    return
                messagebox.showinfo("Backpaw", uninstall(key) if on else install(key), parent=win)
                win.destroy()
                refresh_guard()
                agents_dialog()

            if on or key in found:
                ttk.Button(win, text="Turn off" if on else "Turn on", command=toggle,
                           style="TButton" if on else "Accent.TButton").grid(row=r, column=2, sticky="e")

        for r, (key, a) in enumerate(_agents().items(), start=1):
            add_row(r, key, a)
        ttk.Label(win, text="Restart an agent after changing its guard.", style="Muted.TLabel").grid(
            row=99, column=0, columnspan=3, sticky="w", pady=(12, 0))

    # --- warnings ---
    folders = {os.path.abspath(r["cwd"]) for r in scan() if r["cwd"]} | {os.getcwd()}
    folders = [f for f in folders if not any(f != g and f.startswith(g + os.sep) for g in folders)]
    warnings = sorted({w for f in folders for w in backup_warnings(f)})
    if warnings:
        bar = tk.Frame(root, bg=C["warn_bg"], padx=14, pady=10)
        bar.pack(fill="x", padx=20, pady=(4, 8))
        tk.Label(bar, text="⚠  Not backed up", font=bold, bg=C["warn_bg"], fg=C["warn_fg"]).pack(anchor="w")
        tk.Label(bar, text="\n".join(warnings), bg=C["warn_bg"], fg=C["warn_fg"], justify="left").pack(anchor="w")
        if IS_WIN:
            act = tk.Frame(bar, bg=C["warn_bg"])
            act.pack(anchor="w", pady=(6, 0))
            ttk.Button(act, text="System Protection (shadow copies)…",
                       command=lambda: subprocess.Popen(["SystemPropertiesProtection.exe"])).pack(side="left")
            ttk.Button(act, text="OneDrive backup settings…",
                       command=lambda: open_path("ms-settings:backup")).pack(side="left", padx=6)

    # --- tabs ---
    tabs = ttk.Notebook(root)
    tabs.pack(fill="both", expand=True, padx=20, pady=(0, 16))

    def table(parent, cols, widths):
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.pack(fill="both", expand=True)
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        for c, w in zip(cols, widths):
            tv.heading(c, text=c.title(), anchor="w")
            tv.column(c, width=int(w * k), anchor="w", stretch=c in ("path", "command"))
        sb = ttk.Scrollbar(frame, command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tv.tag_configure("stripe", background=C["stripe"])
        for status, color in (("restorable", C["fg"]), ("missing", C["muted"]),
                              ("restored", C["muted"]), ("unknown", C["muted"])):
            tv.tag_configure(status, foreground=color)
        return tv

    restore_tab = ttk.Frame(tabs, style="Card.TFrame", padding=12)
    history_tab = ttk.Frame(tabs, style="Card.TFrame", padding=12)
    tabs.add(restore_tab, text="Restore")
    tabs.add(history_tab, text="Session history")

    btns = ttk.Frame(restore_tab, style="Card.TFrame")
    btns.pack(side="bottom", fill="x", pady=(10, 0))

    tv = table(restore_tab, ("time", "action", "path", "status"), (175, 80, 640, 110))
    entries = {}

    def refresh():
        tv.delete(*tv.get_children())
        entries.clear()
        for n, e in enumerate(reversed(read_log())):
            entries[e["id"]] = e
            path = e.get("path") or f'{e.get("src")}  →  {e.get("dst")}'
            tags = (e["status"], "stripe") if n % 2 else (e["status"],)
            tv.insert("", "end", iid=e["id"], values=(e["time"], e["op"], path, e["status"]), tags=tags)
        n = sum(e["status"] == "restorable" for e in entries.values())
        count_lbl.configure(text=f"{n} restorable · {len(entries)} logged" if entries else
                            "Nothing logged yet.")

    def select_restorable():
        tv.selection_set([i for i, e in entries.items() if e["status"] == "restorable"])

    def do_restore():
        picked = [entries[i] for i in tv.selection() if entries[i]["status"] == "restorable"]
        if not picked:
            messagebox.showinfo("Backpaw", "Select one or more rows marked 'restorable'.")
            return
        errors = []
        for e in picked:
            try:
                restore(e)
            except Exception as ex:  # report each failure, keep going
                errors.append(str(ex))
        refresh()
        messagebox.showinfo("Backpaw", f"Restored {len(picked) - len(errors)} item(s)."
                            + ("\n\nProblems:\n" + "\n".join(errors) if errors else ""))

    ttk.Button(btns, text="Restore selected", style="Accent.TButton", command=do_restore).pack(side="right")
    ttk.Button(btns, text="Select all restorable", command=select_restorable).pack(side="right", padx=6)
    ttk.Button(btns, text="Refresh", command=refresh).pack(side="right")
    count_lbl = ttk.Label(btns, style="Muted.TLabel", background=C["card"])
    count_lbl.pack(side="left")

    hbtns = ttk.Frame(history_tab, style="Card.TFrame")
    hbtns.pack(side="bottom", fill="x", pady=(10, 0))
    ttk.Label(hbtns, style="Muted.TLabel", background=C["card"],
              text="Commands Claude ran in past sessions. These can't be undone here. "
                   "Check the Recycle Bin or OneDrive version history.").pack(side="left")
    if IS_WIN:
        ttk.Button(hbtns, text="Open Recycle Bin",
                   command=lambda: open_path("shell:RecycleBinFolder")).pack(side="right")
    hv = table(history_tab, ("time", "kind", "folder", "command"), (175, 70, 260, 560))
    for n, r in enumerate(scan()):
        hv.insert("", "end", values=(r["time"], r["kind"], r["cwd"], r["command"].replace("\n", " ⏎ ")[:300]),
                  tags=("stripe",) if n % 2 else ())

    refresh_guard()
    refresh()
    root.mainloop()


def main(argv):
    cmd, args = (argv[0], argv[1:]) if argv else ("gui", [])
    if cmd == "trash":
        failed = 0
        for p in args:
            try:
                print("trashed:", trash(p)["path"])
            except OSError as ex:
                failed = 1
                print("error:", ex, file=sys.stderr)
        return failed
    if cmd == "list":
        for e in read_log():
            print(e["time"], e["op"], e.get("path") or f'{e["src"]} -> {e["dst"]}', e["status"], sep="  ")
    elif cmd == "scan":
        for r in scan():
            print(r["time"], r["kind"], r["cwd"], r["command"].replace("\n", " ")[:200], sep="  ")
    elif cmd == "check":
        w = backup_warnings(args[0] if args else os.getcwd())
        print("\n".join(w) or "OK: folder is backed up.")
    elif cmd in ("install", "uninstall"):
        agents = _agents()
        targets = args or (detected_agents() if cmd == "install" else [k for k in agents if installed(k)])
        unknown = [t for t in targets if t not in agents]
        if unknown:
            print(f"unknown agent(s): {', '.join(unknown)}. Choose from: {', '.join(agents)}", file=sys.stderr)
            return 1
        for t in targets:
            print((install if cmd == "install" else uninstall)(t))
        if not targets:
            print("No agents found." if cmd == "install" else "Guard is not on for any agent.")
    elif cmd == "agents":
        found = detected_agents()
        for k, a in _agents().items():
            state = "guard on" if installed(k) else "guard off" if k in found else "not installed"
            print(f"{k:<9} {a['name']:<20} {state}")
    elif cmd in ("--version", "-V", "version"):
        print(f"backpaw {__version__}")
    elif cmd == "hook":
        return hook(args[0] if args else "claude")
    elif cmd == "gui":
        gui()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
