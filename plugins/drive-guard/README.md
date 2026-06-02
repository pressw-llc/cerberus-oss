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

## Bash directory-change (`cd`) handling

`bash_block_reason`/`bash_write_reason` model `cd`/`pushd`/`popd` left-to-right so a relative
path resolves against the directory in force when it runs — `cd <mount> && cat "Shared drives/x"`
is denied, not just an absolute `…/Shared drives/x`. This traversal pass is **additive** and
**fails open to the legacy event-cwd analysis** on any internal parsing error, so a parser bug
can never newly over-block a command that worked before. A `cd` target is trusted only when it
fully resolves to a literal (captured `VAR=value` assignments + `expandvars`/`expanduser`, no
`$(...)`/backtick/unset-var/glob); a computed `cd` target leaves the cwd UNKNOWN and adds no
new denial — that stays in the documented dynamic residual backstopped by Google-side Viewer-only.

Path tokens are classified by the same slash/`~`/`.` heuristic as the legacy analysis: a bare
*data* argument that merely equals a protected basename (e.g. `grep "Shared drives" notes.txt`
while the cwd is the Drive mount) is **not** treated as a path, so it is never falsely denied.
The trade-off is one accepted residual — naming the protected child by a bare word from its
parent (`cd <mount> && ls "Shared drives"`); the slash forms (`ls "Shared drives/"`,
`cat "Shared drives/x"`) and any `cd` straight into the tree are still blocked.

> POSIX `sh` script: it does not run on native Windows (cmd / PowerShell). Windows seats need
> a POSIX `sh` (Git Bash / WSL), or the hook must be switched to a PowerShell variant.

This is the hook layer. There is no separate canonical copy, sync tool, or test suite in this
repo — `scripts/drive-guard.py` is the source of truth.
