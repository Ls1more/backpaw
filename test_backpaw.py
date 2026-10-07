"""Smoke test: python test_backpaw.py  (uses a temp HOME-style data dir; really trashes a temp file)."""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import backpaw

tmp = Path(tempfile.mkdtemp(prefix="backpaw-test-"))
backpaw.DATA, backpaw.LOG = tmp / "data", tmp / "data" / "log.jsonl"


def run_hook(payload):
    sys.stdin = io.StringIO(json.dumps(payload))
    return backpaw.hook()


# delete detection
for cmd in ["rm -rf build", "ls && rm x", "Remove-Item foo -Recurse", "del a.txt", "find . -name '*.o' -delete",
            "python -c \"import shutil; shutil.rmtree('x')\"", "cd x; rmdir y"]:
    assert backpaw.DELETE_RE.search(cmd), cmd
for cmd in ["ssh root@nas 'rm -f /tmp/x'", "git rm --cached a", "echo firmware", "npm run format", "python backpaw.py trash a", "mv a b"]:
    assert not backpaw.is_delete(cmd), cmd

# hook blocks deletes, allows + logs moves
assert run_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "rm a"}, "cwd": str(tmp)}) == 2
(tmp / "dir").mkdir()
assert run_hook({"hook_event_name": "PreToolUse", "tool_name": "PowerShell",
                 "tool_input": {"command": "Move-Item -Path a.txt -Destination dir"}, "cwd": str(tmp)}) == 0
assert run_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "mv b.txt c.txt"}, "cwd": str(tmp)}) == 0
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

print("all backpaw checks passed")
