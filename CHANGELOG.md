# Changelog

All notable changes to Backpaw. Versions follow `MAJOR.MINOR.PATCH`.

## Unreleased
- Code snapshots to a network share are tested (snapshot and restore over a UNC path). README and the
  Settings hint now present a local backup (NAS share, another drive) alongside cloud folders.
- Demo GIF shows a local backup share (`\\local-backup\backups\Backpaw snapshots`) as the snapshot folder.

## 0.9.2 — docs and demo refresh
- Demo GIF rebuilt from the current version, with two new scenes: "ask first" for a `terraform destroy`,
  and the Settings dialog turning on code snapshots.
- README: guard section covers wrappers, launchers, one-liners, `~` expansion and the ssh change; restore
  section covers auto-refresh, the confirmation, sensitive-destination warnings and `suspicious` entries;
  privacy section covers code snapshots, synced folders and every file Backpaw writes.
- About box mentions ask-first and code snapshots.
- SECURITY.md: git calls, snapshots and ask-first are listed as in scope.

## 0.9.1 — security review fixes

A full review of everything Backpaw runs outside agent sandboxes. All fixes have regression tests.

**Guard**
- `~`, `%VAR%`, `$VAR` and `` `…` `` in delete targets are expanded or blocked the way the shell would see
  them. Previously, `rm -rf ~` from an agent working in the temp folder was judged as a temp path and allowed.
- Deletes launched through other tools are caught: `timeout`/`stdbuf`/`ionice`/`busybox …`, `find … -exec rm`,
  `fd -x rm`, and `python -c` / `node -e` / `perl -e` (and other interpreter) one-liners.
- git rules now see past global options: `git -C dir clean -fdx`, `git -c x=y reset --hard`,
  `git --no-pager stash drop`, `git -C dir push --force`.
- `ssh host …; local command`: only the ssh part is exempt now; local commands after it are checked.
  (A heredoc piped to ssh is still treated as entirely remote.)
- Changing Backpaw's own files (`~/.backpaw`) or an agent's hook settings from the shell now asks the user
  first, so an agent can't quietly switch the guard off. Backpaw's own `trash`, `list`, `agents`, `check`
  and snapshot commands are unaffected.

**Hook robustness**
- Hook input is read as UTF-8 regardless of the console code page.
- If the hook hits an unexpected error, delete-looking commands are blocked instead of allowed.

**Running git safely**
- git is located by absolute path from `PATH`. On Windows, a bare `git` would look in the current folder
  (the agent's project) first.
- Every git call disables hooks, commit/tag signing and signature display, alongside the 0.9.0
  `core.fsmonitor` and filter-driver protections.
- Snapshot commits are created with `--no-gpg-sign`.

**Restore**
- Restore refuses when a folder on the way back has been replaced by a symlink or junction since the
  original delete or move.
- Damaged or incomplete log lines are skipped instead of crashing the restore window.

**Snapshots**
- Only files that really live inside the repo are copied: links and junctions pointing elsewhere are skipped.
- Files over 100 MB are skipped.
- A repo whose working tree is redirected elsewhere (`core.worktree`) is not snapshotted.
- A global 60-second gap between background snapshots, on top of the 30-minute per-folder interval.

**Install**
- Agent configs are written with correct quoting when the user's path contains `'`, `&`, `%` or spaces
  (the Kimi TOML and the hook command line).
- Backpaw's data folder is created along with any missing parent folders.

**Window**
- System Protection opens by its full Windows path.

**Docs**
- README: antivirus/EDR quarantine is outside Backpaw's reach, and shadow copies aren't a backup.
- README: fix for "Python was not found" right after installing Python.
- README: security model and limits updated for everything above. This changelog added.

## 0.9.0 — code snapshots
- **Code snapshots (opt-in).** While an agent works in a git repo, Backpaw saves the repo's full state every
  30 minutes (history, uncommitted edits and new files, respecting `.gitignore`) as one git bundle file into
  a folder you choose, ideally one that syncs off the machine. It keeps the newest 5 per repo and never
  touches branches, the staging area or files. New commands: `snapshot`, `snapshots`, `restore-snapshot`.
  Settings field with **Browse…** and one-click **Use OneDrive** / **Use iCloud Drive**.
- **Security:** the session-start backup check ran `git status` inside the project, outside any agent
  sandbox, and a repo's own config (`core.fsmonitor`, or a filter driver) could make that run a program.
  `fsmonitor` is now disabled and every defined filter is blanked for Backpaw's status check.

## 0.8.1
- The restore window refreshes itself: new entries within ~2 s, a full refresh every 30 s and on focus,
  keeping the selection and scroll position.

## 0.8.0 — ask before destructive cloud & API commands
- Asks before cloud storage/resource deletes, `terraform`/`pulumi destroy`, `kubectl`/`helm` deletes,
  `docker` prunes and volume removal, `gh … delete`, force pushes, HTTP `DELETE`, `DROP`/`TRUNCATE`/unscoped
  `DELETE FROM`, `FLUSHALL`, and MCP tools whose names say they delete data. Claude Code, Cursor and Copilot
  CLI show a confirmation; other agents are blocked and told to ask. Switchable in Settings.
- Claude Code hook also watches `mcp__*` tools; Cursor gets `beforeMCPExecution`.
- CI actions pinned to commit SHAs; Dependabot for action updates.

## 0.7.1
- Demo GIF in the README.
- Fix: trashing via an 8.3 short path (`C:\Users\RUNNER~1\…`) recycled the file but didn't log it.

## 0.7.0
- **Allowed folders** setting: build output and caches (`node_modules`, `dist`, `build`, `.venv`,
  `__pycache__`, …) are deleted directly instead of filling the Recycle Bin. Matching is on the folder being
  deleted (or one inside the project on the way to it), on both the written and real path.
- GitHub Actions: tests on Windows and macOS, Python 3.11 and 3.13.

## 0.6.1
- Settings dialog with an adjustable git reminder threshold (1–30 days).

## 0.6.0
- Git projects count as backed up when pushed. Reminders when a repo has no remote, has unpushed commits or
  stale uncommitted changes. OneDrive is no longer suggested for git repos.
- Collapsible "Not backed up" banner, remembered between launches.

## 0.5.1
- Hooks always use the console `python.exe`; the block message gives the full Python path.

## 0.5.0 — security hardening
- Fixed a restore hijack: moves are logged only if the source exists, with its file ID; restore verifies the
  ID, the real Recycle Bin location and Windows' own Recycle Bin record. Refused entries show as `suspicious`.
- Extra confirmation for sensitive restore destinations (Startup, shell profiles, `.ssh`, agent configs).
- Shell wrappers unwrapped (`bash -c`, `powershell -Command`, `xargs`, `env`, `sudo`, `cmd /c`); PowerShell
  `-EncodedCommand`, emptying the Recycle Bin, destructive git commands, `rsync --delete`, `robocopy /MIR`
  and overwriting copies blocked; agents can't run `uninstall`.
- Hooks run a private copy in `~/.backpaw`; atomic config writes; original config kept as `*.backpaw-bak`;
  macOS Trash path passed as an argument; no command text in the log.
- Dark logo; MIT license, `SECURITY.md`, disclaimer, privacy and trademark notes.

## 0.4.0
- Qwen Code, Kimi Code CLI and OpenCode support.

## 0.3.0
- Codex, Gemini CLI, Cursor, GitHub Copilot CLI and Windsurf support alongside Claude Code; Agents dialog.

## 0.2.0
- Modern light/dark window, About dialog, guard on/off switch; `git clean` / `reset --hard`, `cmd /c del`
  and `[IO.File]::Delete` blocked; temp-folder deletes allowed; `uninstall` command.

## 0.1.0
- First version: Claude Code hook that sends deletes to the Recycle Bin/Trash, move log, restore window,
  session history, backup check.
