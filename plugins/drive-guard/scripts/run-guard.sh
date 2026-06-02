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
# PowerShell). This deployment is macOS-centric (Google Drive "CloudStorage" mounts).
# Windows seats must provide a POSIX sh (Git Bash / WSL) OR the hook must be switched to
# a "shell":"powershell" variant. See open_concerns.

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$@"
elif command -v python >/dev/null 2>&1; then
  exec python "$@"
else
  echo "drive-guard: WARNING — no python3 or python interpreter found; skipping guard (allowing)." >&2
  exit 0
fi
