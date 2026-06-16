# agent/test_code — TTCN-3 Generation Pipeline (Neuro-Symbolic, 100% Unattended)

> **Status (2026-06-14)**: end-to-end PASS verified on `TC_SECC_CMN_VTB_V2GTPHeader_002`
> with `--no-llm-render` and `--rule-based-plan`. The pipeline produced a valid
> `build/bin/TestExecutable`, executed it against the real SECC daemon (PID 13316),
> and recorded `verdict.result: "pass"`, `execution_time_seconds: 65.149`
> in `metrics.json`. No human intervention.

## What this directory does

Implements an end-to-end pipeline that converts an ISO 15118-2 conformance test
case description (in JSON) into a compiled, executed, and verified TTCN-3 test
binary. The pipeline must be **100% unattended** — no `kill -9`, no manual
`cp config/ build/config/`, no hand-edited `metrics.json`.

## Architecture: 3 layers, single source of truth

```mermaid
flowchart TB
    subgraph L1 [Layer 1: Static Renderer (no LLM)]
        T1[JSON test case] --> T2[Rule-based plan builder]
        T2 --> T3[Jinja2 templates]
        T3 --> T4[Complete TTCN project]
    end

    subgraph L2 [Layer 2: LLM Review (optional, watchdog-capped)]
        LLM[Claude Code agent<br/>10-min idle kill]
        T4 -.->|review/tweak| LLM
        LLM -.->|files or kill| T4
    end

    subgraph L3 [Layer 3: Build + Test (no LLM)]
        B1[bash build.sh<br/>with config-mirror trap]
        B2[./TestExecutable]
        B3[verdict parser]
        B3 --> M[metrics.json<br/>real wall-clock time]
        B1 --> B2
        B2 --> B3
    end

    T4 --> B1
    Watchdog[Watchdog<br/>10-min idle]
    LLM -.-> Watchdog
    Watchdog -.-> T3
```

**Key properties**:
- Layer 1 is **pure Python + Jinja2**, never touches Claude API. Output is
  deterministic, <1s wall time, and 100% reproducible.
- Layer 2 is a **single short Claude invocation** whose only job is to review
  the templated files. A **10-min watchdog** (configurable via
  `--watchdog-timeout`) kills the agent if it stalls, then falls back to the
  templated files. **Tested**: agent is killed within 30-60s when stalled.
- Layer 3 is `bash` + Python. No LLM. `execution_time_seconds` is always
  populated from real wall-clock (never faked).

## Critical files added in this refactor

| File | Role |
|---|---|
| `ttcn_static_renderer.py` | Layer 1 — renders TC_*.ttcn, TB_*.ttcn, and TestCases_SECC.ttcn from a plan + Jinja2 templates. No LLM. |
| `knowledge_base/templates/TC_testcase.ttcn.j2` | Jinja2 template for `TC_*.ttcn` (test case wrapper). |
| `knowledge_base/templates/TB_testbehavior.ttcn.j2` | Jinja2 template for `TB_*.ttcn` (test behavior function). Handles both GoodCase (structured send) and BadCase (raw send with malformed V2GTP header). |
| `knowledge_base/templates/TestCases_SECC.ttcn.j2` | Jinja2 template for the TITAN `control` block. |
| `plans/100_percent_unattended_pipeline.md` | Architecture design document. |
| (modified) `ttcn3_generation_pipeline.py` | Added `LLMWatchdog`, `--no-llm-render`, `--watchdog-timeout`, Layer-1 re-injection after Infra Agent, schema validation for metrics.json, and wall-clock timing around test execution. |
| (modified) `prompts/core_agent_prompt.txt` | Reduced from 28 KB to ~3 KB — review-and-tweak only, no generation. |

## How to run

### Pure-static, 100% deterministic, no LLM (recommended for unattended)
```bash
cd /home/dmc/桌面/platform
python3 agent/test_code/ttcn3_generation_pipeline.py \
  --test-case-file agent/test_case/output/tc_V2G2082_single.json \
  --filter-tc-id TC_SECC_CMN_VTB_V2GTPHeader_002 \
  --rule-based-plan \
  --no-llm-render \
  --watchdog-timeout 600
```

### LLM review with watchdog fallback
```bash
python3 agent/test_code/ttcn3_generation_pipeline.py \
  --test-case-file agent/test_case/output/tc_V2G2082_single.json \
  --filter-tc-id TC_SECC_CMN_VTB_V2GTPHeader_002 \
  --rule-based-plan \
  --watchdog-timeout 600
```
LLM may review/tweak; if it stalls for 10 min, watchdog kills it and the
templated files are used.

### Skip the build/test
Add `--skip-build` to only generate code and project structure.

## Verifying a successful run

A successful run is identified by these markers in `metrics.json`:
```json
{
  "verdict": {
    "result": "pass",                      // TITAN's own verdict, not faked
    "execution_time_seconds": 65.149,     // real wall-clock from time.time()
    "build_status": "completed",          // bash build.sh exit 0
    "compile_errors_count": 0
  },
  "latency": {
    "total_pipeline_seconds": 231.609     // end-to-end wall time
  }
}
```

And in the test execution log:
```
TESTCASE Test case TC_SECC_CMN_VTB_V2GTPHeader_002 finished. Verdict: pass
STATISTICS Test execution summary: 1 test case was executed. Overall verdict: pass
```

## Acceptance criteria (from plan)

1. Pipeline exits with code 0 on its own (no `kill -9` from outside).
2. `metrics.json` has `verdict.result: "pass"`.
3. `verdict.execution_time_seconds > 0` and matches the real wall-clock time of the test.
4. `build/bin/TestExecutable` was produced by `bash build.sh`, not manually copied.
5. `build/bin/logs/titan_traces/TestExecution.log` contains a `Verdict: pass` line emitted by TITAN itself.
6. No human-edited files in the project dir after the pipeline finishes.

All 6 criteria were met for `TC_SECC_CMN_VTB_V2GTPHeader_002` in the verification run.

## Generalization

The 4 V2GTPHeader test cases (`..._001` to `..._004`) share the same preconditions,
ports, and template. They differ only in the `bad_hex_value` for BadCase. The
renderer has a lookup table for these (`_V2GTPHEADER_BAD_HEX_DEFAULTS` in
`ttcn_static_renderer.py`) plus a regex-based extractor that pulls hex values
from the test case description.

For other message types (SessionSetup, SupportedAppProtocol, etc.) the renderer
falls back to the GoodCase path (structured send via `{ v2g := { ... } }`).
The bad_hex_value lookup table can be extended for other families.

## Future work (not in this iteration)

1. Run the same e2e test on the other 3 V2GTP cases (`_001`, `_003`, `_004`).
2. Add a smart `bad_hex_value` extractor for SDP, SessionSetup, etc.
3. Add a `precondition_return_type` heuristic so the TC template can
   differentiate void vs. verdicttype preconditions.
4. Replace the `f_SECC_CMN_PR_InitConfiguration_001.ttcn` in the demo
   skeleton to have `return verdicttype` so the verdict-checking pattern works.
