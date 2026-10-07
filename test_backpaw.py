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

import shutil
shutil.rmtree(work)
print("all backpaw checks passed")
