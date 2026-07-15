#!/usr/bin/env python3
"""
dry_run_llm_test.py
===================
Lightweight dry-run that asks the Core Agent LLM to generate a single
precondition file (f_SECC_CMN_PR_SessionSetup_001.ttcn) for the
TC_SECC_CMN_VTB_ServiceDiscovery_001 test case.

The scenario: the SECC returned INCONC because SessionID was all-zeros,
causing a protocol timeout. The LLM must demonstrate correct use of:
  - setparameter("session_id", oct2char(...))  ← our fixed directive
  - Session ID extraction from the response message
  - Proper TTCN-3 syntax

This script does NOT call the TITAN toolchain, build system, or any
subprocess that requires the SECC hardware. It ONLY calls the Claude CLI
via the same call_claude_agent() path used by ttcn3_generation_pipeline.

Usage:
    python3 dry_run_llm_test.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
PROMPTS_DIR = SCRIPT_DIR / "prompts"
CORE_AGENT_PROMPT_PATH = PROMPTS_DIR / "core_agent_prompt.txt"

# ---------------------------------------------------------------------------
# Minimal dependencies — only the exact functions we need from the pipeline
# ---------------------------------------------------------------------------

def load_text(path: Path) -> str:
    if not path.exists():
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated, total {len(text)} chars]"


def _find_claude_cmd() -> str:
    """Resolve the Claude Code CLI path, same logic as ttcn3_generation_pipeline."""
    import shutil
    npm_paths = [
        r"D:\nodejs\node_global",
        r"C:\Program Files\nodejs\node_global",
        os.path.expanduser(r"~\AppData\Roaming\npm"),
    ]
    for npm_path in npm_paths:
        for name in ("claude.cmd", "claude.CMD"):
            candidate = os.path.join(npm_path, name)
            if os.path.exists(candidate):
                return candidate
    return shutil.which("claude.cmd") or shutil.which("claude") or ("claude.cmd" if os.name == "nt" else "claude")


def _call_llm_raw(prompt: str, agent_name: str = "Core-DryRun", timeout: int = 300) -> str:
    """
    Call the Claude CLI directly, mirroring call_claude_agent() from
    ttcn3_generation_pipeline.py. Returns the raw stdout text.
    """
    claude_cmd = _find_claude_cmd()
    print(f"[DRY-RUN] Claude CLI: {claude_cmd}", file=sys.stderr)

    tmp_prompt_file = Path(tempfile.gettempdir()) / f"dryrun_prompt_{os.getpid()}_{time.time():.0f}.txt"
    tmp_prompt_file.write_text(prompt, encoding="utf-8")

    # Allow Write/Read/Edit so the LLM can inspect project files if it wants
    allowed_tools = "Write,Read,Edit,Bash(bash *)"

    if os.name == "nt":
        cmd_str = (
            f'chcp 65001 >nul & "{claude_cmd}" --bare --verbose -p - '
            f'--output-format text '
            f'--allowedTools "{allowed_tools}" '
            f'<"{tmp_prompt_file}"'
        )
        proc = subprocess.Popen(
            cmd_str,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=True,
        )
    else:
        cmd_list = [
            claude_cmd, "--bare", "--verbose", "-p", "--output-format", "text",
            "--allowedTools", allowed_tools,
        ]
        proc = subprocess.Popen(
            cmd_list,
            stdin=open(tmp_prompt_file, "r", encoding="utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        print(f"[DRY-RUN] TIMEOUT after {timeout}s", file=sys.stderr)
        return ""

    if proc.returncode != 0 and stderr:
        print(f"[DRY-RUN] stderr:\n{stderr[:2000]}", file=sys.stderr)

    # Clean up temp file
    try:
        tmp_prompt_file.unlink()
    except OSError:
        pass

    return stdout


def extract_json_from_response(text: str) -> dict | list | None:
    """Extract JSON from LLM response (works for both text and json modes)."""
    if not text:
        return None

    # Remove the init event array prefix that Claude Code --output-format json emits
    # before the actual JSON block (it prefixes the response with [...  {"type":"system",...}...])
    text = re.sub(r'^\[\s*\{[^}]*"type"\s*:\s*"system"[^}]*\}\s*,?\s*', '', text, flags=re.DOTALL)
    text = text.strip()

    # Strategy 1: find markdown ```json ... ``` block
    m = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 2: first balanced {...}
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    # Strategy 3: first balanced [...]
    m = re.search(r'\[[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*\]', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Build the dry-run prompt
# ---------------------------------------------------------------------------

def build_dry_run_prompt(system_prompt: str) -> str:
    """Assemble the focused dry-run user prompt."""

    # Minimal protocol snapshot so the LLM has realistic field names
    protocol_snapshot = {
        "message_type": "ServiceDiscovery",
        "union_field_req": "serviceDiscoveryReq",
        "union_field_res": "serviceDiscoveryRes",
        "naming": {
            "send_template": "mw_CMN_V2G_Message_TC_SECC_CMN_VTB_ServiceDiscovery_001",
            "receive_template": "mw_CMN_V2G_Message_TC_SECC_CMN_VTB_ServiceDiscovery_001_Res",
        },
        "port_name": "pt_V2G_TCP_TLS_ALM_SECC_Port",
        "uses_raw_octetstring": False,
        "verdict_logic": {
            "pass_condition": "receive ServiceDiscoveryRes with ResponseCode=OK"
        },
    }

    # This is the runtime feedback that would come from the Fault Corrector
    runtime_feedback = """
## Runtime Fault — Please Analyze and Fix

**Fault Type:** session_id_mismatch
**Description:** The SECC returned INCONC because the SessionID field in the
ServiceDiscoveryReq was all zeros ('0000000000000000'O). The SECC timed out
waiting for a valid session.

**Root Cause:** The precondition f_SECC_CMN_PR_SessionSetup_001 successfully
received the SessionSetupRes from the SECC, but the TTCN-3 code did NOT call
setparameter("session_id", ...) to persist the session ID before sending
the next request. As a result, the C++ adapter sent '0000000000000000'O as
the SessionID in the V2GTP header, which the SECC rejected.

**Suggested Fix:** After receiving SessionSetupRes, extract the sessionID
field from the decoded message and call:
    setparameter("session_id", oct2char(vc_session_id));
Then the next send() via the same port will include the correct SessionID.
"""

    generation_plan = {
        "tc_id": "TC_SECC_CMN_VTB_ServiceDiscovery_001",
        "testcase_name": "TC_SECC_CMN_VTB_ServiceDiscovery_001",
        "precondition_chain": ["f_SECC_CMN_PR_SessionSetup_001"],
        "message_type": "ServiceDiscovery",
        "port_name": "pt_V2G_TCP_TLS_ALM_SECC_Port",
        "union_field_req": "serviceDiscoveryReq",
        "union_field_res": "serviceDiscoveryRes",
        "verdict_logic": {"pass_condition": "receive ServiceDiscoveryRes with ResponseCode=OK"},
        "sut_type": "V2G_TCP_TLS_ALM_SECC",
    }

    user_prompt = f"""{system_prompt}

---

## TASK: Fix f_SECC_CMN_PR_SessionSetup_001 (SessionID Persistence Bug)

A precondition function f_SECC_CMN_PR_SessionSetup_001 has a critical bug:
it receives the SessionSetupRes from the SECC but fails to persist the
SessionID, causing subsequent requests to be rejected with FAILED_UnknownSession.

{runtime_feedback}

## Generation Plan
```json
{json.dumps(generation_plan, indent=2, ensure_ascii=False)}
```

## Your Task
Generate ONLY the precondition file `f_SECC_CMN_PR_SessionSetup_001.ttcn`
(Module: f_SECC_CMN_PR_SessionSetup_001).

Return a JSON object with this exact shape:
```json
{{
  "precondition_files": [
    {{
      "path": "src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_SessionSetup_001.ttcn",
      "content": "// full TTCN-3 module code here"
    }}
  ],
  "testcase": "",
  "postcondition_files": []
}}
```

IMPORTANT:
- The file MUST include: receive SessionSetupRes → extract sessionID → setparameter(...)
- Use oct2char() to convert the octetstring session ID to charstring before passing to setparameter
- Return ONLY the JSON. Do not explain or narrate.
"""

    return user_prompt


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70, file=sys.stderr)
    print("DRY RUN: Core Agent SessionID setparameter Test", file=sys.stderr)
    print("=" * 70, file=sys.stderr)

    # 1. Load system prompt
    system_prompt = load_text(CORE_AGENT_PROMPT_PATH)
    if not system_prompt:
        print(f"[ERROR] Could not load: {CORE_AGENT_PROMPT_PATH}", file=sys.stderr)
        return 1

    print(f"[DRY-RUN] Loaded system prompt: {len(system_prompt)} chars", file=sys.stderr)
    print(f"[DRY-RUN] Checking for Framework API section...", file=sys.stderr)
    if "setparameter" in system_prompt:
        print("  ✓ 'setparameter' directive found in prompt", file=sys.stderr)
    else:
        print("  ✗ 'setparameter' NOT in prompt — check the prompt file!", file=sys.stderr)

    if "oct2char" in system_prompt:
        print("  ✓ 'oct2char' directive found in prompt", file=sys.stderr)
    elif "oct2hex" in system_prompt:
        print("  ✗ 'oct2hex' still in prompt (should be oct2char!)", file=sys.stderr)
    else:
        print("  ? 'oct2char' NOT in prompt", file=sys.stderr)

    print("", file=sys.stderr)

    # 2. Build user prompt
    user_prompt = build_dry_run_prompt(system_prompt)
    print(f"[DRY-RUN] User prompt size: {len(user_prompt)} chars", file=sys.stderr)
    print("[DRY-RUN] Calling LLM (timeout=300s)...", file=sys.stderr)
    print("-" * 70, file=sys.stderr)

    # 3. Call LLM
    raw_response = _call_llm_raw(user_prompt, agent_name="Core-DryRun", timeout=300)

    if not raw_response:
        print("[DRY-RUN] LLM returned empty response.", file=sys.stderr)
        return 1

    # 4. Extract JSON
    parsed = extract_json_from_response(raw_response)

    if not parsed:
        print("[DRY-RUN] Could not parse JSON from LLM response.", file=sys.stderr)
        print("Raw response (first 2000 chars):", file=sys.stderr)
        print(raw_response[:2000], file=sys.stderr)
        return 1

    # 5. Extract and display the generated code
    precondition_files = parsed.get("precondition_files", [])
    testcase = parsed.get("testcase", "")
    postcondition_files = parsed.get("postcondition_files", [])

    all_files = precondition_files + ([{"path": "testcase", "content": testcase}] if testcase else []) + postcondition_files

    print("\n" + "=" * 70, file=sys.stderr)
    print("GENERATED TTCN-3 CODE", file=sys.stderr)
    print("=" * 70, file=sys.stderr)

    if not all_files:
        print("[WARNING] No TTCN-3 files in response. Showing raw response:", file=sys.stderr)
        print(raw_response[:3000], file=sys.stderr)
        return 0

    for fdata in all_files:
        path = fdata.get("path", "?")
        content = fdata.get("content", "")
        print(f"\n{'─' * 70}", file=sys.stderr)
        print(f"FILE: {path}", file=sys.stderr)
        print(f"{'─' * 70}", file=sys.stderr)
        # Print to stdout for the user to review
        print(f"\n// === FILE: {path} ===\n")
        print(content)

    # 6. Validation checks
    print(f"\n{'=' * 70}", file=sys.stderr)
    print("VALIDATION CHECKS", file=sys.stderr)
    print(f"{'=' * 70}", file=sys.stderr)

    checks_passed = 0
    checks_failed = 0

    for fdata in all_files:
        content = fdata.get("content", "")
        if not content:
            continue
        path = fdata.get("path", "")

        # Check 1: setparameter is used
        if "setparameter" in content:
            print(f"  ✓ [{path}] Uses setparameter", file=sys.stderr)
            checks_passed += 1
        else:
            print(f"  ✗ [{path}] MISSING setparameter!", file=sys.stderr)
            checks_failed += 1

        # Check 2: oct2char is used (not oct2hex)
        if "oct2char" in content and "oct2hex" not in content:
            print(f"  ✓ [{path}] Uses oct2char (correct)", file=sys.stderr)
            checks_passed += 1
        elif "oct2hex" in content:
            print(f"  ✗ [{path}] Still uses oct2hex (WRONG! should be oct2char)", file=sys.stderr)
            checks_failed += 1
        else:
            print(f"  ? [{path}] No oct2char/oct2hex found", file=sys.stderr)

        # Check 3: session_id string literal is correct
        if '"session_id"' in content:
            print(f"  ✓ [{path}] Uses '\"session_id\"' (lowercase, quoted)", file=sys.stderr)
            checks_passed += 1
        else:
            print(f"  ? [{path}] No '\"session_id\"' string literal found", file=sys.stderr)

        # Check 4: no oct2hex (would cause TITAN compile error)
        if "oct2hex" not in content:
            print(f"  ✓ [{path}] No oct2hex (safe)", file=sys.stderr)
            checks_passed += 1
        else:
            print(f"  ✗ [{path}] CONTAINS oct2hex — will fail TITAN compile!", file=sys.stderr)
            checks_failed += 1

    print(f"\n{checks_passed} checks passed, {checks_failed} failed.", file=sys.stderr)

    if checks_failed > 0:
        print("\n[RESULT] FAILED — the LLM did not follow the Framework API correctly.", file=sys.stderr)
        return 1
    else:
        print("\n[RESULT] PASSED — the LLM correctly used setparameter and oct2char.", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
