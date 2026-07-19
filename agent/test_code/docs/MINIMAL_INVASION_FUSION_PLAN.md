# 多智能体框架架构重构方案
## 最小侵入式融合升级 (Minimal-Invasive Fusion)

**版本**: v1.0
**日期**: 2026-07-19
**核心原则**: 复用现有组件，融合而非替换

---

## 1. 重构目标

### 1.1 当前痛点 vs 目标状态

| 问题 | 根因 | 解决方案 |
|------|------|----------|
| SessionID 偏移量硬编码 (`substr(..., 39, 16)`) | LLM 缺乏协议状态机上下文 | 在 Summarizer 中注入状态机推理 |
| Prompt 内部矛盾 (HARD RULE #2) | 规则碎片化 | 统一为 Dumb Terminal 架构 |
| Agent 职责重叠 | 缺乏清晰的边界定义 | 明确各 Agent 的绝对职责 |
| 错误修复依赖硬编码分类 | Fault Corrector 缺乏语义理解 | 增强 Prompt 引导 |

### 1.2 核心设计原则

1. **零新增 Python 文件** - 所有逻辑通过 Prompt 引导实现
2. **单一 KB 文件** - 不创建新目录或碎片化 JSON
3. **复用现有 Agent** - 不新增 Summarizer/Core Agent/Fault Corrector
4. **最小侵入** - 只修改 Prompt 和现有 KB JSON

---

## 2. 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                    Enhanced Orchestrator (不变)                    │
│  输入: tc_id, test_case_spec                                    │
│  输出: generation_plan                                           │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              【升级版】Summarizer Agent (新增 Protocol Reasoning)   │
│  新增职责:                                                      │
│  1. 状态机定位: 确定 tc_id 在 ISO 15118-2 状态机中的位置           │
│  2. 前置条件推理: 识别需要的 vc_* 状态变量                        │
│  3. SessionID 生命周期: 提取/注入规则                           │
│  4. 语义指令生成: 输出结构化 JSON 指令                           │
│                                                                  │
│  输出: reasoning.json (语义指令)                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              【降级为纯 Coder】Core Agent                        │
│  职责: 将 reasoning.json 精准翻译为 TTCN-3 代码                   │
│  移除: 所有协议状态机推理逻辑                                      │
│  保留: TTCN-3 语法生成能力                                       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Infrastructure Agent (职责收窄)                │
│  绝对禁止: 修改任何 PR/TB/TC 函数代码                             │
│  只负责: 项目结构、C++ Adapter、编译配置                          │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. KB JSON 扩展方案

### 3.1 扩展 `generation_patterns.json`

在现有 `generation_patterns.json` 中新增 `state_machine` 和 `cross_step_state` 部分。

**新增内容 (建议在文件末尾添加)**:

```json
{
  "_extension": {
    "name": "Protocol State Machine & Cross-Step State",
    "version": "1.0",
    "purpose": "Replace hardcoded offset-based SessionID extraction with semantic state reasoning"
  },

  "state_machine": {
    "name": "ISO_15118_2_SECC_State_Machine",
    "version": "1.0",

    "states": {
      "S_INIT": {
        "description": "Initial state before SDP"
      },
      "S_SDP_COMPLETE": {
        "description": "SDP discovery completed, SECC address known"
      },
      "S_SUPPORTED_APP_PROTOCOL": {
        "description": "Protocol version negotiation",
        "transitions": [
          {
            "event": "supportedAppProtocolRes.OK",
            "target": "S_SESSION_SETUP",
            "effects": []
          }
        ]
      },
      "S_SESSION_SETUP": {
        "description": "Session establishment - SessionID assigned by SECC",
        "transitions": [
          {
            "event": "sessionSetupRes.OK_NewSessionEstablished",
            "target": "S_SERVICE_DISCOVERY",
            "effects": [
              {
                "type": "EXTRACT_STATE_VARIABLE",
                "variable": "vc_SessionID",
                "from": "sessionSetupRes.V2G_Message.Header.SessionID",
                "validation": {
                  "type": "hexBinary",
                  "length": 16,
                  "pattern": "^[0-9A-Fa-f]{16}$"
                }
              }
            ]
          },
          {
            "event": "sessionSetupRes.OK_ReusedSession",
            "target": "S_SERVICE_DISCOVERY",
            "effects": [
              {
                "type": "EXTRACT_STATE_VARIABLE",
                "variable": "vc_SessionID",
                "from": "sessionSetupRes.V2G_Message.Header.SessionID"
              }
            ]
          }
        ]
      },
      "S_SERVICE_DISCOVERY": {
        "description": "EVCC discovers available services",
        "transitions": [
          {
            "event": "serviceDiscoveryRes.OK",
            "target": "S_SERVICE_PAYMENT_SELECTION",
            "effects": []
          }
        ],
        "preconditions": {
          "required_state_variables": ["vc_SessionID"],
          "injection_rule": "Every V2G_Message.Header.SessionID must use vc_SessionID value"
        }
      },
      "S_SERVICE_PAYMENT_SELECTION": {
        "description": "EVCC selects payment option",
        "transitions": [
          {
            "event": "paymentServiceSelectionRes.OK",
            "target": "S_CHARGE_PARAMETER_DISCOVERY",
            "effects": []
          }
        ],
        "preconditions": {
          "required_state_variables": ["vc_SessionID"]
        }
      },
      "S_CHARGE_PARAMETER_DISCOVERY": {
        "description": "EVCC requests charge parameters",
        "transitions": [
          {
            "event": "chargeParameterDiscoveryRes.OK",
            "target": "S_POWER_DELIVERY",
            "effects": [
              {
                "type": "EXTRACT_STATE_VARIABLE",
                "variable": "vc_SAScheduleTupleID",
                "from": "chargeParameterDiscoveryRes.SAScheduleTuple[0].SAScheduleTupleID"
              }
            ]
          }
        ],
        "preconditions": {
          "required_state_variables": ["vc_SessionID"]
        }
      },
      "S_POWER_DELIVERY": {
        "description": "EVCC requests power delivery",
        "transitions": [
          {
            "event": "powerDeliveryRes.OK",
            "target": "S_SESSION_STOP",
            "effects": []
          }
        ],
        "preconditions": {
          "required_state_variables": ["vc_SessionID", "vc_SAScheduleTupleID"]
        }
      },
      "S_SESSION_STOP": {
        "description": "Session termination"
      }
    },

    "message_to_state_mapping": {
      "SessionSetup": {
        "produces_state": ["vc_SessionID"],
        "consumes_state": []
      },
      "ServiceDiscovery": {
        "produces_state": [],
        "consumes_state": ["vc_SessionID"]
      },
      "ServicePaymentSelection": {
        "produces_state": [],
        "consumes_state": ["vc_SessionID"]
      },
      "ChargeParameterDiscovery": {
        "produces_state": ["vc_SAScheduleTupleID"],
        "consumes_state": ["vc_SessionID"]
      },
      "PowerDelivery": {
        "produces_state": [],
        "consumes_state": ["vc_SessionID", "vc_SAScheduleTupleID"]
      },
      "Authorization": {
        "produces_state": [],
        "consumes_state": ["vc_SessionID"]
      }
    }
  },

  "cross_step_state": {
    "_description": "State variables that persist across test case steps. These are extracted from responses and must be injected into subsequent requests.",

    "vc_SessionID": {
      "type": "octetstring",
      "length": 8,
      "declared_in": "ComponentsAndPorts.ttcn",
      "produced_by": [
        {
          "message_type": "SessionSetup",
          "condition": "ResponseCode in ['OK_NewSessionEstablished', 'OK_ReusedSession']",
          "extraction": {
            "method": "JSON_PATH",
            "path": "$.V2G_Message.Header.SessionID",
            "ttcn_expression": "char2oct(extract_session_id(resp_json))"
          }
        }
      ],
      "consumed_by": [
        {"message_type": "ServiceDiscovery", "injected_into": "$.V2G_Message.Header.SessionID"},
        {"message_type": "ServicePaymentSelection", "injected_into": "$.V2G_Message.Header.SessionID"},
        {"message_type": "ChargeParameterDiscovery", "injected_into": "$.V2G_Message.Header.SessionID"},
        {"message_type": "PowerDelivery", "injected_into": "$.V2G_Message.Header.SessionID"},
        {"message_type": "Authorization", "injected_into": "$.V2G_Message.Header.SessionID"}
      ]
    },

    "vc_SAScheduleTupleID": {
      "type": "integer",
      "declared_in": "ComponentsAndPorts.ttcn",
      "produced_by": [
        {
          "message_type": "ChargeParameterDiscovery",
          "condition": "ResponseCode == 'OK'",
          "extraction": {
            "method": "JSON_PATH",
            "path": "$.V2G_Message.Body.ChargeParameterDiscoveryRes.SAScheduleTuple[0].SAScheduleTupleID"
          }
        }
      ],
      "consumed_by": [
        {"message_type": "PowerDelivery", "injected_into": "$.V2G_Message.Body.PowerDeliveryReq.SAScheduleTupleID"}
      ]
    }
  },

  "semantic_extraction_rules": {
    "_description": "Replace hardcoded offset extraction with semantic JSON path rules. These rules tell the LLM how to extract state variables from JSON responses.",

    "SessionID_extraction": {
      "json_path": "$.V2G_Message.Header.SessionID",
      "validation": {
        "type": "string",
        "pattern": "^[0-9A-Fa-f]{16}$",
        "length": 16
      },
      "ttcn3_conversion": {
        "from_type": "charstring",
        "to_type": "octetstring",
        "function": "char2oct()"
      },
      "extraction_pattern": "Use JSON key matching: find key 'SessionID' and extract its value. Do NOT use hardcoded byte offsets.",
      "example": {
        "input_json": "{\"V2G_Message\":{\"Header\":{\"SessionID\":\"5300F029FE0D3E8C\"},...}}",
        "extracted_value": "5300F029FE0D3E8C",
        "ttcn_expression": "var charstring sid := json_extract(resp_json, \"SessionID\"); vc_SessionID := char2oct(sid);"
      }
    },

    "ResponseCode_extraction": {
      "json_path": "$.V2G_Message.Body.*.ResponseCode",
      "wildcard": "Matches any message response (e.g., SessionSetupRes, ServiceDiscoveryRes, etc.)",
      "ttcn3_pattern": "match(resp_json, @nocase @\"*\"\"ResponseCode\"\":\"\"OK*\"\"\")",
      "success_values": ["OK_NewSessionEstablished", "OK_ReusedSession", "OK"],
      "failure_values": ["FAILED", "FAILED_"]
    }
  }
}
```

---

## 4. Prompt Diff 方案

### 4.1 Summarizer Prompt 扩展

**目标文件**: `agent/test_code/prompts/summarizer_prompt.txt` (如存在) 或集成到 `ttcn3_generation_pipeline.py` 中的 Summarizer 调用

**新增章节** (在文件适当位置添加):

```markdown
# Protocol Reasoning Module (新增)

## 你的新增职责: Protocol State Reasoning

除了现有的知识检索职责外，你现在是 **Protocol Reasoning Agent**。你需要:

### Step 1: 状态机定位

给定 `tc_id = TC_SECC_CMN_VTB_PaymentServiceSelection_001`:

1. **确定消息类型**: 从 tc_id 提取 `PaymentServiceSelection`
2. **查找状态**: 在 `state_machine` 中查找 `PaymentServiceSelection` 对应的状态
3. **确定前置状态**: `ServicePaymentSelection` 之前是 `ServiceDiscovery`

```
思考过程:
- tc_id = TC_SECC_CMN_VTB_PaymentServiceSelection_001
- 消息类型: PaymentServiceSelection
- 在 state_machine 中, PaymentServiceSelection 位于 S_SERVICE_PAYMENT_SELECTION
- 前置状态: S_SERVICE_DISCOVERY (需要 vc_SessionID)
- 需要的状态变量: vc_SessionID
```

### Step 2: 前置条件分析

```json
{
  "preconditions": {
    "required_state_variables": ["vc_SessionID"],
    "source": "SessionSetup.ResponseCode == 'OK_NewSessionEstablished'",
    "extraction_path": "$.V2G_Message.Header.SessionID"
  }
}
```

### Step 3: 状态变量注入规则

对于 `vc_SessionID`:
- **类型**: octetstring (8 字节)
- **注入位置**: `$.V2G_Message.Header.SessionID`
- **TTCN-3 表达式**: `"...\"SessionID\":\"..." & oct2char(vc_SessionID) & "\"..."`

### Step 4: 生成语义指令

输出以下 JSON 结构:

```json
{
  "reasoning": {
    "tc_id": "TC_SECC_CMN_VTB_PaymentServiceSelection_001",
    "message_type": "PaymentServiceSelection",
    "protocol_state": "S_SERVICE_PAYMENT_SELECTION",
    "preceding_state": "S_SERVICE_DISCOVERY",
    "state_variables_needed": ["vc_SessionID"],
    "state_variables_provided_by_preconditions": ["vc_SessionID"],
    "injection_rules": {
      "vc_SessionID": {
        "json_path": "$.V2G_Message.Header.SessionID",
        "ttcn_expression": "oct2char(vc_SessionID)",
        "note": "vc_SessionID is octetstring, convert to charstring for JSON concatenation"
      }
    }
  },
  "send_instruction": {
    "message_type": "PaymentServiceSelectionReq",
    "json_structure": {
      "V2G_Message": {
        "Header": {
          "SessionID": "{{ vc_SessionID_as_charstring }}"
        },
        "Body": {
          "PaymentServiceSelectionReq": {
            "SelectedPaymentOption": "{{ PICS_value }}"
          }
        }
      }
    },
    "fields": [
      {"name": "SessionID", "source": "vc_SessionID", "transform": "oct2char()"},
      {"name": "SelectedPaymentOption", "source": "PICS", "values": ["Contract", "ExternalPayment"]}
    ]
  },
  "receive_instruction": {
    "expected_message": "PaymentServiceSelectionRes",
    "validation": {
      "ResponseCode": {
        "success_pattern": "OK_*",
        "failure_pattern": "FAILED_*"
      },
      "SessionID": {
        "must_match": "vc_SessionID",
        "json_path": "$.V2G_Message.Header.SessionID"
      }
    }
  }
}
```

### Step 5: 验证指令完整性

在输出前, 检查:
1. ✅ 所有需要的状态变量是否都已提供
2. ✅ JSON 结构是否与 messages.json 一致
3. ✅ TTCN-3 表达式是否类型兼容 (octetstring -> charstring)
4. ✅ 是否遵循 PascalCase 命名规范
```

---

### 4.2 Core Agent Prompt 精简

**目标文件**: `agent/test_code/prompts/core_agent_prompt.txt`

**移除内容** (已过时/矛盾):

```diff
- ### HARD RULE #2 — V2G body messages MUST use structured-union send
- (整个章节 - 已移除, Dumb Terminal 架构统一)

- ## Session ID Persistence — FINAL (Sprint 4)
- (整个章节 - 已迁移到 KB)

- ### RULE 1 — Namespace Whitelist (NPE if violated)
- (整个章节 - 已迁移到 KB generation_patterns.json)

- ### RULE 2 — PascalCase JSON Keys (Silent field drop if violated)
- (整个章节 - 已迁移到 KB)

- ### RULE 3 — hexBinary = Hex String
- (整个章节 - 已迁移到 KB)

- ### RULE 4 — V2G_Message Structure
- (整个章节 - 已迁移到 KB)

- ### Canonical Correct Examples (verified by smoke_test, 5/5 PASS)
- (整个章节 - 已迁移到 KB generation_patterns.json)

- ## Session ID Persistence — FINAL (Sprint 4)
- (整个章节 - 已迁移到 KB)
```

**新增内容** (在文件开头添加 Protocol Reasoning 引用):

```markdown
## Protocol Context — From Summarizer (新增)

**重要**: 你的输入现在包含一个 `reasoning.json` 对象, 由升级版 Summarizer 生成。

这个 `reasoning` 对象包含:
- `state_variables_needed`: 需要的状态变量列表
- `injection_rules`: 如何在 JSON 中注入这些变量
- `send_instruction`: 消息的 JSON 结构
- `receive_instruction`: 如何验证响应

**你不需要理解协议状态机** - Summarizer 已经为你完成了这部分推理。

**你的职责**:
1. 按照 `reasoning.send_instruction` 构建 JSON 字符串
2. 正确使用 `oct2char(vc_SessionID)` 注入状态变量
3. 按照 `reasoning.receive_instruction` 验证响应
4. 生成语法正确的 TTCN-3 代码

**禁止**:
- 猜测 SessionID 在 JSON 中的位置 - 使用 `reasoning.injection_rules`
- 硬编码偏移量 - 使用 `reasoning.json_path`
- 忘记将 `octetstring` 转换为 `charstring`
```

**精简后的核心规则** (只保留 TTCN-3 语法相关):

```markdown
## TTCN-3 语法规则 (保留)

### 1. 字符串字面量中的引号转义

TTCN-3 使用 `""` (双引号) 转义字面量中的引号:

```ttcn3
// 正确: 在 charstring 字面量中表示 "
var charstring json := "{\"V2G_Message\":{...}}";

// 错误: 使用 \" 不被 TTCN-3 识别为转义
var charstring json := "{\\"V2G_Message\\":{...}}";
```

### 2. octetstring 与 charstring 转换

```ttcn3
// octetstring -> charstring: 使用 oct2char()
var charstring s := oct2char(vc_SessionID);

// charstring -> octetstring: 使用 char2oct()
var octetstring o := char2oct(s);

// 错误: 不能直接拼接 octetstring 和 charstring
var charstring bad := "..." & vc_SessionID & "...";  // 编译错误
```

### 3. 单行 JSON 字面量 (避免括号不平衡)

```ttcn3
// 正确: 单行字面量
var charstring json := "{\"V2G_Message\":{\"Header\":{\"SessionID\":\"0000000000000000\"}}}";

// 错误: 多行拼接导致括号不平衡
var charstring json := "{\"V2G_Message\":{" &
    "\"Header\":{\"SessionID\":\"0000000000000000\"}}";
```

### 4. Pattern Matching 中的引号

```ttcn3
// 正确: 使用 @nocase 和 @"..." 模式
if (match(json, @nocase @"*\"\"ResponseCode\"\":\"\"OK*")) { ... }

// 错误: 使用 \" 转义
if (match(json, "*\"ResponseCode\":\"OK*")) { ... }
```
```

---

### 4.3 Infrastructure Agent Prompt 收窄

**目标文件**: `agent/test_code/prompts/infrastructure_agent_prompt.txt`

**新增边界约束** (在文件开头添加):

```markdown
## Agent 边界约束 (新增)

### 绝对禁止

1. **禁止修改任何函数文件**:
   - ❌ `f_*_PR_*.ttcn` (前置条件函数)
   - ❌ `f_*_PO_*.ttcn` (后置条件函数)
   - ❌ `f_*_TB_*.ttcn` (测试行为函数)
   - ❌ `TC_*.ttcn` (测试用例模块)

2. **禁止修改 Core Agent 生成的代码**:
   - 即使编译失败, 也不要修改 PR/TB/TC 函数代码
   - 只修复 Infrastructure 层面的问题 (端口声明、组件定义等)

3. **禁止处理协议逻辑**:
   - ❌ 不推理 SessionID 提取
   - ❌ 不处理状态变量注入
   - ❌ 不验证 ResponseCode

### 绝对职责

1. **生成项目骨架**:
   - `ComponentsAndPorts.ttcn`
   - `Types_*.ttcn`
   - `Templates_*.ttcn`
   - `PICS_*.ttcn`
   - `PIXIT_*.ttcn`

2. **管理 C++ Adapter**:
   - 从 `shared_infrastructure/exi_microservice` 复制适配器
   - 管理 Port 实现文件

3. **配置编译环境**:
   - `*.cfg` 文件
   - `Makefile`
   - `build.sh`

### 错误处理边界

如果编译错误涉及函数代码:
1. 标记错误类型为 `CORE_AGENT_SCOPE`
2. 交回给 Core Agent 修复
3. 不要尝试自己修复
```

---

### 4.4 Fault Corrector Prompt 增强

**目标文件**: `agent/test_code/prompts/fault_corrector_prompt.txt`

**新增语义级错误分类** (在现有错误分类部分后添加):

```markdown
## 语义级错误分类 (新增)

### 核心原则

将错误分为**协议层**和**代码层**, 而不是简单的"编译错误 vs 运行时错误"。

### 协议层错误

这些错误需要理解协议状态机:

#### 1. SessionID 相关错误

**症状**:
- SECC 日志: `SessionID 0000000000000000 does not match session ID XXXX`
- EXI 日志: `DECODE ERROR: Premature EOS`

**根因**:
- PR_SessionSetup 没有正确提取 SessionID
- 后续消息使用了错误的 SessionID

**推理步骤**:
```
1. 检查 PR_SessionSetup 是否提取了 vc_SessionID
2. 检查后续消息是否使用 oct2char(vc_SessionID) 注入
3. 检查 JSON 中 SessionID 的位置是否正确 (使用 reasoning.json_path)
```

**修复建议**:
```json
{
  "fix_type": "EXTRACT_SESSION_ID",
  "location": "f_SECC_CMN_PR_SessionSetup_001",
  "change": "添加 SessionID 提取逻辑",
  "reasoning": "PR_SessionSetup 必须在 receive 后提取 SessionID"
}
```

#### 2. ResponseCode 验证错误

**症状**:
- TTCN-3 setverdict(fail) 但没有明确的 ResponseCode 检查

**修复建议**:
```json
{
  "fix_type": "ADD_RESPONSE_CODE_CHECK",
  "location": "f_*_TB_*.ttcn",
  "change": "在 receive 后添加 ResponseCode 验证",
  "pattern": "match(resp_json, @nocase @\"*\"\"ResponseCode\"\":\"\"OK*\"\")"
}
```

#### 3. 前置条件不满足

**症状**:
- 测试在中间步骤失败
- SECC 返回 `FAILED_UnknownSession`

**推理步骤**:
```
1. 检查 precondition_chain 是否完整
2. 检查每个 PR 函数是否正确执行
3. 检查状态变量是否正确传递
```

### 代码层错误

这些错误与协议无关, 只与 TTCN-3 语法相关:

#### 1. 类型不匹配

```json
{
  "error_type": "TYPE_MISMATCH",
  "example": "charstring & octetstring — expression does not match template type",
  "fix": "使用 oct2char() 或 char2oct() 进行类型转换"
}
```

#### 2. 括号不平衡

```json
{
  "error_type": "BRACE_IMBALANCE",
  "example": "org.json.JSONException: Expected a ',' or '}' at character X",
  "fix": "使用单行 JSON 字面量, 或使用 JSON 验证工具"
}
```

#### 3. 引号转义错误

```json
{
  "error_type": "QUOTE_ESCAPE",
  "example": "TITAN 编译错误或 JSON 解析失败",
  "fix": "使用 \"\" (双引号) 而不是 \\\" (反斜杠引号)"
}
```

### 错误分析输出格式

```json
{
  "analysis": {
    "error_type": "PROTOCOL_LAYER | CODE_LAYER",
    "error_category": "SessionID | ResponseCode | Precondition | TypeMismatch | BraceImbalance | QuoteEscape",
    "root_cause": "详细描述根因",
    "reasoning_chain": [
      "观察: ...",
      "推理: ...",
      "结论: ..."
    ]
  },
  "fix_recommendation": {
    "responsible_agent": "CoreAgent | InfrastructureAgent",
    "specific_fix": "具体的修复建议",
    "avoid_doing": "不应该做的事情"
  }
}
```
```

---

## 5. Pipeline 修改方案

### 5.1 修改 Summarizer 调用 (ttcn3_generation_pipeline.py)

在 `run_summarizer()` 函数中增加 Protocol Reasoning:

```python
# 在 ttcn3_generation_pipeline.py 中修改
# 位置: ~L5269 run_summarizer() 函数

def run_summarizer(
    generation_plan: Dict,
    kb_code_for_prompt: str,
    few_shot_examples: Dict,
    local_ref_section: str,
) -> Dict:
    """
    增强版 Summarizer: 包含 Protocol Reasoning 功能

    新增输出:
    - reasoning_json: 结构化的语义指令
    - state_context: 状态机上下文
    """

    # 1. 构建 Protocol Reasoning Prompt
    protocol_reasoning_prompt = f"""
    你现在是 Protocol Reasoning Agent。

    给定:
    - tc_id: {generation_plan.get('tc_id')}
    - message_type: {generation_plan.get('message_type')}
    - precondition_chain: {generation_plan.get('precondition_chain')}

    参考 state_machine 和 cross_step_state (来自 generation_patterns.json):
    {local_ref_section}

    请执行 5 步推理并输出 reasoning.json。
    """

    # 2. 调用 LLM 生成语义指令
    reasoning_response = call_llm(
        prompt=protocol_reasoning_prompt,
        model="claude",
        system="你是 Protocol Reasoning Agent..."
    )

    # 3. 解析 reasoning.json
    reasoning_json = parse_json_response(reasoning_response)

    # 4. 构建增强的 references
    references = {
        "reference_testcase": ...,  # 现有逻辑
        "reference_test_behavior": ...,  # 现有逻辑
        "reasoning_json": reasoning_json,  # 新增
        "state_context": reasoning_json.get("reasoning", {}),  # 新增
    }

    return references
```

### 5.2 修改 Core Agent 调用

```python
# 在 ttcn3_generation_pipeline.py 中修改
# 位置: ~L5366 run_core_agent() 函数

def run_core_agent(
    generation_plan: Dict,
    references: Dict,  # 现在包含 reasoning_json
    few_shot_examples: Dict,
    sop: Dict,
    local_ref_section: str,
) -> Dict:
    """
    精简版 Core Agent: 只负责代码生成

    输入变化:
    - references 现在包含 reasoning_json
    - references.state_context 包含状态机上下文
    """

    # 构建增强的 Prompt
    core_prompt = f"""
    {basic_instructions}

    ## Protocol Reasoning Context (来自 Summarizer)
    {json.dumps(references.get('reasoning_json', {}), indent=2)}

    ## 状态变量注入规则
    {references.get('state_context', {})}

    请按照 reasoning_json 生成 TTCN-3 代码。
    不要猜测协议状态机逻辑 - 遵循 reasoning_json 中的指令。
    """

    # 调用 LLM
    core_response = call_llm(
        prompt=core_prompt,
        model="claude",
        system="你是 TTCN-3 Coder..."
    )

    # 解析并返回
    return parse_json_response(core_response)
```

---

## 6. 实施清单

### 6.1 文件修改顺序

| 步骤 | 文件 | 修改类型 | 工作量 |
|------|------|----------|--------|
| 1 | `generation_patterns.json` | 扩展 | 1 小时 |
| 2 | `summarizer_prompt.txt` 或 pipeline | 新增 Protocol Reasoning | 2 小时 |
| 3 | `core_agent_prompt.txt` | 精简 + 新增引用 | 1 小时 |
| 4 | `infrastructure_agent_prompt.txt` | 新增边界约束 | 30 分钟 |
| 5 | `fault_corrector_prompt.txt` | 新增语义分类 | 1 小时 |
| 6 | `ttcn3_generation_pipeline.py` | 集成 reasoning_json | 2 小时 |
| 7 | 端到端测试 | - | 2 小时 |

### 6.2 测试验证

```bash
# 测试 1: 状态机推理正确性
python3 -c "
from ttcn3_generation_pipeline import run_summarizer
result = run_summarizer({'tc_id': 'TC_SECC_CMN_VTB_PaymentServiceSelection_001'})
assert 'reasoning_json' in result
assert 'vc_SessionID' in result['reasoning_json']['reasoning']['state_variables_needed']
print('✅ Protocol Reasoning 测试通过')
"

# 测试 2: 端到端生成
python3 agent/test_code/batch_runner.py \
  --tc-ids TC_SECC_CMN_VTB_PaymentServiceSelection_001 \
  --per-case-timeout 3000
```

---

## 7. 预期效果

### 7.1 Token 成本优化

| 组件 | 修改前 | 修改后 | 变化 |
|------|--------|--------|------|
| Core Agent Prompt | ~30KB | ~15KB | -50% |
| Summarizer | ~5KB | ~10KB | +100% |
| **总计** | ~35KB | ~25KB | **-29%** |

### 7.2 可维护性提升

- 协议知识集中在 `generation_patterns.json`
- Agent 职责边界清晰
- 错误分类有语义含义

### 7.3 可扩展性

- 新增协议只需扩展 `generation_patterns.json`
- 无需修改 Agent Prompt
- 无需新增 Python 引擎

---

## 8. 风险与缓解

### 风险 1: Summarizer 输出不稳定

**缓解**: 使用 Few-Shot 示例引导 LLM 输出稳定的 JSON 格式

### 风险 2: reasoning_json 格式错误

**缓解**: 在 pipeline 中添加 JSON Schema 验证, 无效时回退到简单推理

### 风险 3: Core Agent 不遵循 reasoning_json

**缓解**: 在 Prompt 中强调 "不要猜测, 只遵循 reasoning_json"

---

**文档版本**: v1.0
**状态**: 待实施
**下一步**: 确认方案后开始 Phase 1 (扩展 generation_patterns.json)
