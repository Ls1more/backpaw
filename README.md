# Backpaw 🐾

**Get back what Claude Code deleted or moved.**

Claude Code's `/rewind` only undoes edits made with its file-editing tools. Anything it removes or moves with
shell commands (`rm`, `mv`, `Remove-Item`, `Move-Item`, ...) is outside checkpoint tracking.
Backpaw covers that gap:

- **Guard hook**: blocks shell deletes and tells Claude to use `backpaw trash` instead, which sends files to
  the real **Recycle Bin** (Windows) or **Trash** (macOS). Also blocks `git clean` and `git reset --hard`.
  Deletes inside the system temp folder (Claude's scratch space) and commands run over `ssh` are allowed.
- **Move log**: every `mv` / `Move-Item` is recorded so it can be undone. A move that would overwrite an
  existing file is blocked until that file is trashed.
- **Restore window**: select rows, click **Restore**. Turn the guard on or off from the header. Follows the
  system light/dark theme.
- **Session history**: lists deletes and moves Claude ran in past sessions (read from `~/.claude/projects`).
- **Backup check**: warns at session start if the project folder isn't in OneDrive (Windows), or in
  iCloud / Time Machine (macOS). Includes a shortcut to Windows System Protection (shadow copies).

Pure Python standard library. Windows and macOS.

## Install

```bash
git clone https://github.com/Ls1more/backpaw
python backpaw/backpaw.py install    # adds PreToolUse + SessionStart hooks to ~/.claude/settings.json (backs it up first)
python backpaw/backpaw.py uninstall  # removes them
```

Restart Claude Code after installing.

## Use

```bash
python backpaw.py              # restore window
python backpaw.py trash FILE   # recycle a file (what Claude is told to run)
python backpaw.py list         # log of trashed/moved items
python backpaw.py scan         # deletes/moves from past Claude sessions
python backpaw.py check [DIR]  # backup warning for a folder
python backpaw.py --version
python test_backpaw.py         # smoke test
```

The log lives in `~/.backpaw/log.jsonl`.

## Limits

- Detection is pattern-based. It catches common delete verbs, `find -delete`, and inline
  `shutil.rmtree` / `os.remove` / `fs.rm`, but a determined script can still delete files another way.
  Keep a real backup (OneDrive / File History / Time Machine).
- Once the Recycle Bin is emptied, a trashed item shows as `gone`.
- On a drive with no Recycle Bin (e.g. network shares), Backpaw refuses to delete rather than delete permanently.
- A command starting with `ssh` is treated as remote in full, so `ssh host x; rm local` is not caught.
- macOS: the first `trash` asks for permission to control Finder (System Settings → Privacy → Automation).
