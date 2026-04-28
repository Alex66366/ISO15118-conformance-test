---
name: Refactor fault corrector static check loop
overview: "Remove the redundant 'static check small loop' from ttcn3_generation_pipeline.py. After refactoring, the pipeline flows as: Core Agent -> Infrastructure Agent -> Build -> [Compile Loop: Core Agent + Infrastructure Agent + Build + Fault Corrector(compile)] -> [Runtime Loop: Fault Corrector(runtime) + Core Agent + Infrastructure Agent + Build]."
todos:
  - id: refactor-pipeline
    content: Refactor generate_code_for_test_case() - remove static loop, extend compile loop (Core+Infra+Build+FC compile) and runtime loop (FC runtime+Core+Infra+Build)
    status: pending
  - id: update-docstring
    content: Update run_fault_corrector() docstring - mark static phase deprecated
    status: pending
  - id: update-claude-md
    content: Update CLAUDE.md - remove static check, describe remaining compile/runtime loops
    status: pending
isProject: false
---

## Plan: Eliminate Static Check Loop, Defer All Corrections to Build/Test-Time Feedback

### Background

The current pipeline has a "small loop" (Steps 3-4) where Core Agent generates TTCN-3 code, then Fault Corrector runs `phase="static"` checks (up to 3 iterations) before Infrastructure Agent is invoked. This is redundant because:

- The static phase is purely LLM-based with no actual project structure context.
- The Infrastructure Agent generates all other project modules as stubs — the Core Agent's module is inherently isolated.
- TITAN's compiler is the real validation engine; the static phase cannot catch import mismatches, undefined references, or module naming conflicts.
- The `phase="static"` checks (syntax, naming, timer, verdict, 4-phase model, templates, docs) are all either caught by the compiler or should be fixed with real error messages.

### Pipeline Loops After Refactoring (Target Architecture)

After removing the static check loop, the pipeline retains **two meaningful feedback loops**, both driven by real build/test results. Each loop involves the complete agent chain:

```
测试用例 -> Orchestrator -> Summarizer -> Core Agent -> Infrastructure Agent -> Build
                                                                                 |
                                                       +-------------------------+
                                                       v                         |
                                          +-----------------------------+        |
                                          |  Compile Loop (<=3 轮)      |        |
                                          |  1. Core Agent              |<-------+
                                          |  2. Infrastructure Agent    |   (TITAN 编译失败时触发)
                                          |  3. Build                  |        |
                                          |  4. Fault Corrector(compile)|        |
                                          +-------------+---------------+        |
                                          +-------------v-------------------------+
                                          |  Runtime Loop (<=2 轮)                |
                                          |  1. Fault Corrector(runtime)         |
                                          |  2. Core Agent (if manual_review)   |
                                          |  3. Infrastructure Agent             |
                                          |  4. Build                           |
                                          +-------------------------------------+  (运行结果不符合预期时触发)
```

#### Compile Loop (Step 7, ~lines 2158-2195)

- **触发条件**: TITAN `ttcn3_make` 编译失败
- **完整流程**: 解析编译错误 -> `run_core_agent(error_feedback)` -> `run_infrastructure_agent()` (重新生成骨架) -> 写入所有文件 -> `run_build_and_test()` -> `run_fault_corrector(phase="compile")` -> 若仍失败则反馈给 Core Agent 进入下一轮
- **终止条件**: 编译成功，或达到 `MAX_CORRECTION_ITERATIONS=3` 轮上限
- **注意**: 当前代码中编译循环只调用了 Core Agent，**需要补全** Infrastructure Agent 调用和 Fault Corrector(phase="compile") 调用

#### Runtime Loop (Step 7c, `_runtime_self_repair()` + 扩展)

- **触发条件**: TITAN 执行完成但测试结果不符合测试用例设计的预期（通过 `fault_result.status == "completed"` 判断存在需要修复的问题）
- **完整流程**:
  - `run_fault_corrector(phase="runtime")` (TTCN-3 log + PCAP 双轨分析)
  - 若只有 `auto_fix` 类型 -> 直接字符串替换 -> rebuild
  - 若存在 `manual_review` 类型 -> `run_core_agent(fault_feedback)` -> `run_infrastructure_agent()` -> rebuild -> 进入下一轮 Runtime Loop
- **终止条件**: 测试通过，或达到 2 轮上限
- **注意**: 当前代码中运行循环只在 `_runtime_self_repair()` 中做字符串替换，**需要扩展**以调用 Core Agent 和 Infrastructure Agent

#### Why These Two Loops Are Meaningful

- **Compile loop**: TITAN's actual compiler is the real validation engine. `phase="compile"` Fault Corrector uses `titan_troubleshooting_db.json` + LLM to reason about real error locations and fixes.
- **Runtime loop**: `phase="runtime"` Fault Corrector uses dual-track analysis (TTCN-3 log + PCAP with Python Correlator) to diagnose protocol-level mismatches. Complex failures go back to Core Agent for semantic-level fixes.

---

### Changes

#### 1. Refactor `generate_code_for_test_case()` in `ttcn3_generation_pipeline.py`

##### Part A — Remove static check loop (lines 2009-2070)

Remove the entire `for iteration` loop (lines 2013-2070). Replace with a single `run_core_agent()` call:

```python
# === Step 3: Core Agent (代码生成, 单次调用) ===
log("\n--- Step 3: Core Agent (代码生成) ---")
ttcn3_code = run_core_agent(
    generation_plan, references, few_shot_examples, sop, ""
)
if not ttcn3_code:
    log("Core Agent 未生成代码", "ERROR")
    return None
log(f"生成代码 {len(ttcn3_code)} 字符")
# NO static check. Compile loop (Step 7) handles all corrections.
```

##### Part B — Extend compile loop (lines 2158-2195)

当前编译循环只调用 Core Agent，需要扩展为完整 Agent 链：

```python
# === Step 7: Compile Loop (扩展: 每次迭代包含完整 Agent 链) ===
for retry_iter in range(1, MAX_CORRECTION_ITERATIONS + 1):
    log(f"编译修复迭代 {retry_iter}/{MAX_CORRECTION_ITERATIONS}...")

    # 1. Core Agent 根据编译错误修复 TTCN-3 代码
    fixed_code = run_core_agent(
        generation_plan, references, few_shot_examples, sop, new_error_feedback
    )
    if not fixed_code:
        log("Core Agent 未生成修复代码", "ERROR")
        break
    ttcn3_code = fixed_code

    # 2. Infrastructure Agent 重新生成项目骨架（纳入修复后的代码）
    if with_infrastructure:
        infra_result = run_infrastructure_agent(
            generation_plan=generation_plan,
            ttcn3_code=ttcn3_code,
            tc_id=plan_tc_id,
            sut_ip=sut_ip,
        )
        if infra_result.get("status") != "error":
            write_infrastructure_files(tc_project_dir, infra_result)
        with open(tc_ttcn_dest, "w", encoding="utf-8") as f:
            f.write(ttcn3_code)

    # 3. 重新编译
    retry_result = run_build_and_test(tc_dir=tc_project_dir, ...)
    retry_errors = retry_result.get("compile_errors", [])

    if retry_result.get("status") == "completed" or (
        retry_result.get("status") == "build_failed" and not retry_errors
    ):
        log("编译成功!")
        break

    # 4. Fault Corrector (phase="compile") 分析本次编译错误
    fc_result = run_fault_corrector(
        ttcn3_code, generation_plan, sop,
        phase="compile", compile_errors=retry_errors
    )
    new_error_feedback = build_error_to_feedback(retry_errors)
    if retry_iter == MAX_CORRECTION_ITERATIONS:
        break
```

##### Part C — Extend runtime loop (`_runtime_self_repair()`, lines 1791-1924)

当前 `_runtime_self_repair()` 只做字符串替换。扩展为：当存在 `manual_review` 类型修复时，调用 Core Agent + Infrastructure Agent 进入下一轮 Runtime Loop。

```python
# 在 _runtime_self_repair() 中:
# 1. 尝试 auto_fix 字符串替换
# 2. 若有 manual_review 类型修复存在:
if manual_review_repairs:
    runtime_feedback = build_runtime_feedback(fault_result)
    fixed_code = run_core_agent(
        generation_plan, references, few_shot_examples, sop, runtime_feedback
    )
    infra_result = run_infrastructure_agent(generation_plan, fixed_code, plan_tc_id, sut_ip)
    write_infrastructure_files(tc_project_dir, infra_result)
    retry_result = run_build_and_test(...)
    # 若仍失败，进入下一轮 Runtime Loop
```

#### 2. Update `generate_code_for_test_case()` docstring

**Location**: Lines 1942-1962. Update the pipeline diagram to reflect the two full agent-chain loops.

#### 3. Update `run_fault_corrector()` docstring

**Location**: Lines 975-1005. Add a note that `phase="static"` is deprecated — retained only for manual/offline use.

#### 4. Update `CLAUDE.md` documentation

In `agent/CLAUDE.md`:

- **Section 2.1**: Update the data flow diagram to remove `-> [小循环: static check x 3]` and show the two remaining loops with full agent chains (Compile: Core+Infra+Build+FC compile; Runtime: FC runtime+Core+Infra+Build).
- **Section 2.2**: Remove "Step 4: Fault Corrector (phase=static)". Add explicit description of compile loop (Step 7) and runtime loop (Step 7c).
- **Section 6.4**: Rename and rewrite to describe the two remaining loops with their complete agent chains.
- **Section 6.5**: Update to note that `static` phase is deprecated.

### Key Design Rationale

1. **Static checks are redundant**: Syntax/naming/timer/verdict issues are either caught by TITAN's compiler or represent failures fixed by the compile loop with real error messages.
2. **Compile loop (Core + Infra + Fault Corrector compile)**: `phase="compile"` uses `titan_troubleshooting_db.json` + LLM on actual compile errors.
3. **Runtime loop (Fault Corrector runtime + Core + Infra)**: `phase="runtime"` uses dual-track analysis (TTCN-3 log + PCAP) to diagnose protocol-level issues. Complex failures go back to Core Agent for semantic-level fixes.
4. **Reduced LLM calls**: Eliminates up to 6 extra sub-agent calls from the code generation phase (3 iterations x 2 agents).

# 4 To-dos

- [ ] Refactor `generate_code_for_test_case()` - remove static loop, extend compile loop (Core+Infra+Build+FC compile) and runtime loop (FC runtime+Core+Infra+Build)
- [ ] Update `generate_code_for_test_case()` docstring - reflect two full agent-chain loops
- [ ] Update `run_fault_corrector()` docstring - mark static phase deprecated
- [ ] Update `CLAUDE.md` - remove static check, describe remaining compile/runtime loops