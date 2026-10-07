"""Smoke test: python test_backpaw.py  (uses a temp data dir and fake agent configs; really trashes a temp file)."""
import io
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path

import backpaw

tmp = Path(tempfile.mkdtemp(prefix="backpaw-test-"))
backpaw.DATA, backpaw.LOG = tmp / "data", tmp / "data" / "log.jsonl"
home = str(Path.home())


def run_hook(command, cwd, tool="Bash"):
    return run_agent("claude", {"hook_event_name": "PreToolUse", "tool_name": tool,
                                "tool_input": {"command": command}, "cwd": str(cwd)})[0]


def run_agent(agent, payload):
    sys.stdin = io.StringIO(json.dumps(payload))
    out, err = io.StringIO(), io.StringIO()
    sys.stdout, sys.stderr = out, err
    try:
        return backpaw.hook(agent), out.getvalue(), err.getvalue()
    finally:
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__


# deletes are blocked outside temp...
for cmd in ["rm -rf build", "ls && rm x", "Remove-Item foo -Recurse", "del a.txt", "cmd /c del x.txt",
            "find . -name '*.o' -delete", "python -c \"import shutil; shutil.rmtree('x')\"", "cd x; rmdir y",
            "[IO.File]::Delete('a')", "git clean -fdx", "git reset --hard HEAD", "rm $FILE"]:
    assert run_hook(cmd, home) == 2, cmd
# ...but allowed inside the temp dir, and non-deletes pass
for cmd in ["rm -rf scratch", "Remove-Item x.txt", "ssh root@nas 'rm -f /tmp/x'", "git rm --cached a",
            "echo firmware", "npm run format", "python backpaw.py trash a", "git status"]:
    assert run_hook(cmd, tmp) == 0, cmd

# every agent's payload shape is understood, and answered in that agent's format
blocked = {
    "claude": {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "rm -rf src"}, "cwd": home},
    "codex": {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": ["rm", "-rf", "src"]}, "cwd": home},
    "gemini": {"hook_event_name": "BeforeTool", "tool_name": "run_shell_command", "tool_input": {"command": "rm -rf src"}, "cwd": home},
    "cursor": {"hook_event_name": "beforeShellExecution", "command": "rm -rf src", "cwd": home},
    "copilot": {"toolName": "bash", "toolArgs": {"command": "rm -rf src"}, "cwd": home},
    "windsurf": {"agent_action_name": "pre_run_command", "tool_info": {"command_line": "rm -rf src", "cwd": home}},
    "qwen": {"hook_event_name": "PreToolUse", "tool_name": "run_shell_command", "tool_input": {"command": "rm -rf src"}, "cwd": home},
    "kimi": {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "rm -rf src"}, "cwd": home},
    "opencode": {"tool_name": "Bash", "tool_input": {"command": "rm -rf src"}, "cwd": home},
}
for agent, payload in blocked.items():
    code, out, err = run_agent(agent, payload)
    if agent == "cursor":
        assert code == 0 and json.loads(out)["permission"] == "deny", (agent, out)
    elif agent == "copilot":
        assert code == 0 and json.loads(out)["permissionDecision"] == "deny", (agent, out)
    else:
        assert code == 2 and "Backpaw blocked" in err, (agent, code, err)
    # same payload with a harmless command is allowed silently
    safe = json.loads(json.dumps(payload).replace("rm -rf src", "ls").replace('["rm", "-rf", "src"]', '["ls"]'))
    assert run_agent(agent, safe) == (0, "", ""), agent
assert run_agent("copilot", {"toolName": "edit", "toolArgs": {"path": "x"}, "cwd": home}) == (0, "", "")
assert run_agent("copilot", {"toolName": "bash", "toolArgs": json.dumps({"command": "rm a"}), "cwd": home})[0] == 0

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

# install / uninstall every agent into a fake home, keeping unrelated hooks intact
fake = tmp / "home"
backpaw.HOME, backpaw.CLAUDE = fake, fake / ".claude"
backpaw.SETTINGS = backpaw.CLAUDE / "settings.json"
backpaw.SETTINGS.parent.mkdir(parents=True)
backpaw.SETTINGS.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "other"}]}]}}))
assert backpaw.detected_agents() == ["claude"]
kimi_cfg = backpaw._agents()["kimi"]["config"]
kimi_cfg.parent.mkdir(parents=True)
kimi_cfg.write_text('default_model = "kimi-k2"\n\n[models.kimi-k2]\nprovider = "moonshot"\n')
for agent, a in backpaw._agents().items():
    backpaw.install(agent)
    backpaw.install(agent)  # idempotent
    assert backpaw.installed(agent), agent
    text = a["config"].read_text()
    if a.get("kind") == "toml":
        cfg = tomllib.loads(text)
        assert len(cfg["hooks"]) == 1 and cfg["default_model"] == "kimi-k2", cfg
    elif a.get("kind") == "plugin":
        assert sys.executable.replace("\\", "/") in text and '"hook", "opencode"' in text
    else:
        cfg = json.loads(text)
        assert all(sum("backpaw" in json.dumps(i) for i in items) == 1 for items in cfg["hooks"].values()), (agent, cfg)
    backpaw.uninstall(agent)
    assert not backpaw.installed(agent), agent
assert tomllib.loads(kimi_cfg.read_text()) == {"default_model": "kimi-k2", "models": {"kimi-k2": {"provider": "moonshot"}}}
assert json.loads(backpaw.SETTINGS.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "other"

print("all backpaw checks passed")
