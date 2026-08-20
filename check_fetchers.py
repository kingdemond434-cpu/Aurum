#!/usr/bin/env python3
"""FAIL ON A FETCHER THAT DISCARDS ITS RESPONSE — the defect that fabricates freshness.

THE SHAPE, VERBATIM, FROM A SHIPPED RECOMMENDATION PACK

    def fetch_aisc(self):
        response = requests.get(self.WGC_AISC_URL, timeout=30)
        # Parse the page for latest AISC figure
        data = {
            "last_updated": datetime.utcnow().isoformat(),
            "aisc_usd_per_oz": 1395,          # Update from WGC report
        }
        self.cache.write("wgc", "aisc", data)
        return data

The request is made. The response is never read. `1395` is typed. The record is stamped with the
current time and cached with a 720-hour freshness window, so every consumer sees a value that
looks newer than any real measurement and is a constant somebody remembered.

WHY A FENCE RATHER THAN A FIX

Nothing downstream can detect it, and that is the whole problem. The value arrives from a function
named `fetch_*`, in a package named `local_fetchers`, carrying a fresh timestamp — every signal a
consumer could check says "measured". Code review catches it only if a reviewer reads the body,
and the body is forty lines of plausible cache handling around one dead request.

It is also aimed at guards. `supply_side.floor_context` refuses when AISC is None; a fabricated
1395 means the refusal never fires. A guard is not defeated by an argument here — it is defeated
by a number that looks better than the real one.

WHAT IS CHECKED, AND WHY EACH IS SYNTACTIC

  DISCARDED RESPONSE  a function that calls requests.get/post/urlopen and never reads .text,
                      .json(), .content or iterates the result. The request is decoration.

  STAMPED CONSTANT    a dict literal combining a `datetime.now()/utcnow()` freshness stamp with a
                      hardcoded numeric field. That pairing is the fabrication signature: the
                      timestamp exists only to make the constant look fetched.

Both are decidable from the AST, which means the check costs milliseconds and cannot be argued
with at review time.

WHAT IT DELIBERATELY DOES NOT DO

It does not check that the parse is CORRECT — only that a response is read at all. A fetcher that
reads `.text` and parses it wrongly is a bug; a fetcher that never reads it is a lie. Those need
different responses and only the second is mechanically detectable.

    python check_fetchers.py [path ...]

Exit 0 clean, 1 on any finding.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

_REQUEST_CALLS = {"get", "post", "put", "request"}

#: The receiver must be an HTTP client. **WITHOUT THIS THE CHECK MATCHES `dict.get()`** and
#: therefore almost every function ever written: the first version flagged 130 functions in
#: golddesk including `render()`, `cost()` and `matches()`, which is a fence nobody would leave
#: switched on. Rooted at the leftmost identifier so `urllib.request.urlopen` and
#: `self.session.get` both resolve.
#: NOT "s", NOT "c", NOT any single letter. A local `s = self.by_status()` followed by
#: `s.get(...)` is a dict read, and admitting one-letter roots put `absorb.report()` back on the
#: list. A root has to be unambiguously an HTTP client to earn a finding.
_HTTP_ROOTS = {"requests", "httpx", "urllib", "urllib2", "session", "aiohttp", "urlfetch"}
_RESPONSE_READS = {"text", "json", "content", "read", "iter_lines", "iter_content", "raw"}
_CLOCKS = {"utcnow", "now"}

SKIP_PARTS = {".git", "__pycache__", ".venv", "node_modules", "build", "dist"}


def _root_name(node: ast.AST) -> str:
    """Leftmost identifier of an attribute chain: `urllib.request.urlopen` -> "urllib"."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _is_request(node: ast.Call) -> bool:
    """An HTTP call, and NOT `d.get(...)`.

    The receiver is checked, not just the method name. `some_dict.get("k")` and
    `requests.get(url)` are the same AST shape apart from what they are called on, and treating
    them alike makes the check useless.
    """
    f = node.func
    if isinstance(f, ast.Name):
        return f.id in {"urlopen", "urlretrieve"}
    if not isinstance(f, ast.Attribute):
        return False
    if f.attr in {"urlopen", "urlretrieve"}:
        return True
    if f.attr not in _REQUEST_CALLS:
        return False
    root = _root_name(f.value)
    if root in {"self", "cls"}:
        # self.session.get(...) counts; self.cache.get(...) does not.
        return any(part in _HTTP_ROOTS for part in _attr_parts(f.value))
    return root in _HTTP_ROOTS


def _attr_parts(node: ast.AST) -> list[str]:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return parts


def _reads_a_response(fn: ast.AST) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and n.attr in _RESPONSE_READS:
            return True
        if isinstance(n, ast.With):          # `with urlopen(...) as r:` then r.read()
            return True
    return False


def _stamped_constant(fn: ast.AST) -> bool:
    """A dict literal pairing a clock call with a hardcoded number."""
    for n in ast.walk(fn):
        if not isinstance(n, ast.Dict):
            continue
        has_clock = any(
            isinstance(v, ast.Call) and any(
                isinstance(s, ast.Attribute) and s.attr in _CLOCKS for s in ast.walk(v))
            for v in n.values)
        has_const = any(isinstance(v, ast.Constant) and isinstance(v.value, (int, float))
                        and not isinstance(v.value, bool) for v in n.values)
        if has_clock and has_const:
            return True
    return False


def scan(path: Path) -> list[dict]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [{"file": str(path), "line": getattr(exc, "lineno", 0) or 0,
                 "fn": "<unparseable>", "why": f"cannot parse: {exc}"}]
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        requests_made = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _is_request(n)]
        if requests_made and not _reads_a_response(fn):
            out.append({"file": str(path), "line": fn.lineno, "fn": fn.name,
                        "why": ("makes a request and never reads the response. The request is "
                                "decoration; whatever this returns did not come from it")})
        elif _stamped_constant(fn) and fn.name.startswith(("fetch", "get", "load", "pull")):
            out.append({"file": str(path), "line": fn.lineno, "fn": fn.name,
                        "why": ("returns a dict pairing a clock stamp with a hardcoded number. "
                                "That pairing is the fabrication signature — the timestamp exists "
                                "only to make the constant look fetched")})
    return out


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv[1:]] or [ROOT / "golddesk"]
    findings: list[dict] = []
    scanned = 0
    for t in targets:
        files = sorted(t.rglob("*.py")) if t.is_dir() else [t]
        for p in files:
            if SKIP_PARTS & set(p.parts) or p.name == Path(__file__).name:
                continue
            scanned += 1
            findings.extend(scan(p))

    print(f"fetcher honesty: {'OK' if not findings else 'FABRICATED-FRESHNESS'}")
    print(f"  {scanned} file(s) scanned")
    for f in findings:
        print(f"  {f['file']}:{f['line']}  {f['fn']}()")
        print(f"      {f['why']}")
    if not findings:
        print("  every fetcher reads what it fetched")
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
