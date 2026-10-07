"""Backpaw: see what Claude Code deleted or moved, and put it back.

  backpaw                 open the restore window
  backpaw trash PATH...   send paths to the Recycle Bin / Trash (logged)
  backpaw list            print the log
  backpaw scan            print deletes/moves found in past Claude sessions
  backpaw check [DIR]     warn if DIR (default: cwd) is not backed up
  backpaw install         add the Backpaw hooks to ~/.claude/settings.json
  backpaw hook            (called by Claude Code) PreToolUse / SessionStart handler
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
import time
import uuid
from pathlib import Path

HOME = Path.home()
DATA = HOME / ".backpaw"
LOG = DATA / "log.jsonl"
CLAUDE = HOME / ".claude"
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
SHELL_TOOLS = {"Bash", "PowerShell"}

# A delete/move verb at command position: start, or after ; & | ( or a newline.
_POS = r"(?:^|[;&|(\n]\s*)"
DELETE_RE = re.compile(
    _POS + r"(?:sudo\s+)?(rm|rmdir|rd|del|erase|unlink|shred|Remove-Item|ri)\b"
    r"|\s-delete\b|rmtree|os\.(?:remove|unlink)|unlinkSync|fs\.rm",
    re.I,
)
MOVE_VERBS = {"mv", "move", "move-item", "mi"}
# ponytail: a command that starts with ssh is treated as remote as a whole, so
# "ssh host x; rm local" slips through. Split segments if that ever matters.
REMOTE_RE = re.compile(r"^\s*(ssh|plink)\b", re.I)


def is_delete(command):
    return not REMOTE_RE.match(command) and bool(DELETE_RE.search(command))
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;\n|]")


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
    return "restorable" if os.path.lexists(held) else "gone"


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
        raise FileNotFoundError(f"{held} is gone (Recycle Bin emptied or file moved again)")
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


# ---------- hook ----------

def parse_moves(command, cwd):
    """Yield (src, dst) absolute pairs for mv / Move-Item segments in a command."""
    for seg in SEGMENT_SPLIT.split(command):
        try:
            toks = shlex.split(seg.replace("\\", "/") if IS_WIN else seg)
        except ValueError:
            continue
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


def hook():
    data = json.load(sys.stdin)
    if data.get("hook_event_name") == "SessionStart":
        w = backup_warnings(data.get("cwd") or os.getcwd())
        if w:
            print(json.dumps({"systemMessage": "Backpaw: " + " ".join(w)}))
        return 0
    if data.get("tool_name") not in SHELL_TOOLS:
        return 0
    command = (data.get("tool_input") or {}).get("command", "")
    cwd = data.get("cwd") or os.getcwd()
    if is_delete(command):
        me = Path(__file__).resolve().as_posix()
        print(f"Backpaw blocked a delete. Send files to the Recycle Bin/Trash instead so they can be restored:\n"
              f'  python "{me}" trash <path> [<path> ...]\n'
              f"Then run any remaining (non-delete) parts of the command separately.", file=sys.stderr)
        return 2
    for src, dst in parse_moves(command, cwd):
        log({"op": "move", "src": src, "dst": dst, "command": command})
    return 0


def install():
    settings = CLAUDE / "settings.json"
    cfg = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    me = Path(__file__).resolve().as_posix()
    cmd = f'"{Path(sys.executable).as_posix()}" "{me}" hook'
    hooks = cfg.setdefault("hooks", {})
    for event, matcher in (("PreToolUse", "Bash|PowerShell"), ("SessionStart", "")):
        groups = hooks.setdefault(event, [])
        groups[:] = [g for g in groups if not any("backpaw" in h.get("command", "") for h in g.get("hooks", []))]
        groups.append({"matcher": matcher, "hooks": [{"type": "command", "command": cmd}]})
    if settings.exists():
        shutil.copy2(settings, settings.with_suffix(".json.backpaw-bak"))
    settings.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"Installed Backpaw hooks into {settings}")


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
                kind = "delete" if is_delete(cmd) else "move" if any(
                    s.strip().split(" ", 1)[0].lower() in MOVE_VERBS for s in SEGMENT_SPLIT.split(cmd)) else None
                if kind:
                    found.append({"time": rec.get("timestamp", "")[:19].replace("T", " "), "kind": kind,
                                  "cwd": rec.get("cwd", ""), "command": cmd})
    return sorted(found, key=lambda r: r["time"], reverse=True)


# ---------- GUI ----------

def gui():
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("Backpaw - restore what Claude deleted or moved")
    root.geometry("1000x560")

    folders = sorted({r["cwd"] for r in scan() if r["cwd"]} | {os.getcwd()})
    warnings = [w for f in folders for w in backup_warnings(f)]
    if warnings:
        bar = tk.Frame(root, bg="#fde68a")
        bar.pack(fill="x")
        tk.Label(bar, text="\n".join(["Warning:"] + warnings), bg="#fde68a", justify="left",
                 anchor="w").pack(side="left", padx=8, pady=4)
    if IS_WIN:
        tk.Button(root, text="Open System Protection (shadow copies) settings...",
                  command=lambda: subprocess.Popen(["SystemPropertiesProtection.exe"])).pack(anchor="e", padx=8, pady=4)

    tabs = ttk.Notebook(root)
    tabs.pack(fill="both", expand=True, padx=8, pady=4)

    def table(parent, cols, widths):
        frame = tk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        for c, w in zip(cols, widths):
            tv.heading(c, text=c.title())
            tv.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tv

    restore_tab, history_tab = tk.Frame(tabs), tk.Frame(tabs)
    tabs.add(restore_tab, text="Restore")
    tabs.add(history_tab, text="Session history (read-only)")

    tv = table(restore_tab, ("time", "action", "path", "status"), (140, 70, 640, 90))
    entries = {}

    def refresh():
        tv.delete(*tv.get_children())
        entries.clear()
        for e in reversed(read_log()):
            entries[e["id"]] = e
            path = e.get("path") or f'{e.get("src")}  ->  {e.get("dst")}'
            tv.insert("", "end", iid=e["id"], values=(e["time"], e["op"], path, e["status"]))

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

    btns = tk.Frame(restore_tab)
    btns.pack(fill="x", pady=6)
    tk.Button(btns, text="Restore selected", command=do_restore, padx=12).pack(side="left")
    tk.Button(btns, text="Refresh", command=refresh).pack(side="left", padx=6)

    hv = table(history_tab, ("time", "kind", "folder", "command"), (140, 60, 260, 520))
    for r in scan():
        hv.insert("", "end", values=(r["time"], r["kind"], r["cwd"], r["command"].replace("\n", " ⏎ ")[:300]))
    tk.Label(history_tab, anchor="w", justify="left",
             text="Commands Claude ran before Backpaw was guarding. Check the Recycle Bin or OneDrive version history for these.").pack(fill="x")

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
    elif cmd == "install":
        install()
    elif cmd == "hook":
        return hook()
    elif cmd == "gui":
        gui()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
