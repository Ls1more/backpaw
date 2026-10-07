"""Smoke test: python test_backpaw.py  (uses a temp data dir; really trashes a temp file)."""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import backpaw

tmp = Path(tempfile.mkdtemp(prefix="backpaw-test-"))
backpaw.DATA, backpaw.LOG = tmp / "data", tmp / "data" / "log.jsonl"
home = str(Path.home())


def run_hook(command, cwd, tool="Bash"):
    sys.stdin = io.StringIO(json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                                        "tool_input": {"command": command}, "cwd": str(cwd)}))
    sys.stderr, err = io.StringIO(), sys.stderr
    try:
        return backpaw.hook()
    finally:
        sys.stderr = err


# deletes are blocked outside temp...
for cmd in ["rm -rf build", "ls && rm x", "Remove-Item foo -Recurse", "del a.txt", "cmd /c del x.txt",
            "find . -name '*.o' -delete", "python -c \"import shutil; shutil.rmtree('x')\"", "cd x; rmdir y",
            "[IO.File]::Delete('a')", "git clean -fdx", "git reset --hard HEAD", "rm $FILE"]:
    assert run_hook(cmd, home) == 2, cmd
# ...but allowed inside the temp dir, and non-deletes pass
for cmd in ["rm -rf scratch", "Remove-Item x.txt", "ssh root@nas 'rm -f /tmp/x'", "git rm --cached a",
            "echo firmware", "npm run format", "python backpaw.py trash a", "git status"]:
    assert run_hook(cmd, tmp) == 0, cmd

# moves are logged, and a move onto an existing file is blocked
(tmp / "dir").mkdir()
assert run_hook("Move-Item -Path a.txt -Destination dir", tmp, "PowerShell") == 0
assert run_hook("mv b.txt c.txt", tmp) == 0
(tmp / "exists.txt").write_text("x")
assert run_hook("mv b.txt exists.txt", tmp) == 2
moves = [(e["src"], e["dst"]) for e in backpaw.read_log() if e["op"] == "move"]
assert moves == [(str(tmp / "a.txt"), str(tmp / "dir" / "a.txt")), (str(tmp / "b.txt"), str(tmp / "c.txt"))], moves

# move restore round trip
(tmp / "c.txt").write_text("moved")
entry = [e for e in backpaw.read_log() if e["op"] == "move"][1]
assert entry["status"] == "restorable"
backpaw.restore(entry)
assert (tmp / "b.txt").read_text() == "moved" and not (tmp / "c.txt").exists()
assert [e for e in backpaw.read_log() if e["id"] == entry["id"]][0]["status"] == "restored"

# real trash + restore round trip
victim = tmp / "victim.txt"
victim.write_text("save me")
t = backpaw.trash(victim)
assert not victim.exists() and os.path.exists(t["trashed"]), t
backpaw.restore(t)
assert victim.read_text() == "save me"

# install / uninstall round trip on a scratch settings file
backpaw.SETTINGS = tmp / "settings.json"
backpaw.SETTINGS.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "other"}]}]}}))
backpaw.install()
assert backpaw.guard_installed()
backpaw.uninstall()
assert not backpaw.guard_installed()
assert json.loads(backpaw.SETTINGS.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "other"

print("all backpaw checks passed")
