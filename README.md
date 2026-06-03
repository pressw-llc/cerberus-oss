# Cerberus — drive-guard

A Claude Code plugin that blocks Claude from **reading, changing, or deleting Google
Shared drives**, while leaving everything else — your **My Drive**, your **local code**,
and the **web** — fully usable.

Shared drives are how teams expose access to documents they should not hand to an agent
wholesale. `drive-guard` is the guardrail that keeps an agent out of them without crippling
the rest of the workflow.

## What it does

A coding agent can reach a Shared drive through **two doors**, and `drive-guard` shuts both
with a single `PreToolUse` hook:

- **The filesystem.** On macOS a Shared drive mounts as an ordinary folder
  (`~/Library/CloudStorage/GoogleDrive-*/Shared drives/...`). The hook inspects every file
  tool — `Read`, `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `NotebookRead`, `Glob`,
  `Grep`, `LS` — and every `Bash`/`PowerShell` command, and denies anything that targets a protected
  path. For `Bash` it parses redirections, `tee`/`dd`, in-place editors (`sed -i`, `perl -i`),
  `cp`/`mv`/`rsync`, `git` mutations, subshells (`$(...)`, backticks), and `eval`/`-c`
  wrappers, not just the bare command name.
- **The Google Drive MCP connector.** The same hook denies every
  `mcp__claude_ai_Google_Drive__*` tool call outright (search, read, create, copy, download,
  metadata, permissions, etc.).

The guard fails **closed**: if it hits an internal error while deciding on a real target, it
denies the call rather than guessing. (The one exception is an unparseable hook event, which
can't identify any target — that is allowed with a warning on stderr so a malformed event
can't brick every tool call.)

## Requirement

`drive-guard.py` is single-file, **standard-library-only Python 3 — no `pip install`.** Every
seat needs `python3` (or `python`) on its `PATH`; the launcher (`run-guard.sh`) picks whichever
exists at runtime. If neither is present the hook can't run (see [Limits](#limits)).

## Deploy — Option 1: single user

Try it on one machine:

1. In Claude Code, add this repo as a marketplace and install the plugin:

   ```
   /plugin marketplace add pressw-llc/cerberus-oss
   /plugin install drive-guard@cerberus
   ```

   If Claude Code prompts you to approve the plugin's hook, accept it.
2. Confirm it loaded: `/plugin` should list **drive-guard** as installed/enabled.
3. Verify enforcement — see [Verify it works](#verify-it-works).

To update later: `/plugin marketplace update cerberus`.

## Deploy — Option 2: whole organization (enforced)

Per-user install is just packaging; making the guard **non-optional for everyone** comes from
Claude Code **managed settings**, pushed from the **Claude.ai admin console** (Team/Enterprise)
or your MDM. This repo ships the exact policy: **[`managed-settings.plugin.json`](managed-settings.plugin.json)**.

What that policy does:

- **`extraKnownMarketplaces`** registers this repo (`pressw-llc/cerberus-oss`) as a trusted
  marketplace named `cerberus`, so every seat can resolve the plugin without anyone running
  `/plugin marketplace add`.
- **`enabledPlugins`** force-enables `drive-guard@cerberus` — the hook runs on every seat and
  users can't disable it (force-enabled plugin hooks need no per-user approval).
- **`sandbox`** enables the Claude Code sandbox with filesystem-level `denyRead` and `denyWrite`
  on the Shared-drive mount path. `autoAllowBashIfSandboxed` lets Bash run without extra
  prompts when the sandbox is active; `failIfUnavailable` is `false` so Claude Code still works
  on platforms where the sandbox binary isn't present (the other layers still protect).
- **`permissions.deny`** hard-blocks every file tool (`Read`, `Write`, `Edit`, `MultiEdit`,
  `NotebookEdit`) on Shared-drive paths — the macOS mount
  (`~/Library/CloudStorage/GoogleDrive-*/Shared drives/**`), the Git-Bash/MSYS form
  (`/g/Shared drives/**`), and the native Windows form (`G:\Shared drives\**`) — plus every
  `mcp__claude_ai_Google_Drive__*` call. These deny rules are enforced by Claude Code itself
  (no Python needed) and outrank any user or project settings.
- **`permissions.disableBypassPermissionsMode: "disable"`** blocks the mode that would let a
  user skip permission prompts.

Steps:

1. Open the **Claude.ai admin console** for your organization and create/edit a **Claude Code
   managed settings** policy (Enterprise/Team server-managed settings).
2. Paste the contents of [`managed-settings.plugin.json`](managed-settings.plugin.json) and
   **publish** it to the whole org (or a specific user group).
   - *MDM / image alternative:* place the same JSON at the OS managed-settings path instead —
     macOS: `/Library/Application Support/ClaudeCode/managed-settings.json`,
     Windows: `C:\ProgramData\ClaudeCode\managed-settings.json`.
3. On each seat, the next Claude Code launch registers the marketplace, force-installs
   `drive-guard`, and starts enforcing. Managed settings outrank user/project settings, so it
   can't be turned off locally. Verify with [Verify it works](#verify-it-works).

> **Names look mismatched on purpose.** The marketplace is named `cerberus` (declared in
> `marketplace.json`) while the repo is `cerberus-oss`. That's why the policy reads
> `drive-guard@cerberus` with source repo `pressw-llc/cerberus-oss`.

> **Hardened out of the box.** The shipped policy already includes `sandbox` filesystem
> restrictions and `permissions.deny` rules — both enforced by Claude Code itself with no Python
> required (see [Limits](#limits)). The hook adds deeper `Bash` command analysis on top. For
> true write-integrity against any bypass, also set the Shared drive to **Google-side
> Viewer-only** (see [Backstop](#backstop-google-side-viewer-only)).

## Verify it works

In a fresh Claude Code session, after install (Option 1) or deployment (Option 2):

- Ask Claude to **read or edit a file under** `…/Shared drives/…` → it must **refuse**.
- Ask Claude to use the **Google Drive connector** (e.g. search Drive) → it must **refuse**.
- Ask Claude to read a **My Drive** file, your **local code**, or fetch a **web** page → all
  work normally.

If a Shared-drive request gets through: confirm `python3`/`python` is on `PATH`, and that
`/plugin` shows `drive-guard` enabled (Option 1) or that the managed policy published (Option 2).

## Configuration

The guard reads four environment variables (all optional):

| Variable | Default | Effect |
| --- | --- | --- |
| `DRIVE_GUARD_MODE` | `block` | `block` denies all access (read and write) to protected paths. `readonly` allows reads but denies writes/deletes/renames. The MCP connector is denied in **both** modes. **Note:** the bundled `hooks.json` invokes the guard with an explicit `--mode block`, and the CLI flag wins over the env var — so setting `DRIVE_GUARD_MODE=readonly` has **no effect** unless you edit the hook's args to drop `--mode block` (or change it to `--mode readonly`). |
| `DRIVE_GUARD_PROTECTED` | see below | `PATH`-separated list of protected paths/globs. Overrides the built-in default entirely. Supports `*` and `**`. |
| `DRIVE_GUARD_AUDIT` | _(off)_ | If set to a file path, appends one JSON line per decision (`allow`/`deny`) with timestamp, tool, target, and reason. |
| `DRIVE_GUARD_SHELL` | `auto` | Windows-only hint for how to tokenize `Bash` commands: `posix`/`bash`/`wsl` vs. `windows`/`powershell`/`cmd`. Ignored on macOS/Linux. |

### Default protected path

If `DRIVE_GUARD_PROTECTED` is unset, the guard protects the platform default:

- **macOS:** `~/Library/CloudStorage/GoogleDrive-*/Shared drives` (the glob covers any
  signed-in account).
- **Windows:** `G:\Shared drives` and the Git-Bash/MSYS form `/g/Shared drives`.

**Confirm your team's actual Drive mount path.** Drive for Desktop can mount under a different
account folder or a different drive letter; if yours differs, set `DRIVE_GUARD_PROTECTED`
explicitly (it replaces the default — it does not add to it).

## Limits

Be honest about what a `PreToolUse` hook can and cannot do:

- **Static wildcards and `find` are handled.** The `Bash` parser does more than match the
  literal path text. It expands shell globs against the real filesystem (`glob.glob()`), so a
  wildcard token that resolves into the protected tree is denied. It also inspects `find`'s own
  argv — `-path`/`-ipath`/`-wholename` patterns that name a protected path, and search roots
  at or above a protected directory combined with any filter — and denies those too. So
  shell-glob and `find`-based access are **largely mitigated**, not a wide-open hole.
- **Directory changes (`cd`/`pushd`) are modeled.** The parser tracks the working directory
  left-to-right, so a relative path is resolved against the directory in force when it actually
  runs: `cd <mount> && cat "Shared drives/x"` (and `pushd`, chained/`..`-normalizing cds,
  symlinked mounts, `cd "$HOME/…"`, and `D=<mount>; cd "$D"`) are denied, not just the absolute
  `…/Shared drives/x`. This pass is additive and **fails open to the literal-text analysis** on
  any parse error, so it never over-blocks a command that worked before.
- **A simple variable still gets caught.** Because the guard also scans the raw command text
  for the protected path, an assignment like `D="<protected path>"; cat "$D/x"` is **denied** —
  the literal path string appears in the command. Naive variable assembly is *not* a bypass.
- **The true residual is fully dynamic obfuscation.** What the hook genuinely cannot see is a
  protected path that never appears literally and is not a static wildcard — e.g. a path
  *decoded from base64 at runtime*, *assembled from the contents of a file*, or reached by
  `cd`-ing into a directory whose name is *itself computed at runtime* (`cd "$(…)"`), which
  leaves the working directory unknowable to static analysis. There the path string simply does
  not exist in the command text or as a glob the shell can expand ahead of time, so text/glob
  inspection has nothing to match. This is a fundamental limit of inspecting
  command strings, not a bug to be patched away — the backstop for it is **Google-side
  Viewer-only** (below), which is unbypassable regardless of obfuscation.
- **The hook is the convenience layer, not the security boundary.** It runs inside Claude
  Code; anything that doesn't go through Claude Code's tools isn't seen by it.

### Operator warning: the hook is best-effort and fails open

The launcher (`run-guard.sh`) is a POSIX `sh` script. It probes for a Python interpreter at
runtime. If **no `python3` or `python` is on `PATH`**, it warns on stderr and **exits 0 —
allowing the call** (fail-open on the launcher, so a missing interpreter never bricks every
tool call). The guard's own fail-closed posture only applies *once the script actually runs*.
Treat the plugin as best-effort.

#### Windows and the hook

The launcher requires a POSIX `sh` — it runs natively on macOS/Linux but **not on native
Windows** (cmd / PowerShell). Windows seats need one of:

- **Git Bash** on `PATH` (provides `sh.exe`) — the hook works as-is.
- **WSL** — same story; `sh` is available inside the WSL environment.
- **Neither** — the `sh` command is not found and the hook does not run. The
  `permissions.deny` and `sandbox` layers in the managed policy still protect (see below),
  but the deep shell-command analysis is absent. Use your MDM to push Git Bash or Python
  onto every Windows seat to close this gap.

The hook matcher includes `PowerShell` alongside `Bash`, so on Windows seats that have
`sh` + Python available, `PowerShell` tool calls are inspected by the guard. The guard's
command parser already handles `cmd /c`, `powershell -Command`, and `-EncodedCommand` flag
detection (the encoded payload itself is not decoded — same dynamic-residual class as
base64-decoded paths on POSIX).

#### Controls that don't depend on the hook

The shipped [`managed-settings.plugin.json`](managed-settings.plugin.json) includes two
controls enforced by Claude Code itself — no interpreter or `sh` needed:

1. **`sandbox.filesystem.denyRead` / `denyWrite`** on the Shared-drive mount path (macOS
   only — the Claude Code sandbox binary is not available on Windows).
2. **`permissions.deny`** rules for every file tool (`Read`, `Write`, `Edit`, `MultiEdit`,
   `NotebookEdit`) on the macOS, Git-Bash/MSYS, and native Windows (`G:\Shared drives`)
   Shared-drive paths, plus all `mcp__claude_ai_Google_Drive__*` calls. These outrank
   user/project settings and work on **every platform**.

| Layer | macOS / Linux | Windows (Git Bash) | Windows (native) |
| --- | --- | --- | --- |
| `permissions.deny` | all file tools blocked | all file tools blocked | all file tools blocked |
| `sandbox.filesystem` | reads + writes blocked | N/A | N/A |
| Hook (shell analysis) | deep command parsing | deep command parsing | not available (`sh` missing) |
| Backstop | Google-side Viewer-only | Google-side Viewer-only | Google-side Viewer-only |

For true write-integrity against any bypass, pair these with:

3. **Google-side Viewer-only** on the Shared drive — the unbypassable backstop for the dynamic
   residual above (see next section).

### Backstop: Google-side Viewer-only

The unbypassable control is on **Google's side**: set the in-scope accounts to **Viewer** on
the Shared drive. Viewer permission is enforced by Google for every client and platform — the
agent (or anyone using the account) simply cannot write, regardless of how a command is
obfuscated or which tool is used. Use `drive-guard` for fast, friendly, in-session blocking;
use Viewer-only on the Shared drive for true integrity. (This repo does **not** rely on any
macOS OS-level sandbox as a backstop — that is out of scope here.)

## What's inside

```
.
├── README.md                         # this file
├── LICENSE                           # repo license
├── managed-settings.plugin.json      # org policy: force-install the plugin (Option 2)
├── .claude-plugin/marketplace.json   # marketplace "cerberus" -> drive-guard
└── plugins/drive-guard/
    ├── .claude-plugin/plugin.json    # plugin manifest
    ├── hooks/hooks.json              # single PreToolUse hook entry
    ├── scripts/run-guard.sh          # launcher: picks python3/python at runtime
    ├── scripts/drive-guard.py        # the guard (stdlib-only Python 3)
    └── README.md                     # plugin-internal notes
```

## License

See [LICENSE](LICENSE).
