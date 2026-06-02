#!/usr/bin/env python3
# Drive-guard PreToolUse hook.
# (a) Hard dependency on Python 3 (shebang is python3; uses py3-only stdlib behavior).
# (b) Fail-closed posture: a parseable event whose per-tool protected decision raises
#     an internal error is DENIED (fail closed) rather than silently allowed. An
#     unparseable stdin event cannot identify a target, so it is allowed (with a STDERR
#     warning) to avoid bricking every legitimate tool call.
import sys, os, json, re, shlex

IS_WIN = os.name == "nt"
RE_FLAGS = re.IGNORECASE
_SEP = r"[^\\/]" if IS_WIN else r"[^/]"
_CHILD = r"([/\\].*)?$" if IS_WIN else r"(/.*)?$"
_TRIM = "/\\" if IS_WIN else "/"

WRITER_ANYARG = {"rm", "rmdir", "unlink", "shred", "truncate", "mkdir", "touch",
                 "mkfifo", "mknod", "chmod", "chown", "chgrp", "chflags", "ln"}
WRITER_DEST_LAST = {"cp", "mv", "install", "rsync", "ditto"}
INPLACE_FLAGGED = {"sed": ("-i", "-i.bak", "--in-place"),
                   "perl": ("-i", "-pi", "-pi.bak"), "gsed": ("-i",)}
SHELLS = ("bash", "sh", "zsh", "dash", "ksh")

if IS_WIN:
    WRITER_ANYARG |= {"del", "erase", "rd", "ren", "rename", "attrib", "icacls", "takeown",
                      "remove-item", "ri", "rm", "new-item", "ni", "set-content", "sc",
                      "add-content", "ac", "out-file", "clear-content"}
    WRITER_DEST_LAST |= {"copy", "xcopy", "robocopy", "move", "copy-item", "cpi", "move-item", "mi"}
    SHELLS = SHELLS + ("cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe")

def _is_msys(p):
    return IS_WIN and isinstance(p, str) and p.startswith("/")

def _posix_norm(p):
    lead = "/" if p.startswith("/") else ""
    parts = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not lead:
                parts.append("..")
        else:
            parts.append(seg)
    return lead + "/".join(parts) if (lead or parts) else "."

def _win_both_forms(p):
    forms = [p]
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if m:
        forms.append("/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/"))
    else:
        m = re.match(r"^/([A-Za-z])/(.*)$", p)
        if m:
            forms.append(m.group(1).upper() + ":\\" + m.group(2).replace("/", "\\"))
    return forms

PATH_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "NotebookRead",
              "Read", "Glob", "Grep", "LS")
MCP_BLOCKED_PREFIXES = ("mcp__claude_ai_Google_Drive__",)
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

def mode():
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        al = a.strip().lower()
        if al in ("--readonly", "-readonly"):
            return "readonly"
        if al in ("--block", "-block"):
            return "block"
        if al.startswith("--mode="):
            return "readonly" if al.split("=", 1)[1] == "readonly" else "block"
        if al in ("--mode", "-mode") and i + 1 < len(argv):
            return "readonly" if argv[i + 1].strip().lower() == "readonly" else "block"
    m = os.environ.get("DRIVE_GUARD_MODE", "block").strip().lower()
    return "readonly" if m == "readonly" else "block"

def protected_raw():
    env = os.environ.get("DRIVE_GUARD_PROTECTED", "").strip()
    if env:
        entries = [p for p in env.split(os.pathsep) if p]
        if IS_WIN:
            out = []
            for e in entries:
                for f in _win_both_forms(e):
                    if f not in out:
                        out.append(f)
            return out
        return entries
    if IS_WIN:
        return ["G:\\Shared drives", "/g/Shared drives"]
    return [os.path.join("~", "Library", "CloudStorage", "GoogleDrive-*", "Shared drives")]

def prep_pattern(pat):
    pat = os.path.expandvars(os.path.expanduser(pat)).rstrip(_TRIM)
    if _is_msys(pat):
        return _posix_norm(pat)
    star = pat.find("*")
    if star == -1:
        try:
            return os.path.realpath(pat)
        except Exception:
            return pat
    slash = max(pat.rfind("/", 0, star), pat.rfind("\\", 0, star)) if IS_WIN else pat.rfind("/", 0, star)
    static, rest = (pat[:slash], pat[slash:]) if slash != -1 else ("", pat)
    try:
        static = os.path.realpath(static) if static else static
    except Exception:
        pass
    return static + rest

def _glob_to_regex(pat):
    out, i = [], 0
    while i < len(pat):
        if pat[i] == "*":
            if pat[i:i + 2] == "**":
                out.append(".*"); i += 2
            else:
                out.append(_SEP + "*"); i += 1
        else:
            out.append(re.escape(pat[i])); i += 1
    return "".join(out)

def canon(path, cwd):
    if not path:
        return None
    path = os.path.expandvars(os.path.expanduser(str(path)))
    if _is_msys(path):
        return _posix_norm(path)
    if not os.path.isabs(path):
        if IS_WIN and _is_msys(cwd):
            return _posix_norm(cwd.rstrip("/\\") + "/" + path.replace("\\", "/"))
        path = os.path.join(cwd or os.getcwd(), path)
    try:
        return os.path.realpath(path)
    except Exception:
        return os.path.normpath(path)

def matches(path, raws):
    if not path:
        return False
    for r in raws:
        if re.match(_glob_to_regex(prep_pattern(r)) + _CHILD, path, RE_FLAGS):
            return True
    return False

def command_mentions(command, raws):
    for r in raws:
        for form in {os.path.expandvars(os.path.expanduser(r)).rstrip(_TRIM), prep_pattern(r)}:
            if re.search(_glob_to_regex(form), command, RE_FLAGS):
                return r
    return None

def looks_like_path(t):
    if ("/" in t) or t.startswith("~") or t.startswith("."):
        return True
    if IS_WIN:
        if "\\" in t:
            return True
        if len(t) >= 2 and t[1] == ":" and t[0].isalpha():
            return True
    return False

def _strip_redirect(t):
    return (t.lstrip("0123456789").lstrip("&").lstrip(">").lstrip("|").lstrip("<")) or t

def _want_posix(seg):
    if not IS_WIN:
        return True
    forced = os.environ.get("DRIVE_GUARD_SHELL", "auto").strip().lower()
    if forced in ("posix", "bash", "gitbash", "msys", "wsl"):
        return True
    if forced in ("windows", "win", "powershell", "pwsh", "cmd"):
        return False
    return "\\" not in seg

def _split(seg):
    posix = _want_posix(seg)
    try:
        toks = shlex.split(seg, posix=posix)
    except ValueError:
        return seg.split()
    if not posix:
        toks = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t
                for t in toks]
    return toks

def _is_inline_cmd_flag(t):
    if t == "-c" or (t.startswith("-") and "c" in t):
        return True
    if IS_WIN and t.lower() in ("/c", "/k", "-command", "/command", "-encodedcommand"):
        return True
    return False

_SUBSHELL_RE = re.compile(r"\$\((.+?)\)|`(.+?)`")

def _extract_subshell_contents(text):
    """Yield inner command text from $(...) and `...` constructs."""
    for m in _SUBSHELL_RE.finditer(text):
        yield m.group(1) or m.group(2)

def _segments(command):
    seps, segs, buf = {";", "|", "&"}, [], ""
    for ch in command:
        if ch in seps or ch == "\n":
            segs.append(buf); buf = ""
        else:
            buf += ch
    segs.append(buf)
    return segs

def bash_path_tokens(command):
    for seg in _segments(command):
        seg = seg.strip()
        if not seg:
            continue
        toks = _split(seg)
        for j, t in enumerate(toks):
            if t in (">", ">>", ">|", "&>", "&>>", "<") and j + 1 < len(toks):
                yield toks[j + 1]
            elif t.startswith(">") or t.startswith("&>") or t.startswith("<") or (t[:1].isdigit() and ">" in t):
                yield _strip_redirect(t)
            elif t.startswith("of=") and len(t) > 3:
                yield t[3:]
            elif looks_like_path(t):
                yield t
        for j, t in enumerate(toks):
            for inner in _extract_subshell_contents(t):
                yield from bash_path_tokens(inner)
        for j, t in enumerate(toks):
            if os.path.basename(t) in SHELLS:
                for k in range(j + 1, len(toks)):
                    if _is_inline_cmd_flag(toks[k]) and k + 1 < len(toks):
                        yield from bash_path_tokens(toks[k + 1])
                break
            if os.path.basename(t) == "eval" and j + 1 < len(toks):
                yield from bash_path_tokens(" ".join(toks[j + 1:]))
                break

def bash_block_reason(command, cwd, raws):
    if matches(canon(cwd, cwd), raws):
        return f"command runs inside a protected Shared-drive path (cwd: {cwd})"
    for tok in bash_path_tokens(command):
        if matches(canon(tok, cwd), raws):
            return f"command references a protected Shared-drive path: {tok}"
    if command_mentions(command, raws):
        return "command text references a protected Shared-drive path"
    return None

def bash_write_reason(command, cwd, raws):
    for seg in _segments(command):
        seg = seg.strip()
        if not seg:
            continue
        toks = _split(seg)
        if not toks:
            continue
        for j, t in enumerate(toks):
            red = None
            if t in (">", ">>", ">|", "&>", "&>>"):
                red = toks[j + 1] if j + 1 < len(toks) else None
            elif t.startswith(">") or t.startswith("&>"):
                red = _strip_redirect(t)
            if red and looks_like_path(red) and matches(canon(red, cwd), raws):
                return f"redirection writes into protected path: {red}"
        k = 0
        while k < len(toks) and "=" in toks[k] and not looks_like_path(toks[k].split("=")[0]):
            k += 1
        if k >= len(toks):
            continue
        cmd, args = os.path.basename(toks[k]), toks[k + 1:]
        pathargs = [a for a in args if looks_like_path(a)]
        if cmd in SHELLS:
            for ai, a in enumerate(args):
                if _is_inline_cmd_flag(a) and ai + 1 < len(args):
                    inner = bash_write_reason(args[ai + 1], cwd, raws)
                    if inner:
                        return inner
        if cmd == "eval" and args:
            inner = bash_write_reason(" ".join(args), cwd, raws)
            if inner:
                return inner
        if cmd == "tee":
            for a in pathargs:
                if matches(canon(a, cwd), raws):
                    return f"tee writes into protected path: {a}"
        if cmd == "dd":
            for a in args:
                if a.startswith("of=") and matches(canon(a[3:], cwd), raws):
                    return f"dd of= writes into protected path: {a[3:]}"
        if cmd in WRITER_ANYARG:
            for a in pathargs:
                if matches(canon(a, cwd), raws):
                    return f"{cmd} modifies/deletes protected path: {a}"
        if cmd in WRITER_DEST_LAST and pathargs:
            if matches(canon(pathargs[-1], cwd), raws):
                return f"{cmd} writes to protected destination: {pathargs[-1]}"
        if cmd in INPLACE_FLAGGED:
            if any(a == f or a.startswith(f) for a in args for f in INPLACE_FLAGGED[cmd]):
                for a in pathargs:
                    if matches(canon(a, cwd), raws):
                        return f"{cmd} edits in place inside protected path: {a}"
        if cmd == "git":
            mut = {"add", "commit", "checkout", "restore", "reset", "rm", "mv",
                   "clean", "stash", "apply", "merge", "rebase", "pull"}
            if any(a in mut for a in args[:2]):
                for a in pathargs:
                    if matches(canon(a, cwd), raws):
                        return f"git mutates protected path: {a}"
                if matches(canon(cwd, cwd), raws):
                    return f"git mutates inside protected working tree: {cwd}"
    return None

def emit_deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))

def audit(decision, tool, target, reason):
    path = os.environ.get("DRIVE_GUARD_AUDIT", "").strip()
    if not path:
        return
    try:
        import datetime
        with open(path, "a") as f:
            f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                                "mode": mode(), "decision": decision, "tool": tool,
                                "target": str(target), "reason": reason}) + "\n")
    except Exception:
        pass

def deny_now(tool, target, reason):
    audit("deny", tool, target, reason)
    emit_deny(reason)
    sys.exit(0)

def main():
    try:
        data = json.loads(sys.stdin.read().lstrip("\ufeff"))
    except Exception as e:
        # Unparseable stdin cannot identify a protected target; allow rather than brick
        # every tool call, but warn on STDERR so the failure is not silent.
        print(f"drive-guard: WARNING \u2014 could not parse hook stdin as JSON ({e}); allowing.",
              file=sys.stderr)
        sys.exit(0)
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    cwd = data.get("cwd") or os.getcwd()
    raws = protected_raw()
    m = mode()
    path_field = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")

    # MCP connector block — applies in both block and readonly modes
    if any(tool.startswith(p) for p in MCP_BLOCKED_PREFIXES):
        deny_now(tool, tool, f"Google Drive MCP connector blocked: {tool} is not permitted.")

    if m == "block":
        if tool in PATH_TOOLS:
            try:
                target = canon(path_field, cwd) if path_field else canon(cwd, cwd)
                hit = matches(target, raws)
                # Glob carries its location in "pattern"; Grep in "glob". These may
                # contain wildcards (**), so resolve their static prefix the same way
                # prep_pattern() does, then test against matches().
                pat_target = None
                if not hit and tool == "Glob" and ti.get("pattern"):
                    pat_target = canon(prep_pattern(ti.get("pattern")), cwd)
                    hit = matches(pat_target, raws)
                if not hit and tool == "Grep" and ti.get("glob"):
                    pat_target = canon(prep_pattern(ti.get("glob")), cwd)
                    hit = matches(pat_target, raws)
            except Exception as e:
                deny_now(tool, path_field, f"Locked Google Drive (Shared drives): "
                         f"{tool} denied (fail closed on internal error: {e}).")
            if hit:
                shown = pat_target or target
                deny_now(tool, shown, f"Locked Google Drive (Shared drives): {tool} on "
                         f"'{shown}' is blocked — no operations permitted on this path.")
            audit("allow", tool, target, "")
            sys.exit(0)
        if tool == "Bash":
            try:
                reason = bash_block_reason(ti.get("command", "") or "", cwd, raws)
            except Exception as e:
                deny_now(tool, ti.get("command", ""), f"Locked Google Drive (Shared drives): "
                         f"Bash denied (fail closed on internal error: {e}).")
            if reason:
                deny_now(tool, ti.get("command", ""), f"Locked Google Drive (Shared drives): "
                         f"{reason}. Blocked.")
            audit("allow", tool, ti.get("command", ""), "")
            sys.exit(0)
        sys.exit(0)

    if tool in WRITE_TOOLS:
        try:
            target = canon(path_field, cwd)
            hit = matches(target, raws)
        except Exception as e:
            deny_now(tool, path_field, f"Read-only Google Drive: {tool} denied "
                     f"(fail closed on internal error: {e}).")
        if hit:
            deny_now(tool, target, f"Read-only Google Drive: '{target}' is inside a protected "
                     f"path; {tool} blocked.")
        audit("allow", tool, target, "")
        sys.exit(0)
    if tool == "Bash":
        try:
            reason = bash_write_reason(ti.get("command", "") or "", cwd, raws)
        except Exception as e:
            deny_now(tool, ti.get("command", ""), f"Read-only Google Drive: Bash denied "
                     f"(fail closed on internal error: {e}).")
        if reason:
            deny_now(tool, ti.get("command", ""), f"Read-only Google Drive: {reason}. "
                     f"Blocked (OS sandbox also enforces writes).")
        audit("allow", tool, ti.get("command", ""), "")
        sys.exit(0)
    sys.exit(0)

if __name__ == "__main__":
    main()
