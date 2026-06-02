# drive-guard

Blocks Claude Code from reading, changing, or deleting Google **Shared drives**, while
leaving My Drive, local code, and the web usable. It shuts both doors to a Shared drive —
the **filesystem** and the **Google Drive MCP connector** — with one `PreToolUse` hook.

**See the [root README](../../README.md) for what it does, install, org-wide enforcement,
configuration env vars, and limits.** This file only covers plugin internals.

## Files

```
drive-guard/
├── .claude-plugin/plugin.json   # manifest
├── hooks/hooks.json             # single PreToolUse hook entry (matcher + command)
├── scripts/run-guard.sh         # launcher: picks python3 or python at runtime
└── scripts/drive-guard.py       # the guard (single-file, stdlib-only Python 3)
```

## How the hook is wired

`hooks/hooks.json` registers **one** `PreToolUse` entry whose matcher covers the file tools
(`Read`, `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `NotebookRead`, `Glob`, `Grep`, `LS`),
`Bash`, and the `mcp__claude_ai_Google_Drive__*` connector tools. It runs
`run-guard.sh drive-guard.py --mode block`.

`run-guard.sh` exists because Claude Code spawns exec-form hook commands directly (no shell):
a single launcher probes for `python3` then `python` at runtime and invokes whichever is
present, so a missing interpreter name can't spawn-error on every tool call. If no interpreter
is found it warns on stderr and exits 0 (the launcher fails open; the guard script keeps its
own fail-closed posture once it runs).

> POSIX `sh` script: it does not run on native Windows (cmd / PowerShell). Windows seats need
> a POSIX `sh` (Git Bash / WSL), or the hook must be switched to a PowerShell variant.

This is the hook layer. There is no separate canonical copy, sync tool, or test suite in this
repo — `scripts/drive-guard.py` is the source of truth.
