# 测试用例生成 Agent 重构 TODO

## 一、背景与目标

### 现状问题
1. **test_case_generator.py** 通过 OpenAI GPT-4 API 调用生成测试用例，不符合 Claude Code Agent 架构
2. **iso15118_preprocessor_enhanced.py** 缺少 figure/table 引用提取，需求中提到的图表无法自动关联
3. **iso15118_2_hierarchical_tree.json** 缺少 figure/table 索引，v2g_messages 只提取了 4 条，timing_params 只有 14 条
4. 当前只支持"单条需求 → 单个测试用例"模式，不支持"多条需求 → 一个综合测试用例"
5. 缺少论文中的低层建模（Packet Field / FSM / Message Time Sequence / Protocol-Specific Functional Modeling）
6. 缺少论文中的测试用例验证与迭代精化机制（Coverage Breadth + Depth）

### 目标架构
参考 NeTestLLM 论文 §4-§7 和现有 test_code/ 的 Claude Code Agent 模式：
- 测试用例生成改为 **Claude Code Agent 子进程调用**（`claude -p`）
- 多 Agent 协作：Orchestrator（意图分解）→ Modeler（低层建模）→ Generator（用例生成）→ Verifier（覆盖验证）
- 支持单需求和多需求两种生成模式
- 自动检索 figure/table 注入上下文

---

## 二、文件修改清单

### 2.1 `document/iso15118_preprocessor_enhanced.py` — 增强预处理器

#### 新增功能

**A. Figure/Table 引用提取**
- 扫描 markdown 全文，构建 figure 索引：`{figure_num: {title, image_paths[], line_num}}`
  - 注意多页 figure（如 figure_105_1.png, figure_105_2.png）
  - 匹配模式：`^#*\s*Figure\s+(\d+)\s*[—–-]\s*(.+)$`
- 构建 table 索引：`{table_num: {title, file_path, line_num}}`
  - 匹配模式：`^Table\s+(\d+)\s*[—–-]\s*(.+)$`
  - table 文件路径：`document/table/table_{N}.md`

**B. 需求-图表关联**
- 在 `_extract_requirements()` 中，扫描需求上下文（前后 500 字符）中的 Figure/Table 引用
  - 匹配模式：`Figure\s+(\d+)`, `Table\s+(\d+)`
- 每条需求增加字段：
  ```json
  {
    "requirement_id": "V2G2-082",
    "content": "...",
    "section": "7.8.3.1",
    "referenced_figures": [8, 9],
    "referenced_tables": [9, 10]
  }
  ```

**C. 章节-图表关联**
- 在 Section.to_dict() 中增加 `figures` 和 `tables` 字段
- 扫描章节 content 中出现的所有 Figure N / Table N 引用

**D. 输出 JSON 增加顶层索引**
```json
{
  "figure_index": {
    "1": {"title": "Communication relationship...", "image_paths": ["image/figure_1.png"], "line_num": 178},
    "105": {"title": "Overview AC...", "image_paths": ["image/figure_105_1.png", "image/figure_105_2.png"], "line_num": 6548}
  },
  "table_index": {
    "1": {"title": "Basic Certificate Fields", "file_path": "table/table_1.md", "line_num": 880}
  }
}
```

**E. 改进消息定义提取**
- 当前 `_extract_message_def()` 只匹配了 4 条消息，需要改进匹配逻辑
- 增加对 Schema Diagram 章节标题的匹配（如 "Schema Diagram – SessionSetupReq"）
- 增加对 Table 中 "Semantics and type definition for XXXReq/Res" 的匹配

**F. 改进时序参数提取**
- 当前只提取了 14 条，需要增加对 Table 108-111 中定义的所有 timer 参数的提取
- 匹配 `par_V2G_*`, `V2G_EVCC_Msg_*`, `V2G_SECC_Msg_*` 等命名模式

#### 修改 main() 路径
```python
input_file = "/data/cdy/code/v2g2_082/iso15118/agent/document/MinerU_markdown_ISO_15118-2-2014_2034243645514502144.md"
output_file = "/data/cdy/code/v2g2_082/iso15118/agent/document/iso15118_2_hierarchical_tree.json"
```

---

### 2.2 `test_case/test_case_generator.py` — 完全重写为 Agent 模式

#### 架构设计（参考论文 §4-§6 + test_code/ttcn3_generation_pipeline.py）

```
输入: 需求ID(s) + 模式(single/multi)
  │
  ├─ [预处理] 加载 hierarchical_tree.json, 检索需求上下文
  │   └─ 自动检索关联的 figure/table 内容
  │
  ├─ [Agent 1: Orchestrator] 意图分解
  │   ├─ 单需求模式: 分析单条需求 → 确定测试类型/SUT/消息类型
  │   └─ 多需求模式: 分析多条需求的关联性 → 确定综合测试目标
  │
  ├─ [Agent 2: Modeler] 低层建模（论文 §4.2）
  │   ├─ Packet Field Modeling: 提取消息字段约束
  │   ├─ FSM Modeling: 提取状态转换
  │   ├─ Message Time Sequence: 提取时序逻辑
  │   └─ Protocol-Specific Functional: 提取功能测试点
  │
  ├─ [Agent 3: Generator] 测试用例生成（论文 §5.1）
  │   ├─ 遍历 Modeler 输出的 testing points
  │   ├─ 注入: 协议摘要 + 模块元数据 + 章节内容 + figure/table + few-shot
  │   ├─ 单需求: 生成 1+ 个测试用例（GoodCase + BadCase）
  │   └─ 多需求: 生成 1 个综合测试用例（覆盖所有需求的交互场景）
  │
  └─ [Agent 4: Verifier] 覆盖验证（论文 §5.2）
      ├─ Breadth: 检查是否覆盖所有重要章节
      ├─ Depth: LLM-as-judge 评估基本功能 + 边界条件覆盖
      └─ 不足时: 反馈给 Generator 补充/精化
```

#### 核心类和方法

```python
class TestCaseAgent:
    """测试用例生成 Agent - 基于 Claude Code CLI"""
    
    def __init__(self, protocol_json_path, image_dir, table_dir):
        """初始化，加载协议数据和图表索引"""
    
    # === 图表检索 ===
    def resolve_figure_references(self, figure_nums: List[int]) -> List[Dict]:
        """根据 figure 编号列表，返回图片路径和标题（支持多页）"""
    
    def resolve_table_references(self, table_nums: List[int]) -> str:
        """根据 table 编号列表，读取并返回 table markdown 内容"""
    
    def get_requirement_with_context(self, req_id: str) -> Dict:
        """获取需求详情 + 关联的 figure/table 内容"""
    
    # === Agent 调用 ===
    def call_agent(self, prompt: str, agent_name: str, timeout: int = 180) -> str:
        """调用 Claude Code CLI 子进程"""
    
    # === 4 个 Agent ===
    def run_orchestrator(self, requirements: List[Dict], mode: str) -> Dict:
        """Agent 1: 意图分解
        - mode='single': 单需求分析
        - mode='multi': 多需求关联分析
        输出: 生成计划 JSON
        """
    
    def run_modeler(self, orchestrator_plan: Dict, section_content: str) -> Dict:
        """Agent 2: 低层建模
        - 根据 plan 中的消息类型，提取 packet fields / FSM / time sequence
        - 使用 focus-moving 机制遍历相关章节
        输出: testing_points 列表
        """
    
    def run_generator(self, plan: Dict, testing_points: List, 
                      context: Dict, mode: str) -> List[Dict]:
        """Agent 3: 测试用例生成
        - context 包含: 协议摘要, 模块元数据, 章节内容, figure/table, few-shot
        - mode='single': 为每个 testing point 生成测试用例
        - mode='multi': 综合所有 testing points 生成一个测试用例
        输出: 测试用例 JSON 列表
        """
    
    def run_verifier(self, test_cases: List[Dict], requirements: List[Dict]) -> Dict:
        """Agent 4: 覆盖验证
        - 评估 breadth (章节覆盖) + depth (功能+边界覆盖)
        - 输出: 覆盖评分 + 改进建议
        """
    
    # === 主流程 ===
    def generate_single(self, requirement_id: str) -> List[Dict]:
        """单需求模式: 一条需求 → 多个测试用例（GoodCase + BadCase）"""
    
    def generate_multi(self, requirement_ids: List[str]) -> Dict:
        """多需求模式: 多条需求 → 一个综合测试用例"""
    
    def generate_batch(self, requirement_ids: List[str]) -> List[Dict]:
        """批量模式: 多条需求 → 逐个生成（兼容旧行为）"""
```

#### 命令行接口

```bash
# 单需求模式（生成 GoodCase + BadCase）
python test_case_generator.py --requirement V2G2-082

# 多需求综合模式（多条需求 → 一个综合测试用例）
python test_case_generator.py --requirements V2G2-082,V2G2-083,V2G2-084 --mode multi

# 批量模式（多条需求 → 逐个生成，兼容旧行为）
python test_case_generator.py --requirements V2G2-082,V2G2-165 --mode batch

# 指定 SUT 类型
python test_case_generator.py --requirement V2G2-082 --sut SECC
```

#### 多需求综合测试用例格式

```json
{
  "Test_Case": {
    "TC_Id": "TC_SECC_CMN_VTB_V2GTPHeader_001",
    "Test_Objective": "综合验证 V2GTP 消息头结构、字节序和载荷类型的合规性",
    "Test_Description": "...",
    "Document_Reference": {
      "Document": "ISO:15118-2:2014:IS",
      "Section": "15118-2:7.8.3.1",
      "Requirements": ["[V2G2-082]", "[V2G2-083]", "[V2G2-084]", "[V2G2-085]"]
    },
    "Related_Figures": [
      {"figure_num": 8, "title": "V2GTP Message structure", "paths": ["image/figure_8.png"]},
      {"figure_num": 9, "title": "V2GTP Message header structure", "paths": ["image/figure_9.png"]}
    ],
    "Related_Tables": [
      {"table_num": 9, "title": "Generic V2GTP header structure", "path": "table/table_9.md"},
      {"table_num": 10, "title": "Overview on V2GTP payload types", "path": "table/table_10.md"}
    ],
    "Config_Id": "CF_04_001",
    "PICS_Selection": {"PICS_CMN_CMN_V2gtpV2g": true},
    "PreCondition": "f_SECC_CMN_PR_SupportedAppProtocol_001",
    "Test_Sequence": [
      {"Step": 1, "Name": "...", "Instruction": "...", "Traced_Requirements": ["V2G2-082"]},
      {"Step": 2, "Name": "...", "Instruction": "...", "Traced_Requirements": ["V2G2-083", "V2G2-085"]}
    ],
    "Expected_Behavior": "...",
    "parameters": {}
  }
}
```

#### Prompt 模板设计

需要新建以下 prompt 文件到 `test_case/prompts/` 目录：

1. **orchestrator_prompt.txt** — 意图分解
   - 输入: 需求列表 + 需求上下文 + 关联图表
   - 输出: 生成计划 JSON（测试类型、SUT、消息类型、前置条件链、关联需求分组）
   - 多需求模式下需要分析需求间的关联性（同一消息流、同一状态机、同一安全机制等）

2. **modeler_prompt.txt** — 低层建模
   - 输入: 生成计划 + 章节全文 + figure/table 内容
   - 输出: testing_points JSON 列表
   - 包含 4 种建模类型的指令

3. **generator_prompt.txt** — 测试用例生成
   - 输入: 生成计划 + testing_points + 协议摘要 + 模块元数据 + few-shot
   - 输出: 测试用例 JSON
   - 单需求模式: 为每个 testing point 生成
   - 多需求模式: 综合所有 testing points 生成一个

4. **verifier_prompt.txt** — 覆盖验证
   - 输入: 测试用例列表 + 需求列表 + 章节内容
   - 输出: 覆盖评分 + 改进建议 JSON

#### Figure/Table 注入策略

当需求引用了 figure 或 table 时：
1. **Figure**: 在 prompt 中以文本描述 + 图片路径的方式注入
   ```
   [Figure 8 — V2GTP Message structure]
   图片路径: image/figure_8.png
   （此图展示了 V2GTP PDU 的 header 和 body 结构）
   ```
   - 注意：Claude Code CLI 目前不支持直接传图片，所以 figure 主要作为引用信息
   - 但 figure 标题和上下文文本可以提供有价值的语义信息

2. **Table**: 直接读取 `table/table_N.md` 的 HTML 内容注入 prompt
   ```
   [Table 9 — Generic V2GTP header structure]
   <table>...</table>
   ```
   - Table 内容对测试用例生成非常关键（字段定义、取值范围、语义说明）

3. **多页 Figure**: 列出所有页的路径
   ```
   [Figure 105 — Overview AC Request-Response Message Sequence EIM (多页)]
   图片路径: image/figure_105_1.png, image/figure_105_2.png
   ```

---

### 2.3 重新生成 `document/iso15118_2_hierarchical_tree.json`

运行改进后的预处理器，生成包含以下新字段的 JSON：
- `figure_index`: 全部 figure 索引（含多页）
- `table_index`: 全部 table 索引
- 每条 requirement 增加 `referenced_figures` 和 `referenced_tables`
- 每个 section 增加 `figures` 和 `tables` 列表
- 改进的 `v2g_messages`（目标 50+ 条）
- 改进的 `v2g_timing_params`（目标 30+ 条）

---

### 2.4 更新 `CLAUDE.md`

增加以下内容：
- 测试用例生成 Agent 架构说明
- 单需求 / 多需求 / 批量模式的使用方法
- Figure/Table 自动检索机制说明
- 新的目录结构（test_case/prompts/）

---

### 2.5 更新 `test_code/ttcn3_generation_pipeline.py`

- 修改 `find_test_case_for_requirement()` 以适配新的测试用例 JSON 格式
- 增加对多需求综合测试用例的支持（一个测试用例追溯多条需求）
- 增加 figure/table 上下文传递给 Core Agent

---

## 三、实现优先级

| 优先级 | 任务 | 文件 | 预计工作量 |
|--------|------|------|-----------|
| P0 | 增强预处理器（figure/table 索引 + 需求关联） | iso15118_preprocessor_enhanced.py | 中 |
| P0 | 重新生成 hierarchical_tree.json | 运行脚本 | 小 |
| P0 | 重写 test_case_generator.py 为 Agent 模式 | test_case_generator.py | 大 |
| P0 | 新建 4 个 prompt 模板 | test_case/prompts/*.txt | 中 |
| P1 | 增加多需求综合生成模式 | test_case_generator.py | 中 |
| P1 | 更新 ttcn3_generation_pipeline.py 适配 | ttcn3_generation_pipeline.py | 小 |
| P2 | 更新 CLAUDE.md | CLAUDE.md | 小 |
| P2 | 增加覆盖验证与迭代精化 | test_case_generator.py | 中 |

---

## 四、关键设计决策

### 4.1 为什么用 Claude Code CLI 而不是 API？
- 与 test_code/ 的 Agent 架构保持一致
- Claude Code CLI 支持更丰富的工具调用（文件读写、搜索等）
- 无需管理 API key，直接复用本地 Claude Code 环境
- 子 Agent 可以自主探索代码库和文档

### 4.2 多需求综合测试用例 vs 批量逐个生成
- **批量逐个（batch）**: 每条需求独立生成，适合需求间无关联的场景
- **综合生成（multi）**: 多条需求合并为一个测试用例，适合：
  - 同一消息流中的多个约束（如 V2G2-082~V2G2-088 都是 V2GTP header 相关）
  - 同一状态机的多个转换条件
  - 同一安全机制的多个验证点
- Orchestrator Agent 负责判断需求间的关联性并决定分组策略

### 4.3 Figure 的使用策略
- Claude Code CLI 不支持直接传图片给模型
- Figure 的价值在于：标题提供语义信息、上下文文本描述图的内容
- 对于 Schema Diagram 类 figure，对应的 Table 通常包含完整的字段定义，Table 更有价值
- 对于状态机图（Figure 6/7/101-104），需要依赖 FSM Modeler 从文本中提取状态转换

### 4.4 低层建模的简化
- 论文中的 4 种建模是针对通用网络协议设计的
- 对于 ISO 15118-2，最关键的是：
  - **Packet Field Modeling**: 消息字段约束（对应 Table 中的 Semantics and type definition）
  - **FSM Modeling**: V2G 通信状态机（Figure 6/7 + 文本描述）
  - **Message Time Sequence**: 消息时序（Table 108-111 + Figure 98-100）
- Protocol-Specific Functional Modeling 可以简化为对安全机制（TLS/证书/签名）的专项建模

---

## 五、测试验证计划

### 5.1 单需求测试
```bash
python test_case_generator.py --requirement V2G2-082
# 预期: 生成 V2GTP header 相关的 GoodCase + BadCase 测试用例
# 验证: 测试用例引用了 Figure 8/9, Table 9/10
```

### 5.2 多需求综合测试
```bash
python test_case_generator.py --requirements V2G2-082,V2G2-083,V2G2-084,V2G2-085 --mode multi
# 预期: 生成一个综合测试用例，覆盖 V2GTP header 的结构、字节序、载荷类型
# 验证: 测试用例的 Test_Sequence 中每步都追溯到具体需求
```

### 5.3 端到端测试
```bash
# Step 1: 生成测试用例
python test_case/test_case_generator.py --requirement V2G2-082
# Step 2: 生成 TTCN-3 代码
python test_code/ttcn3_generation_pipeline.py --requirement V2G2-082
# 验证: 两步流水线正常衔接
```

---

## 六、参考资料

- **NeTestLLM 论文**: `document/MinerU_markdown_Automated_Network_Protocol_Testing_with_LLM_Agent_*.md`
  - §4: Hierarchical RFC Understanding（层次化协议理解）
  - §5: Test Case Generation with Verification（测试用例生成与验证）
  - §6: Executable Artifact Generation（可执行制品生成 — 多 Agent 架构）
  - §7: Runtime Feedback Analysis（运行时反馈 — 大小循环）
- **现有 Code Agent 实现**: `test_code/ttcn3_generation_pipeline.py`（4-Agent 架构参考）
- **ISO 15118-2 文档**: `document/MinerU_markdown_ISO_15118-2-2014_*.md`
- **ISO 15118-4 知识库**: `document/iso15118_4_ttcn_knowledge_base.json`
- **提取的图表**: `document/image/` (112 figures) + `document/table/` (112 tables)
