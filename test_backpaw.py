"""Smoke test: python test_backpaw.py  (uses a temp data dir and fake agent configs; really trashes a temp file)."""
import io
import json
import os
import subprocess
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
for cmd in ["rm -rf src", "ls && rm x", "Remove-Item foo -Recurse", "del a.txt", "cmd /c del x.txt",
            "find . -name '*.o' -delete", "python -c \"import shutil; shutil.rmtree('x')\"", "cd x; rmdir y",
            "[IO.File]::Delete('a')", "git clean -fdx", "git reset --hard HEAD", "rm $FILE"]:
    assert run_hook(cmd, home) == 2, cmd
# ...but allowed inside the temp dir, and non-deletes pass
for cmd in ["rm -rf scratch", "Remove-Item x.txt", "ssh root@nas 'rm -f /tmp/x'", "git rm --cached a",
            "echo firmware", "npm run format", "python backpaw.py trash a", "git status"]:
    assert run_hook(cmd, tmp) == 0, cmd

# v0.9.1 regression checks ------------------------------------------------------------------------------
# 1. home and variables expand in the shell, not in temp: blocked even when the agent's folder is in temp
for cmd in ["rm -rf ~", "rm -rf ~/Documents", "del %USERPROFILE%\\Documents", "rm -rf `pwd`/x", "rm -rf $HOME"]:
    assert run_hook(cmd, tmp) == 2, cmd
assert run_hook("rm -rf ./scratch", tmp) == 0  # real temp paths still fine
# 6. commands that launch a delete through another tool
for cmd in ["timeout 5 rm -rf src", "find . -name '*.log' -exec rm {} +", "fd -e tmp -x rm", "git -C repo clean -fdx",
            "git -c core.x=1 reset --hard", "git --no-pager stash drop", "python -c \"import os; os.system('rm -rf src')\"",
            "node -e \"require('child_process').execSync('rm -rf src')\"", "busybox rm -rf src"]:
    assert run_hook(cmd, home) == 2, cmd
for cmd in ["find . -name '*.log'", "timeout 5 npm test", "python -c \"print(1)\"", "git -C repo status"]:
    assert run_hook(cmd, home) == 0, cmd
# 9. only the ssh part is remote: local commands after it are checked
assert run_hook("ssh nas 'rm -rf /tmp/cache'", home) == 0
for cmd in ["ssh nas uptime; rm -rf src", "ssh nas uptime && git clean -fdx", "ssh nas 'ls'\nrm -rf src"]:
    assert run_hook(cmd, home) == 2, cmd
# 2. the hook reads UTF-8 whatever the console code page, and never fails open on a delete
script = str(Path(backpaw.__file__).resolve())
def hook_bytes(payload):
    return subprocess.run([sys.executable, script, "hook", "claude"], input=payload, capture_output=True).returncode
assert hook_bytes(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": home,
                              "tool_input": {"command": "rm -rf Documents # café Ámbar ✓"}},
                             ensure_ascii=False).encode("utf-8")) == 2
assert hook_bytes(b'{"tool_input": {"command": "rm -rf src"') == 2   # unreadable payload with a delete: blocked
assert hook_bytes(b'{"tool_input": {"command": "ls"') == 0           # unreadable payload, harmless: allowed
# 3/4. git is found by absolute path and always runs with hooks, signing and fsmonitor switched off
assert backpaw._git_exe() and os.path.isabs(backpaw._git_exe())
for opt in ("core.fsmonitor=false", "commit.gpgSign=false", "log.showSignature=false"):
    assert opt in backpaw.SAFE_GIT, opt
assert any(o.startswith("core.hooksPath=") for o in backpaw.SAFE_GIT)
# 7. changing Backpaw's files or agent hook settings from the shell needs the user's OK
for cmd in ["mv ~/.backpaw ~/.backpaw-old", "cat ~/.claude/settings.json", "notepad ~/.cursor/hooks.json"]:
    code, out, _ = run_agent("claude", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                        "tool_input": {"command": cmd}, "cwd": home})
    assert code == 0 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask", (cmd, out)
assert run_hook(f'"{sys.executable}" "{home}/.backpaw/backpaw.py" trash x.txt', tmp) == 0  # Backpaw's own trash
assert run_hook(f'python "{home}/.backpaw/backpaw.py" agents', home) == 0
# running uninstall is blocked; merely mentioning it (commit message, echo) is not
for cmd in ["python backpaw.py uninstall", "python ~/.backpaw/backpaw.py uninstall --all", "backpaw uninstall claude",
            "bash -c 'python backpaw.py uninstall'"]:
    assert run_hook(cmd, home) == 2, cmd
for cmd in ['git commit -m "docs: backpaw uninstall --all"', "echo 'run backpaw uninstall to remove it'"]:
    assert run_hook(cmd, home) == 0, cmd

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

# --- adversarial: ways an agent might sneak a delete, overwrite or self-disable past the guard ---
for cmd in ['bash -c "rm -rf ~/Documents"', "powershell -Command \"Remove-Item -Recurse C:/Users/x/Documents\"",
            'pwsh -NoProfile -c "rm -r src"', 'C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe "Remove-Item x"',
            'find . -name "*.py" | xargs rm', "xargs -0 rm -f < list", "env FOO=1 rm -rf src", "sudo -u root rm -rf /etc",
            "/bin/rm -rf src", "powershell -EncodedCommand UgBlAG0AbwB2AGUA", "pwsh -enc UgBlAG0A", "powershell -ec AAAA",
            "Clear-RecycleBin -Force", "Clear-Content important.log", "git checkout -- .", "git checkout HEAD -- src",
            "git restore src/app.py", "git stash clear", "git stash drop", "rsync -a --delete empty/ src/",
            "robocopy empty src /MIR", "perl -e 'unlink glob(\"*\")'", "node -e \"require('fs').unlinkSync('a')\"",
            "python -c \"from pathlib import Path; Path('a').unlink()\"", "npx rimraf src",
            "python C:/Users/x/.backpaw/backpaw.py uninstall", "backpaw uninstall claude"]:
    assert run_hook(cmd, home) == 2, cmd
# ...while ordinary commands that look similar still run
for cmd in ["bash -c 'ls -la'", "powershell -ExecutionPolicy Bypass -File build.ps1", "git restore --staged a.py",
            "xargs echo", "env | sort", "npm run format", "git checkout main", "git stash", "rsync -a src/ dst/",
            "python backpaw.py agents", "cp README.md README.copy.md"]:
    assert run_hook(cmd, home) == 0, cmd

# --- ask first: destructive cloud / API / database commands can't be recycled, so the user is asked ---
asks = ["aws s3 rm s3://bucket/data --recursive", "aws s3 rb s3://bucket --force", "aws s3 sync ./empty s3://b --delete",
        "aws ec2 terminate-instances --instance-ids i-1", "aws rds delete-db-instance --db-instance-identifier prod",
        "gsutil -m rm -r gs://bucket", "gcloud compute instances delete web-1", "az group delete -n prod --yes",
        "terraform destroy -auto-approve", "terraform apply -destroy", "pulumi destroy", "kubectl delete ns prod",
        "helm uninstall api", "docker system prune -a", "docker volume rm pgdata", "docker compose down -v",
        "gh repo delete Ls1more/x --yes", "gh release delete v1", "gh api -X DELETE repos/a/b",
        "git push --force origin main", "git push -f", "git push origin --delete feature", "git push origin :old",
        "git push origin +main", "curl -X DELETE https://api.example.com/users/1",
        "Invoke-RestMethod -Uri https://api.example.com/x -Method Delete",
        "psql -c 'DROP TABLE users;'", 'sqlite3 app.db "DELETE FROM users;"', "mysql -e 'TRUNCATE TABLE logs'",
        "mongosh --eval 'db.dropDatabase()'", "redis-cli FLUSHALL", "npx prisma migrate reset --force",
        'ssh prod "kubectl delete deploy api"']
for cmd in asks:
    assert backpaw.destructive_api(cmd), cmd
    code, out, err = run_agent("claude", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                          "tool_input": {"command": cmd}, "cwd": home})
    assert code == 0 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask", (cmd, out, err)
for cmd in ["aws s3 ls", "aws s3 cp a s3://b/a", "aws ec2 describe-instances", "kubectl get pods", "terraform plan",
            "helm list", "docker ps", "docker compose down", "gh repo view", "git push", "git push origin main",
            "git push -u origin feature", "curl https://api.example.com", "curl -X GET https://x", "SELECT * FROM t",
            "psql -c 'DELETE FROM users WHERE id = 1'", "truncate -s 0 out.log", "gcloud config list"]:
    assert not backpaw.destructive_api(cmd), cmd
# MCP tools whose names say they destroy data
for name in ["mcp__github__delete_repository", "mcp__db__drop_table", "mcp__drive__remove_file", "deleteFile",
             "mcp__aws__terminate_instance", "purgeQueue"]:
    assert backpaw.destructive_tool(name), name
for name in ["mcp__github__get_file_contents", "mcp__slack__post_message", "Edit", "Write", "view", "mcp__db__query",
             "mcp__notes__deleted_items_count_report"]:
    assert not backpaw.destructive_tool(name) or "deleted" in name, name
code, out, _ = run_agent("claude", {"hook_event_name": "PreToolUse", "tool_name": "mcp__github__delete_repository",
                                    "tool_input": {"repo": "x"}, "cwd": home})
assert code == 0 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask", out
# each agent answers in its own way: a prompt where supported, otherwise a block that says to ask the user
code, out, _ = run_agent("cursor", {"hook_event_name": "beforeShellExecution", "command": "terraform destroy", "cwd": home})
assert code == 0 and json.loads(out)["permission"] == "ask", out
code, out, _ = run_agent("cursor", {"hook_event_name": "beforeMCPExecution", "tool_name": "drop_table", "tool_input": {}})
assert code == 0 and json.loads(out)["permission"] == "ask", out
code, out, _ = run_agent("copilot", {"toolName": "bash", "toolArgs": {"command": "kubectl delete ns prod"}, "cwd": home})
assert code == 0 and json.loads(out)["permissionDecision"] == "ask", out
for agent, payload in [("codex", {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "terraform destroy"}, "cwd": home}),
                       ("windsurf", {"agent_action_name": "pre_run_command", "tool_info": {"command_line": "terraform destroy", "cwd": home}})]:
    code, out, err = run_agent(agent, payload)
    assert code == 2 and "ask the user" in err, (agent, code, err)
# a real file delete is still a hard block, not a question
code, out, err = run_agent("claude", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                      "tool_input": {"command": "rm -rf src"}, "cwd": home})
assert code == 2 and "Recycle Bin" in err, (code, err)
# the setting turns asking off
backpaw._save_prefs(ask_api=False)
assert run_hook("terraform destroy", home) == 0
assert run_agent("claude", {"hook_event_name": "PreToolUse", "tool_name": "mcp__x__delete_all", "cwd": home}) == (0, "", "")
backpaw._save_prefs(ask_api=True)

# moves/copies are checked outside temp, where overwrite protection applies
work = Path(tempfile.mkdtemp(dir=home, prefix=".backpaw-test-"))
(work / "dir").mkdir()
(work / "a.txt").write_text("a")
(work / "b.txt").write_text("b")
(work / "exists.txt").write_text("x")
assert run_hook("mv b.txt exists.txt", work) == 2
assert run_hook("cp a.txt exists.txt", work) == 2
assert run_hook("Copy-Item a.txt -Destination exists.txt", work, "PowerShell") == 2
assert run_hook('bash -c "mv b.txt exists.txt"', work) == 2
assert run_hook("cp a.txt new.txt", work) == 0
assert run_hook("Move-Item -Path a.txt -Destination dir", work, "PowerShell") == 0
assert run_hook("mv b.txt c.txt", work) == 0
assert run_hook("mv ghost.txt d.txt", work) == 0  # source doesn't exist: allowed, but not logged
moves = [(e["src"], e["dst"]) for e in backpaw.read_log() if e["op"] == "move"]
assert moves == [(str(work / "a.txt"), str(work / "dir" / "a.txt")), (str(work / "b.txt"), str(work / "c.txt"))], moves

# allowed folders: build output is deleted directly, everything else still goes through the guard
(work / "node_modules").mkdir()
(work / "src").mkdir()
for cmd in ["rm -rf node_modules", "rm -rf dist build .next", "rm -rf node_modules/.cache",
            "Remove-Item -Recurse __pycache__", "rm -rf src/__pycache__", "rm -rf dist/*"]:
    assert run_hook(cmd, work) == 0, cmd
for cmd in ["rm -rf src", "rm -rf node_modules/../src", "rm -rf node_modules src", f'rm -rf "{work.parent}"']:
    assert run_hook(cmd, work) == 2, cmd
(work / "build" / "proj").mkdir(parents=True)
assert run_hook("rm -rf src", work / "build" / "proj") == 2  # a 'build' parent doesn't unlock what's below
link = work / "dist"  # a 'dist' that is really a link to src is judged by where it points
if backpaw.IS_WIN:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(work / "src")], check=True, capture_output=True)
else:
    os.symlink(work / "src", link)
assert run_hook("rm -rf dist", work) == 2
os.rmdir(link) if backpaw.IS_WIN else os.unlink(link)  # removes the link only
backpaw._save_prefs(allowed_dirs=[])
assert run_hook("rm -rf node_modules", work) == 2  # empty list: nothing is allowed
backpaw._save_prefs(allowed_dirs=["out"])
assert run_hook("rm -rf out", work) == 0 and run_hook("rm -rf node_modules", work) == 2
backpaw._save_prefs(allowed_dirs=["../..", "C:/Users", "*", "$HOME", "ok"])
assert backpaw.allowed_dirs() == ["ok"]  # only plain names survive a hand-edited settings file
backpaw._save_prefs(allowed_dirs="node_modules")
assert backpaw.allowed_dirs() == backpaw.DEFAULT_ALLOWED
backpaw._save_prefs(allowed_dirs=backpaw.DEFAULT_ALLOWED)

# move restore round trip (the move really happens, so the file identity matches)
os.rename(work / "b.txt", work / "c.txt")
entry = [e for e in backpaw.read_log() if e["op"] == "move"][1]
assert entry["status"] == "restorable", entry
backpaw.restore(entry)
assert (work / "b.txt").read_text() == "b" and not (work / "c.txt").exists()
assert [e for e in backpaw.read_log() if e["id"] == entry["id"]][0]["status"] == "restored"

# 11. damaged or incomplete log lines are skipped instead of breaking the restore window
with backpaw.LOG.open("a", encoding="utf-8") as f:
    f.write("not json\n{\"op\": \"trash\"}\n[1, 2]\n{\"op\": \"move\", \"id\": 5}\n")
assert all(e["op"] in ("trash", "move") for e in backpaw.read_log())

# 5. restore refuses when a folder on the way back has been replaced by a link
(work / "orig").mkdir()
(work / "orig" / "f.txt").write_text("f")
(work / "elsewhere").mkdir()
assert run_hook("mv orig/f.txt f-moved.txt", work) == 0
os.rename(work / "orig" / "f.txt", work / "f-moved.txt")
link_entry = [e for e in backpaw.read_log() if e.get("dst") == str(work / "f-moved.txt")][-1]
os.rename(work / "orig", work / "orig-old")
if backpaw.IS_WIN:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(work / "orig"), str(work / "elsewhere")], check=True, capture_output=True)
else:
    os.symlink(work / "elsewhere", work / "orig")
try:
    backpaw.restore(link_entry)
    raise AssertionError("restored through a link")
except PermissionError:
    pass
assert not (work / "elsewhere" / "f.txt").exists()
os.rmdir(work / "orig") if backpaw.IS_WIN else os.unlink(work / "orig")

# restore hijack: a pre-logged move "from" Startup, then a payload dropped in the workspace
startup = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\evil.bat")
before = len(backpaw.read_log())
assert run_hook(f'mv "{startup}" ./payload.bat', work) == 0
assert len(backpaw.read_log()) == before, "move of a non-existent source must not be logged"
(work / "payload.bat").write_text("calc.exe")
# forged log lines are refused even if written straight into the log file
other = work / "other.txt"
other.write_text("x")
forged = [
    backpaw.log({"op": "move", "src": startup, "dst": str(work / "payload.bat"), "ino": os.lstat(other).st_ino}),
    backpaw.log({"op": "move", "src": startup, "dst": str(work / "payload.bat")}),
    backpaw.log({"op": "trash", "path": startup, "trashed": str(work / "payload.bat")}),
]
for f in forged:
    f = [e for e in backpaw.read_log() if e["id"] == f["id"]][0]
    assert f["status"] == "suspicious", f
    try:
        backpaw.restore(f)
        raise AssertionError("forged entry restored")
    except PermissionError:
        pass
assert not os.path.exists(startup)
# even a perfectly forged entry is flagged as a risky destination for the GUI's extra warning
assert backpaw.risky_destination({"op": "move", "src": startup})
assert backpaw.risky_destination({"op": "trash", "path": home + "/.ssh/authorized_keys"})
assert backpaw.risky_destination({"op": "trash", "path": home + "/.bashrc"})
assert not backpaw.risky_destination({"op": "trash", "path": home + "/Desktop/report.docx"})

# real trash + restore round trip
# (macOS needs Finder automation permission, which headless CI can't grant: opt in with BACKPAW_TEST_TRASH=1)
if backpaw.IS_WIN or os.environ.get("BACKPAW_TEST_TRASH"):
    victim = tmp / "victim.txt"
    victim.write_text("save me")
    t = backpaw.trash(victim)
    assert not victim.exists() and os.path.exists(t["trashed"]), t
    backpaw.restore(t)
    assert victim.read_text() == "save me"
    if backpaw.IS_WIN:  # trashing via an 8.3 short path (e.g. C:\Users\RUNNER~1) must still be recorded
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        ctypes.windll.kernel32.GetShortPathNameW(str(victim), buf, len(buf))
        expected = backpaw._long_path(str(victim)).lower()  # while the file still exists
        t = backpaw.trash(buf.value or str(victim))
        assert t["path"].lower() == expected and "~" not in t["path"], t
        backpaw.restore(t)
        assert victim.read_text() == "save me"
else:
    print("skipped real Trash round trip (set BACKPAW_TEST_TRASH=1 to run it)")

# install / uninstall every agent into a fake home, keeping unrelated hooks intact
fake = tmp / "home"
backpaw.HOME, backpaw.CLAUDE = fake, fake / ".claude"
backpaw.SETTINGS = backpaw.CLAUDE / "settings.json"
backpaw.SETTINGS.parent.mkdir(parents=True)
backpaw.SETTINGS.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "other"}]}]}}))
assert backpaw.detected_agents() == ["claude"]
# 10. a user path with ' and & must still produce valid agent configs (TOML parses, command line quoted)
real_data = backpaw.DATA
backpaw.DATA = tmp / "O'Neil & Co" / ".backpaw"
assert backpaw._hook_cmd("claude").count('"') >= 2, backpaw._hook_cmd("claude")
kimi_cfg = backpaw._agents()["kimi"]["config"]
kimi_cfg.parent.mkdir(parents=True)
kimi_cfg.write_text('default_model = "kimi-k2"\n\n[models.kimi-k2]\nprovider = "moonshot"\n')
for agent, a in backpaw._agents().items():
    backpaw.install(agent)
    backpaw.install(agent)  # idempotent
    assert backpaw.installed(agent), agent
    text = a["config"].read_text()
    # hooks run the private copy in ~/.backpaw, not the clone (which may sit in an agent's workspace)
    assert backpaw._installed_script().as_posix() in text and backpaw._installed_script().exists(), (agent, text)
    assert Path(__file__).resolve().parent.as_posix() not in text, (agent, text)
    assert not a["config"].with_suffix(a["config"].suffix + ".backpaw-tmp").exists()
    if a.get("kind") == "toml":
        cfg = tomllib.loads(text)
        assert len(cfg["hooks"]) == 1 and cfg["default_model"] == "kimi-k2", cfg
    elif a.get("kind") == "plugin":
        assert sys.executable.replace("\\", "/") in text and '"hook", "opencode"' in text
    else:
        cfg = json.loads(text)
        want = {e: len(v) if isinstance(v, list) else 1 for e, v in a["events"].items()}
        got = {e: sum("backpaw" in json.dumps(i) for i in items) for e, items in cfg["hooks"].items() if e in want}
        assert got == want, (agent, got, want)  # installing twice never duplicates entries
    backpaw.uninstall(agent)
    assert not backpaw.installed(agent), agent
assert tomllib.loads(kimi_cfg.read_text()) == {"default_model": "kimi-k2", "models": {"kimi-k2": {"provider": "moonshot"}}}
assert json.loads(backpaw.SETTINGS.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "other"
# the backup is the config from before Backpaw ever touched it
bak = json.loads(backpaw.SETTINGS.with_suffix(".json.backpaw-bak").read_text())
assert "backpaw" not in json.dumps(bak) and bak["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "other"
backpaw.DATA = real_data

# v0.9.4: 'uninstall --all' removes hooks, shortcuts and program files, but keeps the user's log and settings
desk, progs = tmp / "fake-desktop", tmp / "fake-programs"
desk.mkdir(), progs.mkdir()
(desk / "Backpaw.lnk").write_text("x"), (progs / "Backpaw.lnk").write_text("x")
real_shell_folder = backpaw._shell_folder
backpaw._shell_folder = lambda name: {"Desktop": desk, "Programs": progs}[name]  # never the real ones
try:
    backpaw.install("claude")
    backpaw.install("cursor")
    assert backpaw._installed_script().exists() and backpaw.installed("claude")
    backpaw.full_uninstall()
finally:
    backpaw._shell_folder = real_shell_folder
assert not backpaw.installed("claude") and not backpaw.installed("cursor")
assert not backpaw._installed_script().exists() and not (backpaw.DATA / "assets").exists()
if backpaw.IS_WIN:
    assert not (desk / "Backpaw.lnk").exists() and not (progs / "Backpaw.lnk").exists()
assert backpaw.LOG.exists() and (backpaw.DATA / "settings.json").exists()  # user data kept
assert json.loads(backpaw.SETTINGS.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "other"

# git reminders: local-only work gets flagged; a pushed repo counts as backed up (no OneDrive nag)
import subprocess
old = {"GIT_AUTHOR_DATE": "2026-01-01T12:00:00", "GIT_COMMITTER_DATE": "2026-01-01T12:00:00"}


def git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True,
                   env={**os.environ, **(env or {})})


remote, repo = tmp / "remote.git", tmp / "proj"
git(tmp, "init", "-q", "--bare", str(remote))
git(tmp, "init", "-q", str(repo))
(repo / "a.py").write_text("1")
git(repo, "add", ".")
git(repo, "commit", "-qm", "one", env=old)
w = backpaw.backup_warnings(repo)
assert len(w) == 1 and "no remote" in w[0], w
git(repo, "remote", "add", "origin", str(remote))
w = backpaw.backup_warnings(repo)
assert len(w) == 1 and "1 commit(s) not pushed" in w[0], w  # old commit, never pushed
# the reminder threshold is a user setting; bad values in settings.json fall back to 3 days
backpaw._save_prefs(stale_days=365)
assert backpaw.stale_days() == 365 and backpaw.backup_warnings(repo) == []  # commit is ~9 months old
for bad in ("abc", 0, -5, 9999, 2.5, None):
    backpaw._save_prefs(stale_days=bad)
    assert backpaw.stale_days() == 3, bad
backpaw._save_prefs(stale_days=3)
git(repo, "push", "-q", "origin", "HEAD")
assert backpaw.backup_warnings(repo) == [], backpaw.backup_warnings(repo)  # pushed: fine, no OneDrive warning
(repo / "a.py").write_text("2")
w = backpaw.backup_warnings(repo)
assert len(w) == 1 and "uncommitted changes" in w[0], w  # edits sitting on top of an old commit
git(repo, "commit", "-qam", "two")  # fresh commit: not stale yet
assert backpaw.backup_warnings(repo) == [], backpaw.backup_warnings(repo)
assert backpaw.git_warnings(work) is None  # not a repo: falls back to the OneDrive/Time Machine check

# a repo's own config must never make Backpaw (running outside any sandbox) execute a program
marker = tmp / "pwned.txt"
evil = tmp / "evil.sh"
evil.write_text(f'#!/bin/sh\necho pwned > "{marker.as_posix()}"\n')
os.chmod(evil, 0o755)
git(repo, "config", "core.fsmonitor", evil.as_posix())
(repo / "a.py").write_text("3")  # dirty, so the status check runs
backpaw.backup_warnings(repo)
assert not marker.exists(), "core.fsmonitor from repo config was executed"

# code snapshots: full state (history + uncommitted + untracked, minus ignored) to a bundle, restorable elsewhere
(repo / ".gitignore").write_text("secret.env\n")
(repo / ".gitattributes").write_text("*.py filter=evil\n")  # a filter git add would run; snapshots must not
git(repo, "config", "filter.evil.clean", f"sh -c 'echo pwned > \"{marker.as_posix()}\"'")
(repo / "new_module.py").write_text("brand new, never added")
(repo / "secret.env").write_text("TOKEN=ignored")
(repo / "gone.py").write_text("x")
snaps = tmp / "snaps"
# the status check itself must not run the filter either (git status runs clean filters on changed files)
w = backpaw.backup_warnings(repo)
status_before = backpaw.safe_status(repo)
assert not marker.exists(), "a repo-defined filter ran during Backpaw's status check"
assert "new_module.py" in status_before and "a.py" in status_before, status_before
head_before = backpaw._git(repo, "rev-parse", "HEAD")
b = backpaw.snapshot(repo, dest=snaps)
assert b.exists() and b.suffix == ".bundle" and not marker.exists(), (b, marker.exists())
# the user's repo is untouched: same HEAD, same status (index and working tree unchanged)
assert backpaw._git(repo, "rev-parse", "HEAD") == head_before and backpaw.safe_status(repo) == status_before
restored = backpaw.restore_snapshot(b, tmp / "restored")
assert (restored / "a.py").read_text() == "3"                       # uncommitted edit
assert (restored / "new_module.py").read_text() == "brand new, never added"  # untracked file
assert not (restored / "secret.env").exists()                       # ignored stays out
assert not marker.exists()
# keeps only the newest few per repo
for _ in range(backpaw.SNAPSHOT_KEEP + 2):
    backpaw.snapshot(repo, dest=snaps)
assert len(list(b.parent.glob("*.bundle"))) == backpaw.SNAPSHOT_KEEP
assert not list(b.parent.glob("*.partial"))
# 8. snapshots copy only files that really live in the repo, within the size limit
outside = tmp / "outside-secrets"
outside.mkdir()
(outside / "key.txt").write_text("outside the repo")
if backpaw.IS_WIN:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(repo / "linked"), str(outside)], check=True, capture_output=True)
else:
    os.symlink(outside, repo / "linked")
(repo / "big.bin").write_bytes(b"x" * 64)
limit, backpaw.SNAPSHOT_MAX_BYTES = backpaw.SNAPSHOT_MAX_BYTES, 32
b2 = backpaw.snapshot(repo, dest=tmp / "snaps2")
backpaw.SNAPSHOT_MAX_BYTES = limit
r2 = backpaw.restore_snapshot(b2, tmp / "restored2")
assert not (r2 / "linked").exists() and not (r2 / "big.bin").exists(), list(r2.iterdir())
assert (r2 / "new_module.py").exists()
os.rmdir(repo / "linked") if backpaw.IS_WIN else os.unlink(repo / "linked")
# a repo whose working tree is redirected elsewhere (core.worktree) is not snapshotted
git(repo, "config", "core.worktree", str(outside))
try:
    backpaw.snapshot(repo, dest=tmp / "snaps3")
    raise AssertionError("snapshotted a redirected working tree")
except OSError:
    pass
git(repo, "config", "--unset", "core.worktree")

# v0.9.4: untracked secret files stay out of snapshots even when .gitignore misses them
for name, keep_it in [(".env.local", False), ("deploy.pem", False), ("id_rsa", False), (".npmrc", False),
                      ("server.key", False), (".env.example", True), ("config.py", True)]:
    (repo / name).write_text("x")
r4 = backpaw.restore_snapshot(backpaw.snapshot(repo, dest=tmp / "snaps4"), tmp / "restored4")
for name, keep_it in [(".env.local", False), ("deploy.pem", False), ("id_rsa", False), (".npmrc", False),
                      ("server.key", False), (".env.example", True), ("config.py", True)]:
    assert (r4 / name).exists() == keep_it, name
# v0.9.4: each snapshot's outcome is recorded for the window; non-repos aren't reported
backpaw._save_prefs(snapshot_dir=str(tmp / "snaps5"))
backpaw.run_snapshot(repo)
st = backpaw.snapshot_status()[os.path.normcase(backpaw._git(repo, "rev-parse", "--show-toplevel"))]
assert st["ok"] and st["detail"].endswith(".bundle") and backpaw.snapshot_failures() == [], st
(tmp / "not-a-folder").write_text("a file, so the snapshot folder can't be created")
backpaw._save_prefs(snapshot_dir=str(tmp / "not-a-folder" / "snaps"))
try:
    backpaw.run_snapshot(repo)
    raise AssertionError("snapshot into an impossible folder succeeded")
except OSError:
    pass
fails = backpaw.snapshot_failures()
assert len(fails) == 1 and not fails[0]["ok"] and fails[0]["repo"] == repo.name, fails
before = backpaw.snapshot_status()
try:
    backpaw.run_snapshot(work)  # not a repo
except OSError:
    pass
assert backpaw.snapshot_status() == before
backpaw._save_prefs(snapshot_dir="")
# v0.9.4: settings are written atomically (no temp files left behind, file always valid JSON)
assert json.loads((backpaw.DATA / "settings.json").read_text())["snapshot_dir"] == ""
assert not list(backpaw.DATA.glob("*.tmp"))
# snapshots need a folder; off by default
try:
    backpaw.snapshot(repo)
    raise AssertionError("snapshot without a folder")
except OSError:
    pass
# the hook starts at most one background snapshot per folder per interval, only when a folder is set
launched = []
real_popen = subprocess.Popen
backpaw.subprocess.Popen = lambda *a, **k: launched.append(a[0])
try:
    backpaw.maybe_snapshot(str(repo))
    assert launched == []                            # off: nothing
    backpaw._save_prefs(snapshot_dir=str(snaps))
    backpaw.maybe_snapshot(str(repo))
    backpaw.maybe_snapshot(str(repo))
    assert len(launched) == 1 and launched[0][-2:] == ["snapshot", os.path.normcase(os.path.abspath(repo))], launched
    # 13. hopping to other folders right away doesn't start more snapshots (global gap)
    for other in (tmp, work, outside):
        backpaw.maybe_snapshot(str(other))
    assert len(launched) == 1, launched
    backpaw._save_prefs(snapshot_dir="relative/path")
    assert backpaw.snapshot_dir() is None             # only absolute folders count
finally:
    backpaw.subprocess.Popen = real_popen
    backpaw._save_prefs(snapshot_dir="")
git(repo, "config", "--unset", "core.fsmonitor")

import shutil
shutil.rmtree(work)
print("all backpaw checks passed")
