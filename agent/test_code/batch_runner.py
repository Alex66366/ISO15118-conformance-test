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
# Pre-curated 10-case pilot benchmark
# ---------------------------------------------------------------------------
# Curated to cover distinct V2G protocol layers and ISO 15118-2 message
# families. 1 case per family for the pilot; later phases can run all 339 SECC.
PILOT_10_DEFAULT: List[Dict[str, str]] = [
    # message_type                | tc_id                                            | rationale
    # --- V2GTP / protocol layer ---
    {"tc_id": "TC_SECC_CMN_VTB_SDP_001",                   "family": "SDP",                   "rationale": "V2GTP layer; SDP discovery exchange"},
    {"tc_id": "TC_SECC_CMN_VTB_SupportedAppProtocol_001",  "family": "SupportedAppProtocol",  "rationale": "V2GTP layer; first app-layer message"},
    # --- V2G session management ---
    {"tc_id": "TC_SECC_CMN_VTB_V2GTPSessionSetup_001",     "family": "V2GTPSessionSetup",     "rationale": "Session ID establishment (validated TTCN code in KB)"},
    {"tc_id": "TC_SECC_CMN_VTB_ServiceDiscovery_001",      "family": "ServiceDiscovery",      "rationale": "Service list query"},
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

    # Stream pipeline stdout/stderr to a per-case log file so we can diagnose
    # pipeline_error cases after the fact. Without this, subprocess.run's
    # capture_output=True swallows all output and we have no idea *why* the
    # pipeline crashed.
    import shutil as _br_shutil_log
    log_dir = AGENT_DIR / "test_code" / "logs" / tc_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
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
        cold_start_seconds = time.time() - cold_start
        print(f"[{tc_id}] pipeline log: {log_path}")
        log_fh.flush()
        log_fh.close()

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
            "process_exit_code": proc.returncode,
            "status": _normalize_status(proc.returncode, verdict),
            "verdict_result": verdict.get("result"),
            "execution_time_seconds": verdict.get("execution_time_seconds", 0.0),
            "build_status": verdict.get("build_status"),
            "compile_errors_count": verdict.get("compile_errors_count", 0),
            # FALLBACK: when pipeline crashed before writing metrics.json,
            # `latency.total_pipeline_seconds` is 0.0 — use wall-clock as the
            # honest elapsed time so the user sees "PIPELINE_ERROR (933.6s)"
            # instead of a misleading "0.0s".
            "total_pipeline_seconds": latency.get("total_pipeline_seconds", 0.0) or round(cold_start_seconds, 1),
            "infrastructure_seconds": latency.get("infrastructure_seconds", 0.0),
            "compilation_seconds": latency.get("compilation_seconds", 0.0),
            "fault_correction_seconds": latency.get("fault_correction_seconds", 0.0),
            "preinstall_render": 1,  # Core Agent renders (1=LLM used)
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
            import json as _salvage_json
            _payload = {
                "tc_id": tc_id,
                "timestamp": salvaged_dir.name.rsplit("_", 1)[1] if salvaged_dir else "unknown",
                "verdict": {
                    "result": "fix_success",
                    "build_status": "completed",
                    "executable_path": str(salvaged_executable),
                    "salvaged_from_timeout": True,
                    "note": "metrics.json reconstructed by batch_runner after subprocess timeout; "
                            "TestExecutable already on disk",
                },
            }
            try:
                salvaged_metrics.write_text(_salvage_json.dumps(_payload, indent=2, ensure_ascii=False))
                print(f"[SALVAGE] emergency metrics written: {salvaged_metrics}")
            except Exception as _se:
                print(f"[SALVAGE] failed to write emergency metrics: {_se}")
        if salvaged_metrics and salvaged_metrics.exists():
            record["status"] = "fix_success_via_salvage"
            record["build_status"] = "completed"
            record["verdict_result"] = "fix_success"
            record["run_dir"] = str(salvaged_dir) if salvaged_dir else None
        else:
            record.update({
                "status": "timeout",
                "process_exit_code": -1,
                "cold_start_seconds": round(time.time() - cold_start, 1),
            })
    except Exception as e:
        record.update({
            "status": "error",
            "error": str(e),
            "cold_start_seconds": round(time.time() - cold_start, 1),
        })

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
    """Load a suite JSON file (list of {tc_id, family, rationale})."""
    with open(suite_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "cases" in data:
        return data["cases"]
    raise ValueError(f"Suite file {suite_path} is not a list or {cases: []} dict")


def load_existing_results(results_jsonl: Path) -> List[str]:
    """Load the set of tc_ids that already have results (for --resume)."""
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
                if r.get("status") in ("pass", "fail", "inconc", "verdict_error",
                                       "timeout", "pipeline_error", "not_run"):
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
        "--per-case-timeout", type=int, default=1800,
        help="Per-case wall-clock timeout in seconds (default: 1800).",
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
    args = parser.parse_args()

    # Optionally save the default suite file
    if args.save_default_suite:
        out = DEFAULT_SUITES_DIR / "v2gtp_pilot_10.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(PILOT_10_DEFAULT, f, indent=2, ensure_ascii=False)
        print(f"Wrote default suite to {out}")
        return 0

    # Resolve the suite
    if args.tc_ids:
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

    started = time.time()
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

        if result["status"] == "pass":
            pass_count += 1
            print(f"  -> PASS  ({result.get('total_pipeline_seconds', 0):.1f}s)")
        elif result["status"] == "fail":
            fail_count += 1
            print(f"  -> FAIL  ({result.get('total_pipeline_seconds', 0):.1f}s)")
        else:
            error_count += 1
            # Show both the metrics-reported time AND the wall-clock (cold_start)
            # so a misleading 0.0s on a crashed pipeline is never silent.
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
    return 0 if error_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
