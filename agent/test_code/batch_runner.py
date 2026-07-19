#!/usr/bin/env python3
"""
batch_runner.py
================
Serial, fully-isolated batch runner for the TTCN-3 generation pipeline.

Intended for the **100% unattended paper-grade benchmark**:

  * Runs N test cases in **strict serial order** to prevent socket collisions
    against the single live SECC daemon (192.168.4.1).
  * Between cases, cleans `.claude/` and `/tmp/titan_build_*` so the next
    case starts in a known state.
  * Records per-case results in a single **JSONL** file (one line per case)
    that can be directly loaded into a pandas DataFrame for statistical
    evaluation.
  * Each record includes:
        - input tc_id, message_type, sut_type, test_type
        - status (pass / fail / error / timeout)
        - latency.total_pipeline_seconds
        - latency.cold_start_seconds
        - verdict.execution_time_seconds
        - verdict.compile_errors_count
        - preinstall_render (1 if static renderer was used)
        - llm_watchdog_triggered (1 if watchdog killed the LLM)
        - timestamp
        - run_dir (the timestamped project directory)
  * Supports a `--dry-run` flag that prints the planned execution plan
    **without** running the pipeline.

Usage
-----
    # Run 10 pre-curated test cases
    python3 agent/test_code/batch_runner.py \\
        --suite suites/v2gtp_pilot_10.json

    # Run an ad-hoc list of TC IDs (auto-curated)
    python3 agent/test_code/batch_runner.py \\
        --tc-ids TC_SECC_CMN_VTB_SDP_001,TC_SECC_CMN_VTB_SessionSetup_001

    # Dry-run: show what would be executed
    python3 agent/test_code/batch_runner.py \\
        --suite suites/v2gtp_pilot_10.json --dry-run

    # Resume from a previous run (skip cases already in results.jsonl)
    python3 agent/test_code/batch_runner.py \\
        --suite suites/v2gtp_pilot_10.json --resume

Author: Cursor (Claude Opus 4)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
AGENT_DIR = SCRIPT_DIR.parent
DOC_DIR = AGENT_DIR / "document"
PIPELINE_SCRIPT = SCRIPT_DIR / "ttcn3_generation_pipeline.py"
KNOWLEDGE_BASE_PATH = DOC_DIR / "iso15118_4_ttcn_knowledge_base.json"

DEFAULT_RESULTS_DIR = AGENT_DIR / "batch_results"
DEFAULT_RESULTS_JSONL = DEFAULT_RESULTS_DIR / "batch_results.jsonl"
DEFAULT_SUITES_DIR = SCRIPT_DIR / "suites"

# ---------------------------------------------------------------------------
# Knowledge-base keyword filter (satisfies all 4 hard requirements)
# ---------------------------------------------------------------------------
KEYWORDS_DEFAULT = [
    "SDP",
    "V2GTPSessionSetup",
    "SupportedAppProtocol",
    "SessionSetup",         # Fixed: was ServiceSetup
    "ServiceDiscovery",
]

KEYWORD_TO_CHOICE: Dict[str, List[str]] = {
    "sdp":         ["SDP"],
    "session":     ["V2GTPSessionSetup"],
    "appprotocol": ["SupportedAppProtocol"],
    "discovery":   ["ServiceDiscovery", "SessionSetup"],  # Fixed: added SessionSetup
    "all":         KEYWORDS_DEFAULT,
}


# Protocol-stage priority for deterministic execution order
# Lower number = earlier in the protocol flow
# This is based on the tc_id prefix (e.g., TC_SECC_CMN_VTB_SDP_* -> SDP)
PROTOCOL_STAGE_PRIORITY: Dict[str, int] = {
    "SDP":                       1,
    "V2GTPSDP":                  2,
    "V2GTPSessionSetup":          3,
    "SupportedAppProtocol":        4,
    "SessionSetup":               5,
    "ServiceDiscovery":           6,
    "ServiceDetail":              6,
    "PaymentServiceSelection":    7,
    "PaymentDetails":             8,
    "Authorization":              9,
    "CertificateInstallation":   10,
    "CertificateUpdate":         11,
    "CableCheck":               12,
    "PowerDelivery":            13,
    "MeteringReceipt":          14,
    "SessionStop":              15,
    "ChargingStatus":           16,
    "MeteringStatus":           17,
    "WeldingDetection":          18,
    "DC_PreCharge":             19,
    "CurrentDemand":             20,
}


def _extract_tc_prefix(tc_id: str) -> str:
    """Extract protocol type prefix from tc_id.

    Examples:
        TC_SECC_CMN_VTB_SDP_001                    -> SDP
        TC_SECC_CMN_VTB_V2GTPSessionSetup_001     -> V2GTPSessionSetup
        TC_SECC_CMN_VTB_SupportedAppProtocol_001   -> SupportedAppProtocol
        TC_SECC_CMN_VTB_SessionSetup_001          -> SessionSetup
        TC_SECC_CMN_VTB_ServiceDiscovery_001      -> ServiceDiscovery
        TC_SECC_AC_VTB_ServiceDiscovery_001       -> ServiceDiscovery
        TC_SECC_DC_VTB_CableCheck_001             -> CableCheck
    """
    import re
    # Pattern: TC_SECC_{category}_VTB_{ProtocolType}_{number} or TC_SECC_{category}_VTB_{ProtocolType}
    # Match the protocol type after VTB_ and before _XXX (where XXX is digits)
    match = re.search(r'VTB_([A-Za-z0-9]+?)_\d', tc_id)
    if match:
        prefix = match.group(1)
        return prefix
    # Fallback: try to find VTB_ followed by alphanumeric prefix before TC_SECC or end
    match = re.search(r'VTB_([A-Za-z0-9]+?)(?:TC_SECC|$)', tc_id)
    if match:
        prefix = match.group(1)
        return prefix
    return "Zzz"  # Unknown prefix sorts last


def _tc_id_sort_key(rec: Dict[str, Any]) -> tuple:
    """Sort key for test case records: by protocol stage, then by tc_id."""
    tc_id = rec.get("tc_id", "")
    # Extract prefix from tc_id
    prefix = _extract_tc_prefix(tc_id)
    # Get priority (unknown prefixes get 999 = last)
    priority = PROTOCOL_STAGE_PRIORITY.get(prefix, 999)
    return (priority, tc_id)


def _suite_sort_key(item: Dict[str, str]) -> tuple:
    """Sort key for suite items: by protocol stage (extracted from tc_id), then by tc_id."""
    tc_id = item.get("tc_id", "")
    prefix = _extract_tc_prefix(tc_id)
    priority = PROTOCOL_STAGE_PRIORITY.get(prefix, 999)
    return (priority, tc_id)


def filter_kb_by_keywords(
    keywords: List[str],
    sut_filter: str = "SECC",
) -> List[Dict[str, Any]]:
    """
    Extract tc_records from the knowledge base matching the given criteria:

    1) tc_id matches any keyword (case-insensitive substring)
    2) sut_type == sut_filter  (default SECC; pass None to disable)
    3) Python set() deduplication — same tc_id matching multiple keywords
       is enqueued exactly once

    Returns records sorted by:
    1) Protocol-stage priority (SDP -> V2GTPSessionSetup -> SupportedAppProtocol ->
       SessionSetup -> ServiceDiscovery, etc.)
    2) tc_id for deterministic order within same stage
    """
    seen: set = set()                  # Python-level dedup (hard requirement #2)
    keywords_lower = [k.lower() for k in keywords]
    matched: List[Dict[str, Any]] = []

    for rec in load_kb():
        tc_id = rec.get("tc_id", "")
        # Hard requirement #3: drop EVCC records (SECC-only)
        if sut_filter and rec.get("sut_type") != sut_filter:
            continue
        # Hard requirement #2: set-based dedup
        if any(k in tc_id.lower() for k in keywords_lower):
            if tc_id not in seen:
                seen.add(tc_id)
                matched.append(rec)

    # Sort by protocol-stage priority (extracted from tc_id), then by tc_id
    matched.sort(key=_tc_id_sort_key)
    return matched


# ---------------------------------------------------------------------------
# Pre-curated 10-case pilot benchmark
# ---------------------------------------------------------------------------
# Curated to cover distinct V2G protocol layers and ISO 15118-2 message
# families. 1 case per family for the pilot; later phases can run all 339 SECC.
#
# KB-filter mode (--kb-filter): extracts SECC-side tc_records from the
# knowledge base that match any of the following protocol-stage keywords:
#   SDP, V2GTPSessionSetup, SupportedAppProtocol, ServiceSetup, ServiceDiscovery
# Python set() deduplication guarantees no tc_id is enqueued twice, even if
# it matches multiple keywords.
# Protocol flow order: SDP -> V2GTPSessionSetup -> SupportedAppProtocol ->
# SessionSetup -> ServiceDiscovery -> ...
# Entries are sorted by _suite_sort_key() at load time.
PILOT_10_DEFAULT: List[Dict[str, str]] = [
    # --- Protocol layer: SDP (UDP multicast discovery) ---
    {"tc_id": "TC_SECC_CMN_VTB_SDP_001",                   "family": "SDP",                   "rationale": "V2GTP layer; SDP discovery exchange"},
    # --- V2G session management: V2GTPSessionSetup ---
    {"tc_id": "TC_SECC_CMN_VTB_V2GTPSessionSetup_001",     "family": "V2GTPSessionSetup",     "rationale": "Session ID establishment (validated TTCN code in KB)"},
    # --- Supported Application Protocol ---
    {"tc_id": "TC_SECC_CMN_VTB_SupportedAppProtocol_001",  "family": "SupportedAppProtocol",  "rationale": "V2GTP layer; first app-layer message"},
    # --- Session Setup ---
    {"tc_id": "TC_SECC_CMN_VTB_SessionSetup_001",           "family": "SessionSetup",          "rationale": "App-layer session establishment"},
    # --- Service Discovery ---
    {"tc_id": "TC_SECC_CMN_VTB_ServiceDiscovery_001",      "family": "ServiceDiscovery",      "rationale": "Service list query"},
    # --- Payment Service Selection ---
    {"tc_id": "TC_SECC_CMN_VTB_PaymentServiceSelection_001", "family": "PaymentServiceSelection", "rationale": "Service selection by EVCC"},
    # --- Authorization / certificates ---
    {"tc_id": "TC_SECC_CMN_VTB_Authorization_001",          "family": "Authorization",         "rationale": "EIM/PnC authorization setup"},
    {"tc_id": "TC_SECC_CMN_VTB_CertificateInstallation_001", "family": "CertificateInstallation", "rationale": "Contract cert provisioning"},
    # --- Charging loop (AC) ---
    {"tc_id": "TC_SECC_AC_VTB_PowerDelivery_001",           "family": "PowerDelivery_AC",      "rationale": "AC charging start/stop"},
    {"tc_id": "TC_SECC_AC_VTB_MeteringReceipt_001",         "family": "MeteringReceipt_AC",    "rationale": "AC meter data exchange"},
    # --- DC charging control ---
    {"tc_id": "TC_SECC_DC_VTB_CableCheck_001",              "family": "CableCheck_DC",         "rationale": "DC isolation pre-charge check"},
]


# ---------------------------------------------------------------------------
# Knowledge-base query helpers
# ---------------------------------------------------------------------------

def load_kb() -> List[Dict[str, Any]]:
    """Load the test-case knowledge base."""
    if not KNOWLEDGE_BASE_PATH.exists():
        return []
    with open(KNOWLEDGE_BASE_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    return d.get("tc_records", [])


def find_kb_record(tc_id: str) -> Optional[Dict[str, Any]]:
    """Find a single KB record by tc_id (exact match)."""
    for r in load_kb():
        if r.get("tc_id") == tc_id:
            return r
    return None


# ---------------------------------------------------------------------------
# Per-case execution
# ---------------------------------------------------------------------------

def make_test_case_file(kb_record: Dict[str, Any], work_dir: Path) -> Path:
    """
    Convert a KB record into the Test_Case JSON file format that the
    pipeline expects (`--test-case-file`).  We also embed a copy of
    `testcase_code` / `test_behavior.code` / `precondition.code` from the KB
    into the file so the rule-based plan builder has richer context.
    """
    tc_id = kb_record["tc_id"]
    tc = {
        "Test_Case": {
            "TC_Id": tc_id,
            "TC_Name": tc_id.replace("TC_", "").replace("_", " "),
            "Purpose": kb_record.get("description", {}).get("test_objective", ""),
            "Document_Reference": {
                "Requirements": [
                    f"[{r}]" for r in kb_record.get("description", {}).get(
                        "referenced_requirements", []
                    )
                ]
            },
        },
        "metadata": {
            "sut_type": kb_record.get("sut_type"),
            "message_type": kb_record.get("message_type"),
            "test_type": kb_record.get("test_type"),
            "source_kb": str(KNOWLEDGE_BASE_PATH.name),
        },
    }
    out_path = work_dir / f"{tc_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(tc, f, indent=2, ensure_ascii=False)
    return out_path


def clean_between_runs() -> None:
    """
    Clean .claude/ and /tmp/titan_build_* between runs to prevent
    state contamination (especially session-history bloat).
    """
    project_root = AGENT_DIR.parent
    claude_dir = project_root / ".claude"
    if claude_dir.is_dir():
        import shutil
        for entry in claude_dir.iterdir():
            if entry.name == "settings.local.json":
                continue
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
            except OSError:
                pass
    # /tmp/titan_build_*
    try:
        for entry in Path("/tmp").iterdir():
            if entry.name.startswith("titan_build_") and entry.is_dir():
                import shutil
                shutil.rmtree(entry, ignore_errors=True)
    except OSError:
        pass


def run_one_case(
    kb_record: Dict[str, Any],
    work_dir: Path,
    extra_args: Optional[List[str]] = None,
    timeout: int = 1800,
    watchdog_timeout: int = 1200,
    no_llm_render: bool = False,
) -> Dict[str, Any]:
    """
    Run the pipeline for a single test case and return a result record.

    Parameters
    ----------
    kb_record : dict
        The KB record for this test case.
    work_dir : Path
        A scratch directory in which to write the test-case JSON file.
    extra_args : list of str, optional
        Extra CLI args passed to the pipeline (e.g. --no-llm-render).
    timeout : int
        Per-case wall-clock timeout in seconds.

    Returns
    -------
    dict
        Result record with verdict, latency, etc.
    """
    tc_id = kb_record["tc_id"]
    started_at = datetime.now(timezone.utc).isoformat()
    cold_start = time.time()
    cold_start_seconds = None  # set immediately; available in both try and except paths

    record = {
        "tc_id": tc_id,
        "sut_type": kb_record.get("sut_type"),
        "message_type": kb_record.get("message_type"),
        "test_type": kb_record.get("test_type"),
        "started_at_utc": started_at,
        "status": "unknown",
    }

    # Make test-case JSON file for the pipeline
    tc_json_path = make_test_case_file(kb_record, work_dir)

    # Build the pipeline command.
    # Default: ENABLE LLM rendering so the Core Agent + Infrastructure Agent
    # can generate code for unusual test cases. Pass --no-llm-render to fall
    # back to the static-renderer-only path (faster, deterministic, but won't
    # adapt to edge cases that aren't covered by generation_patterns.json).
    cmd = [
        sys.executable, "-u", str(PIPELINE_SCRIPT),
        "--test-case-file", str(tc_json_path),
        "--filter-tc-id", tc_id,
        "--rule-based-plan",
        "--watchdog-timeout", str(watchdog_timeout),
    ]
    if no_llm_render:
        cmd.append("--no-llm-render")
    if extra_args:
        cmd.extend(extra_args)

    print(f"\n[{tc_id}] RUNNING: {' '.join(cmd)}")

    # Remove orphan non-timestamped directory from any previous interrupted run
    # before the pipeline starts. Without this, the Infrastructure Agent's sub-agent
    # sees both <TC_ID>/ and <TC_ID>_<ts>/ and may pick the wrong one.
    legacy_dir = AGENT_DIR / "test_code" / "generated" / tc_id
    if legacy_dir.exists() and legacy_dir.is_dir():
        import shutil as _br_shutil
        _br_shutil.rmtree(legacy_dir)
        print(f"[BATCH CLEANUP] Removed orphan dir before pipeline: {legacy_dir}")

    # Stream pipeline stdout/stderr to a per-case log file so we can diagnose
    # pipeline_error cases after the fact. Without this, subprocess.run's
    # capture_output=True swallows all output and we have no idea *why* the
    # pipeline crashed.
    import shutil as _br_shutil_log
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = AGENT_DIR / "test_code" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_{tc_id}_{ts_str}.log"
    log_fh = open(log_path, "w", encoding="utf-8")
    log_fh.write(f"# Command: {' '.join(cmd)}\n# CWD: {AGENT_DIR.parent}\n\n")
    log_fh.flush()

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(AGENT_DIR.parent),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        print(f"[{tc_id}] pipeline log: {log_path}")
        log_fh.flush()

        # === 清理遗留的 non-timestamped 目录 ===
        # 该目录由 pipeline 内部（如 install_shared_ttcn_files 在 no-llm 模式）或
        # 早期 LLM 运行创建；内容已在 ts 目录中，清理不影响功能。
        legacy_dir = AGENT_DIR / "test_code" / "generated" / kb_record["tc_id"]
        if legacy_dir.exists() and legacy_dir.is_dir():
            import shutil as _br_shutil
            _br_shutil.rmtree(legacy_dir)
            print(f"[BATCH CLEANUP] Removed legacy non-timestamped dir: {legacy_dir}")

        # Find the latest generated project dir
        run_dir = _find_latest_generated_dir(tc_id)

        # Record elapsed time (available in both try and except paths).
        # NOTE: this assignment stays in the try-block so the except handler
        # also reads the same value (already set to None before the try).
        cold_start_seconds = time.time() - cold_start

        # Parse metrics.json
        verdict = {}
        latency = {}
        if run_dir:
            metrics_path = run_dir / "metrics.json"
            if metrics_path.exists():
                try:
                    with open(metrics_path, "r", encoding="utf-8") as f:
                        m = json.load(f)
                    verdict = m.get("verdict", {})
                    latency = m.get("latency", {})
                except (json.JSONDecodeError, OSError):
                    pass

        # Parse log for watchdog trigger
        llm_watchdog_triggered = 0
        if run_dir:
            log_path = run_dir / "logs" / "system_logs" / "build.log"
            if log_path.exists():
                try:
                    if "WATCHDOG" in log_path.read_text(encoding="utf-8", errors="replace"):
                        llm_watchdog_triggered = 1
                except OSError:
                    pass

        record.update({
            "run_dir": str(run_dir) if run_dir else None,
            "log_path": str(log_path),
            "process_exit_code": proc.returncode,
            "status": _normalize_status(proc.returncode, verdict),
            "verdict_result": verdict.get("result"),
            "execution_time_seconds": verdict.get("execution_time_seconds", 0.0),
            "build_status": verdict.get("build_status"),
            "compile_errors_count": verdict.get("compile_errors_count", 0),
            # === HONEST WALL-CLOCK TIME ===
            # Sprint 4 fix: `cold_start_seconds` is the AUTHORITATIVE elapsed
            # time (wall-clock from subprocess.start to subprocess.end).
            #
            # `latency.total_pipeline_seconds` is the SUM of *named* subtimings
            # reported by the pipeline internals (planning + generation +
            # infrastructure + compilation + ...). Two reasons it's WRONG:
            #   1. Subtimings don't cover ALL the time the subprocess is alive
            #      — sleep waits, subprocess.run() of TITAN/external tools, and
            #      even idle I/O waits are not bucketed into any subtimer.
            #      In the 2026-07-15 23:08 run, subtimings summed to 556s but
            #      wall-clock was 1762s — a 3× under-report.
            #   2. If the pipeline adds a NEW subtimer field without writing
            #      it to `total_pipeline_seconds`, the sum silently goes stale.
            #
            # Therefore: report cold_start_seconds (= wall-clock) as the
            # canonical `total_pipeline_seconds`. Keep the per-bucket
            # breakdown in their own fields for diagnostics.
            "total_pipeline_seconds": round(cold_start_seconds, 1),
            "infrastructure_seconds": latency.get("infrastructure_seconds", 0.0),
            "compilation_seconds": latency.get("compilation_seconds", 0.0),
            "fault_correction_seconds": latency.get("fault_correction_seconds", 0.0),
            "preinstall_render": 0 if no_llm_render else 1,
            "llm_watchdog_triggered": llm_watchdog_triggered,
            "cold_start_seconds": round(cold_start_seconds, 1),
        })
    except subprocess.TimeoutExpired:
        # === Salvage check: even if the pipeline subprocess timed out, the
        # LLM may have already produced a successful build. Look for a
        # TestExecutable in the latest ts_dir and write emergency metrics
        # so we don't lose the win.
        salvaged_dir = _find_latest_generated_dir(tc_id)
        salvaged_executable = None
        if salvaged_dir:
            for _candidate in (salvaged_dir / "build" / "bin" / "TestExecutable",
                               salvaged_dir / "build" / "TestExecutable"):
                if _candidate.exists():
                    salvaged_executable = _candidate
                    break
        salvaged_metrics = salvaged_dir / "metrics.json" if salvaged_dir else None
        if salvaged_executable and salvaged_metrics and not salvaged_metrics.exists():
            # === Phase 3.1 fix #2: honest salvage ===
            # Before declaring "fix_success_via_salvage", we MUST read the real
            # TITAN verdict from TestExecution.log if one exists. A previous
            # run may have completed (verdict pass/fail/inconc) even though the
            # current pipeline subprocess timed out. Reporting "fix_success"
            # when the actual verdict was "fail" is the false positive bug
            # uncovered in BATCH_ANALYSIS_20260714.md §3.
            salvaged_titan_verdict, salvaged_overall_count = (
                _parse_titan_test_execution_log(salvaged_dir)
            )
            if salvaged_titan_verdict:
                # Real verdict exists on disk — trust it over optimistic guess.
                _salvaged_result = salvaged_titan_verdict
                _salvaged_note = (
                    f"metrics.json reconstructed by batch_runner after subprocess "
                    f"timeout; TestExecution.log shows real TITAN overall "
                    f"verdict={salvaged_titan_verdict} ({salvaged_overall_count} "
                    f"test case(s))."
                )
            elif salvaged_executable.exists():
                # TestExecutable exists but no TITAN log → TITAN never ran.
                # This is the silent-skip mode: pipeline built the binary but
                # never executed it. HONEST verdict: pipeline_silent_skip.
                _salvaged_result = "pipeline_silent_skip"
                _salvaged_note = (
                    "metrics.json reconstructed by batch_runner after subprocess "
                    "timeout; TestExecutable exists but TestExecution.log is "
                    "absent — TITAN did not actually run. This is a SILENT-SKIP, "
                    "NOT a successful TITAN verdict."
                )
            else:
                # Should not reach here — caller already confirmed existence.
                _salvaged_result = "fix_success"
                _salvaged_note = (
                    "metrics.json reconstructed by batch_runner after subprocess "
                    "timeout; TestExecutable already on disk."
                )
            import json as _salvage_json
            # Phase 3.1 fix #2.5 (Sprint 4 audit): also write a `latency`
            # block in the salvage path so batch_runner doesn't have to
            # fall back to cold_start_seconds. The fallback was correct,
            # but writing the canonical latency block keeps the metrics
            # JSON consistent with non-salvage cases (dashboard / time
            # series consumers can rely on a uniform schema).
            # Compute actual elapsed (available in except path via cold_start).
            _salvage_pipeline_seconds = time.time() - cold_start
            _payload = {
                "tc_id": tc_id,
                "timestamp": salvaged_dir.name.rsplit("_", 1)[1] if salvaged_dir else "unknown",
                "latency": {
                    "total_pipeline_seconds": _salvage_pipeline_seconds,
                    "infrastructure_seconds": 0.0,
                    "compilation_seconds": 0.0,
                    "fault_correction_seconds": 0.0,
                    "salvaged_from_timeout": True,
                },
                "verdict": {
                    "result": _salvaged_result,
                    "build_status": "completed",
                    "executable_path": str(salvaged_executable),
                    "salvaged_from_timeout": True,
                    "note": _salvaged_note,
                    **(
                        {"real_titan_verdict_source": "TestExecution.log"}
                        if salvaged_titan_verdict
                        else {"real_titan_verdict_source": "missing_TestExecution.log"}
                    ),
                },
            }
            try:
                salvaged_metrics.write_text(_salvage_json.dumps(_payload, indent=2, ensure_ascii=False))
                print(f"[SALVAGE] emergency metrics written: {salvaged_metrics} "
                      f"(real verdict: {_salvaged_result})")
            except Exception as _se:
                print(f"[SALVAGE] failed to write emergency metrics: {_se}")
        # === Phase 3.1 fix #2: status assignment now reflects REAL verdict ===
        if salvaged_metrics and salvaged_metrics.exists():
            _sm = json.loads(salvaged_metrics.read_text(encoding="utf-8"))
            _real_verdict = _sm.get("verdict", {}).get("result", "unknown")
            # Map the salvaged verdict onto the public status field honestly.
            if _real_verdict in ("pass", "fail", "inconc", "error", "verdict_error"):
                record["status"] = _real_verdict
                record["verdict_result"] = _real_verdict
            elif _real_verdict == "pipeline_silent_skip":
                record["status"] = "pipeline_silent_skip"
                record["verdict_result"] = "pipeline_silent_skip"
            else:
                # Unknown / unverifiable salvage — keep the historical label
                # but add a transparent provenance marker.
                record["status"] = "fix_success_via_salvage"
                record["verdict_result"] = _real_verdict
            record["build_status"] = "completed"
            record["run_dir"] = str(salvaged_dir) if salvaged_dir else None
            record["salvaged_from_timeout"] = True
        else:
            record.update({
                "status": "timeout",
                "log_path": str(log_path),
                "process_exit_code": -1,
                "cold_start_seconds": round(time.time() - cold_start, 1),
            })
    except Exception as e:
        record.update({
            "status": "error",
            "log_path": str(log_path),
            "error": str(e),
            "cold_start_seconds": round(time.time() - cold_start, 1),
        })
    finally:
        # Always close the per-case log file, even on subprocess.TimeoutExpired
        # or other exceptions — otherwise a leaked log_fh can produce confusing
        # truncated logs and fd exhaustion across many cases.
        try:
            if not log_fh.closed:
                log_fh.close()
        except Exception:
            pass

    record["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
    return record


def _find_latest_generated_dir(tc_id: str) -> Optional[Path]:
    """Find the most recent timestamped project dir for the given TC."""
    gen_dir = AGENT_DIR / "test_code" / "generated"
    if not gen_dir.is_dir():
        return None
    pattern = f"{tc_id}_*"
    candidates = sorted(
        [d for d in gen_dir.glob(pattern) if d.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _parse_titan_test_execution_log(salvaged_dir: Path) -> tuple[Optional[str], int]:
    """
    Parse the salvaged TITAN ``TestExecution.log`` and return the real
    overall verdict along with the test-case count, or ``(None, 0)`` if
    parsing fails.

    TITAN emits lines like::

        STATISTICS - Verdict statistics: 0 none (0.00 %), 0 pass (0.00 %),
        0 inconc (0.00 %), 1 fail (100.00 %), 0 error (0.00 %).
        STATISTICS - Test execution summary: 1 test case was executed.
        Overall verdict: fail

    Note: TITAN often inlines "Overall verdict: fail" on the SAME line as
    the "Test execution summary: N test case(s)" line. We must therefore
    handle both single-line and multi-line variants.
    """
    if salvaged_dir is None:
        return None, 0
    log_path = salvaged_dir / "build" / "bin" / "logs" / "titan_traces" / "TestExecution.log"
    if not log_path.exists():
        log_path = salvaged_dir / "build" / "logs" / "titan_traces" / "TestExecution.log"
    if not log_path.exists():
        return None, 0
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, 0
    overall = None
    total_count = 0
    import re as _re

    # 1) PRIMARY: inline "Overall verdict: <pass|fail|inconc|error>" may
    #    appear on the same line as "STATISTICS - Test execution summary".
    #    Look anywhere in the log for the trailing verdict marker.
    overall_matches = list(_re.finditer(
        r"(?i)\bOverall\s+verdict\s*:\s*(\w+)",
        text,
    ))
    if overall_matches:
        overall = overall_matches[-1].group(1).strip().lower()
    # 2) Test execution summary count (find the LAST occurrence).
    summary_matches = list(_re.finditer(
        r"(?i)\bTest\s+execution\s+summary\s*:\s*(\d+)\s+test\s+case",
        text,
    ))
    if summary_matches:
        total_count = int(summary_matches[-1].group(1))
    # 3) FALLBACK: parse the "Verdict statistics" line and infer overall
    #    bucket. May be split across multiple lines so use DOTALL.
    if not overall or total_count == 0:
        stat_match = _re.search(
            r"(?is)\bVerdict\s+statistics\s*:?\s*(.+?)(?:Test execution summary|\Z)",
            text,
        )
        if stat_match:
            counts = {
                "none":   0, "pass":   0, "inconc":  0,
                "fail":   0, "error":  0,
            }
            for kind in counts.keys():
                m = _re.search(rf"\b(\d+)\s+{kind}\b", stat_match.group(1))
                if m:
                    counts[kind] = int(m.group(1))
            tc = sum(counts.values())
            if total_count == 0:
                total_count = tc
            if not overall:
                non_zero = {k: v for k, v in counts.items() if v > 0}
                if not non_zero:
                    overall = "none"
                elif len(non_zero) == 1:
                    overall = next(iter(non_zero.keys()))
                else:
                    # Mixed: default to inconc on ambiguity.
                    overall = "inconc"
    # Normalize verdict spelling variants TITAN may use.
    if overall:
        overall = overall.strip().lower()
        if overall in ("pass", "pass."):
            overall = "pass"
        elif overall in ("fail", "fail."):
            overall = "fail"
        elif overall in ("inconc", "inconclusive", "inconc."):
            overall = "inconc"
        elif overall in ("error", "error."):
            overall = "error"
        else:
            # Unknown label — refuse to guess, keep None.
            overall = None
    return overall, total_count


def _normalize_status(exit_code: int, verdict: Dict[str, Any]) -> str:
    """Map (exit_code, verdict) to a normalized status string.

    TITAN returns a non-zero exit code whenever the test case ends with a
    verdict other than 'pass' (fail, inconc, error). When the metrics.json
    has been written with a clear verdict, trust that verdict and ignore the
    exit code — a 'fail' should be reported as 'fail', not 'pipeline_error'.
    Only fall back to 'pipeline_error' when there is no verdict (the pipeline
    truly crashed before reaching the test).
    """
    result = verdict.get("result", "unknown")
    # If we have a clean TITAN verdict, trust it regardless of exit code.
    if result == "pass":
        return "pass"
    if result == "fail":
        return "fail"
    if result == "inconc":
        return "inconc"
    if result == "error":
        return "verdict_error"
    if result == "not_run":
        return "not_run"
    # No clean verdict → fall back to exit-code-based classification.
    if exit_code != 0:
        return "pipeline_error"
    return "unknown"


# ---------------------------------------------------------------------------
# Suite management
# ---------------------------------------------------------------------------

def load_suite(suite_path: Path) -> List[Dict[str, str]]:
    """Load a suite JSON file (list of {tc_id, family, rationale}).

    Items are sorted by protocol-stage priority before being returned.
    """
    with open(suite_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and "cases" in data:
        items = data["cases"]
    else:
        raise ValueError(f"Suite file {suite_path} is not a list or {cases: []} dict")
    # Sort by protocol stage, then by tc_id
    items.sort(key=_suite_sort_key)
    return items


def load_existing_results(results_jsonl: Path) -> List[str]:
    """Load the set of tc_ids that already have a PASS result (for --resume).

    Only "pass" status is treated as truly completed. Any other status
    (fail, pipeline_error, inconc, verdict_error, timeout, fix_success_via_salvage,
    etc.) leaves the case eligible for retry on the next --resume run.

    Rationale: --resume is meant to skip work that's already done. If a case
    failed last time, re-running with a code fix should retry it, not skip it.
    """
    if not results_jsonl.exists():
        return []
    completed = set()
    with open(results_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                # Only PASS counts as completed
                if r.get("status") == "pass":
                    completed.add(r.get("tc_id"))
            except json.JSONDecodeError:
                continue
    return sorted(completed)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Serial batch runner for the TTCN-3 generation pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--suite", type=Path,
        help="Path to a suite JSON file (list of {tc_id, family, rationale}). "
             f"Default: {DEFAULT_SUITES_DIR / 'v2gtp_pilot_10.json'}",
    )
    parser.add_argument(
        "--tc-ids", type=str,
        help="Comma-separated list of TC IDs to run (overrides --suite).",
    )
    parser.add_argument(
        "--kb-filter",
        choices=["sdp", "session", "appprotocol", "discovery", "all"],
        default=None,
        help="Extract test cases from the knowledge base by protocol stage. "
             "'all' covers SDP + V2GTPSessionSetup + SupportedAppProtocol + "
             "ServiceSetup + ServiceDiscovery.  SECC-only enforced by --sut-filter.",
    )
    parser.add_argument(
        "--sut-filter",
        default="SECC",
        choices=["SECC", "EVCC", "ALL"],
        help="Restrict to SECC/EVCC side.  Default SECC (charging station side). "
             "EVCC is disabled because the physical integration stack "
             "(OpenV2G / TITAN adapters / Python emulators) is only validated "
             "for the SECC-under-test topology.  Use 'ALL' to disable this guard.",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_RESULTS_DIR,
        help=f"Output directory (default: {DEFAULT_RESULTS_DIR})",
    )
    parser.add_argument(
        "--results-name", type=str, default="batch_results.jsonl",
        help="Output JSONL filename (default: batch_results.jsonl)",
    )
    parser.add_argument(
        "--save-default-suite", action="store_true",
        help="Write the default pilot-10 suite to "
             f"{DEFAULT_SUITES_DIR / 'v2gtp_pilot_10.json'} and exit.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the planned execution plan and exit without running.",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip TC IDs that already have results in the output JSONL.",
    )
    parser.add_argument(
        "--per-case-timeout", type=int, default=4500,
        help="Per-case wall-clock timeout in seconds (default: 4500 = 75 min). "
             "Sized to allow Infrastructure Agent's multi-iteration compile-fix loop "
             "to complete (IA base timeout is 5400s in ttcn3_generation_pipeline.py).",
    )
    parser.add_argument(
        "--no-clean-between", action="store_true",
        help="Skip the .claude/ and /tmp/titan_build_* cleanup between cases.",
    )
    parser.add_argument(
        "--no-llm-render", action="store_true",
        help="Skip LLM-based code generation (Core Agent + Infrastructure Agent). "
             "When set, pipeline uses static-renderer-only output. Use this when "
             "Claude CLI sandbox is blocked (e.g. non-ASCII paths). Default is OFF "
             "(LLM rendering enabled) so LLM can generate code for unusual cases.",
    )
    parser.add_argument(
        "--watchdog-timeout", type=int, default=1200,
        help="Per-LLM-call watchdog in seconds (default: 1200 = 20 min).",
    )
    parser.add_argument(
        "--scope", choices=["current", "all"], default="current",
        help="Filter the summary report by run scope. 'current' (default) shows only "
             "records whose started_at_utc >= the moment this batch started. 'all' shows "
             "the full JSONL history (matches the pre-2026-07-14 behavior — useful for "
             "archival digests, not for paper-grade statistics).",
    )
    args = parser.parse_args()

    # Optionally save the default suite file
    if args.save_default_suite:
        out = DEFAULT_SUITES_DIR / "v2gtp_pilot_10.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(PILOT_10_DEFAULT, f, indent=2, ensure_ascii=False)
        print(f"Wrote default suite to {out}")
        return 0

    # Resolve the suite (priority: --kb-filter > --tc-ids > --suite > pilot-10)
    if args.kb_filter:
        # Hard requirement #3: enforce SUT filter
        sut_filter = None if args.sut_filter == "ALL" else args.sut_filter
        keywords = KEYWORD_TO_CHOICE[args.kb_filter]
        kb_records = filter_kb_by_keywords(keywords, sut_filter=sut_filter)
        suite = [
            {
                "tc_id":    r["tc_id"],
                "family":   r.get("message_type", "?"),
                "rationale": f"KB-filter={args.kb_filter}; sut={sut_filter}",
            }
            for r in kb_records
        ]
        print(f"[KB-FILTER] mode={args.kb_filter}  sut={sut_filter}  "
              f"matched={len(suite)} (Python set() dedup)")
    elif args.tc_ids:
        suite = [
            {"tc_id": tc_id.strip(), "family": "ad_hoc", "rationale": "user-specified"}
            for tc_id in args.tc_ids.split(",") if tc_id.strip()
        ]
    elif args.suite:
        suite = load_suite(args.suite)
    else:
        # Default: use built-in pilot 10
        suite = PILOT_10_DEFAULT

    print(f"=== Batch Runner ===")
    print(f"Suite size: {len(suite)} test cases")
    for i, c in enumerate(suite, 1):
        print(f"  {i:2d}. {c['tc_id']:<55} [{c.get('family', '?')}]")

    if args.dry_run:
        print("\n[DRY-RUN] No test cases were executed. Re-run without --dry-run.")
        return 0

    # Resolve KB records (fail fast if any tc_id is missing)
    print(f"\nResolving TC IDs against knowledge base ({KNOWLEDGE_BASE_PATH.name})...")
    records = []
    missing = []
    for c in suite:
        rec = find_kb_record(c["tc_id"])
        if rec is None:
            missing.append(c["tc_id"])
        else:
            records.append(rec)
    if missing:
        print(f"\nERROR: {len(missing)} TC IDs not found in knowledge base:", file=sys.stderr)
        for tc_id in missing[:10]:
            print(f"  - {tc_id}", file=sys.stderr)
        return 1

    # Filter out already-completed cases if --resume
    results_jsonl = args.output_dir / args.results_name
    if args.resume:
        completed = load_existing_results(results_jsonl)
        before = len(records)
        records = [r for r in records if r["tc_id"] not in completed]
        print(f"--resume: skipping {before - len(records)} already-completed cases")
        if not records:
            print("Nothing to do.")
            return 0

    # Make output dir
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if not results_jsonl.exists():
        results_jsonl.write_text("")

    # Run serially
    print(f"\n=== Running {len(records)} cases serially ===")
    print(f"Results: {results_jsonl}")
    print(f"Summary scope: {args.scope} (--scope to change)")

    started = time.time()
    # Snapshot batch start time (UTC, ISO-8601) for "scope=current" filter.
    # Anything written to JSONL with started_at_utc < this is historical.
    batch_started_at_utc = datetime.now(timezone.utc).isoformat()
    pass_count = fail_count = error_count = 0
    for i, rec in enumerate(records, 1):
        tc_id = rec["tc_id"]
        print(f"\n--- [{i}/{len(records)}] {tc_id} ({rec.get('message_type')}) ---")

        if not args.no_clean_between:
            clean_between_runs()

        result = run_one_case(
            rec,
            work_dir=args.output_dir / "tc_json_files",
            extra_args=None,
            timeout=args.per_case_timeout,
            watchdog_timeout=args.watchdog_timeout,
            no_llm_render=args.no_llm_render,
        )

        # Append to JSONL (write-then-rename for atomicity)
        with open(results_jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
            f.flush()

        # === INCONC Self-Healing Retry (UNCONDITIONAL) ===
        # INCONC means: build succeeded, but TITAN verdict was inconclusive.
        # Common root causes: SessionID mismatch, receive template mismatch,
        # precondition chain break, C++ adapter log parse failure.
        # ALWAYS retry — the next pass with the upgraded KB rules + better
        # fault analysis can usually fix the issue. We do NOT gate on whether
        # fault_analysis.json has 'issues_found' or 'repairs' fields, because
        # the Fault Corrector's output is often mostly thinking_tokens and
        # the saved JSON may be empty/malformed.
        if result["status"] == "inconc":
            print(f"[RETRY] INCONC detected — triggering unconditional self-healing retry "
                  f"(prior run_dir={result.get('run_dir')})...")
            _latest_dir = _find_latest_generated_dir(tc_id)
            _fault_json = _latest_dir / "fault_analysis.json" if _latest_dir else None
            _fault_present = _fault_json.exists() if _fault_json else False
            _has_heal_hints = False
            if _fault_present:
                try:
                    _fault = json.loads(_fault_json.read_text(encoding="utf-8"))
                    _has_repairs = bool(_fault.get("repairs"))
                    _has_issues = bool(_fault.get("issues_found"))
                    _has_heal_hints = _has_repairs or _has_issues
                except (json.JSONDecodeError, OSError) as _e:
                    print(f"[RETRY] Could not parse fault_analysis.json: {_e}", file=sys.stderr)
            print(f"[RETRY] fault_analysis.json present={_fault_present}, "
                  f"has_heal_hints={_has_heal_hints} — proceeding with retry regardless")
            _retry_result = run_one_case(
                rec,
                work_dir=args.output_dir / "tc_json_files",
                extra_args=None,
                timeout=args.per_case_timeout,
                watchdog_timeout=args.watchdog_timeout,
                no_llm_render=args.no_llm_render,
            )
            # Append retry to JSONL too
            with open(results_jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps({**_retry_result, "is_retry": True}, ensure_ascii=False) + "\n")
                f.flush()
            if _retry_result["status"] == "pass":
                result = _retry_result
                print(f"[RETRY] Self-healing succeeded — verdict upgraded to PASS")
            elif _retry_result["status"] == "inconc":
                print(f"[RETRY] Self-healing still INCONC after retry — keeping original INCONC "
                      f"(verdict stable, may need human intervention)")
            else:
                print(f"[RETRY] Self-healing returned {_retry_result['status']} — keeping original INCONC")
        # === end INCONC Self-Healing ===

        # Print verdict (reuse same pattern as existing pass/fail/error branch)
        if result["status"] == "pass":
            pass_count += 1
            print(f"  -> PASS  ({result.get('total_pipeline_seconds', 0):.1f}s)")
        elif result["status"] == "fail":
            fail_count += 1
            print(f"  -> FAIL  ({result.get('total_pipeline_seconds', 0):.1f}s)")
        else:
            error_count += 1
            reported = result.get('total_pipeline_seconds', 0)
            wallclock = result.get('cold_start_seconds', 0)
            if reported > 0:
                print(f"  -> {result['status'].upper()}  ({reported:.1f}s)")
            else:
                print(f"  -> {result['status'].upper()}  (0.0s reported / {wallclock:.1f}s wall-clock)")

    elapsed = time.time() - started
    print(f"\n=== Batch complete ===")
    print(f"Total time:  {elapsed:.1f}s")
    print(f"Pass:        {pass_count}")
    print(f"Fail:        {fail_count}")
    print(f"Error/etc:   {error_count}")
    print(f"Results:     {results_jsonl}")

    # -------------------------------------------------------------------------
    # Summary report: ASCII table + batch_summary.txt (追加,不覆盖历史)
    # -------------------------------------------------------------------------
    _summary_path = AGENT_DIR / "test_code" / "logs" / "batch_summary.txt"
    # "scope=current" filter against historical accumulation.
    # batch_started_at_utc is the run's UTC ISO-8601 timestamp before any
    # pipeline case started; any JSONL row whose started_at_utc is older
    # is treated as a historical record (not part of THIS run's headline).
    generate_summary_report(
        results_jsonl,
        _summary_path,
        scope=args.scope,
        batch_started_at_utc=batch_started_at_utc,
        batch_label=f"batch @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    )

    return 0 if error_count == 0 else 2


def generate_summary_report(
    results_jsonl: Path,
    summary_path: Path,
    scope: str = "all",
    batch_started_at_utc: Optional[str] = None,
    batch_label: Optional[str] = None,
) -> None:
    """
    Read the JSONL output, print an aligned ASCII table to stdout,
    and append the run block to ``summary_path`` (never overwrites).

    Parameters
    ----------
    scope : {'current', 'all'}
        'current' (default since 2026-07-14): show only records whose
        ``started_at_utc`` is >= ``batch_started_at_utc``. This prevents
        the historical accumulation of prior runs from inflating the
        pass/fail tally of the active batch.
        'all': legacy behavior — dump everything in the JSONL.

    batch_started_at_utc : ISO-8601 string
        The UTC timestamp captured at the start of this batch invocation.
        Required when scope='current'. Used to filter JSONL rows.

    batch_label : str
        A short human-readable label for this batch. Appended to the
        header line so multi-batch summary files stay traceable.
    """
    rows: List[Dict[str, Any]] = []
    if not results_jsonl.exists():
        print(f"[SUMMARY] {results_jsonl} not found — skipping report.", file=sys.stderr)
        return

    with open(results_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append({
                "tc_id":   r.get("tc_id", "?"),
                "verdict": r.get("verdict_result") or r.get("status", "?"),
                "log":     r.get("log_path", ""),
                "time_s":  r.get("total_pipeline_seconds", 0.0),
                "started_at_utc": r.get("started_at_utc", ""),
            })

    # === Scope filter (Phase 3.1 fix #1: --scope today/current) ===
    # JSONL is append-only by design, so historical records accumulate.
    # The headline summary must reflect this batch only. Legacy 'all'
    # behavior is preserved for archival digests.
    total_rows_loaded = len(rows)
    filtered_out = 0
    if scope == "current" and batch_started_at_utc:
        kept = []
        for row in rows:
            ts = row.get("started_at_utc", "")
            # Lex compare on ISO-8601 strings is correct (UTC lexicographic).
            if ts and ts >= batch_started_at_utc:
                kept.append(row)
            else:
                filtered_out += 1
        rows = kept

    if not rows:
        print(f"[SUMMARY] No result rows found in JSONL"
              f"{' for current batch' if scope == 'current' else ''}.")
        return

    # Tally
    from collections import Counter
    cnt = Counter(row["verdict"] for row in rows)

    # Column widths
    tc_w = max(len(r["tc_id"]) for r in rows) + 2
    sep  = f"+{'-' * (tc_w + 1)}+{'-' * 11}+{'-' * 12}+"
    hdr  = (f"| {'TC_ID':<{tc_w - 1}}| {'Verdict':<9} | {'Wall(s)':<10} |")
    # Compose a free-text scope annotation. Printed as a single comment line
    # ABOVE the table so the table's column geometry stays untouched.
    scope_tag = f"[scope={scope}]"
    if batch_label:
        scope_tag += f"  ({batch_label})"
    if filtered_out:
        scope_tag += (f"  (filtered out {filtered_out} historical row(s); "
                      f"JSONL contains {total_rows_loaded} total)")
    elif scope == "all":
        scope_tag += "  (showing ALL historical rows)"
    lines_out: List[str] = [sep, hdr, sep]
    for row in rows:
        lines_out.append(
            f"| {row['tc_id']:<{tc_w - 1}}| "
            f"{row['verdict']:<9}| {row['time_s']:<10.1f} |"
        )
    lines_out.append(sep)
    lines_out.append(
        f"Total: {len(rows)} | "
        f"Pass: {cnt.get('pass', 0)} | "
        f"Fail: {cnt.get('fail', 0)} | "
        f"Inconc: {cnt.get('inconc', 0)} | "
        f"Timeout: {cnt.get('timeout', 0)} | "
        f"Error: {cnt.get('pipeline_error', 0)}"
    )

    text = "\n".join(lines_out)
    # Print scope tag first so the table is self-describing
    if scope_tag:
        print(f"\n# {scope_tag}")
    print("\n" + text)

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    ts_hdr = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(summary_path, "a", encoding="utf-8") as f:
        # Scope tag stays attached to the file too, so batch_summary.txt
        # can be diffed across runs without ambiguity.
        header = f"\n\n# ===== Batch run @ {ts_hdr} =====\n# {scope_tag}\n"
        f.write(f"{header}{text}\n")
    print(f"[SUMMARY] Report appended to {summary_path}")


if __name__ == "__main__":
    sys.exit(main())
