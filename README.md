# Backpaw 🐾

**Get back what your AI coding agent deleted or moved.**

AI agents run shell commands. When one runs `rm -rf`, `Remove-Item` or `mv` on the wrong thing, the agent's
own undo (like Claude Code's `/rewind`) can't help: shell side effects are outside checkpoint tracking.
Backpaw covers that gap for every major agent:

| Agent | Hook used | Config Backpaw writes |
|---|---|---|
| Claude Code | `PreToolUse` + `SessionStart` | `~/.claude/settings.json` |
| OpenAI Codex | `PreToolUse` + `SessionStart` | `~/.codex/hooks.json` |
| Gemini CLI | `BeforeTool` + `SessionStart` | `~/.gemini/settings.json` |
| Cursor | `beforeShellExecution` | `~/.cursor/hooks.json` |
| GitHub Copilot CLI | `preToolUse` | `~/.copilot/hooks/backpaw.json` |
| Windsurf | `pre_run_command` | `~/.codeium/windsurf/hooks.json` |

What it does:

- **Guard**: blocks shell deletes and tells the agent to use `backpaw trash` instead, which sends files to
  the real **Recycle Bin** (Windows) or **Trash** (macOS). Also blocks `git clean` and `git reset --hard`.
  Deletes inside the system temp folder (agent scratch space) and commands run over `ssh` are allowed.
- **Move log**: every `mv` / `Move-Item` is recorded so it can be undone. A move that would overwrite an
  existing file is blocked until that file is trashed.
- **Restore window**: select rows, click **Restore**. Turn the guard on or off per agent. Follows the
  system light/dark theme.
- **Session history**: lists deletes and moves from past Claude Code sessions (`~/.claude/projects`).
- **Backup check**: warns at session start (Claude Code, Codex, Gemini CLI) if the project folder isn't in
  OneDrive (Windows) or in iCloud / Time Machine (macOS). Includes a shortcut to Windows System Protection.

Pure Python standard library. Windows and macOS.

## Install

```bash
git clone https://github.com/Ls1more/backpaw
python backpaw/backpaw.py install            # guard every agent found on this machine (configs are backed up first)
python backpaw/backpaw.py install cursor     # or pick agents
python backpaw/backpaw.py uninstall          # remove it everywhere
```

Restart the agent after installing.

## Use

```bash
python backpaw.py              # restore window
python backpaw.py agents       # which agents are found / guarded
python backpaw.py trash FILE   # recycle a file (what agents are told to run)
python backpaw.py list         # log of trashed/moved items
python backpaw.py scan         # deletes/moves from past Claude Code sessions
python backpaw.py check [DIR]  # backup warning for a folder
python backpaw.py --version
python test_backpaw.py         # smoke test
```

The log lives in `~/.backpaw/log.jsonl`.

## Limits

- Detection is pattern-based. It catches common delete verbs, `find -delete`, and inline
  `shutil.rmtree` / `os.remove` / `fs.rm`, but a determined script can still delete files another way.
  Keep a real backup (OneDrive / File History / Time Machine).
- Once the Recycle Bin is emptied, a trashed item shows as `missing`.
- On a drive with no Recycle Bin (e.g. network shares), Backpaw refuses to delete rather than delete permanently.
- A command starting with `ssh` is treated as remote in full, so `ssh host x; rm local` is not caught.
- Agent support beyond Claude Code is built from each agent's published hook docs and tested with simulated
  payloads; please report anything that misbehaves. Gemini CLI hooks may need enabling in its settings.
- Not yet supported: Cline (hooks moved to code plugins), Aider (no hook system).
- macOS: the first `trash` asks for permission to control Finder (System Settings → Privacy → Automation).
