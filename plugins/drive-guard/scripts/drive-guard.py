#!/usr/bin/env python3
# Drive-guard PreToolUse hook.
# (a) Hard dependency on Python 3 (shebang is python3; uses py3-only stdlib behavior).
# (b) Fail-closed posture: a parseable event whose per-tool protected decision raises
#     an internal error is DENIED (fail closed) rather than silently allowed. An
#     unparseable stdin event cannot identify a target, so it is allowed (with a STDERR
#     warning) to avoid bricking every legitimate tool call.
import sys, os, json, re, shlex, glob as _glob, fnmatch

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

_GLOB_META = ("*", "?", "[")

def _has_glob_meta(t):
    return any(ch in t for ch in _GLOB_META)

def _glob_expand_hits(tok, cwd, raws):
    """A token may contain shell wildcards; the shell expands them at runtime before
    the tool ever sees a literal path. Mirror that: expand the token with glob.glob()
    (non-recursive, after expanduser/expandvars, relative tokens resolved against the
    event cwd) and report the first expansion that canon()s into a protected path.
    Because glob only yields paths that actually exist on disk, this fires only when a
    wildcard genuinely resolves into the protected tree (near-zero false positives)."""
    p = os.path.expandvars(os.path.expanduser(tok))
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    try:
        results = _glob.glob(p)
    except Exception:
        return None
    for r in results:
        if matches(canon(r, cwd), raws):
            return r
    return None

def _protected_canon_forms(cwd, raws):
    """Representative resolved protected path strings for fnmatch-style comparison.
    A protected pattern may retain a glob segment (e.g. GoogleDrive-*/Shared drives)
    because prep_pattern() only realpath()s the static prefix; that is fine for the
    -path/-ipath substring tests, which themselves operate on glob patterns."""
    forms = []
    for r in raws:
        c = canon(prep_pattern(r), cwd)
        if c and c not in forms:
            forms.append(c)
    return forms

def _protected_concrete_dirs(cwd, raws):
    """glob.glob()-expanded concrete protected directories that exist on disk. Used as
    the *name* side of find -path tests so the only wildcards in play come from find's
    own pattern (the protected pattern's own glob segment is resolved away)."""
    out = []
    for r in raws:
        pp = os.path.expandvars(os.path.expanduser(r))
        if not os.path.isabs(pp):
            pp = os.path.join(cwd or os.getcwd(), pp)
        try:
            for g in _glob.glob(pp):
                c = canon(g, cwd)
                if c and c not in out:
                    out.append(c)
        except Exception:
            pass
    return out

def _root_is_protected_ancestor(rc, raws):
    """True if the concrete directory rc is at/above a protected path, honoring glob
    segments in the protected pattern (so rc=.../GoogleDrive-test@x.com counts as an
    ancestor of .../GoogleDrive-*/Shared drives). Compares segment-by-segment, matching
    a protected glob segment against the concrete rc segment via fnmatch."""
    rc_segs = [s for s in rc.split("/") if s]
    for r in raws:
        prot = canon(prep_pattern(r), None) if rc.startswith("/") else prep_pattern(r)
        prot = prot or prep_pattern(r)
        p_segs = [s for s in prot.split("/") if s]
        if len(rc_segs) > len(p_segs):
            continue
        ok = True
        for rs, ps in zip(rc_segs, p_segs):
            a, b = (rs.lower(), ps.lower()) if RE_FLAGS else (rs, ps)
            if not fnmatch.fnmatch(a, b):
                ok = False
                break
        if ok:
            return True
    return False

def _find_reason(toks, cwd, raws):
    """Targeted handling for find(1): its -path/-ipath/-wholename patterns are evaluated
    by find itself (not the shell), and a search root at/above a protected ancestor
    combined with any filter can surface protected children via -exec. Neither is caught
    by literal token matching, so inspect find's own argv here."""
    if not toks or os.path.basename(toks[0]) != "find":
        return None
    # Concrete (existing) protected dirs for the -path tests; fall back to the canon
    # forms (which may keep a glob segment) only when nothing is on disk yet.
    concrete = _protected_concrete_dirs(cwd, raws)
    prots = concrete or _protected_canon_forms(cwd, raws)
    if not prots:
        return None
    PATH_PREDS = ("-path", "-ipath", "-wholename", "-iwholename")
    FILTER_PREDS = PATH_PREDS + ("-name", "-iname", "-regex", "-iregex")
    roots, has_filter = [], False
    i = 1
    while i < len(toks):
        t = toks[i]
        if t in PATH_PREDS and i + 1 < len(toks):
            has_filter = True
            pat = toks[i + 1]
            ci = t in ("-ipath", "-iwholename")
            fp = pat.lower() if ci else pat
            # Literal (wildcard-stripped) content of the find pattern. Requiring the
            # protected dir's basename to appear here keeps us from firing on an
            # incidental substring match (e.g. '*x*' happening to hit a char in the
            # resolved path) — the pattern must actually name the protected tree.
            fp_literal = fp.replace("*", "").replace("?", "")
            for prot in prots:
                p = prot.lower() if ci else prot
                base = os.path.basename(p.rstrip("/"))
                names_prot = base in fp_literal
                cand = [p, p + "/", p + "/__dg_child__", p + "/a/b"]
                hit = fp == p or fp.startswith(p + "/") or any(fnmatch.fnmatch(c, fp) for c in cand)
                if hit and (names_prot or fp_literal.startswith(p) or p in fp_literal):
                    return f"find -path/-ipath pattern targets a protected Shared-drive path: {pat}"
            i += 2
            continue
        if t in FILTER_PREDS:
            has_filter = True
            i += 2
            continue
        if t.startswith("-") or t in ("(", ")", "!", ";", "+", "{}"):
            i += 1
            continue
        if looks_like_path(t) or not t.startswith("-"):
            roots.append(t)
        i += 1
    if has_filter:
        for root in roots:
            rc = canon(root, cwd)
            if not rc:
                continue
            # search root is at/inside a protected path -> matches() covers it.
            if matches(rc, raws):
                return f"find searches inside a protected Shared-drive path: {root}"
            # search root is an ancestor of a protected path -> a filter can surface
            # protected children via -exec; conservatively deny.
            if _root_is_protected_ancestor(rc, raws):
                return f"find search root is above a protected Shared-drive path: {root}"
    return None

# ── working-directory emulation (cd / pushd / popd traversal) ────────────────
# The legacy analysis below resolves every relative path token against ONE cwd
# (the event's cwd). But a command can change directory mid-stream — `cd <mount>
# && cat "Shared drives/x"` — so a later relative token resolves into the
# protected tree at runtime even though it does NOT under the event cwd. That was
# a real bypass. _walk_cwd models the directory left-to-right and yields each
# simple-command together with the effective cwd in force when it runs, so the
# token check can resolve against the right base.
#
# Design constraints (false positives are the operational danger here):
#   * This layer is ADDITIVE. It runs before the legacy analysis and only ever
#     ADDS denials; the legacy event-cwd analysis still runs unchanged afterward.
#   * It is wrapped in try/except by its callers: ANY error in this hand-rolled
#     parsing falls back to today's behavior (fail OPEN to legacy), so a parser
#     bug can never newly over-block a command that works today.
#   * eff_cwd is trusted (KNOWN) only when a `cd` target fully resolves to a
#     literal via captured NAME=VALUE assignments + expandvars(os.environ) +
#     expanduser, with no remaining $(...)/backtick/unset-var/glob. Otherwise it
#     is UNKNOWN (None) and this layer adds NO denial for that command — the
#     computed-cd / fully-dynamic case stays in the documented residual that is
#     backstopped by Google-side Viewer-only, not guessed at here.

_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
_VAR_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")

def _expand_vars(s, env):
    # Substitute ${NAME}/$NAME from env; leave unknown names intact so a residual
    # '$' marks the value as not statically resolvable.
    def repl(m):
        name = m.group(1) or m.group(2)
        return env[name] if name in env else m.group(0)
    return _VAR_RE.sub(repl, s)

def _is_static_literal(s):
    return not ("$" in s or "`" in s or any(c in s for c in _GLOB_META))

def _resolve_cd_target(raw, eff, env):
    # KNOWN -> canon()'d absolute dir; UNKNOWN -> None. `$HOME`/`${HOME}` and ~
    # resolve to literals (nil FP risk: only matters if they land under the
    # protected glob); $(...)/backtick/unset-var/glob stay UNKNOWN.
    if raw is None:
        return os.path.expanduser("~")            # bare `cd` -> home
    s = os.path.expanduser(_expand_vars(raw, env))
    if not _is_static_literal(s):
        return None
    if not os.path.isabs(s) and not eff:          # relative target, base unknown
        return None
    return canon(s, eff)

def _read_paren(s, i):
    """From index i (just past '('), return (inner_text, index_past_matching ')'),
    honoring quotes and nested parens. Tolerant of an unbalanced '(' (takes rest)."""
    depth, buf, n = 1, "", len(s)
    sq = dq = False
    while i < n:
        c = s[i]
        if sq:
            buf += c; sq = c != "'"; i += 1; continue
        if dq:
            buf += c; dq = c != '"'; i += 1; continue
        if c == "'":
            sq = True; buf += c; i += 1; continue
        if c == '"':
            dq = True; buf += c; i += 1; continue
        if c == "\\" and i + 1 < n:
            buf += c + s[i + 1]; i += 2; continue
        if c == "(":
            depth += 1; buf += c; i += 1; continue
        if c == ")":
            depth -= 1
            if depth == 0:
                return buf, i + 1
            buf += c; i += 1; continue
        buf += c; i += 1
    return buf, i

def _scan_segments(command):
    """Split a command into ordered items at the top level, honoring quotes:
      ('cmd', text)  — a simple-command between connectors (; | & && || newline)
      ('sub', inner) — a `( ... )` subshell group (its cwd changes don't leak)
    `$( ... )` is left inside the surrounding token (command substitution is part
    of a simple command and its cd never affects the parent), so it is NOT split."""
    items, buf, i, n = [], "", 0, len(command)
    sq = dq = False
    def flush():
        if buf.strip():
            items.append(("cmd", buf))
    while i < n:
        c = command[i]
        if sq:
            buf += c; sq = c != "'"; i += 1; continue
        if dq:
            buf += c; dq = c != '"'; i += 1; continue
        if c == "'":
            sq = True; buf += c; i += 1; continue
        if c == '"':
            dq = True; buf += c; i += 1; continue
        if c == "\\" and i + 1 < n:
            buf += c + command[i + 1]; i += 2; continue
        if c == "(" and not buf.endswith("$"):
            flush(); buf = ""
            inner, i = _read_paren(command, i + 1)
            items.append(("sub", inner)); continue
        if c == "(" and buf.endswith("$"):          # command substitution: keep in token
            inner, i = _read_paren(command, i + 1)
            buf += "(" + inner + ")"; continue
        if c == ")":
            i += 1; continue                         # stray close paren
        if c in ";|&\n":
            flush(); buf = ""; i += 1; continue
        buf += c; i += 1
    flush()
    return items

def _apply_cd(cmd0, args, state):
    merged = dict(os.environ); merged.update(state["env"])
    if cmd0 == "popd":
        if state["stack"]:
            state["prev"], state["eff"] = state["eff"], state["stack"].pop()
        return
    nonflag = [a for a in args if not (a.startswith("-") and a != "-")]
    if cmd0 == "pushd":
        state["stack"].append(state["eff"])
    if len(nonflag) > 1:
        new = None                                   # ambiguous -> UNKNOWN
    elif not nonflag:
        new = _resolve_cd_target(None, state["eff"], merged)   # home
    elif nonflag[0] == "-":
        new = state["prev"]                          # `cd -` (may be None)
    else:
        new = _resolve_cd_target(nonflag[0], state["eff"], merged)
    state["prev"], state["eff"] = state["eff"], new

def _walk_items(items, state):
    for kind, text in items:
        if kind == "sub":
            # subshell: its cd/pushd do NOT leak to the parent — recurse on a copy
            child = {"eff": state["eff"], "prev": state["prev"],
                     "stack": list(state["stack"]), "env": dict(state["env"])}
            yield from _walk_items(_scan_segments(text), child)
            continue
        try:
            toks = _split(text)
        except Exception:
            toks = text.split()
        k = 0                                        # capture leading assignments
        while k < len(toks) and _ASSIGN_RE.match(toks[k]):
            name, _, val = toks[k].partition("=")
            merged = dict(os.environ); merged.update(state["env"])
            v = os.path.expanduser(_expand_vars(val, merged))
            if _is_static_literal(v):
                state["env"][name] = v
            else:
                state["env"].pop(name, None)
            k += 1
        rest = toks[k:]
        if rest and os.path.basename(rest[0]) in ("cd", "pushd", "popd"):
            _apply_cd(os.path.basename(rest[0]), rest[1:], state)
        yield rest, state["eff"]

def _walk_cwd(command, base_cwd):
    """Yield (tokens, eff_cwd) per simple-command in execution order. eff_cwd is a
    resolved absolute path, or None (UNKNOWN) when it can't be statically tracked."""
    base = canon(base_cwd, base_cwd) or base_cwd
    state = {"eff": base, "prev": None, "stack": [], "env": {}}
    yield from _walk_items(_scan_segments(command), state)

def _seg_path_tokens(toks):
    """Path-looking tokens of a single already-split simple-command (the top-level
    extraction from bash_path_tokens; subshell/-c/eval recursion stays in the
    legacy bash_path_tokens pass, which still runs against the event cwd)."""
    out = []
    for j, t in enumerate(toks):
        if t in (">", ">>", ">|", "&>", "&>>", "<") and j + 1 < len(toks):
            out.append(toks[j + 1])
        elif t.startswith(">") or t.startswith("&>") or t.startswith("<") or (t[:1].isdigit() and ">" in t):
            out.append(_strip_redirect(t))
        elif t.startswith("of=") and len(t) > 3:
            out.append(t[3:])
        elif looks_like_path(t):
            out.append(t)
    return out

def _cwd_block_reason(command, cwd, raws):
    for toks, eff in _walk_cwd(command, cwd):
        if eff is None:                              # UNKNOWN cwd -> add no denial
            continue
        if matches(canon(eff, eff), raws):
            return (f"command runs inside a protected Shared-drive path after a "
                    f"directory change (cwd: {eff})")
        fr = _find_reason(toks, eff, raws)
        if fr:
            return fr
        # Only slash/~/.-bearing tokens are treated as paths (the legacy
        # looks_like_path heuristic). A bare data argument that merely equals a
        # protected basename — e.g. `grep "Shared drives" notes.txt` while cwd is
        # the Drive mount — must NOT be resolved as a path, or it would falsely
        # deny a legitimate read. The "cd straight into the protected tree" case is
        # already covered by the cwd-is-protected check above; the only thing not
        # caught here is naming the protected child by a bare word from its parent
        # (`cd <mount> && ls "Shared drives"`), which is an accepted residual —
        # the slash forms (`ls "Shared drives/"`, `cat "Shared drives/x"`) ARE
        # caught.
        for tok in _seg_path_tokens(toks):
            if matches(canon(tok, eff), raws):
                return f"command references a protected Shared-drive path: {tok}"
            if _has_glob_meta(tok):
                hit = _glob_expand_hits(tok, eff, raws)
                if hit:
                    return f"command wildcard expands into a protected Shared-drive path: {hit}"
    return None

def _seg_write_reason(toks, wd, raws):
    """Write/delete/mutate detection for one already-split simple-command, resolved
    against working dir `wd`. Extracted from bash_write_reason so both the cwd-aware
    walker and the legacy event-cwd loop share identical logic."""
    if not toks:
        return None
    for j, t in enumerate(toks):
        red = None
        if t in (">", ">>", ">|", "&>", "&>>"):
            red = toks[j + 1] if j + 1 < len(toks) else None
        elif t.startswith(">") or t.startswith("&>"):
            red = _strip_redirect(t)
        if red and matches(canon(red, wd), raws):
            return f"redirection writes into protected path: {red}"
    k = 0
    while k < len(toks) and "=" in toks[k] and not looks_like_path(toks[k].split("=")[0]):
        k += 1
    if k >= len(toks):
        return None
    cmd, args = os.path.basename(toks[k]), toks[k + 1:]
    # Path operands are slash/~/.-bearing tokens (legacy looks_like_path). A bare
    # data argument that merely equals a protected basename — e.g. a
    # `git commit -m "Shared drives"` message while cwd is the Drive mount root —
    # must NOT be treated as a path, or it would falsely deny a legitimate write.
    # EXCEPTION: when the working dir is itself inside the protected tree, a bare
    # filename operand really does resolve into protected content (e.g.
    # `cd <shared> && touch new.txt`), so there we widen to every non-flag operand.
    # The per-command gating below is unchanged, so reads that happen to run in a
    # protected cwd — `git log`, `sed 's/a/b/' f` (no -i) — stay allowed.
    if matches(canon(wd, wd), raws):
        pathargs = [a for a in args if not a.startswith("-")]
    else:
        pathargs = [a for a in args if looks_like_path(a)]
    if cmd in SHELLS:
        for ai, a in enumerate(args):
            if _is_inline_cmd_flag(a) and ai + 1 < len(args):
                inner = bash_write_reason(args[ai + 1], wd, raws)
                if inner:
                    return inner
    if cmd == "eval" and args:
        inner = bash_write_reason(" ".join(args), wd, raws)
        if inner:
            return inner
    if cmd == "tee":
        for a in pathargs:
            if matches(canon(a, wd), raws):
                return f"tee writes into protected path: {a}"
    if cmd == "dd":
        for a in args:
            if a.startswith("of=") and matches(canon(a[3:], wd), raws):
                return f"dd of= writes into protected path: {a[3:]}"
    if cmd in WRITER_ANYARG:
        for a in pathargs:
            if matches(canon(a, wd), raws):
                return f"{cmd} modifies/deletes protected path: {a}"
    if cmd in WRITER_DEST_LAST and pathargs:
        if matches(canon(pathargs[-1], wd), raws):
            return f"{cmd} writes to protected destination: {pathargs[-1]}"
    if cmd in INPLACE_FLAGGED:
        if any(a == f or a.startswith(f) for a in args for f in INPLACE_FLAGGED[cmd]):
            for a in pathargs:
                if matches(canon(a, wd), raws):
                    return f"{cmd} edits in place inside protected path: {a}"
    if cmd == "git":
        mut = {"add", "commit", "checkout", "restore", "reset", "rm", "mv",
               "clean", "stash", "apply", "merge", "rebase", "pull"}
        if any(a in mut for a in args[:2]):
            for a in pathargs:
                if matches(canon(a, wd), raws):
                    return f"git mutates protected path: {a}"
            if matches(canon(wd, wd), raws):
                return f"git mutates inside protected working tree: {wd}"
    return None

def bash_block_reason(command, cwd, raws):
    # cwd-aware traversal layer (additive; fails OPEN to the legacy analysis on any
    # internal error, so a parser bug can never newly over-block a working command).
    try:
        r = _cwd_block_reason(command, cwd, raws)
        if r:
            return r
    except Exception:
        pass
    if matches(canon(cwd, cwd), raws):
        return f"command runs inside a protected Shared-drive path (cwd: {cwd})"
    for tok in bash_path_tokens(command):
        if matches(canon(tok, cwd), raws):
            return f"command references a protected Shared-drive path: {tok}"
        if _has_glob_meta(tok):
            hit = _glob_expand_hits(tok, cwd, raws)
            if hit:
                return f"command wildcard expands into a protected Shared-drive path: {hit}"
    for seg in _segments(command):
        seg = seg.strip()
        if not seg:
            continue
        toks = _split(seg)
        fr = _find_reason(toks, cwd, raws)
        if fr:
            return fr
    if command_mentions(command, raws):
        return "command text references a protected Shared-drive path"
    return None

def bash_write_reason(command, cwd, raws):
    # cwd-aware traversal layer (additive; fails OPEN to the legacy loop on any
    # internal error). Catches writes/deletes after a `cd` into the protected tree.
    try:
        for toks, eff in _walk_cwd(command, cwd):
            if eff is None:                          # UNKNOWN cwd -> add no denial
                continue
            r = _seg_write_reason(toks, eff, raws)
            if r:
                return r
    except Exception:
        pass
    # Legacy: per-segment analysis against the event cwd (unchanged behavior).
    for seg in _segments(command):
        seg = seg.strip()
        if not seg:
            continue
        toks = _split(seg)
        r = _seg_write_reason(toks, cwd, raws)
        if r:
            return r
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

    # The target actually evaluated for Glob/Grep is their pattern/glob, not path_field
    # (which is None for those tools). Surface it to deny_now()/audit() instead of None.
    eval_target = path_field or ti.get("pattern") or ti.get("glob")

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
                deny_now(tool, eval_target, f"Locked Google Drive (Shared drives): "
                         f"{tool} denied (fail closed on internal error: {e}).")
            if hit:
                shown = pat_target or target
                deny_now(tool, shown, f"Locked Google Drive (Shared drives): {tool} on "
                         f"'{shown}' is blocked — no operations permitted on this path.")
            audit("allow", tool, eval_target or target, "")
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
                     f"Blocked.")
        audit("allow", tool, ti.get("command", ""), "")
        sys.exit(0)
    sys.exit(0)

if __name__ == "__main__":
    main()
