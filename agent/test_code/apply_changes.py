#!/usr/bin/env python3
"""
Apply all changes to ttcn3_generation_pipeline.py
"""
import re
import sys
import shutil
from pathlib import Path

FILE_PATH = Path("/home/dmc/桌面/platform/agent/test_code/ttcn3_generation_pipeline.py")
BACKUP_PATH = FILE_PATH.with_suffix('.py.bak')

def backup():
    shutil.copy2(FILE_PATH, BACKUP_PATH)
    print(f"Backup created: {BACKUP_PATH}")

def restore():
    shutil.copy2(BACKUP_PATH, FILE_PATH)
    print(f"Restored from backup")

def apply_changes():
    backup()
    content = Path(FILE_PATH).read_text(encoding="utf-8")
    lines = content.split('\n')
    
    changes_made = []
    
    # ===== CHANGE 1: Add Phase 3a/3b infrastructure after imports =====
    # Find the line with typing imports
    change1_marker = "from typing import Any, Dict, List, Optional, Union"
    idx1 = None
    for i, line in enumerate(lines):
        if change1_marker in line:
            idx1 = i
            break
    
    if idx1 is not None:
        new_block1 = '''

# === Token tracking & metrics for metrics.json ===
import collections

AgentMetrics = collections.namedtuple(
    "AgentMetrics", ["elapsed_seconds", "input_tokens", "output_tokens", "calls"]
)

# Per-agent cumulative token counters (populated by call_claude_agent)
_agent_input_tokens: Dict[str, int] = {}
_agent_output_tokens: Dict[str, int] = {}
_agent_call_counts: Dict[str, int] = {}


def _parse_token_usage_from_claude_output(stderr: str) -> Tuple[int, int]:
    """Extract input/output token counts from claude -p stderr."""
    import re
    input_match = re.search(r'Input:\\s*(\\d+)\\s*tokens?', stderr)
    output_match = re.search(r'Output:\\s*(\\d+)\\s*tokens?', stderr)
    return (
        int(input_match.group(1)) if input_match else 0,
        int(output_match.group(1)) if output_match else 0
    )


def _write_metrics_json(tc_project_dir, metrics):
    """Write metrics.json to the TC project directory."""
    if tc_project_dir is None:
        return
    metrics_path = tc_project_dir / "metrics.json"
    try:
        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _build_metrics_dict(
    tc_id, run_timestamp, test_case, generation_plan,
    orchestrator_elapsed, summarizer_elapsed,
    core_agent_elapsed, infra_agent_elapsed, build_elapsed,
    compile_loop_core_total, compile_loop_infra_total,
    compile_loop_build_total, compile_loop_fc_total,
    fc_runtime_elapsed, compile_loop_iterations,
    build_result,
):
    """Build the metrics.json dictionary."""
    import subprocess
    git_commit = "unknown"
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent), timeout=5,
        )
        git_commit = r.stdout.strip() or "unknown"
    except Exception:
        pass

    tc = test_case.get("Test_Case", test_case)
    test_case_name = tc.get("Test_Case_Name", tc.get("Test_Case_Description", tc_id))

    planning_in = _agent_input_tokens.get("orchestrator", 0) + _agent_input_tokens.get("summarizer", 0)
    planning_out = _agent_output_tokens.get("orchestrator", 0) + _agent_output_tokens.get("summarizer", 0)
    core_gen_in = _agent_input_tokens.get("core_agent", 0) + _agent_input_tokens.get("infra_agent", 0)
    core_gen_out = _agent_output_tokens.get("core_agent", 0) + _agent_output_tokens.get("infra_agent", 0)
    fc_in = _agent_input_tokens.get("fault_corrector", 0)
    fc_out = _agent_output_tokens.get("fault_corrector", 0)

    verdict = (build_result.get("verdict") if build_result else "not_run") or "not_run"
    build_status = (build_result.get("status") if build_result else "not_run") or "not_run"
    exec_time = build_result.get("execution_time", 0.0) if build_result else 0.0

    total_input = sum(_agent_input_tokens.values())
    total_output = sum(_agent_output_tokens.values())
    planning_sec = orchestrator_elapsed + summarizer_elapsed
    generation_sec = core_agent_elapsed + infra_agent_elapsed
    compile_loop_sec = compile_loop_core_total + compile_loop_infra_total + compile_loop_build_total + compile_loop_fc_total
    fc_sec = fc_runtime_elapsed
    total_pipeline = planning_sec + generation_sec + compile_loop_sec + build_elapsed + fc_sec
    total_self_fixes = compile_loop_iterations + (1 if fc_runtime_elapsed > 0 else 0)

    return {
        "experiment_metadata": {
            "tc_id": tc_id,
            "run_timestamp": run_timestamp,
            "pipeline_version": "sop_v2.0_pure_static",
            "git_commit": git_commit,
            "test_case_name": test_case_name,
        },
        "token_usage": {
            "planning": {
                "input_tokens": planning_in,
                "output_tokens": planning_out,
                "calls": _agent_call_counts.get("orchestrator", 0) + _agent_call_counts.get("summarizer", 0),
            },
            "core_generation": {
                "input_tokens": core_gen_in,
                "output_tokens": core_gen_out,
                "calls": _agent_call_counts.get("core_agent", 0) + _agent_call_counts.get("infra_agent", 0),
            },
            "compilation_loop": {
                "input_tokens": _agent_input_tokens.get("core_agent", 0),
                "output_tokens": _agent_output_tokens.get("core_agent", 0),
                "iterations": compile_loop_iterations,
            },
            "fault_correction": {
                "compile_phase": {
                    "input_tokens": fc_in if compile_loop_fc_total > 0 else 0,
                    "output_tokens": fc_out if compile_loop_fc_total > 0 else 0,
                },
                "runtime_phase": {
                    "input_tokens": fc_in if fc_runtime_elapsed > 0 else 0,
                    "output_tokens": fc_out if fc_runtime_elapsed > 0 else 0,
                },
            },
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
        },
        "latency": {
            "planning_seconds": round(planning_sec, 3),
            "generation_seconds": round(generation_sec, 3),
            "compilation_seconds": round(build_elapsed, 3),
            "compilation_loop_seconds": round(compile_loop_sec, 3),
            "fault_correction_seconds": round(fc_sec, 3),
            "total_pipeline_seconds": round(total_pipeline, 3),
        },
        "self_healing": {
            "compile_loop_iterations": compile_loop_iterations,
            "compile_loop_max": 3,
            "runtime_loop_iterations": 1 if fc_runtime_elapsed > 0 else 0,
            "runtime_loop_max": 2,
            "total_self_fixes": total_self_fixes,
        },
        "verdict": {
            "result": verdict,
            "execution_time_seconds": round(exec_time, 3),
            "build_status": build_status,
            "compile_errors_count": build_result.get("compile_errors_count", 0) if build_result else 0,
        },
    }

'''
        lines.insert(idx1 + 1, new_block1)
        changes_made.append(f"CHANGE 1: Added token tracking infrastructure after line {idx1 + 1}")
    else:
        print("WARNING: Could not find CHANGE 1 marker")
    
    # Re-join and re-split
    content = '\n'.join(lines)
    lines = content.split('\n')
    
    # ===== CHANGE 2: Wire token tracking in call_claude_agent =====
    # Try multiple patterns
    change2_patterns = [
        'log(f"{agent_name} Agent 完成',
        'log(f"{agent_name} Agent 返回错误码',
        'return text.strip()',
    ]
    
    idx2 = None
    for pattern in change2_patterns:
        for i, line in enumerate(lines):
            if pattern in line and 'call_claude_agent' in ''.join(lines[max(0,i-20):i]):
                idx2 = i
                break
        if idx2:
            break
    
    if idx2:
        # Find the next return statement after the log
        for i in range(idx2 + 1, min(idx2 + 5, len(lines))):
            if 'return text.strip()' in lines[i]:
                new_block2 = '''
        # Track token usage from Claude CLI stderr
        inp, out = _parse_token_usage_from_claude_output(result.stderr)
        _agent_input_tokens[agent_name] = _agent_input_tokens.get(agent_name, 0) + inp
        _agent_output_tokens[agent_name] = _agent_output_tokens.get(agent_name, 0) + out
        _agent_call_counts[agent_name] = _agent_call_counts.get(agent_name, 0) + 1
'''
                lines.insert(i, new_block2)
                changes_made.append(f"CHANGE 2: Added token tracking in call_claude_agent")
                break
    else:
        print("WARNING: Could not find CHANGE 2 marker")
    
    # Re-join and re-split
    content = '\n'.join(lines)
    lines = content.split('\n')
    
    # ===== CHANGE 3: Add run_timestamp at start of generate_code_for_test_case =====
    change3_marker = 'def generate_code_for_test_case('
    idx3 = None
    for i, line in enumerate(lines):
        if change3_marker in line:
            idx3 = i
            break
    
    if idx3:
        # Find the end of the docstring
        docstring_end = None
        for i in range(idx3, min(idx3 + 20, len(lines))):
            if lines[i].strip() == '"""':
                docstring_end = i + 1
                break
        
        if docstring_end:
            new_block3 = '''
    # Run timestamp for per-TC folder isolation (needed at all exit paths for metrics.json)
    import datetime as _run_ts
    run_timestamp = _run_ts.datetime.now().strftime("%Y%m%d_%H%M%S")

'''
            lines.insert(docstring_end, new_block3)
            changes_made.append(f"CHANGE 3: Added run_timestamp after docstring")
        else:
            print("WARNING: Could not find docstring end for CHANGE 3")
    else:
        print("WARNING: Could not find CHANGE 3 marker")
    
    # Re-join and re-split
    content = '\n'.join(lines)
    lines = content.split('\n')
    
    # ===== CHANGE 4: Update plan_path and tc_project_dir creation =====
    change4_marker = 'plan_path = GENERATED_DIR / f"{plan_tc_id}_plan.json"'
    idx4 = None
    for i, line in enumerate(lines):
        if change4_marker in line:
            idx4 = i
            break
    
    if idx4:
        new_block4 = '''    # Save generation plan into the timestamped project directory
    tc_project_dir = GENERATED_DIR / f"{plan_tc_id}_{run_timestamp}"
    tc_project_dir.mkdir(parents=True, exist_ok=True)
    plan_path = tc_project_dir / f"{plan_tc_id}_plan.json"'''
        lines[idx4] = new_block4
        changes_made.append(f"CHANGE 4: Updated plan_path and tc_project_dir creation")
    else:
        print("WARNING: Could not find CHANGE 4 marker")
    
    # Re-join and re-split
    content = '\n'.join(lines)
    lines = content.split('\n')
    
    # ===== CHANGE 5: Remove duplicate tc_project_dir definition =====
    change5_marker = "# === Step 5: 写入 Core Agent 生成的 pre/post condition 文件 ==="
    idx5 = None
    for i, line in enumerate(lines):
        if change5_marker in line:
            idx5 = i
            break
    
    if idx5:
        # Find the tc_project_dir definition in the next few lines
        for i in range(idx5 + 1, min(idx5 + 5, len(lines))):
            if 'tc_project_dir = GENERATED_DIR / plan_tc_id' in lines[i]:
                # Replace lines[idx5+1:i+1] with the comment
                new_block5 = '''    # tc_project_dir was already created with the run_timestamp above.
    # Just ensure it exists (mkdir is idempotent).
'''
                lines = lines[:idx5 + 1] + [new_block5] + lines[i + 1:]
                changes_made.append(f"CHANGE 5: Removed duplicate tc_project_dir definition")
                break
    else:
        print("WARNING: Could not find CHANGE 5 marker - checking for duplicate...")
        # Try alternative: find the duplicate definition directly
        for i, line in enumerate(lines):
            if 'tc_project_dir = GENERATED_DIR / plan_tc_id' in line and '_run_ts' in ''.join(lines[max(0,i-50):i]):
                # This is after the first definition, it's the duplicate
                lines[i] = '    # tc_project_dir was already created with the run_timestamp above.\n    # Just ensure it exists (mkdir is idempotent).'
                changes_made.append(f"CHANGE 5: Replaced duplicate tc_project_dir at line {i+1}")
                break
    
    # Re-join and re-split
    content = '\n'.join(lines)
    lines = content.split('\n')
    
    # ===== CHANGE 6: Add _bundle_exi_payload_library functions before run_orchestrator =====
    change6_marker = 'def run_orchestrator('
    idx6 = None
    for i, line in enumerate(lines):
        if change6_marker in line:
            idx6 = i
            break
    
    if idx6:
        new_block6 = '''
def _bundle_exi_payload_library(tc_dir, generation_plan):
    """Copy exi_payload_library.json into the generated TC project directory."""
    DEMO_PAYLOAD_LIB = DEMO_ROOT / "exi_payload_library.json"
    dest = tc_dir / "exi_payload_library.json"
    if DEMO_PAYLOAD_LIB.exists():
        shutil.copy2(DEMO_PAYLOAD_LIB, dest)
    else:
        _generate_minimal_payload_library(tc_dir, generation_plan)


def _generate_minimal_payload_library(tc_dir, generation_plan):
    """Write a minimal exi_payload_library.json containing only the referenced message types."""
    msg_type = generation_plan.get("normalized_message_type",
                                   generation_plan.get("message_type", ""))
    lib = {
        "source": "generated_minimal",
        "iso15118_root": None,
        "jar_path": None,
        "message_types": [
            {
                "type": msg_type,
                "description": f"Auto-generated stub for {msg_type}",
                "request_bytes": [],
                "response_bytes": [],
            }
        ],
    }
    dest = tc_dir / "exi_payload_library.json"
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(lib, f, indent=2, ensure_ascii=False)

'''
        lines.insert(idx6, new_block6)
        changes_made.append(f"CHANGE 6: Added _bundle_exi_payload_library functions")
    else:
        print("WARNING: Could not find CHANGE 6 marker")
    
    # Re-join and re-split
    content = '\n'.join(lines)
    lines = content.split('\n')
    
    # ===== CHANGE 7: Call _bundle_exi_payload_library =====
    change7_marker = '[SCAFFOLD] C++ adapters deep-copied'
    idx7 = None
    for i, line in enumerate(lines):
        if change7_marker in line:
            idx7 = i
            break
    
    if idx7:
        new_block7 = '''
    # 5b. Bundle exi_payload_library.json for self-contained execution.
    _bundle_exi_payload_library(tc_project_dir, generation_plan)
'''
        lines.insert(idx7 + 1, new_block7)
        changes_made.append(f"CHANGE 7: Added _bundle_exi_payload_library call")
    else:
        print("WARNING: Could not find CHANGE 7 marker")
    
    # Re-join and re-split
    content = '\n'.join(lines)
    lines = content.split('\n')
    
    # ===== CHANGE 8: Add absolute path check in build.sh =====
    change8_marker = 'if [ "$TTCN_CC_COUNT" -eq 0 ]; then'
    idx8 = None
    for i, line in enumerate(lines):
        if change8_marker in line:
            idx8 = i
            break
    
    if idx8:
        # Find the end marker
        change8_end_marker = "Step 5: Fix SECC_TestSystem.hh"
        idx8_end = None
        for i in range(idx8, min(idx8 + 15, len(lines))):
            if change8_end_marker in lines[i]:
                idx8_end = i
                break
        
        if idx8_end:
            new_block8 = '''if [ "$TTCN_CC_COUNT" -eq 0 ]; then
    echo "[WARN] No TTCN .cc files generated. Possible missing source files."
fi

# ==========================================
# Step 4.3: Verify no absolute-path leaks remain in Makefile
# ==========================================
echo "[BUILD] Step 4.3: Verifying relative paths in Makefile..."
if [ -f "Makefile" ]; then
    ABS_LEAKS=$(grep -E "^\\s*(CSRCS|GEN_|USER_|TTCN3_SRC)\\s* [:+]?=" Makefile | grep -E "/[^/]*\\.(cc|cpp|c|hh|hpp|h)" | grep "^[^$]* /" || true)
    if [ -n "$ABS_LEAKS" ]; then
        echo "[BUILD] ERROR: Absolute path leaks detected in Makefile:"
        echo "$ABS_LEAKS"
        echo "[BUILD] Build aborted. All source paths must be relative (../../...)."
        exit 1
    fi
    echo "[BUILD]   All Makefile source paths are relative. OK."
fi

# ==========================================
# Step 5: Fix SECC_TestSystem.hh to include port headers'''
            lines = lines[:idx8] + [new_block8] + lines[idx8_end:]
            changes_made.append(f"CHANGE 8: Added absolute path check in build.sh")
        else:
            print("WARNING: Could not find end marker for CHANGE 8")
    else:
        print("WARNING: Could not find CHANGE 8 marker")
    
    # Re-join and re-split
    content = '\n'.join(lines)
    lines = content.split('\n')
    
    # ===== CHANGE 9: Wire metrics at exit paths =====
    # Find generate_code_for_test_case
    change9_func_marker = 'def generate_code_for_test_case('
    idx9_func = None
    for i, line in enumerate(lines):
        if change9_func_marker in line:
            idx9_func = i
            break
    
    if idx9_func:
        # Find _print_timing_summary calls
        timing_calls = []
        for i in range(idx9_func, len(lines)):
            if lines[i].strip().startswith('def ') and i > idx9_func:
                break
            if '_print_timing_summary' in lines[i]:
                timing_calls.append(i)
        
        # Find return statements in the function
        return_nones = []
        return_results = []
        for i in range(idx9_func, len(lines)):
            if lines[i].strip().startswith('def ') and i > idx9_func:
                break
            if 'return None' in lines[i]:
                return_nones.append(i)
            if 'return result' in lines[i] or 'return str(output_path)' in lines[i]:
                return_results.append(i)
        
        # CHANGE 9a: Add metrics before return None (after timing summary)
        if timing_calls and return_nones:
            # Find the first timing summary followed by return None
            for tc_idx in timing_calls:
                for rn_idx in return_nones:
                    if 0 < rn_idx - tc_idx < 20:  # Within 20 lines
                        # Build the replacement
                        new_block9a = '''        _write_metrics_json(
            tc_project_dir,
            _build_metrics_dict(
                tc_id, run_timestamp, test_case, generation_plan,
                orchestrator_elapsed, summarizer_elapsed,
                core_agent_elapsed, infra_agent_elapsed, build_elapsed,
                compile_loop_core_total, compile_loop_infra_total,
                compile_loop_build_total, compile_loop_fc_total,
                fc_runtime_elapsed, 0, build_result,
            ),
        )
        return None'''
                        # Find the actual return None line
                        for i in range(tc_idx, min(tc_idx + 20, len(lines))):
                            if 'return None' in lines[i]:
                                lines = lines[:i] + [new_block9a] + lines[i + 1:]
                                changes_made.append(f"CHANGE 9a: Added metrics before return None")
                                break
                        break
                else:
                    continue
                break
        
        # Re-join and re-split
        content = '\n'.join(lines)
        lines = content.split('\n')
        
        # Find generate_code_for_test_case again after changes
        idx9_func = None
        for i, line in enumerate(lines):
            if change9_func_marker in line:
                idx9_func = i
                break
        
        # CHANGE 9b: Add metrics before final return result
        if idx9_func:
            # Find the last return statement in the function (final return)
            for i in range(len(lines) - 1, idx9_func, -1):
                if 'return str(output_path)' in lines[i]:
                    # Find timing summary before this return
                    for j in range(i - 1, max(idx9_func, i - 20), -1):
                        if '_print_timing_summary' in lines[j]:
                            new_block9b = '''    cl_iterations = max(
        len(compile_loop_core_times), len(compile_loop_infra_times),
        len(compile_loop_build_times), len(compile_loop_fc_times)
    )
    _write_metrics_json(
        tc_project_dir,
        _build_metrics_dict(
            tc_id, run_timestamp, test_case, generation_plan,
            orchestrator_elapsed, summarizer_elapsed,
            core_agent_elapsed, infra_agent_elapsed, build_elapsed,
            compile_loop_core_total, compile_loop_infra_total,
            compile_loop_build_total, compile_loop_fc_total,
            fc_runtime_elapsed, cl_iterations, build_result,
        ),
    )
    return result'''
                            # Replace return with metrics + return
                            lines = lines[:j + 1] + [new_block9b] + lines[i + 1:]
                            changes_made.append(f"CHANGE 9b: Added metrics before final return")
                            break
                    break
    else:
        print("WARNING: Could not find generate_code_for_test_case for CHANGE 9")
    
    # Write the final content
    final_content = '\n'.join(lines)
    Path(FILE_PATH).write_text(final_content, encoding="utf-8")
    
    print("\n" + "="*60)
    print("CHANGES SUMMARY:")
    for c in changes_made:
        print(f"  - {c}")
    print("="*60)
    
    return True


if __name__ == "__main__":
    try:
        success = apply_changes()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"ERROR: {e}")
        restore()
        sys.exit(1)
