"""
tb_verdict_linter.py
====================

Static lint enforcement for Sprint 3.1 (added 2026-07-14, see
BATCH_ANALYSIS_20260714.md §3).

Every Test Behavior (TB) function MUST validate the JSON `ResponseCode` field
from the SECC response before declaring `setverdict(pass)`. Without this,
the TB may declare PASS when the SECC actually returned `FAILED_*` or a
`Notification` fault, producing FALSE POSITIVES in the batch report.

This module exposes:

  * ``lint_tb_for_response_code_validation(source)`` → list of violations
  * ``auto_patch_tb_response_code_validation(source)`` → best-effort patch
  * ``enforce_response_code_validation(source)``   → lint then auto-patch

The module is intentionally dependency-free so it can be unit-tested in
isolation.

A TB is *valid* iff ALL of the following are true inside the function body
that ends with `setverdict(pass|...)`:

  1. ``oct2char(<some_response>.raw)`` is consumed into a string variable.
  2. That string variable is checked via
     ``match(<str>, "*\"ResponseCode\":\"OK*\"*")`` (the TTCN-3
     string-pattern `match()` is the only safe substring-detection API
     that compiles in TITAN — see HARD RULE #3 in
     ``core_agent_prompt.txt``).  The older
     ``regexp(<str>, pattern(...), 0) != -1`` and ``indexof(<str>, ...)``
     idioms are ILLEGAL and the linter hard-fails on them.
  3. The branch yielding ``setverdict(pass)`` is *guarded* by the OK check
     (i.e. parsed into an ``if (resp_ok) { setverdict(pass, ...) }`` block,
     or the LLM uses an explicit if-else ladder).
  4. A *distinct* ``setverdict(fail)`` exists for `FAILED_*` and `Notification`
     FailureCode cases (so the SECC log can be cross-referenced).

The lint is deliberately conservative — it only flags things that are
PROVABLY wrong (e.g. a TB file that contains `setverdict(pass)` and a `raw :=\?`
receive but NO `match()` of the OK ResponseCode anywhere, OR uses the
illegal `regexp(...)` / `indexof(...)` calls). Heuristic advisories
("could be more strict") are not produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class TbLintViolation:
    rule_id: str
    line: int
    message: str


# ---------------------------------------------------------------------------
# Compiled regex (module-level for speed)
# ---------------------------------------------------------------------------

# Match a TB file: TB module names always start with f_<side>_*_TB_*.
_TB_MODULE_RE = re.compile(
    r"^module\s+(f_\w+_TB_\w+)\s*\{", re.MULTILINE,
)

# `receive({ raw := ? })` — Sprint 3 idiomatic TB receive line.
_RECV_RAW_RE = re.compile(
    r"\[\]\s*pt_V2G_TCP_TLS_ALM_SECC_Port\.receive\(\s*\{\s*raw\s*:=\s*\?\s*\}\s*\)",
    re.MULTILINE,
)

# `oct2char(<var>)` — the response is converted into a charstring.
# Allow either `oct2char(<var>.raw)` or `oct2char(<var>)` where
# <var> was previously assigned `<other>.raw` (the LLM often
# writes `var octetstring X := v_msg.raw;` then `oct2char(X)`).
_OCT2CHAR_RAW_RE = re.compile(
    r"oct2char\s*\(", re.MULTILINE,
)
# Track that the local var was assigned `.raw` somewhere — the LLM
# pattern is `var octetstring X := v_msg.raw; ... oct2char(X)`.
_RAW_ASSIGN_RE = re.compile(
    r":=\s*\w+\.raw\s*", re.MULTILINE,
)

# Anything that asserts an `OK` ResponseCode via substring match.
# The TTCN-3-compatible substring-detection API is `match(<str>, <pattern>)`
# with `*` wildcards.  We accept the canonical pattern:
#   match(<str>, "*\"ResponseCode\":\"OK*\"*")
# Anything containing `ResponseCode` and `OK` together in a match() call.
_MATCH_OK_RESPONSECODE_RE = re.compile(
    r"match\s*\(\s*\w+\s*,\s*\"[^\"]*\\?\"ResponseCode\\?\"[^\"]*\\?\"OK[^\"]*\"",
    re.MULTILINE,
)

# A more permissive fall-back: any `match()` that mentions ResponseCode.
_MATCH_RESPONSECODE_RE = re.compile(
    r"match\s*\(\s*\w+\s*,\s*\"[^\"]*ResponseCode",
    re.MULTILINE,
)

# Sprint 3.3 — reject illegal `regexp(...)` and `indexof(...)` calls on
# JSON-bearing charstrings.  Earlier linter versions encouraged those
# function names but they don't compile in TITAN (regexp returns a
# substring, not -1; indexof doesn't exist for charstrings).
# When detected, the linter must surface a HARD ERROR rather than
# silently pass.
#
# The `regexp(<str>, pattern("..."), <group>)` form is the only legal
# form in TTCN-3 / TITAN.  We accept the form AND detect the illegal
# `regexp(...) != -1` idiom (which is what the LLM was generating).
_ILLEGAL_REGEXP_NE1_RE = re.compile(
    r"regexp\s*\(\s*[^,]+,\s*pattern\s*\([^)]*\)\s*,\s*[^)]+\)\s*!=\s*-1\b",
    re.MULTILINE,
)
_ILLEGAL_INDEXOF_RE = re.compile(
    r"\bindexof\s*\(", re.MULTILINE,
)

# `setverdict(pass,` — raw or as a substring inside an `if (...)`.
_SETVERDICT_PASS_RE = re.compile(
    r"\bsetverdict\s*\(\s*pass\b", re.MULTILINE,
)

# `setverdict(fail,` — required by rule #4 to be present at least once.
_SETVERDICT_FAIL_RE = re.compile(
    r"\bsetverdict\s*\(\s*fail\b", re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Module-isolation helper
# ---------------------------------------------------------------------------

def _extract_tb_module_source(source: str) -> str:
    """Return only the slice of source from `module f_*_TB_* {` onwards.

    The TB may include a top-level `import` block before the module body,
    but our sanitize/lint only operates inside the module body. If no
    TB module is found, the entire source is returned unchanged.
    """
    m = _TB_MODULE_RE.search(source)
    if not m:
        return source
    return source[m.start():]


def _extract_response_handling_block(source: str) -> str:
    """Return the slice of `alt { ... }` that handles the `raw := ?` receive.

    Falls back to the whole source if the structure can't be located.
    """
    m = _RECV_RAW_RE.search(source)
    if not m:
        return source
    # Walk forward to the matching closing brace of the receive arg.
    start = m.start()
    # Find the enclosing alt block opener before this receive.
    alt_open = source.rfind("alt", 0, start)
    if alt_open == -1:
        return source[start:]
    # Find the next `}` AT the same indentation that closes the alt. We
    # over-approximate by returning from the alt opener to EOF and let
    # downstream regex search it.
    return source[alt_open:]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def lint_tb_for_response_code_validation(source: str) -> List[TbLintViolation]:
    """Return a list of violations found in this TB module.

    Empty list = compliant. Violations include line numbers (1-based)
    pointing at the offending construct.
    """
    violations: List[TbLintViolation] = []
    body = _extract_tb_module_source(source)
    if body == source and not _TB_MODULE_RE.search(source):
        # Not a TB file at all — skip silently. Other lints may apply.
        return violations

    # If there's no `receive({ raw := ? })` branch, the TB is likely a BadCase
    # test that only checks timing out / error_indicator. Those are
    # exempt from this rule.
    if not _RECV_RAW_RE.search(body):
        return violations

    # The actual checks must inspect the WHOLE module body (not just the
    # alt-block slice) — variables may be declared before the receive.
    response_block = body

    # Rule 1: oct2char(<var>) must exist somewhere — proves the TB
    # pulled the SECC response into a parseable string. Also accept the
    # common idiom where `var octetstring X := v_msg.raw;` precedes
    # `oct2char(X)`.
    has_oct2char = bool(_OCT2CHAR_RAW_RE.search(response_block))
    has_raw_assign = bool(_RAW_ASSIGN_RE.search(response_block))
    if not (has_oct2char and has_raw_assign):
        violations.append(TbLintViolation(
            rule_id="TB-VAL-1",
            line=_find_line(body, _RECV_RAW_RE.search(body)),
            message=(
                "TB receive branch uses { raw := ? } but does not "
                "decode `v_msg.raw` into a charstring. Either write "
                "`var charstring resp_json := oct2char(v_msg.raw);` "
                "directly, or first assign `var octetstring X := "
                "v_msg.raw;` then call `oct2char(X)`."
            ),
        ))

    # Rule 2: must check a ResponseCode OK substring on that string.
    has_ok_check = bool(_MATCH_OK_RESPONSECODE_RE.search(response_block))
    has_any_rc_check = bool(_MATCH_RESPONSECODE_RE.search(response_block))

    # Sprint 3.3 — HARD ERROR if the TB uses regexp(...) or indexof(...)
    # on JSON-bearing charstrings.  These do not compile in TTCN-3 / TITAN
    # in the way previous linter / prompt versions assumed.
    illegal_regexp = _ILLEGAL_REGEXP_NE1_RE.search(response_block)
    illegal_indexof = _ILLEGAL_INDEXOF_RE.search(response_block)
    if illegal_regexp or illegal_indexof:
        violations.append(TbLintViolation(
            rule_id="TB-VAL-2-ILLEGAL",
            line=_find_line(
                body,
                illegal_regexp or illegal_indexof,
            ),
            message=(
                "TB uses illegal substring-detection API "
                "(regexp(...) != -1 or indexof(...)).  These do NOT compile "
                "in TTCN-3 / TITAN — regexp() returns a substring, not -1; "
                "indexof() does not exist for charstrings.  Replace with "
                "match(<str>, \"*\\\"ResponseCode\\\":\\\"OK*\\\"*\") per "
                "HARD RULE #3."
            ),
        ))
        # Skip the rest of the rules — if the file doesn't compile, the
        # other rules are moot.  Build will fail anyway.
        return violations

    if not has_ok_check:
        # Provide a softer message if the LLM tried something else.
        if has_any_rc_check:
            violations.append(TbLintViolation(
                rule_id="TB-VAL-2",
                line=_find_line(body, _MATCH_RESPONSECODE_RE.search(body)),
                message=(
                    "TB checks ResponseCode but does NOT assert `OK` "
                    "prefix. Any non-OK response should yield fail. "
                    "Use: `match(resp_json, \"*\\\"ResponseCode\\\":\\\"OK*\\\"*\")`."
                ),
            ))
        else:
            violations.append(TbLintViolation(
                rule_id="TB-VAL-2",
                line=_find_line(body, _RECV_RAW_RE.search(body)),
                message=(
                    "TB receive branch does NOT inspect `ResponseCode`. "
                    "MUST use `match(resp_json, \"*\\\"ResponseCode\\\":\\\"OK*\\\"*\")` "
                    "before calling setverdict(pass). Without this check, "
                    "FAILED_* and Notification faults are silently treated as PASS."
                ),
            ))

    # Rule 3: setverdict(pass) must exist (otherwise nothing to gate).
    if not _SETVERDICT_PASS_RE.search(response_block):
        # Not a violation per se — maybe a BadCase. Skip.
        pass

    # Rule 4: setverdict(fail) should also exist; otherwise the TB can
    # only mark pass or inconc, never fail. Without this the SECC's
    # negative response cannot surface.
    if _SETVERDICT_PASS_RE.search(response_block) and not _SETVERDICT_FAIL_RE.search(response_block):
        violations.append(TbLintViolation(
            rule_id="TB-VAL-3",
            line=_find_line(body, _SETVERDICT_PASS_RE.search(body)),
            message=(
                "TB has setverdict(pass) but no setverdict(fail). The "
                "TB cannot surface a negative SECC response — add a "
                "fail branch matching FAILED_* or Notification."
            ),
        ))

    return violations


def _find_line(body: str, m: re.Match | None) -> int:
    """Return 1-based line number of a regex match within body."""
    if not m:
        return 0
    return body.count("\n", 0, m.start()) + 1


# Standard ResponseCode parse snippet to inject at the start of the
# receive-raw branch's body. The LLM is told to use this exact pattern
# (see core_agent_prompt.txt HARD RULE #3, corrected in Sprint 3.3).
# The TTCN-3-compatible substring-detection API is `match(<str>, <pattern>)`
# with `*` wildcards.  Use double quotes in TTCN-3 charstring literals
# (escaped via outer triple-quoted f-string).
_INJECTED_RESPONSE_CODE_LINT_BLOCK = '''\
        // === [LINT] Injected ResponseCode validation (Sprint 3.3) ===
        if (match(resp_json, "*\\"ResponseCode\\":\\"OK*\\"*")) {
            // OK path: real TITAN pass is allowed below.
            var boolean __tb_resp_ok := true;
        } else if (match(resp_json, "*\\"ResponseCode\\":\\"FAILED*\\"*")) {
            setverdict(fail,
                "<msg_type> rejected by SECC. ResponseCode is FAILED_*. See SECC log.");
        } else if (match(resp_json, "*\\"Notification\\"*") or
                   match(resp_json, "*ParsingError*")) {
            setverdict(fail,
                "<msg_type> Notification fault: ", resp_json);
        } else {
            setverdict(fail,
                "<msg_type>: ResponseCode missing or unparseable: ", resp_json);
        }
        // === end [LINT] block ===

'''


def auto_patch_tb_response_code_validation(
    source: str,
    message_type: str = "ResponseCode",
) -> str:
    """Insert the mandatory ResponseCode check before setverdict(pass).

    Conservative: only patches if a `setverdict(pass, ...)` exists in the
    function body *and* no ResponseCode match already exists. Stops at the
    first violation to keep the patch localized.
    """
    if not _TB_MODULE_RE.search(source):
        return source

    module_m = _TB_MODULE_RE.search(source)
    body = source[module_m.start():]

    # Already-compliant: don't patch (match form).
    if _MATCH_RESPONSECODE_RE.search(body):
        return source
    # If the file uses regexp(...) or indexof(...), the auto-patcher's
    # match()-based injection is harmless to ADD but won't fix the
    # illegal tokens that already exist.  We still skip the patch in that
    # case to avoid leaving a half-fixed file; the linter will surface a
    # TB-VAL-2-ILLEGAL violation and the IA fix-loop will rewrite the TB
    # from scratch in the next iteration.
    if _ILLEGAL_REGEXP_NE1_RE.search(body) or _ILLEGAL_INDEXOF_RE.search(body):
        return source

    # Find the first setverdict(pass, ...) and inject just before it.
    pass_m = _SETVERDICT_PASS_RE.search(body)
    if not pass_m:
        return source

    # Walk backwards from pass_m to find the start of its containing line.
    line_start = body.rfind("\n", 0, pass_m.start()) + 1
    # Detect that line's leading whitespace so we can indent the patch.
    j = line_start
    indent_chars = []
    while j < len(body) and body[j] in (" ", "\t"):
        indent_chars.append(body[j])
        j += 1
    indent = "".join(indent_chars)

    block = _INJECTED_RESPONSE_CODE_LINT_BLOCK
    # Indent each non-empty line in the injected block.
    block_lines = block.splitlines()
    block_indented = "\n".join(
        (indent + ln) if ln.strip() else ln
        for ln in block_lines
    )
    if not block_indented.endswith("\n"):
        block_indented += "\n"
    block_indented = block_indented.replace("<msg_type>", message_type)

    # The `pass_m.start()` is an offset INSIDE body; convert to source.
    offset_in_source = module_m.start() + line_start
    return source[:offset_in_source] + block_indented + source[offset_in_source:]


def enforce_response_code_validation(
    source: str,
    message_type: str = "ResponseCode",
) -> tuple[str, list[TbLintViolation]]:
    """Run lint, then auto-patch if needed. Returns (patched_source, violations)."""
    violations = lint_tb_for_response_code_validation(source)
    if violations:
        patched = auto_patch_tb_response_code_validation(source, message_type=message_type)
        return patched, violations
    return source, []


# ---------------------------------------------------------------------------
# CLI usage (so the lint can be run standalone against a .ttcn file)
# ---------------------------------------------------------------------------

def _cli(argv: List[str]) -> int:
    import argparse
    import sys as _sys
    parser = argparse.ArgumentParser(
        description="Sprint 3.1 TB verdict linter (ResponseCode validation)",
    )
    parser.add_argument("files", nargs="+", help=".ttcn file(s) to lint")
    parser.add_argument(
        "--patch", action="store_true",
        help="Auto-patch in place when violations are found",
    )
    parser.add_argument(
        "--message-type", default=None,
        help="Override the <msg_type> tag inserted by --patch",
    )
    args = parser.parse_args(argv)

    rc = 0
    for f in args.files:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                src = fp.read()
        except (OSError, UnicodeDecodeError) as e:
            print(f"[ERROR] cannot read {f}: {e}")
            rc = 1
            continue
        violations = lint_tb_for_response_code_validation(src)
        if not violations:
            print(f"[OK]    {f}")
            continue
        rc = 1
        print(f"[FAIL]  {f}  ({len(violations)} violation(s))")
        for v in violations:
            print(f"  line {v.line}  {v.rule_id}  {v.message[:120]}")
        if args.patch:
            msg_type = args.message_type or "the test message"
            patched = auto_patch_tb_response_code_validation(src, message_type=msg_type)
            if patched != src:
                with open(f, "w", encoding="utf-8") as fp:
                    fp.write(patched)
                print(f"[PATCH] {f}  (auto-patched; re-lint to verify)")
                post = lint_tb_for_response_code_validation(patched)
                if not post:
                    print(f"[OK]    {f}  (after patch)")
                    rc = 0
                else:
                    print(f"  -> still {len(post)} violation(s) after patch")
                    for v in post:
                        print(f"     line {v.line}  {v.rule_id}  {v.message[:120]}")
    return rc


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_cli(_sys.argv[1:]))
