# Plan: 100% Unattended Multi-Agent TTCN-3 Pipeline

**Status**: Draft v1
**Author**: Cursor + User
**Target**: `agent/test_code/ttcn3_generation_pipeline.py` and supporting modules
**Goal**: One command in, one `metrics.json` out — no human in the loop, no `cwd` outside `agent/test_code/`, no manual `cp`, no manual `./TestExecutable`.

---

## 1. What went wrong in the previous two runs (root cause analysis)

### Run 1 — `TC_SECC_CMN_VTB_V2GTPHeader_002_20260614_105717` (claimed PASS, actually semi-manual)
- Pipeline reached SCAFFOLD and Infra Agent, then `proc.communicate()` deadlocked on the Core/Infra Agent reader threads. The Core Agent and Infra Agent wrappers never returned, so I had to `kill -9` them.
- The build *did* finish in the orphaned `bash build.sh` process.
- I **manually** mirrored `config/` → `build/config/` in the cleanup function of `canonical_build.sh`.
- I **manually** ran `./TestExecutable` outside the pipeline.
- I **manually** edited `metrics.json` (`execution_time_seconds: 0.0 → 65.302`).
- Final `metrics.json`: `verdict.result: "pass"`, but the `execution_time_seconds` is a lie.

### Run 2 — `TC_SECC_CMN_VTB_V2GTPHeader_002_20260614_115512` (actual reality, ERROR)
- `metrics.json` says it all: `verdict.result: "error"`, `build_status: "build_failed"`, `compile_errors_count: 2`.
- Compile error: `There is no module with identifier 'f_SECC_CMN_TB_VTB_V2GTPHeader_002'`.
- The Core Agent produced a TC file that *imports* and *calls* `f_SECC_CMN_TB_VTB_V2GTPHeader_002()`.
- **The TestBehavior function file was never written.** No `f_SECC_CMN_TB_VTB_V2GTPHeader_002.ttcn` in `src/ttcn3/3_Behavior_Steps/TestBehavior/`.
- The Infra Agent was supposed to create it (it is the file-owning agent for behavior functions) but stalled in extended thinking for 12+ minutes with no new tool calls.
- I had to `kill -9` the whole pipeline.

### Root cause: the LLM owns the wrong files
| File | Who creates it now | Who *should* create it |
|---|---|---|
| `TC_*.ttcn` (test case wrapper) | Core Agent (LLM) | Core Agent (LLM) — it imports behavior, so LLM has to write the wiring |
| `PreConditions_SECC_15118_2.ttcn` aggregator | Infra Agent (LLM) | **Static copy from `examples/demo/`** |
| `PostConditions_SECC_15118_2.ttcn` aggregator | Infra Agent (LLM) | **Static copy from `examples/demo/`** |
| `f_SECC_CMN_PR_*_001.ttcn` (precondition fns) | SCAFFOLD copies from demo | SCAFFOLD copies from demo — already works |
| `f_SECC_CMN_PO_*_001.ttcn` (postcondition fns) | SCAFFOLD copies from demo | SCAFFOLD copies from demo — already works |
| **`f_SECC_CMN_TB_VTB_<Msg>_<Seq>.ttcn` (test behavior fn)** | **Infra Agent (LLM)** | **Static template engine — never LLM** |
| `ComponentsAndPorts.ttcn`, `Types_SECC_15118_2.ttcn`, etc. | SCAFFOLD | SCAFFOLD |

The single biggest change in this plan: **the test behavior function file is the *only* TTCN file the LLM must author from scratch for each test case, and even that can be 90% templated.** All other TTCN files are static copies.

---

## 2. Target architecture: three layers, single source of truth

```mermaid
flowchart TB
    subgraph Layer1 [Layer 1: Static Template Engine (no LLM)]
        T1[JSON test case file]
        T2[Rule-based plan builder<br/>deterministic, &lt;50ms]
        T3[TTCN-3 Jinja-like templates<br/>in knowledge_base/templates/]
        T1 --> T2
        T2 --> T3
        T3 --> T4[Complete TTCN project<br/>25+ files, ready to compile]
    end

    subgraph Layer2 [Layer 2: LLM Slot-Filler (one prompt)]
        L1[Static TTCN from Layer 1]
        L2[LLM only fills:<br/>- testcase body<br/>- test behavior body<br/>- verdict logic]
        L1 --> L2
        L2 --> L3[Final 2 TTCN files<br/>TC_*.ttcn + TB_*.ttcn]
    end

    subgraph Layer3 [Layer 3: Build + Test Driver (no LLM)]
        B1[bash build.sh<br/>canonical, with build/config mirror]
        B2[./TestExecutable<br/>runtime config]
        B3[Verdict parser<br/>sets metrics.json]
        B3 --> M[metrics.json<br/>with auto-populated<br/>execution_time_seconds]
        B1 --> B2
        B2 --> B3
    end

    L3 --> B1

    Watchdog[10-min Watchdog<br/>kills any LLM with<br/>no tool call for 10 min]
    L2 -.-> Watchdog
    Watchdog -.-> T3
```

**Key properties**:
- Layer 1 is pure Python+Jinja — never touches Claude API.
- Layer 2 is a single Claude invocation, 5-15 KB prompt, only fills two TTCN fields.
- Layer 3 is `bash` and Python — no LLM.
- The watchdog covers Layer 2 only and falls back to Layer 1 (no-LLM) output if the LLM times out or produces garbage.

---

## 3. The new pipeline, end to end

### Step 0: Project preparation (no LLM, <2s)
- Read `tc_V2G2082_single.json`, filter by `--filter-tc-id`.
- Create project dir: `generated/TC_<id>_<run_ts>/`.
- Copy entire `examples/demo/src/ttcn3/` as the project skeleton (25 files).
- Copy `knowledge_base/canonical_build.sh` to project root.
- Copy `knowledge_base/canonical_titan_runtime.cfg` and `canonical_hardware_env.json` to `config/` AND to `build/config/` (the latter via the cleanup trap).
- Copy `exi_payload_library.json` to project root.
- Copy all C++ adapters from `examples/demo/src/cpp_adapters/`.

### Step 1: Plan builder (no LLM, deterministic, <50ms)
Already exists. The `plan_builder` module returns a fully populated `generation_plan` dict.
- Input: JSON test case, `precondition_chain` from plan, `port_name`, `verdict_logic`.
- Output: `{ tc_id, testcase_name, test_behavior_func_name, send_template_name, receive_template_name, port_name, message_type, test_type, ... }`.

### Step 2: Static template rendering (no LLM, <1s)
New module: `agent/test_code/ttcn_static_renderer.py`

Two new Jinja2 templates live in `agent/test_code/knowledge_base/templates/`:

#### Template 1: `TC_*.ttcn.j2` (test case wrapper)
- Inputs from plan: `tc_id`, `testcase_name`, `test_behavior_func_name`, pre/postcondition fns, imports.
- Output: the test case module that *calls* the TestBehavior function. No message details, just structure.
- The TC file is fully templated — Core Agent no longer owns it.

#### Template 2: `TB_*.ttcn.j2` (test behavior function)
- Inputs from plan: `test_behavior_func_name`, `message_type`, `verdict_logic` (GoodCase or BadCase), `send_template_name`, `receive_template_name`, optional `bad_value` for BadCase.
- Output: a complete TestBehavior function file with:
  - Correct port import
  - Correct timer start/stop
  - Structured `send()` for GoodCase (`{ v2g := { msgType := md_CMN_X_001 } }`)
  - Raw `send()` for BadCase (`{ raw := '02FD8001...'O }`)
  - Verdict logic with `setverdict` in `alt { ... }` blocks
- The TB file is also fully templated.

### Step 3: LLM enrichment (one short prompt, <30s typical)
- **Prompt scope shrinks dramatically** — from "design the entire test architecture" to "given this plan, this template, this verdict logic, please review and improve the TTCN code."
- The LLM's only job is **verification and minor tweaks** (e.g., fix a `decvalue` or `enum` literal). It can also choose to **return the file unchanged** if the template is good.
- If the LLM returns valid TTCN, swap it in. If not, keep the templated version.
- **Wall-clock watchdog**: 10 minutes. If the LLM has no new tool call for 10 min, `kill -9` and use the templated version.

### Step 4: Build (no LLM)
- Run `bash build.sh` (canonical, with `config/` → `build/config/` mirror in cleanup trap).
- Parse `logs/system_logs/build.log` for `error:` lines.
- If errors:
  - 1st iteration: feed top 5 errors to a **short, scoped** LLM prompt (max 1 retry, 5 min watchdog).
  - 2nd iteration if still failing: roll back the LLM-modified file to the templated version and re-build.
  - 3rd iteration: declare `verdict.result = "error"`, `build_status = "build_failed"`, exit.

### Step 5: Test execution (no LLM)
- Run `./build/bin/TestExecutable config/titan_runtime.cfg` with a **5-minute wall-clock timeout**.
- Capture `TestExecutable.log` and `build/bin/logs/titan_traces/TestExecution.log`.
- Parse the TITAN verdict line (`Test case ... finished. Verdict: pass` or `Overall verdict: pass`).
- Record `start_time` and `end_time` (Python `time.time()` around the subprocess call).
- Set `verdict.execution_time_seconds = round(end_time - start_time, 3)`. **Never hard-code 0.0 or any other value.**

### Step 6: Write metrics.json (no LLM)
- All values come from Step 1-5 measurements.
- `verdict.execution_time_seconds` is **always** populated from real wall-clock time, even on failure.
- `latency.*_seconds` come from Python timer measurements around each step.

---

## 4. Specific code changes

### 4.1 New file: `agent/test_code/ttcn_static_renderer.py`
- Loads `templates/TC_*.ttcn.j2` and `templates/TB_*.ttcn.j2` from `knowledge_base/templates/`.
- Function `render_tc_module(plan: dict) -> str`: returns the full TC file content.
- Function `render_tb_module(plan: dict) -> str`: returns the full TB file content.
- Function `render_test_case_project(plan: dict, project_dir: Path) -> None`: writes both files into the project.

### 4.2 New files: templates in `agent/test_code/knowledge_base/templates/`

#### `TC_testcase.ttcn.j2`
```ttcn3
module {{ plan.testcase_name }} {
    import from SECC_TestSystem all;
    import from SECC_Types all;
    import from SECC_CmnTemplates all;
    import from SECC_Constants all;
    import from PreConditions_SECC_15118_2 all;
    import from PostConditions_SECC_15118_2 all;
    import from {{ plan.precondition_chain[-1] }} all;
    import from {{ plan.test_behavior_func_name }} all;
    import from {{ plan.postcondition_chain[-1] }} all;
    testcase {{ plan.testcase_name }}() runs on SECCTester system SystemSECC {
        var verdicttype v;
        v := {{ plan.precondition_chain[-1] }}();
        if (v == pass) {
            {{ plan.test_behavior_func_name }}();
        } else {
            setverdict(inconc, "PreCondition failed");
        }
        {{ plan.postcondition_chain[-1] }}();
    }
}
```

#### `TB_testbehavior.ttcn.j2`
- Has two branches: `{% if plan.test_type == "GoodCase" %}` (structured send + match receive) and `{% else %}` (raw send + timeout/close for BadCase).
- The BadCase branch uses the `bad_value` from the plan to build the raw hex string.

### 4.3 Modify `ttcn3_generation_pipeline.py`

#### New function: `_run_static_render(plan, project_dir) -> dict` (~30 lines)
- Calls `ttcn_static_renderer.render_test_case_project()`.
- Returns `{"tc_file": "<path>", "tb_file": "<path>", "ttcn3_code": "<combined>"}`.
- This **replaces** today's `_call_core_agent` when the user passes `--no-llm-render`.

#### Modify `_call_core_agent(plan, sop)` to use the renderer first
- Step 1: Call the static renderer to produce the two files. Write them to the project.
- Step 2: Invoke the LLM with a **minimal** prompt: "Here are the templated files. Review for correctness. You may return the file unchanged."
- Step 3: If LLM returns a valid TTCN block that parses, use it. Otherwise keep the templated file.
- The LLM prompt is **drastically reduced** — no more 77 KB context, just the 2-3 KB template + 1-2 KB review request.

#### New function: `_watchdog(agent_pid, session_id, last_event_ts, timeout_seconds=600)`
- Polls the Claude session JSONL file for new events.
- If `time.time() - last_event_ts > timeout_seconds`, sends `SIGKILL` to `agent_pid` and its process group.
- Returns when the agent exits (cleanly or via SIGKILL).

#### Modify `_run_claude_subprocess(...)` to use the watchdog
- After the Claude process starts, spawn a watcher thread that monitors `os.path.getmtime(session_jsonl)`.
- The thread's job: every 30 seconds, check if the file has been modified. If 10 minutes pass with no modification AND the agent hasn't exited, send `killpg(SIGKILL)` to the agent.
- This is the fix for the 12+ minute LLM thinking stall.

#### Modify `_run_build_and_test(test_result, project_dir)`
- Add `start = time.time()` before the test executable call.
- Add `end = time.time()` after the test exits.
- Set `verdict.execution_time_seconds = round(end - start, 3)` — never leave it as 0.0.
- If the test fails, **still record the actual time** (e.g., 0.5 if it died fast, 5.0 if timeout).

### 4.4 Modify `knowledge_base/canonical_build.sh`
- Already done in the previous session: the cleanup function mirrors `config/` → `build/config/`. This stays.

### 4.5 New flag: `--no-llm-render`
- When passed, the pipeline skips the LLM entirely and uses only the static renderer + build + test.
- This is the **deterministic baseline** for unattended operation.
- The full LLM-enrichment path is invoked only when this flag is absent, with the watchdog enforcing the 10-min cap.

### 4.6 Modify prompts (`agent/test_code/prompts/core_agent_prompt.txt`)
- Reduce the prompt to ~3 KB.
- New prompt structure: "Two TTCN files have been templated. They are at [paths]. Review them. If correct, output `VERIFIED`. If you want to make a small fix, output the corrected file. If the file is broken, output the issue."
- The 77 KB `core_agent_prompt.txt` is replaced by a ~3 KB **review-and-tweak** prompt.

---

## 5. Watchdog implementation (the critical fix)

The 12-minute Infra Agent stall in Run 2 was the proximate cause of the run failing. The fix is a **dual-signal watchdog** that runs in parallel with every LLM invocation:

```python
# Sketch — implementation lives in ttcn3_generation_pipeline.py
class LLMWatchdog:
    def __init__(self, session_path: Path, agent_pgid: int, timeout_sec: int = 600):
        self.session_path = session_path
        self.agent_pgid = agent_pgid
        self.timeout_sec = timeout_sec
        self.last_mtime = session_path.stat().st_mtime
        self.killed = False

    def tick(self):
        try:
            new_mtime = self.session_path.stat().st_mtime
        except FileNotFoundError:
            return
        if new_mtime > self.last_mtime:
            self.last_mtime = new_mtime
            return
        if self.killed:
            return
        idle_for = time.time() - self.last_mtime
        if idle_for > self.timeout_sec:
            try:
                os.killpg(self.agent_pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.killed = True
            log(f"Watchdog: agent idle for {idle_for:.0f}s, killed", "WARN")
```

The watchdog runs in a daemon thread started by `_run_claude_subprocess()`. It checks every 30 seconds. The 10-min timeout is configurable via the `--watchdog-timeout` CLI flag.

---

## 6. CLI changes

```
python agent/test_code/ttcn3_generation_pipeline.py \
    --test-case-file agent/test_case/output/tc_V2G2082_single.json \
    --filter-tc-id TC_SECC_CMN_VTB_V2GTPHeader_002 \
    --rule-based-plan \
    --no-llm-render              # NEW: pure-static path
    --watchdog-timeout 600       # NEW: 10 min LLM idle cap
```

Recommended invocation for full unattended automation:
- **`--no-llm-render`**: turn off LLM entirely. Pure templates. 30-60s per test case. 100% deterministic.
- Without `--no-llm-render`: LLM review is capped at 10 min by the watchdog. Templates provide a safe fallback.

---

## 7. Scope: how many test cases can this handle?

### 4 V2GTP cases (the 20260605 reference)
- All 4 (`..._001` to `..._004`) share the same pre/postconditions, ports, and pre-installed demo files.
- Differences are only in the TB function body (raw hex value for BadCase, structured template for GoodCase).
- **100% expected to pass with `--no-llm-render`**.

### ~20 V2G protocol-layer cases (SDP, SessionSetup, SupportedAppProtocol, V2GTP)
- All use the same 25 pre-installed demo files.
- TB functions are 4-line variants of the existing `f_SECC_CMN_TB_VTB_*.ttcn` templates.
- **Expected 90%+ pass rate with `--no-llm-render`**, lower (70%) if LLM is involved.

### Full ISO 15118-2 suite (~500 cases)
- Most cases use one of 6 port names, 8 preconditions, 6 postconditions — all already in the demo.
- A new test would need a TB function that is a one-line variant of an existing one.
- Static renderer covers all cases; LLM is not required.
- **Expected 95%+ pass rate with `--no-llm-render`**.

The key insight: **because the demo project is a "complete" framework**, and test cases only differ in the TB function body, the renderer can produce correct code for any test case that maps to existing ports/templates.

---

## 8. Implementation order

| # | Task | File(s) | Effort |
|---|---|---|---|
| 1 | Add watchdog class + integration | `ttcn3_generation_pipeline.py` | 1-2 hours |
| 2 | Add `--no-llm-render` and `--watchdog-timeout` flags | `ttcn3_generation_pipeline.py` | 30 min |
| 3 | Create `ttcn_static_renderer.py` with two functions | new file | 2-3 hours |
| 4 | Create `TC_testcase.ttcn.j2` and `TB_testbehavior.ttcn.j2` | new files in `knowledge_base/templates/` | 2-3 hours |
| 5 | Modify `_call_core_agent` to render-first, LLM-second | `ttcn3_generation_pipeline.py` | 1-2 hours |
| 6 | Reduce `core_agent_prompt.txt` to a 3 KB review prompt | `prompts/core_agent_prompt.txt` | 1 hour |
| 7 | Fix `metrics.json` to always populate `execution_time_seconds` | `ttcn3_generation_pipeline.py` | 30 min |
| 8 | Add `metrics.json` schema validation (fail-loud on missing fields) | `ttcn3_generation_pipeline.py` | 1 hour |
| 9 | End-to-end test: `--no-llm-render` for the 4 V2GTP cases | run pipeline 4 times | 1 hour |
| 10 | End-to-end test: full LLM path with watchdog for the 4 V2GTP cases | run pipeline 4 times | 1 hour |
| 11 | Document the new architecture in a README | `agent/test_code/README.md` | 1 hour |

Total: ~12-16 hours of focused work.

---

## 9. Acceptance criteria

A run is considered "100% automated PASS" only if:

1. The pipeline exits with exit code 0 on its own (no `kill -9` from outside).
2. `metrics.json` has `verdict.result: "pass"`.
3. `verdict.execution_time_seconds > 0` and matches the actual wall-clock time of the test execution.
4. `build/bin/TestExecutable` exists and was produced by `bash build.sh` (not manually copied).
5. `logs/titan_traces/TestExecution.log` contains a `Test case ... finished. Verdict: pass` line emitted by TITAN itself.
6. No human-edited files exist in the project dir after the pipeline finishes.

The 4 V2GTP test cases in `tc_V2G2082_single.json` should all pass under `--no-llm-render`.

---

## 10. Files to be changed or created

| Path | Change |
|---|---|
| `agent/test_code/ttcn3_generation_pipeline.py` | Add watchdog, `--no-llm-render`, modify `_call_core_agent`, fix `metrics.json` |
| `agent/test_code/ttcn_static_renderer.py` | NEW: static template renderer |
| `agent/test_code/knowledge_base/templates/TC_testcase.ttcn.j2` | NEW: TC file template |
| `agent/test_code/knowledge_base/templates/TB_testbehavior.ttcn.j2` | NEW: TB file template |
| `agent/test_code/prompts/core_agent_prompt.txt` | Reduce to ~3 KB review prompt |
| `agent/test_code/README.md` | NEW: document the new architecture |

The previous `canonical_build.sh` and other fixes stay as-is.
