# ISO 15118-2/4 TTCN-3 测试代码自动生成系统

本项目基于 **NeTestLLM** 方法论，构建端到端自动化流水线，将 ISO 15118-2 协议安全需求自动转化为符合 ISO 15118-4 规范的 TTCN-3 一致性测试代码。核心引擎为 Claude Code CLI（通过 `claude -p` 调用 sub-agent）。

## 三阶段流水线


| 阶段 | 输入 | 输出 | Agent |
|---|---|---|---|
| **Phase 1** | 安全需求 ID（如 V2G2-082） | 测试用例 JSON（ISO 15118-4 格式） | 测试用例生成（4-Agent） |
| **Phase 2** | 测试用例 JSON | TTCN-3 代码（.ttcn） | Code Agent（5-Agent） |
| **Phase 3** | 生成的 TTCN-3 + 标准参考 | 评估报告（多维度评分） | 实验评估 Pipeline |

## 目录结构

```
agent/
├── CLAUDE.md                          # Claude Code 工作指南（本文档）

├── document/                          # 文档与预处理数据
│   ├── MinerU_markdown_ISO_15118-2-2014_*.md    # ISO 15118-2 原始文档
│   ├── MinerU_markdown_ISO_15118-4-2018_*.md   # ISO 15118-4 原始文档（3 分卷）
│   ├── MinerU_markdown_Automated_*.md            # NeTestLLM 论文
│   ├── iso15118_2_hierarchical_tree.json         # ISO 15118-2 结构化数据（~16MB）
│   ├── iso15118_4_ttcn_knowledge_base.json       # ISO 15118-4 TTCN-3 知识库（~15MB）
│   ├── iso15118_preprocessor.py                 # ISO 15118-2 预处理器
│   ├── iso15118_preprocessor_enhanced.py         # ISO 15118-2 增强预处理器
│   ├── iso15118_4_preprocessor.py                # ISO 15118-4 预处理器
│   ├── extract_figures_tables.py                 # 图表/表格提取脚本
│   ├── table/                                    # 提取的表格（table_1.md ~ table_112.md）
│   └── image/                                    # 提取的图表图片

├── test_case/                            # ===== 测试用例生成 =====
│   ├── test_case_generator.py            # 4-Agent 测试用例生成器
│   ├── generated_test_cases.json         # 已生成的测试用例
│   ├── test_case_TODO.md                # 重构 TODO 计划
│   ├── output/                           # 生成的测试用例输出
│   └── prompts/                          # Prompt 模板
│       ├── orchestrator_prompt.txt       # Orchestrator（意图分解）
│       ├── modeler_prompt.txt            # Modeler（低层建模）
│       ├── generator_prompt.txt          # Generator（测试用例生成）
│       └── verifier_prompt.txt           # Verifier（覆盖验证）

├── test_code/                            # ===== TTCN-3 代码生成 =====
│   ├── ttcn3_generation_pipeline.py      # 核心流水线
│   ├── prompts/                          # Prompt 模板
│   │   ├── orchestrator_prompt.txt       # Orchestrator（意图分解）
│   │   ├── summarizer_prompt.txt         # Summarizer（知识检索）
│   │   ├── core_agent_prompt.txt         # Core Agent（TTCN-3 代码生成）
│   │   ├── fault_corrector_prompt.txt    # Fault Corrector（统一纠错）
│   │   └── infrastructure_agent_prompt.txt
│   ├── knowledge_base/
│   │   ├── ttcn3_generation_sop.json    # TTCN-3 代码生成 SOP
│   │   ├── titan_troubleshooting_db.json # TITAN 故障知识库
│   │   ├── titan_troubleshooting.md      # TITAN 故障排查手册
│   │   ├── titan_reference_summary.json  # TITAN 参考摘要
│   │   └── doc_urls.json                # 文档 URL 索引
│   ├── fault_corrector/                  # Fault Corrector 运行时分析
│   │   ├── correlator.py                # TTCN-3 log + PCAP 时间对齐
│   │   ├── fault_classifier.py          # 故障分类（6 类 + 根因分析）
│   │   ├── pcap_parser.py               # PCAP V2GTP 报文解析
│   │   ├── repair_engine.py             # 修复建议 + 知识库自学习
│   │   ├── sniffer.py                  # scapy 后台抓包
│   │   └── titan_log_parser.py         # TITAN TestExecution.log 解析
│   ├── examples/                         # Few-shot 参考样例
│   │   ├── ttcn3_testcase_with_imports.ttcn  # 含完整 import 的 testcase 样例
│   │   └── demo/                            # 完整 demo（含编译产物）
│   └── generated/                       # 生成的 TTCN-3 代码输出

└── experiment/                           # ===== 实验评估 =====
    ├── run_experiment.py                 # Pipeline 主入口
    ├── code_generator.py                # 代码生成模块包装
    ├── standard_extractor.py            # 标准参考代码提取器
    ├── code_comparator.py               # 代码对比分析器
    ├── evaluation_pipeline.py            # 端到端评估 Pipeline
    ├── report_generator.py              # 报告生成器
    ├── batch_runner.py                  # 批量测试运行器
    ├── test_cases_config.json           # 测试用例配置
    ├── README.md                        # 实验 Pipeline 说明
    ├── standard_references/            # 提取的标准参考代码
    ├── comparison_results/              # 对比结果
    ├── results/                        # 实验结果目录
    ├── reports/                        # 生成的报告
    └── logs/                           # 日志文件
```

---

## Phase 1：测试用例生成 Agent（4-Agent）

入口脚本：`test_case/test_case_generator.py`

### Agent 架构

```
需求 ID(s) + 模式(single/multi)
    │
    ▼
[Orchestrator] 意图分解 → 确定测试类型/SUT/消息类型
    │
    ▼
[Modeler] 低层建模 → testing_points（Packet Field / FSM / Time Sequence / Functional）
    │
    ▼
[Generator] 测试用例生成 → ISO 15118-4 格式 JSON
    │
    ▼
[Verifier] 覆盖验证 → 评分 + 改进建议（可跳过）
```

### Agent 职责速查

| Agent | Prompt 模板 | 输入 | 输出 |
|---|---|---|---|
| **Orchestrator** | `orchestrator_prompt.txt` | 需求列表 + 上下文 | 生成计划 JSON |
| **Modeler** | `modeler_prompt.txt` | 生成计划 + 章节内容 + figure/table | testing_points 列表 |
| **Generator** | `generator_prompt.txt` | testing_points + 协议摘要 | 测试用例 JSON |
| **Verifier** | `verifier_prompt.txt` | 测试用例列表 + 需求列表 | 覆盖评分 JSON |

### 输出格式

测试用例 JSON 字段：`TC_Id`、`Test_Objective`、`Document_Reference`、`PreCondition`、`Test_Sequence`、`Expected_Behavior`、`parameters` 等。详见 `test_case/generated_test_cases.json` 示例。

### 使用方式

```bash
python test_case/test_case_generator.py --requirements V2G2-082 --mode single   # 单需求
python test_case/test_case_generator.py --requirements V2G2-082,V2G2-083 --mode multi  # 多需求合并
python test_case/test_case_generator.py --requirements V2G2-082 --skip-verify        # 跳过验证
```

---

## Phase 2：Code Agent（5-Agent + 反馈回路）

入口脚本：`test_code/ttcn3_generation_pipeline.py`

### Agent 架构

```
测试用例 JSON
    │
    ▼
[Orchestrator] 意图分解 → 生成计划 JSON
    │
    ▼
[Summarizer] 知识检索 → 相关 TTCN-3 参考代码片段
    │
    ▼
[Core Agent] TTCN-3 代码生成
    │
    ▼
[Infrastructure Agent] 生成项目骨架（TTCN-3 import、配置、目录结构）
    │
    ▼
[TITAN Build]
    │
    ├── 编译成功 → 结束
    │
    └── 编译失败 → [Compile Loop, <=3 轮]
                      Core Agent → Infrastructure Agent → Build → Fault Corrector
                      ↓
                  [Runtime Loop, <=2 轮]
                      Fault Corrector(runtime) → auto_fix / manual_review → Build
```

> **静态检查循环已移除**——语法/命名/定时器等检查由 TITAN 编译器完成。

### Agent 职责速查

| Agent | Prompt 模板 | 核心职责 |
|---|---|---|
| **Orchestrator** | `orchestrator_prompt.txt` | 分析测试用例，输出：tc_id / sut_type / message_type / precondition_chain / timer_param / port_name / send_template_name / receive_template_name / test_behavior_func_name |
| **Summarizer** | `summarizer_prompt.txt` | 从 ISO 15118-4 知识库检索最相关的 TTCN-3 参考代码片段 |
| **Core Agent** | `core_agent_prompt.txt` | 生成 TTCN-3 module 代码（testcase + test behavior function + templates） |
| **Infrastructure Agent** | `infrastructure_agent_prompt.txt` | 生成完整项目骨架（import、配置、目录结构、TITAN makefile） |
| **Fault Corrector** | `fault_corrector_prompt.txt` | compile phase：分析编译错误；runtime phase：TTCN-3 log + PCAP 双轨分析 |

### 使用方式

```bash
# 方式 1：通过安全需求 ID（自动查找/生成测试用例 → 生成代码）
python test_code/ttcn3_generation_pipeline.py --requirement V2G2-082

# 方式 2：通过测试用例 ID
python test_code/ttcn3_generation_pipeline.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001

# 方式 3：多需求合并模式（默认 multi）
python test_code/ttcn3_generation_pipeline.py --requirements V2G2-082,V2G2-165 --mode single  # 每个单独生成
python test_code/ttcn3_generation_pipeline.py --requirements V2G2-082,V2G2-165               # 合并生成

# 批量生成
python experiment/code_generator.py --requirement V2G2-082
```

### 数据依赖

| 文件 | 用途 |
|---|---|
| `test_case/generated_test_cases.json` | **测试用例来源**（Phase 1 输出） |
| `document/iso15118_4_ttcn_knowledge_base.json` | **仅作参考**——TTCN-3 代码样例 |
| `test_code/knowledge_base/ttcn3_generation_sop.json` | 7 步 SOP + 前置条件链 |
| `test_code/examples/` | 干净的 Few-shot 样例 |

---

## Phase 3：实验评估 Pipeline

入口脚本：`experiment/run_experiment.py`

### Pipeline 步骤

```
测试用例 ID
    │
    ▼
[Step 1] 代码生成 → test_code/generated/{tc_id}.ttcn
    │
    ▼
[Step 2] 标准提取 → experiment/standard_references/standard_{tc_id}.ttcn
    │
    ▼
[Step 3] 代码对比 → experiment/comparison_results/{tc_id}_comparison.json
    │
    ▼
[Step 4] 报告生成 → experiment/reports/{tc_id}_report.md
```

### 评分体系

| 维度 | 权重 | 描述 |
|---|---|---|
| **结构相似度** | 30% | module、import、testcase 等元素存在性 |
| **语法正确度** | 25% | alt 块、定时器、verdict 等关键语法 |
| **语义覆盖率** | 25% | 必需组件（testcase/runs on/system/setverdict）存在性 |
| **命名规范度** | 10% | 元素命名是否符合 ISO 15118-4 规范 |
| **代码完整性** | 10% | 缺失/多余的函数、模板等 |

评分等级：优秀（90-100）、良好（70-89）、一般（50-69）、较差（0-49）。

### 使用方式

```bash
# 单个测试用例完整流程
python experiment/run_experiment.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001

# 跳过代码生成（使用已有代码）
python run_experiment.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001 --skip-generation

# 批量测试
python run_experiment.py --batch
```

---

## TTCN-3 代码规范（关键约束）

> **代码风格参考**：`test_code/examples/` 和 `test_code/examples/demo/` 下的 .ttcn 文件是权威样例，直接读取使用。

### 命名规范

| 类型 | 模式 | 示例 |
|---|---|---|
| **testcase** | `TC_{SUT}_{Cat}_VTB_{Msg}_{Seq}` | `TC_SECC_CMN_VTB_SessionSetup_001` |
| **测试行为函数** | `f_{SUT}_{Cat}_TB_VTB_{Msg}_{Seq}` | `f_SECC_CMN_TB_VTB_SessionSetup_001` |
| **前置条件函数** | `f_{SUT}_{Cat}_PR_{Name}_{Seq}` | `f_SECC_CMN_PR_SessionSetup_001` |
| **后置条件函数** | `f_{SUT}_{Cat}_PO_{Name}_{Seq}` | `f_SECC_CMN_PO_InitConfiguration_001` |
| **发送模板** | `md_CMN_{Msg}_{Seq}` | `md_CMN_SessionSetupReq_001` |
| **接收模板** | `mdw_{SUT}_{Msg}_{Seq}` | `mdw_SECC_SessionSetupRes_001` |
| **Altstep** | `a_{SUT}_{Name}` | `a_SECC_Unexpected_V2G_Message` |
| **计时器** | `tc_V2G_{SUT}_Msg_Timer` | `tc_V2G_EVCC_Msg_Timer` |
| **组件变量** | `vc_{Name}` | `vc_SessionID` |

参数说明：**SUT** = `SECC`/`EVCC`；**Category** = `CMN`/`AC`/`DC`；**SeqNo**：001 = GoodCase，002+ = BadCase。

### 四阶段测试执行模型

```
Phase A: Initialize   → f_*_PR_InitConfiguration_001（创建 PTC、映射端口）
Phase B: Precondition → 协议特定前置条件链
               SDP → SupportedAppProtocol → SessionSetup → ServiceDiscovery
             → PaymentServiceSelection → Authorization → ChargeParameterDiscovery
             → PowerDelivery → ChargingStatus/CurrentDemand → SessionStop
Phase C: Test Behavior → f_*_TB_VTB_*（发送激励、验证响应、设置 verdict）
Phase D: Postcondition → f_*_PO_*_001（清理、取消端口映射）
```

### GoodCase vs BadCase

| 类型 | SeqNo | pass 条件 |
|---|---|---|
| **GoodCase** | 001 | 发送有效消息，收到正确响应 |
| **BadCase** | 002+ | 发送无效消息，SUT 拒绝/关闭/超时则 pass |

### 关键注意事项

1. **每个 alt 分支都必须停止定时器**（`tc_*.stop`），超时分支除外
2. **测试行为函数必须以 `return getverdict;` 结尾**
3. **模板字段语义**：`omit`（字段不存在）、`?`（任意值）、`*`（可选字段）
4. **所有生成的代码必须包含需求追溯注释**（`@reference` 标注 V2G2-XXX）
5. **V2GTP 头部常量**：`protocolVersion='01'H`、`invProtocolVersion='FE'H`、`payloadType='8001'H`
6. **组件和端口**：SECC 用 `pt_V2G_TCP_TLS_ALM_SECC_Port`，EVCC 用 `pt_V2G_TCP_TLS_ALM_EVCC_Port`

---

## 关键设计决策

### 为什么用 Claude Code CLI 而不是 API？

1. 与 sub-agent 架构保持一致
2. 支持更丰富的工具调用（文件读写、搜索等）
3. 无需管理 API key，直接复用本地环境
4. sub-agent 可自主探索代码库和文档

### 两个反馈回路

**Compile Loop（≤3 轮）**：TITAN 编译失败时触发，流程为 Core Agent → Infrastructure Agent → Build → Fault Corrector(compile)。

**Runtime Loop（≤2 轮）**：TITAN 执行完成但结果不符合预期时触发，流程为 Fault Corrector(runtime) → auto_fix / manual_review → Build。

**移除静态检查循环的原因**：静态检查是纯 LLM 驱动的，无法捕获 import 不匹配、未定义引用等错误。TITAN 编译器才是真正的验证引擎。

### Fault Corrector 两相架构

| Phase | 触发时机 | 分析方式 |
|---|---|---|
| **compile** | TITAN 编译失败 | LLM + `titan_troubleshooting_db.json` |
| **runtime** | TITAN 执行完成 | TTCN-3 log + PCAP 双轨分析 |

---

## 参考资料

- **NeTestLLM 论文**: `document/MinerU_markdown_Automated_Network_Protocol_Testing_with_LLM_Agent_*.md`
  - §4: Hierarchical RFC Understanding
  - §5: Test Case Generation with Verification
  - §6: Executable Artifact Generation（多 Agent 架构）
  - §7: Runtime Feedback Analysis（大小循环）
- **ISO 15118-2:2014**: 协议需求规范
- **ISO 15118-4:2018**: TTCN-3 测试规范
