#!/bin/sh
# Drive-guard launcher: run drive-guard.py with whichever Python interpreter exists.
#
# Why this exists (ISSUE #8): Claude Code's exec-form command hooks spawn the named
# executable DIRECTLY (no shell). Listing two hook entries ("python3" and "python")
# meant the absent one spawn-errored on EVERY matched tool call. There is no per-OS or
# per-interpreter guard field in the hook schema, so we collapse to a SINGLE hook entry
# that runs this launcher, which probes for an interpreter at runtime and invokes only
# the one that is present. No interpreter present -> we print a clear notice and exit 0
# (fail-open on the LAUNCHER itself, so a missing Python never bricks every tool call;
# the guard script keeps its own fail-closed posture once it actually runs).
#
# Platform note: this is a POSIX sh script and does NOT run on native Windows (cmd /
# PowerShell). Windows seats must provide a POSIX sh (Git Bash / WSL) for the hook to
# fire. Without sh, the managed-settings permissions.deny layer still blocks all file
# tools on Shared-drive paths. See the root README's "Windows and the hook" section.

# PATH-hijack hardening (ISSUE #1): hook commands inherit the caller's PATH, so a
# user-writable dir earlier on PATH could shadow "python3"/"python" with a fake interpreter
# that exits 0 and silently disables the guard. Prepend trusted system dirs so the real
# system interpreter is found first; the inherited PATH stays appended, so a python only in
# a non-standard dir is still located (preserving the fallback behavior).
PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"; export PATH

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$@"
elif command -v python >/dev/null 2>&1; then
  exec python "$@"
else
  echo "drive-guard: WARNING — no python3 or python interpreter found; skipping guard (allowing)." >&2
  exit 0
fi
