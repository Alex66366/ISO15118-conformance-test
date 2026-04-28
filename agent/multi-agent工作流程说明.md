# Multi-Agent 工作流程说明

本文档描述平台中两个多 Agent 生成流水线的架构与执行流程。两个流水线基于 NeTestLLM 论文方法，通过 Claude Code CLI（`claude -p`）调用 sub-agent 完成协作。

---

## 一、整体架构概览

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           第一阶段：测试用例生成（4-Agent）                              │
│                   test_case/test_case_generator.py                                  │
│                                                                                     │
│  需求 ID(s) ──→ Orchestrator ──→ Modeler ──→ Generator ──→ Verifier               │
│                  （意图分解）     （低层建模）  （用例生成）     （覆盖验证）             │
│                        ↓                ↓                                          │
│               generation_plan       test_cases JSON                                 │
└─────────────────────────────────────────────────────────────────────────────────────┘
                                      ↓
                                 输出文件保存至
                            test_case/generated_test_cases.json
                                      ↓
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           第二阶段：测试代码生成（5-Agent）                             │
│                 test_code/ttcn3_generation_pipeline.py                             │
│                                                                                     │
│  测试用例JSON ──→ Orchestrator ──→ Summarizer ──→ Core Agent ──→ Fault Corrector    │
│                   （意图分解）     （知识检索）     （代码生成）      （静态校验）          │
│                                              ↓                   ↓                 │
│                                    TTCN-3 .ttcn 文件    ┌──────────────┐            │
│                                                      │ 小循环迭代修复  │            │
│                                                      │  (≤3轮)       │            │
│                                                      └───────┬───────┘            │
│                                                              ↓                     │
│                                                      校验通过?                       │
│                                                   ┌───────┴────────┐               │
│                                                   Yes               No              │
│                                                   ↓                  ↓            │
│                                            Infrastructure     反馈错误 ─→ Core Agent │
│                                            Agent (可选)              重新生成      │
│                                                  ↓                              │
│                                        完整项目结构生成                         │
│                                   (config + cpp_adapters + build.sh)              │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 二、第一阶段：测试用例生成（4-Agent）

### 2.1 Agent 职责总览

| Agent | 核心职责 | 输入 | 输出 |
|-------|---------|------|------|
| **Orchestrator** | 意图分解：分析高层需求，确定测试类型/SUT/消息类型 | 需求 ID + 章节上下文 + Figure/Table | `generation_plan` (dict) |
| **Modeler** | 低层建模：提取 Packet/FSM/TimeSequence/Functional 四类 testing points | generation_plan + 章节全文 + Figure/Table | `testing_points` (list[dict]) |
| **Generator** | 测试用例生成：基于 testing points 生成 ISO 15118-4 格式测试用例 | generation_plan + testing_points + 协议摘要 | `test_cases` (list[dict]) |
| **Verifier** | 覆盖度验证：评估测试用例对需求的覆盖广度和深度 | test_cases + 需求 + 章节内容 | `verification` (dict) |

### 2.2 数据流图（第一阶段）

```
需求 ID(s) (e.g. V2G2-082)
        │
        ▼
┌───────────────────────────────────────────────────────────────┐
│  ProtocolDataStore.load()                                      │
│  从 iso15118_2_hierarchical_tree.json 加载：                  │
│    • all_requirements（所有需求）                               │
│    • section_tree（章节树）                                     │
│    • figure_index / table_index（图表索引）                     │
└───────────────────────────┬───────────────────────────────────┘
                            │
                            ▼
            ┌───────────────────────────────┐
            │  Agent 1: Orchestrator         │
            │  run_orchestrator(rid, mode)   │
            │  意图分解                        │
            │                                 │
            │  输入:                           │
            │    • 需求详情 (shall/shall not)  │
            │    • 章节摘要                    │
            │    • 关联 Figure/Table           │
            │                                 │
            │  输出: generation_plan           │
            │    • mode / test_type / sut_type │
            │    • category / message_type     │
            │    • sections / precondition_chain│
            └──────────────┬──────────────────┘
                           │
              ┌────────────┴────────────┐
              │ Single 模式: 每条需求单独处理  │
              │ Multi 模式: 多需求合并处理     │
              └────────────┬────────────┘
                           │
                           ▼
            ┌───────────────────────────────┐
            │  Agent 2: Modeler             │
            │  run_modeler(plan)             │
            │  低层建模（NeTestLLM §4.2）      │
            │                                 │
            │  4 种建模类型:                  │
            │  • Packet Field    → 消息字段约束│
            │  • FSM             → 状态转换   │
            │  • Time Sequence   → 定时器行为  │
            │  • Functional      → 功能测试点  │
            │                                 │
            │  输出: testing_points list       │
            └──────────────┬──────────────────┘
                           │
                           ▼
            ┌───────────────────────────────┐
            │  Agent 3: Generator           │
            │  run_generator(plan, points)  │
            │  测试用例生成（NeTestLLM §5.1）  │
            │                                 │
            │  输入:                           │
            │    • generation_plan           │
            │    • testing_points            │
            │    • 协议摘要                    │
            │    • Figure/Table 上下文        │
            │                                 │
            │  输出: test_cases               │
            │    ISO 15118-4 格式 JSON:       │
            │    {                            │
            │      "Test_Case": {             │
            │        "TC_Id": "...",          │
            │        "Test_Objective": "...", │
            │        "Test_Sequence": [...],  │
            │        "Expected_Behavior": "...│
            │      }                          │
            │    }                            │
            └──────────────┬──────────────────┘
                           │
                           ▼
            ┌───────────────────────────────┐
            │  Agent 4: Verifier             │
            │  run_verifier(plan, cases)     │
            │  覆盖验证（NeTestLLM §5.2）     │
            │                                 │
            │  广度(Breadth)评分:             │
            │    • 需求追溯覆盖率               │
            │    • 消息类型覆盖率               │
            │    • 场景覆盖率(Good+Bad+边界)   │
            │    • 状态覆盖率                  │
            │    • 功能域覆盖率                │
            │                                 │
            │  深度(Depth)评分:               │
            │    • 基本功能验证                │
            │    • 边界条件测试                │
            │    • 错误处理场景                │
            │    • 测试步骤质量                │
            │    • 命名规范符合度              │
            │                                 │
            │  输出: verification             │
            │    overall_score =             │
            │    (breadth + depth) / 2       │
            └──────────────┬──────────────────┘
                           │
                           ▼
                   test_case/output/*.json
                           │
                           ▼
                 generated_test_cases.json
```

### 2.3 CLI 使用方式

```bash
# 单需求模式
python test_case/test_case_generator.py --requirements V2G2-082 --mode single

# 多需求合并模式
python test_case/test_case_generator.py --requirements V2G2-082,V2G2-083,V2G2-084 --mode multi

# 跳过验证步骤（加速）
python test_case/test_case_generator.py --requirements V2G2-082 --skip-verify
```

---

## 三、第二阶段：测试代码生成（5-Agent）

### 3.1 Agent 职责总览

| Agent | 核心职责 | 输入 | 输出 |
|-------|---------|------|------|
| **Orchestrator** | 意图分解：将测试用例 JSON 转化为 TTCN-3 生成计划 | 测试用例 JSON + 需求上下文 + SOP | `generation_plan` (dict) |
| **Summarizer** | 知识检索：从 ISO 15118-4 知识库中精选最相关的 TTCN-3 参考代码 | generation_plan + 知识库预检索结果 | `references` (dict) |
| **Core Agent** | 代码生成：生成符合 ISO 15118-4 规范的完整 TTCN-3 模块代码 | generation_plan + references + few-shot + SOP + error_feedback | `ttcn3_code` (str) |
| **Fault Corrector** | 统一纠错：三相架构（static/compile/runtime），执行 8 项静态检查 | TTCN-3 代码 + generation_plan + SOP | `{status, issues_found, corrected_code}` |
| **Infrastructure Agent** | 项目结构生成：生成完整的 TITAN 项目骨架（可选） | generation_plan + ttcn3_code + demo 参考 | `{files, warnings}` (dict) |

### 3.2 数据流图（第二阶段，核心 4-Agent 链路）

```
测试用例 JSON (from Phase 1)
        │
        ▼
┌───────────────────────────────────────────────────────────────┐
│  加载基础数据                                                   │
│    • ttcn3_generation_sop.json（SOP + 前置条件链）              │
│    • few_shot_examples/（7 个 .ttcn 样例）                      │
│    • 查找需求上下文                                              │
└───────────────────────────┬───────────────────────────────────┘
                            │
                            ▼
            ┌───────────────────────────────┐
            │  Agent 1: Orchestrator         │
            │  run_orchestrator(tc, ctx, sop)│
            │  意图分解                        │
            │                                 │
            │  输出: generation_plan           │
            │    • tc_id / sut_type / category│
            │    • message_type / test_type   │
            │    • precondition_chain         │
            │    • timer_param / port_name    │
            │    • send/receive_template_name  │
            │    • test_behavior_func_name    │
            └──────────────┬──────────────────┘
                           │
                           ▼
            ┌───────────────────────────────┐
            │  本地预检索: search_knowledge_base()│
            │  基于 message_type + sut_type   │
            │                                 │
            │  优先: by_message_type 视图      │
            │  回退: ttcn_code_by_type 视图    │
            │                                 │
            │  输出: kb_search_results         │
            │    • testcases / functions      │
            │    • templates / altsteps       │
            └──────────────┬──────────────────┘
                           │
                           ▼
            ┌───────────────────────────────┐
            │  Agent 2: Summarizer          │
            │  run_summarizer(plan, kb_res)  │
            │  知识检索                       │
            │                                 │
            │  从预检索结果中精选:             │
            │    • reference_testcase        │
            │    • reference_test_behavior   │
            │    • reference_templates       │
            │    • additional_context        │
            └──────────────┬──────────────────┘
                           │
                           ▼
            ┌───────────────────────────────┐
            │  ┌─────────────────────────┐  │
            │  │ 小循环迭代修复 (≤3轮)    │  │
            │  │                         │  │
            │  │  Agent 3: Core Agent    │  │
            │  │  run_core_agent(...)     │  │
            │  │  代码生成                 │  │
            │  │                         │  │
            │  │  Agent 4: Fault Corrector│ │
            │  │  run_fault_corrector(    │  │
            │  │    phase="static")       │  │
            │  │  8项静态校验               │  │
            │  │                         │  │
            │  │  ┌──────────┐  ┌───────┐ │  │
            │  │  │ pass?   │→ │ 继续   │ │  │
            │  │  └──────────┘  └───────┘ │  │
            │  │     ↓ No                 │  │
            │  │  ┌──────────┐             │  │
            │  │  │ 有critical│            │  │
            │  │  │ issues?  │            │  │
            │  │  └────┬─────┘             │  │
            │  │    Yes ↓ No               │  │
            │  │  反馈错误  使用修复版      │  │
            │  │  给Core  达最大迭代       │  │
            │  │  Agent   ──→ 退出循环    │  │
            │  └──────────┬────────────────┘  │
            │             │                    │
            │             ▼                    │
            │       TTCN-3 .ttcn 文件          │
            └──────────────┬──────────────────┘
                           │
                           ▼
              test_code/generated/{tc_id}.ttcn
```

### 3.3 数据流图（第二阶段，完整链路含 Infrastructure Agent）

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              输入多样化                                           │
│                                                                                 │
│  模式1: 需求 ID ──→ 查找/生成测试用例 ──→ 进入流水线                                 │
│  模式2: TC ID ──→ 查找测试用例 ──→ 进入流水线                                      │
│  模式3: 测试用例 JSON ──→ 直接进入流水线                                           │
│  模式4: 测试用例文件 ──→ 加载 ──→ 进入流水线                                        │
└─────────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                      Step 1-2: Orchestrator + Summarizer                        │
│                              (同 3.2 小节)                                       │
└─────────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     Step 3-4: Core Agent + Fault Corrector                       │
│                         小循环迭代修复（最多 3 轮）                                  │
│                                                                                 │
│  Core Agent 生成代码 ──→ Fault Corrector 静态校验（8项检查）──→ status == "pass"?   │
│                                                ┌──────┴──────┐                  │
│                                             Yes           No                     │
│                                             ↓              ↓                     │
│                                       流程继续      有 critical issues?           │
│                                                     ┌─────┴─────┐               │
│                                                  Yes          No /已达最大迭代     │
│                                                  ↓              ↓               │
│                                           反馈错误给    使用 corrected_code       │
│                                           Core Agent  ──→ 退出循环               │
│                                           重新生成                               │
└─────────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
                    ┌───────────────────────────────────┐
                    │  --with-infrastructure?            │
                    │  ┌─────────────────────────────┐  │
                    │  │  Agent 5: Infrastructure   │  │
                    │  │  run_infrastructure_agent() │  │
                    │  │  生成完整 TITAN 项目结构     │  │
                    │  │                             │  │
                    │  │  config/                    │  │
                    │  │    • titan_runtime.cfg      │  │
                    │  │    • hardware_env.json      │  │
                    │  │  src/                       │  │
                    │  │    • cpp_adapters/          │  │
                    │  │    • ttcn3/                 │  │
                    │  │  logs/                      │  │
                    │  │  build.sh / build.bat       │  │
                    │  └──────────────┬──────────────┘  │
                    │                 │                   │
                    │         --skip-build?              │
                    │        ┌───────┴───────┐          │
                    │       Yes             No         │
                    │       ↓                ↓           │
                    │    跳过编译         Step 6:       │
                    │    直接输出         编译+运行       │
                    │    项目结构         ┌────────────┐ │
                    │                     │ 编译失败?  │ │
                    │                     └─────┬──────┘ │
                    │                    成功      ↓ 失败   │
                    │                     ↓   Step 7a:   │
                    │             ┌──────────┐   编译错误  │
                    │             │ Step 7b: │   反馈给    │
                    │             │ Fault    │   Core Agent│
                    │             │ Corrector│   重新生成  │
                    │             │ (runtime)│            │
                    │             └────┬─────┘            │
                    │                  │                  │
                    │           ┌──────┴──────┐            │
                    │           │ 有可自动修复  │            │
                    │           │ 的 repairs?  │            │
                    │           └──────┬──────┘            │
                    │            Yes    ↓ No              │
                    │      Step 7c:    保留分析             │
                    │     运行时自修复   结果               │
                    │    + 重新编译     输出                 │
                    │      验证                                │
                    └──────────────────────────────────────────┘
```

### 3.4 Fault Corrector 三相架构详解

```
                    Fault Corrector (统一入口)
                           run_fault_corrector(phase=?)
                                   │
               ┌───────────────────┼───────────────────┐
               ↓                   ↓                   ↓
          phase=             phase=              phase=
         "static"            "compile"           "runtime"
          (小循环)             (大循环)            (大循环)
               │                   │                   │
               ▼                   ▼                   ▼
    ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
    │ 触发时机:          │  │ 触发时机:          │  │ 触发时机:          │
    │ Core Agent生成后   │  │ TITAN编译失败      │  │ TITAN执行完成      │
    │                  │  │                  │  │ (log + PCAP可用)   │
    │ 分析方式:          │  │ 分析方式:          │  │                  │
    │ 纯LLM静态校验      │  │ LLM + 知识库       │  │ 分析方式:          │
    │ 8项检查清单        │  │ titan_troublesh-  │  │ 双轨分析           │
    │                  │  │ ooting_db.json    │  │                  │
    │                  │  │                  │  │ • Track 1:        │
    │ 输出:              │  │ 输出:              │  │   TTCN-3 log      │
    │ • status          │  │ • repairs         │  │ • Track 2:        │
    │ • issues_found    │  │ • corrected_code  │  │   PCAP报文         │
    │ • corrected_code  │  │                  │  │                  │
    └────────┬─────────┘  └────────┬─────────┘  │ + 交叉验证         │
             │                    │             │                  │
             │                    │             │ 输出:              │
             │                    │             │ • fault_type      │
             │                    │             │ • evidence         │
             │                    │             │ • repairs          │
             │                    │             └────────┬─────────┘
             │                    │                      │
             ▼                    ▼                      ▼
        返回给 pipeline    返回给 pipeline      ┌──────────────────────┐
        用于小循环决策       用于大循环修复       │ 运行时自修复循环 (Step 7c)│
                                               │  对 auto_fix 类型     │
                                               │  的 repairs 自动应用   │
                                               │  + 重新编译验证        │
                                               │  + 知识库自学习        │
                                               └──────────────────────┘
```

### 3.5 8 项静态检查清单（phase="static"）

| # | 检查项 | 描述 |
|---|--------|------|
| 1 | **语法检查** | 花括号/括号匹配、分号、关键字拼写 |
| 2 | **命名规范** | TC_ID / function / template 命名是否符合 ISO 15118-4 规范 |
| 3 | **组件端口** | runs on / system / port name 是否匹配 SUT 类型 |
| 4 | **定时器** | 启动/停止配对、超时分支不调用 stop |
| 5 | **判定逻辑** | GoodCase/BadCase 判定条件是否正确 |
| 6 | **四阶段模型** | Phase A/B/C/D 是否完整 |
| 7 | **模板检查** | send/receive 模板字段是否正确 |
| 8 | **文档注释** | @desc / @reference / @requirement 是否齐全 |

### 3.6 完整 Agent 对应关系对比

| 维度 | 第一阶段（测试用例） | 第二阶段（测试代码） |
|------|-------------------|-------------------|
| **Agent 1** | Orchestrator（需求意图→生成计划） | Orchestrator（测试用例意图→TTCN-3生成计划） |
| **Agent 2** | Modeler（协议层低层建模） | Summarizer（代码层知识检索） |
| **Agent 3** | Generator（生成测试用例JSON） | Core Agent（生成TTCN-3代码） |
| **Agent 4** | Verifier（覆盖度验证） | Fault Corrector（static校验） |
| **Agent 5** | — | Infrastructure Agent（项目结构生成） |
| **数据来源** | ISO 15118-2 协议规范 | TTCN-3 知识库 + SOP + Few-shot |
| **输出** | 结构化测试用例 JSON | 可编译的 TTCN-3 模块 + 完整项目 |

### 3.7 CLI 使用方式

```bash
# ========== 输入模式 1: 通过安全需求 ID ==========
python test_code/ttcn3_generation_pipeline.py --requirement V2G2-082
python test_code/ttcn3_generation_pipeline.py --requirements V2G2-082,V2G2-165,V2G2-434

# ========== 输入模式 2: 通过测试用例 ID ==========
python test_code/ttcn3_generation_pipeline.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001

# ========== 输入模式 3: 直接传入测试用例 JSON ==========
python test_code/ttcn3_generation_pipeline.py --test-case '{"Test_Case": {...}}'

# ========== 输入模式 4: 从文件读取测试用例 ==========
python test_code/ttcn3_generation_pipeline.py --test-case-file ./my_test_case.json

# ========== 生成完整项目结构（+ Infrastructure Agent） ==========
python test_code/ttcn3_generation_pipeline.py --requirement V2G2-082 --with-infrastructure

# ========== 生成项目但跳过编译 ==========
python test_code/ttcn3_generation_pipeline.py --requirement V2G2-082 --with-infrastructure --skip-build

# ========== 指定 SUT IP ==========
python test_code/ttcn3_generation_pipeline.py --requirement V2G2-082 --with-infrastructure --sut-ip 192.168.1.100
```

---

## 四、两阶段衔接机制

### 4.1 自动触发逻辑

当用户通过需求 ID 调用第二阶段时，`get_test_cases_for_requirements()` 会自动查找 `generated_test_cases.json`：

1. **命中**：找到匹配的测试用例，直接进入第二阶段
2. **未命中**：调用 `call_test_case_generator()` 触发第一阶段，生成测试用例后返回，继续第二阶段

这使得整个流程可以从"需求 ID"直接到"TTCN-3 代码"，两阶段透明串联。

### 4.2 数据流向

```
第一阶段输出 (JSON)
  test_case/output/tc_{rid}_{mode}.json
  或 generated_test_cases.json（合并版）
         │
         ▼
  第二阶段 test_code_pipeline.py
         │
         ▼
  输出: test_code/generated/{tc_id}.ttcn
```

---

## 五、大小循环机制总结

```
┌─────────────────────────────────────────────────────────────┐
│                    大循环（大圈）                               │
│              (编译/运行时 — Fault Corrector phase)           │
│                                                             │
│  .ttcn 文件 ──→ TITAN 编译 ──→ 编译失败? ──→ phase=compile  │
│                    │         │                              │
│                    │         └─ 成功 ──→ TITAN 运行 ──→ phase=runtime
│                    │                                │         │
│                    │                    ┌────────────┴────────┐│
│                    │                 运行成功   运行失败/异常  ││
│                    │                    ↓          ↓        ││
│                    │            流程结束     诊断分析       ││
│                    │                          + 自修复      ││
│                    └──── 失败反馈给 Core Agent ──────────────┘│
│                           重新生成                              │
│                                                             │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                    小循环（小圈）                               │
│              (代码生成时 — Fault Corrector phase=static)      │
│                                                             │
│  Core Agent ──→ 生成代码 ──→ Fault Corrector ──→ pass?      │
│    ↑            (第N轮)     (静态校验8项)     ┌───┴───┐     │
│    │                                      Yes     No        │
│    │ 反馈错误                               ↓       ↓       │
│    └─────────────── ← ─ ─ ─ ─ ─ ─ ─ ─  结束    还有critical│
│                                              (使用修复版)    issues?  │
│                                                        ↓        │
│                                                   Yes    No/达最大  │
│                                                   ↓        ↓       │
│                                                重新生成   结束     │
│                                                (第N+1轮) (使用修复版)│
└─────────────────────────────────────────────────────────────┘
```

---

*文档版本：2026-04-23*
