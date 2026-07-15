#!/usr/bin/env python3
"""
test_token_parser.py — Standalone verification for _parse_token_usage_from_claude_output

Usage:
    python3 test_token_parser.py

This script calls Claude Code CLI with a trivial prompt, captures the raw output,
feeds it through the exact same three-path resolver logic used in the pipeline,
and reports whether token data was successfully extracted.

No pipeline run needed — completes in ~10-30 seconds.
"""

import subprocess
import json
import re
import sys


# ---------------------------------------------------------------------------
# Three-path resolver — IDENTICAL logic to the pipeline's implementation.
# Any fix made here should be reflected in ttcn3_generation_pipeline.py.
# ---------------------------------------------------------------------------

def _get_tokens(d: dict) -> tuple:
    if not isinstance(d, dict):
        return 0, 0
    in_t  = d.get("input_tokens")  or d.get("inputTokens")  or 0
    out_t = d.get("output_tokens") or d.get("outputTokens") or 0
    return int(in_t or 0), int(out_t or 0)


def parse_token_usage(stderr: str, stdout: str) -> tuple:
    """Three-path resolver matching _parse_token_usage_from_claude_output."""

    input_tok = 0
    output_tok = 0
    last_assistant_usage = (0, 0)
    candidates = []
    ndjson_scanned = 0
    ndjson_parse_errors = 0
    model_name = None

    # ── Path A & B: NDJSON scan ────────────────────────────────────────────
    if stdout:
        raw = stdout.strip()
        raw_lines = raw.split("\n")

        for rl in raw_lines:
            stripped = rl.strip().rstrip(",")
            if stripped and stripped not in ("[", "]"):
                candidates.append((stripped, True))

        # Single-line JSON array fallback (bracket-aware slicing)
        if not candidates:
            depth, buf_start = 0, -1
            for i, ch in enumerate(raw):
                if ch in ("{", "["):
                    if depth == 0:
                        buf_start = i
                    depth += 1
                elif ch in ("}", "]"):
                    depth -= 1
                    if depth == 0 and buf_start >= 0:
                        candidates.append((raw[buf_start:i + 1], False))
                        buf_start = -1

        path_b_found = False

        for raw_line, _ in candidates:
            ndjson_scanned += 1
            try:
                parsed = json.loads(raw_line)
            except json.JSONDecodeError:
                ndjson_parse_errors += 1
                continue

            items = parsed if isinstance(parsed, list) else [parsed]

            for obj in items:
                if not isinstance(obj, dict):
                    continue

                # Capture model name (first seen wins)
                if model_name is None:
                    _m = obj.get("model") or (
                        obj.get("message", {}).get("model")
                        if isinstance(obj.get("message"), dict) else None
                    )
                    if _m:
                        model_name = _m
                        print(f"    [MODEL] detected: {model_name}")

                # ── Path A: {type:"assistant", message.usage} ──────────────
                if obj.get("type") == "assistant":
                    msg = obj.get("message")
                    if isinstance(msg, dict):
                        usage = msg.get("usage")
                        in_t, out_t = _get_tokens(usage)
                        if in_t or out_t:
                            last_assistant_usage = (in_t, out_t)
                            print(f"    [PATH_A] assistant usage: in={in_t} out={out_t}")

                # ── Path B: {type:"result", modelUsage} ─────────────────────
                if obj.get("type") == "result":
                    mu = obj.get("modelUsage")
                    if isinstance(mu, dict):
                        for _model, u in mu.items():
                            if not isinstance(u, dict):
                                continue
                            in_t, out_t = _get_tokens(u)
                            if in_t or out_t:
                                input_tok += in_t
                                output_tok += out_t
                                path_b_found = True
                                print(f"    [PATH_B] modelUsage: in={in_t} out={out_t}")

    # Promote Path A result (only if Path B didn't fire)
    if last_assistant_usage != (0, 0) and not path_b_found:
        input_tok, output_tok = last_assistant_usage

    # ── Path C: Regex fallback ─────────────────────────────────────────────
    if input_tok == 0 and output_tok == 0:
        combined = stdout
        _input_patterns = [
            (r"Input[:\s]+(\d+)[\s]*tokens?",          "Input: N tokens"),
            (r"input\s*tokens?[:\s]+(\d+)",            "input tokens: N"),
            (r"Input\s+Tokens?\s*[:\-=]\s*(\d+)",      "Input Tokens: N"),
        ]
        _output_patterns = [
            (r"Output[:\s]+(\d+)[\s]*tokens?",         "Output: N tokens"),
            (r"output\s*tokens?[:\s]+(\d+)",            "output tokens: N"),
            (r"Output\s+Tokens?\s*[:\-=]\s*(\d+)",     "Output Tokens: N"),
        ]
        found_in = found_out = None
        for pattern, label in _input_patterns:
            m = re.search(pattern, combined, re.IGNORECASE)
            if m:
                found_in = int(m.group(1))
                print(f"    [PATH_C] input pattern '{label}' -> {found_in}")
                break
        for pattern, label in _output_patterns:
            m = re.search(pattern, combined, re.IGNORECASE)
            if m:
                found_out = int(m.group(1))
                print(f"    [PATH_C] output pattern '{label}' -> {found_out}")
                break
        if found_in is not None or found_out is not None:
            input_tok  = found_in  if found_in  is not None else 0
            output_tok = found_out if found_out is not None else 0
            print(f"    [PATH_C] resolved via regex")

    active_path = "NONE"
    if last_assistant_usage != (0, 0) and not path_b_found:
        active_path = "A"
    elif path_b_found:
        active_path = "B"
    elif input_tok != 0 or output_tok != 0:
        active_path = "C"

    print(f"    [SUMMARY] path={active_path} "
          f"scanned={ndjson_scanned} parse_err={ndjson_parse_errors} "
          f"in={input_tok} out={output_tok}")

    return input_tok, output_tok, active_path, model_name


def run_cli_and_parse() -> tuple:
    """Call Claude Code CLI and return (input_tokens, output_tokens, path)."""

    prompt = "Say hello in exactly 3 words. Reply with only those 3 words."

    cmd = [
        "claude",
        "--bare",
        "--verbose",
        "-p", "-",
        "--output-format", "json",
    ]

    print(f"\n{'='*60}")
    print(f"Calling: {' '.join(cmd[:3])} ... (prompt length={len(prompt)})")
    print(f"{'='*60}")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    try:
        stdout_bytes, stderr_bytes = proc.communicate(
            input=prompt.encode("utf-8"),
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout_bytes, stderr_bytes = proc.communicate()
        print("TIMEOUT — CLI did not respond in 60s")
        return 0, 0, "TIMEOUT"

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    print(f"\n--- Raw Output ({len(stdout)} bytes in stdout, {len(stderr)} bytes in stderr) ---")
    preview = stdout[:300].replace("\n", "\\n")
    print(f"stdout preview: {preview}{'...' if len(stdout) > 300 else ''}")

    if stderr:
        print(f"stderr preview: {stderr[:200].replace(chr(10), '|')}")

    print(f"\n--- Parsing ---")
    in_t, out_t, path, model = parse_token_usage(stderr, stdout)
    return in_t, out_t, path, model


def main():
    print("=" * 60)
    print("  Claude Token Parser — Standalone Verification")
    print("=" * 60)

    in_t, out_t, path, model = run_cli_and_parse()

    print(f"\n{'='*60}")
    print("  RESULT")
    print(f"{'='*60}")

    if path == "NONE":
        print("\n  ❌  FAIL — No token data extracted.")
        print("      The parser found nothing. Check debug output above.")
        print("      Likely cause: Claude CLI output format changed,")
        print("      or the usage field is missing from the response.")
        sys.exit(1)

    if path == "TIMEOUT":
        print("\n  ⏱️  TIMEOUT — CLI did not respond.")
        sys.exit(2)

    if in_t == 0 and out_t == 0:
        print("\n  ⚠️  WARNING — Parsed successfully (Path {path}) but both token")
        print("      counts are zero. This may indicate a real zero-token response,")
        print("      or that the token data is in a non-standard location.")
        sys.exit(3)

    print(f"\n  ✅  SUCCESS (Path {path})")
    print(f"      Model Name    : {model}")
    print(f"      Input Tokens  : {in_t}")
    print(f"      Output Tokens: {out_t}")
    print(f"\n  Token metrics and model name are being recorded correctly.")
    print(f"  metrics.json will contain real data after a pipeline run.")
    sys.exit(0)


if __name__ == "__main__":
    main()
