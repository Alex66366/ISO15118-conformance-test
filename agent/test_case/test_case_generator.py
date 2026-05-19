#!/usr/bin/env python3
"""
ISO 15118-2 测试用例生成器 - 4-Agent 架构 (基于 NeTestLLM 论文方法)

架构:
  Orchestrator → Modeler → Generator → Verifier

调用方式: Claude Code CLI (subprocess)
支持模式: single (单需求) / multi (多需求关联)
"""

import json
import re
import os
import sys
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime


# ==================== 路径配置 ====================
BASE_DIR = Path(__file__).parent
PROMPT_DIR = BASE_DIR / "prompts"
DOCUMENT_DIR = BASE_DIR.parent / "document"

PROTOCOL_JSON = DOCUMENT_DIR / "iso15118_2_hierarchical_tree.json"
IMAGE_DIR = DOCUMENT_DIR / "image"
TABLE_DIR = DOCUMENT_DIR / "table"

DEFAULT_OUTPUT_DIR = BASE_DIR / "output"
DEFAULT_AGENT_TIMEOUT = 180  # seconds
GENERATOR_TIMEOUT = 300  # Generator needs more time for complex multi-point generation
GENERATOR_RETRIES = 1  # Allow one retry on timeout


# ==================== 日志 ====================
def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")


# ==================== Claude CLI 调用 ====================
def call_claude_agent(prompt: str, agent_name: str, timeout: int = DEFAULT_AGENT_TIMEOUT) -> str:
    """调用 Claude Code CLI 作为 sub-agent"""
    log(f"调用 {agent_name} Agent (timeout={timeout}s)...")

    # Windows subprocess requires .cmd extension for batch-file lookups
    claude_cmd = "claude.cmd" if os.name == "nt" else "claude"

    try:
        result = subprocess.run(
            [claude_cmd, "-p", prompt, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            log(f"{agent_name} Agent 返回错误码 {result.returncode}", "ERROR")
            if result.stderr:
                log(f"stderr: {result.stderr[:500]}", "ERROR")
            return ""

        # 解析 JSON 输出，提取 result 字段
        try:
            output = json.loads(result.stdout)
            text = output.get("result", result.stdout)
        except json.JSONDecodeError:
            text = result.stdout

        log(f"{agent_name} Agent 完成，输出 {len(text)} 字符")
        return text.strip()

    except subprocess.TimeoutExpired:
        log(f"{agent_name} Agent 超时 ({timeout}s)", "ERROR")
        return ""
    except FileNotFoundError:
        log("claude CLI 未找到，请确认已安装 Claude Code", "ERROR")
        sys.exit(1)


def extract_json_from_response(text: str) -> Optional[Dict]:
    """从 Agent 响应中提取 JSON 对象，支持截断响应恢复。

    解析策略（3 层）：
      1. 直接解析
      2. 从 markdown JSON 代码块解析
      3. 从 markdown JSON 代码块解析 + 截断恢复（花括号平衡追踪）
    """
    if not text:
        return None

    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试从 markdown 代码块中提取
    json_block = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if json_block:
        block_content = json_block.group(1)
        try:
            return json.loads(block_content)
        except json.JSONDecodeError:
            pass
        # 截断恢复：使用花括号平衡追踪尝试恢复截断的 JSON
        recovered = _recover_truncated_json(block_content)
        if recovered:
            log("Phase 1 JSON 截断恢复成功", "WARN")
            return recovered

    return None


def _recover_truncated_json(text: str) -> Optional[Dict]:
    """尝试恢复截断的 JSON 响应（花括号平衡追踪）"""
    first_brace = text.find('{')
    if first_brace == -1:
        first_brace = text.find('[')
        if first_brace == -1:
            return None

    candidate = text[first_brace:]
    depth = 0
    in_string = False
    escape_next = False
    last_complete_pos = -1

    for i, ch in enumerate(candidate):
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                last_complete_pos = i
                break
        elif ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                last_complete_pos = i
                break

    if last_complete_pos >= 0:
        truncated = candidate[:last_complete_pos + 1]
        try:
            return json.loads(truncated)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    # 回退 B：截断最后一个不完整字段
    last_comma = candidate.rfind(',')
    if last_comma > 0:
        for suffix in ['\n  ]\n}', '\n]\n}', '\n}']:
            attempt = candidate[:last_comma] + suffix
            try:
                result = json.loads(attempt)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

    return None


def load_prompt_template(name: str) -> str:
    """加载 prompt 模板"""
    path = PROMPT_DIR / f"{name}_prompt.txt"
    if not path.exists():
        log(f"Prompt 模板不存在: {path}", "ERROR")
        return ""
    return path.read_text(encoding="utf-8")


# ==================== 协议数据访问层 ====================
class ProtocolDataStore:
    """协议数据存储与查询"""

    def __init__(self, json_path: str = str(PROTOCOL_JSON)):
        self.json_path = json_path
        self.data: Dict = {}

    def load(self):
        log(f"加载协议数据: {self.json_path}")
        with open(self.json_path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
        req_count = len(self.data.get("all_requirements", []))
        fig_count = len(self.data.get("figure_index", {}))
        tbl_count = len(self.data.get("table_index", {}))
        log(f"协议数据: {req_count} 需求, {fig_count} figures, {tbl_count} tables")

    def get_protocol_summary(self) -> str:
        return json.dumps(self.data.get("protocol_summary", {}), indent=2, ensure_ascii=False)

    def get_requirement(self, req_id: str) -> Optional[Dict]:
        for r in self.data.get("all_requirements", []):
            if r.get("requirement_id") == req_id:
                return r
        return None

    def get_requirements_by_section(self, section_num: str) -> List[Dict]:
        return [r for r in self.data.get("all_requirements", [])
                if r.get("section", "").startswith(section_num)]

    def find_section(self, section_num: str) -> Optional[Dict]:
        """递归查找章节"""
        def _search(sections: List[Dict], target: str) -> Optional[Dict]:
            for sec in sections:
                if sec.get("section_number") == target:
                    return sec
                child = _search(sec.get("children", []), target)
                if child:
                    return child
            return None
        return _search(self.data.get("section_tree", {}).get("sections", []), section_num)

    def get_section_content(self, section_num: str, max_len: int = 12000) -> str:
        sec = self.find_section(section_num)
        if sec:
            content = sec.get("content", "")
            return content[:max_len] if len(content) > max_len else content
        return ""

    def get_section_summary(self, section_num: str) -> str:
        sec = self.find_section(section_num)
        if sec:
            return (f"Section {sec.get('section_number', '')}: {sec.get('title', '')}\n"
                    f"Classification: {sec.get('classification', 'N/A')}\n"
                    f"Importance: {sec.get('test_importance', 'N/A')}\n"
                    f"Figures: {sec.get('figures', [])}\n"
                    f"Tables: {sec.get('tables', [])}")
        return ""

    def get_module_name(self, section_num: str) -> str:
        top = section_num.split('.')[0] if '.' in section_num else section_num
        module_map = {
            "7": "Basic Requirements (V2G Communication)",
            "8": "Application Layer Messages",
        }
        return module_map.get(top, f"Section {top}")

    def get_figure_info(self, fig_num: int) -> Optional[Dict]:
        return self.data.get("figure_index", {}).get(str(fig_num))

    def get_table_content(self, tbl_num: int) -> str:
        """读取 table markdown 文件内容"""
        tbl_info = self.data.get("table_index", {}).get(str(tbl_num))
        if not tbl_info:
            return ""
        file_path = tbl_info.get("file_path")
        if not file_path:
            return ""
        abs_path = DOCUMENT_DIR / file_path
        if abs_path.exists():
            content = abs_path.read_text(encoding="utf-8")
            return content[:5000]  # 限制长度
        return ""

    def collect_figure_table_context(self, fig_nums: List[int], tbl_nums: List[int]) -> str:
        """收集 figure/table 上下文信息"""
        parts = []
        for fn in fig_nums[:5]:  # 限制数量
            info = self.get_figure_info(fn)
            if info:
                parts.append(f"[Figure {fn}] {info.get('title', '')}")
                for img in info.get("image_paths", []):
                    parts.append(f"  Image: {img}")
        for tn in tbl_nums[:5]:
            tbl_info = self.data.get("table_index", {}).get(str(tn))
            if tbl_info:
                parts.append(f"[Table {tn}] {tbl_info.get('title', '')}")
            content = self.get_table_content(tn)
            if content:
                parts.append(f"  Content:\n{content[:2000]}")
        return "\n".join(parts) if parts else "(no figure/table context)"


# ==================== 4-Agent Pipeline ====================

class TestCaseGeneratorPipeline:
    """4-Agent 测试用例生成管线

    Pipeline:
      1. Orchestrator: 意图分解 → generation_plan
      2. Modeler: 低层建模 → testing_points
      3. Generator: 测试用例生成 → test_cases
      4. Verifier: 覆盖度验证 → verdict
    """

    def __init__(self, store: ProtocolDataStore, output_dir: str = str(DEFAULT_OUTPUT_DIR)):
        self.store = store
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ---- Agent 1: Orchestrator ----
    def run_orchestrator(self, requirement_ids: List[str], mode: str = "single") -> Dict:
        """意图分解 Agent"""
        template = load_prompt_template("orchestrator")
        if not template:
            return {"error": "orchestrator prompt not found"}

        # 收集需求信息
        req_details = []
        all_figs = set()
        all_tbls = set()
        for rid in requirement_ids:
            req = self.store.get_requirement(rid)
            if req:
                req_details.append(req)
                all_figs.update(req.get("referenced_figures", []))
                all_tbls.update(req.get("referenced_tables", []))

        if not req_details:
            return {"error": f"No requirements found for IDs: {requirement_ids}"}

        # 获取章节上下文
        sections = set(r.get("section", "") for r in req_details)
        section_summaries = "\n".join(self.store.get_section_summary(s) for s in sections if s)

        # 构建 prompt
        prompt = template + f"""

## INPUT

**Mode:** {mode}

**Requirements:**
{json.dumps(req_details, indent=2, ensure_ascii=False)}

**Section Summaries:**
{section_summaries}

**Referenced Figures/Tables:**
{self.store.collect_figure_table_context(sorted(all_figs), sorted(all_tbls))}

Please output the generation plan as a JSON object.
"""

        response = call_claude_agent(prompt, "Orchestrator", timeout=DEFAULT_AGENT_TIMEOUT)
        plan = extract_json_from_response(response)

        if not plan:
            log("Orchestrator 未返回有效 JSON，使用默认 plan", "WARN")
            # 构建默认 plan
            first_req = req_details[0]
            plan = {
                "mode": mode,
                "test_type": "GoodCase",
                "sut_type": "SECC",
                "category": "CMN",
                "message_type": self._infer_message_type(first_req),
                "requirement_ids": requirement_ids,
                "sections": list(sections),
                "referenced_figures": sorted(all_figs),
                "referenced_tables": sorted(all_tbls),
                "precondition_chain": [],
                "testing_scope": "single_requirement"
            }

        sut = plan.get('sut_type') or '?'
        mode_in_plan = plan.get('mode', mode)
        if mode_in_plan == 'single':
            test_type = plan.get('test_type') or '?'
        else:
            # multi 模式下没有 test_type，看 grouping_strategy
            test_type = plan.get('grouping_strategy') or 'multi'
        log(f"Orchestrator plan: mode={mode_in_plan}, sut={sut}, strategy={test_type}")
        return plan

    # ---- Agent 2: Modeler ----
    def run_modeler(self, plan: Dict) -> List[Dict]:
        """低层建模 Agent — 提取 testing points"""
        template = load_prompt_template("modeler")
        if not template:
            return []

        # 收集章节内容
        sections = plan.get("sections", [])
        section_contents = []
        for s in sections:
            content = self.store.get_section_content(s)
            if content:
                section_contents.append(f"### Section {s}\n{content}")

        # 收集 figure/table 内容
        fig_tbl_ctx = self.store.collect_figure_table_context(
            plan.get("referenced_figures", []),
            plan.get("referenced_tables", [])
        )

        prompt = template + f"""

## INPUT

**Generation Plan:**
{json.dumps(plan, indent=2, ensure_ascii=False)}

**Section Content:**
{chr(10).join(section_contents)[:15000]}

**Figure/Table Context:**
{fig_tbl_ctx}

Please output the testing_points as a JSON array.
"""

        response = call_claude_agent(prompt, "Modeler", timeout=DEFAULT_AGENT_TIMEOUT)
        result = extract_json_from_response(response)

        if isinstance(result, list):
            testing_points = result
        elif isinstance(result, dict):
            testing_points = result.get("testing_points", [result])
        else:
            log("Modeler 未返回有效 testing_points，使用默认", "WARN")
            testing_points = [{
                "type": "functional",
                "description": f"Verify requirement {plan.get('requirement_ids', ['?'])[0]}",
                "test_approach": "positive",
                "expected_behavior": "Requirement satisfied"
            }]

        log(f"Modeler 提取 {len(testing_points)} 个 testing points")
        return testing_points

    # ---- Agent 3: Generator ----
    def run_generator(self, plan: Dict, testing_points: List[Dict]) -> List[Dict]:
        """测试用例生成 Agent"""
        template = load_prompt_template("generator")
        if not template:
            return []

        # 协议摘要
        protocol_summary = self.store.get_protocol_summary()

        # 模块元数据
        sections = plan.get("sections", [])
        module_name = self.store.get_module_name(sections[0]) if sections else "General"

        prompt = template + f"""

## INPUT

**Generation Plan:**
{json.dumps(plan, indent=2, ensure_ascii=False)}

**Testing Points:**
{json.dumps(testing_points, indent=2, ensure_ascii=False)}

**Protocol Summary:**
{protocol_summary[:3000]}

**Module:** {module_name}

**Figure/Table Context:**
{self.store.collect_figure_table_context(
    plan.get("referenced_figures", []),
    plan.get("referenced_tables", [])
)}

Please output the test cases as a JSON object with a "test_cases" array.
"""

        # Multi 模式下使用更长超时，并支持重试
        timeout = GENERATOR_TIMEOUT
        for attempt in range(GENERATOR_RETRIES + 1):
            response = call_claude_agent(prompt, "Generator", timeout=timeout)
            result = extract_json_from_response(response)

            test_cases = []
            if isinstance(result, dict):
                if "test_cases" in result:
                    test_cases = result["test_cases"]
                elif "Test_Case" in result:
                    test_cases = [result]
                else:
                    test_cases = [result]
            elif isinstance(result, list):
                test_cases = result

            if test_cases:
                break

            if attempt < GENERATOR_RETRIES:
                log(f"Generator 重试 {attempt + 2}/{GENERATOR_RETRIES + 1} ...", "WARN")
                # 缩短 prompt 以加速下次尝试：去掉 figure/table context
                prompt = template + f"""

## INPUT

**Generation Plan:**
{json.dumps(plan, indent=2, ensure_ascii=False)}

**Testing Points:**
{json.dumps(testing_points[:8], indent=2, ensure_ascii=False)[:8000]}

**Protocol Summary:**
{protocol_summary[:1500]}

**Module:** {module_name}

Please output the test cases as a JSON object with a "test_cases" array.
"""
                timeout = 240

        # Generator 完全失败时，使用 fallback 直接从 testing_points 生成
        if not test_cases:
            log("Generator 未能生成测试用例，使用 fallback 机制", "WARN")
            test_cases = self._generate_fallback_test_cases(plan, testing_points)

        log(f"Generator 生成 {len(test_cases)} 个测试用例")
        return test_cases

    def _generate_fallback_test_cases(self, plan: Dict, testing_points: List[Dict]) -> List[Dict]:
        """Fallback: 直接从 testing_points 生成简化测试用例（当 Generator 超时时）"""
        if not testing_points:
            return []

        # 提取元数据
        sut = plan.get("sut_type", "SECC")
        category = plan.get("category", "CMN")
        message_type = plan.get("message_type", "V2GTPHeader")
        req_ids = plan.get("source_requirements", plan.get("requirement_ids", []))
        fig_refs = plan.get("figure_references", [])
        tbl_refs = plan.get("table_references", [])
        precondition = plan.get("precondition_chain", ["f_SECC_CMN_PR_TCP_Connected_001"])
        precond_str = precondition[0] if precondition else "f_SECC_CMN_PR_TCP_Connected_001"

        # 分离 positive 和 negative testing points
        positive_tps = [tp for tp in testing_points if tp.get("test_approach") == "positive"]
        negative_tps = [tp for tp in testing_points if tp.get("test_approach") == "negative"]

        test_cases = []

        # GoodCase (seq 001) - 合并所有 positive points
        if positive_tps:
            seq_steps = []
            for i, tp in enumerate(positive_tps[:5], 1):
                field = tp.get("details", {}).get("field_name", "field")
                desc = tp.get("description", "")
                reqs = tp.get("requirement_basis", [])
                seq_steps.append({
                    "Step": i,
                    "Name": f"Verify {field} Valid",
                    "Instruction": f"Verify SUT correctly handles {field}: {desc}",
                    "Traced_Requirements": reqs
                })

            test_cases.append({
                "Test_Case": {
                    "TC_Id": f"TC_{sut}_{category}_VTB_{message_type}_001",
                    "Test_Objective": f"Verify {sut} correctly processes valid V2GTP header fields per {', '.join(req_ids)}",
                    "Test_Description": f"The test verifies that the {sut} correctly parses and accepts a conformant V2GTP header with all valid field values as specified in {', '.join(req_ids)}.",
                    "Document_Reference": {
                        "Document": "ISO:15118-2:2014:IS",
                        "Section": f"15118-2:{sections[0] if (sections := plan.get('sections', [])) else '8'}",
                        "Requirements": [f"[{rid}]" for rid in req_ids]
                    },
                    "Related_Figures": fig_refs[:3],
                    "Related_Tables": tbl_refs[:3],
                    "Config_Id": f"CF_{category}_001",
                    "PICS_Selection": {f"PICS_{category}_V2GTP_Header": True},
                    "PreCondition": precond_str,
                    "Test_Sequence": seq_steps,
                    "Expected_Behavior": f"f_{sut}_{category}_TB_VTB_{message_type}_001",
                    "parameters": {}
                }
            })

        # BadCase (seq 002+) - 每个 negative point 一个测试用例
        for i, tp in enumerate(negative_tps[:8], 2):
            field = tp.get("details", {}).get("field_name", "field")
            desc = tp.get("description", "")
            reqs = tp.get("requirement_basis", [])
            boundary = tp.get("details", {}).get("boundary_values", [])

            seq_steps = [
                {
                    "Step": 1,
                    "Name": "Establish TCP Connection",
                    "Instruction": "Establish a TCP connection to the SUT.",
                    "Traced_Requirements": reqs[:1] if reqs else ["V2G2-082"]
                },
                {
                    "Step": 2,
                    "Name": f"Send Invalid V2GTP Header",
                    "Instruction": f"Send a V2GTP message with invalid {field}: {desc}",
                    "Traced_Requirements": reqs[:1] if reqs else ["V2G2-082"]
                },
                {
                    "Step": 3,
                    "Name": "Verify Rejection",
                    "Instruction": f"Verify that the SUT rejects or ignores the malformed message. Expected: connection closed or error response.",
                    "Traced_Requirements": reqs[:1] if reqs else ["V2G2-082"]
                }
            ]

            test_cases.append({
                "Test_Case": {
                    "TC_Id": f"TC_{sut}_{category}_VTB_{message_type}_{i:03d}",
                    "Test_Objective": f"Verify {sut} rejects V2GTP header with invalid {field}",
                    "Test_Description": f"The test verifies that the {sut} correctly rejects a V2GTP message with invalid {field} as specified in {', '.join(reqs or req_ids)}.",
                    "Document_Reference": {
                        "Document": "ISO:15118-2:2014:IS",
                        "Section": f"15118-2:{sections[0] if (sections := plan.get('sections', [])) else '8'}",
                        "Requirements": [f"[{rid}]" for rid in (reqs or req_ids)]
                    },
                    "Related_Figures": fig_refs[:2],
                    "Related_Tables": tbl_refs[:2],
                    "Config_Id": f"CF_{category}_{i:03d}",
                    "PICS_Selection": {f"PICS_{category}_V2GTP_Header": True},
                    "PreCondition": precond_str,
                    "Test_Sequence": seq_steps,
                    "Expected_Behavior": f"f_{sut}_{category}_TB_VTB_{message_type}_{i:03d}",
                    "parameters": {
                        "Invalid_Value": boundary[0] if boundary else "invalid"
                    }
                }
            })

        log(f"Fallback 生成了 {len(test_cases)} 个测试用例 (positive={len(positive_tps)}, negative={len(negative_tps)})", "WARN")
        return test_cases

    # ---- Agent 4: Verifier ----
    def run_verifier(self, plan: Dict, test_cases: List[Dict]) -> Dict:
        """覆盖度验证 Agent"""
        template = load_prompt_template("verifier")
        if not template:
            return {"verdict": "skipped", "reason": "verifier prompt not found"}

        # 收集需求内容
        req_contents = []
        for rid in plan.get("requirement_ids", []):
            req = self.store.get_requirement(rid)
            if req:
                req_contents.append(f"[{rid}] {req.get('content', '')}")

        # 章节内容
        sections = plan.get("sections", [])
        section_content = ""
        for s in sections[:3]:
            section_content += self.store.get_section_content(s, max_len=5000) + "\n"

        prompt = template + f"""

## INPUT

**Test Cases:**
{json.dumps(test_cases, indent=2, ensure_ascii=False)[:10000]}

**Requirements:**
{chr(10).join(req_contents)}

**Section Content:**
{section_content[:8000]}

Please output the verification result as a JSON object.
"""

        response = call_claude_agent(prompt, "Verifier", timeout=DEFAULT_AGENT_TIMEOUT)
        result = extract_json_from_response(response)

        if not result:
            return {"verdict": "unknown", "reason": "Verifier returned no valid JSON"}

        log(f"Verifier verdict: {result.get('verdict', '?')}, "
            f"breadth={result.get('breadth_score', '?')}, depth={result.get('depth_score', '?')}")
        return result

    # ---- Pipeline 入口 ----
    def generate(
        self,
        requirement_ids: List[str],
        mode: str = "single",
        skip_verify: bool = False
    ) -> Dict:
        """执行完整 4-Agent pipeline

        Args:
            requirement_ids: 需求 ID 列表
            mode: "single" 或 "multi"
            skip_verify: 是否跳过验证步骤

        Returns:
            完整的生成结果 dict
        """
        log(f"{'='*60}")
        log(f"Pipeline 启动: mode={mode}, requirements={requirement_ids}")
        log(f"{'='*60}")

        t0 = time.time()
        orchestrator_elapsed = 0.0
        modeler_elapsed = 0.0
        generator_elapsed = 0.0
        verifier_elapsed = 0.0
        generator_retry_count = 0

        # Step 1: Orchestrator
        t_orch = time.time()
        plan = self.run_orchestrator(requirement_ids, mode)
        orchestrator_elapsed = time.time() - t_orch
        log(f"[Orchestrator] 耗时: {orchestrator_elapsed:.1f}s")
        if "error" in plan:
            return {"error": plan["error"]}

        # Step 2: Modeler
        t_modeler = time.time()
        testing_points = self.run_modeler(plan)
        modeler_elapsed = time.time() - t_modeler
        log(f"[Modeler] 耗时: {modeler_elapsed:.1f}s")

        # Step 3: Generator
        t_gen = time.time()
        test_cases = self.run_generator(plan, testing_points)
        generator_elapsed = time.time() - t_gen
        log(f"[Generator] 耗时: {generator_elapsed:.1f}s")
        # 检测是否有 fallback
        if not test_cases:
            generator_retry_count = -1  # 标记为 fallback
        elif generator_elapsed > GENERATOR_TIMEOUT:
            generator_retry_count = GENERATOR_RETRIES

        # Step 4: Verifier (optional)
        verification = {}
        if not skip_verify and test_cases:
            t_ver = time.time()
            verification = self.run_verifier(plan, test_cases)
            verifier_elapsed = time.time() - t_ver
            log(f"[Verifier] 耗时: {verifier_elapsed:.1f}s")
            if isinstance(verification, dict) and verification.get("verdict") == "unknown":
                log(f"Verifier 返回 unknown，可能影响测试用例质量，继续生成", "WARN")

        elapsed = time.time() - t0

        # 打印 Agent 耗时汇总表
        log("========== Agent 执行耗时 ==========")
        log("| Agent              | 耗时(s) |")
        log("|--------------------|---------|")
        log(f"| Orchestrator      |  {orchestrator_elapsed:7.1f}  |")
        log(f"| Modeler           |  {modeler_elapsed:7.1f}  |")
        retry_suffix = f" (retry {generator_retry_count}x)" if generator_retry_count > 0 else ""
        fallback_suffix = " (fallback)" if generator_retry_count == -1 else ""
        log(f"| Generator{retry_suffix}{fallback_suffix}        |  {generator_elapsed:7.1f}  |")
        if not skip_verify:
            log(f"| Verifier           |  {verifier_elapsed:7.1f}  |")
        log("|--------------------|---------|")
        log(f"| 总计(Agent)       |  {elapsed:7.1f}  |")
        log("==================================")

        # 组装结果
        result = {
            "generation_metadata": {
                "timestamp": datetime.now().isoformat(),
                "mode": mode,
                "requirement_ids": requirement_ids,
                "pipeline": "4-Agent (Orchestrator→Modeler→Generator→Verifier)",
                "elapsed_seconds": round(elapsed, 1),
                "agent_timings": {
                    "orchestrator": round(orchestrator_elapsed, 1),
                    "modeler": round(modeler_elapsed, 1),
                    "generator": round(generator_elapsed, 1),
                    "generator_retry_count": generator_retry_count,
                    "verifier": round(verifier_elapsed, 1),
                    "total": round(elapsed, 1),
                },
            },
            "orchestrator_plan": plan,
            "testing_points": testing_points,
            "test_cases": test_cases,
            "verification": verification,
        }

        # 保存
        safe_name = "_".join(requirement_ids[:3]).replace("-", "")
        output_file = self.output_dir / f"tc_{safe_name}_{mode}.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        log(f"Pipeline 完成: {len(test_cases)} 测试用例, {elapsed:.1f}s")
        log(f"输出文件: {output_file}")

        return result

    # ---- 辅助方法 ----
    def _infer_message_type(self, req: Dict) -> str:
        """从需求内容推断消息类型"""
        content = req.get("content", "")
        msg_patterns = [
            # ISO 15118-2 协议消息类型（16种）
            (r'SupportedAppProtocol', 'SupportedAppProtocol'),
            (r'SessionSetup', 'SessionSetup'),
            (r'ServiceDiscovery', 'ServiceDiscovery'),
            (r'ServiceDetail', 'ServiceDetail'),
            (r'PaymentServiceSelection', 'PaymentServiceSelection'),
            (r'PaymentDetails', 'PaymentDetails'),
            (r'Authorization', 'Authorization'),
            (r'ChargeParameterDiscovery', 'ChargeParameterDiscovery'),
            (r'PowerDelivery', 'PowerDelivery'),
            (r'ChargingStatus', 'ChargingStatus'),
            (r'CableCheck', 'CableCheck'),
            (r'PreCharge', 'PreCharge'),
            (r'MeteringReceipt', 'MeteringReceipt'),
            (r'CurrentDemand', 'CurrentDemand'),
            (r'WeldingDetection', 'WeldingDetection'),
            (r'CertificateUpdate', 'CertificateUpdate'),
            (r'CertificateInstallation', 'CertificateInstallation'),
            (r'SessionStop', 'SessionStop'),
            # 协议层消息
            (r'V2GTP', 'V2GTP'),
            (r'SDP', 'SDP'),
            # 通配
            (r'Session\w*(?:Req|Res)', 'SessionSetup'),
            (r'Service\w*(?:Req|Res)', 'ServiceDiscovery'),
            (r'Payment\w*(?:Req|Res)', 'PaymentServiceSelection'),
            (r'Certificate\w*(?:Req|Res)', 'Authorization'),
            (r'Charge\w*(?:Req|Res)', 'ChargeParameterDiscovery'),
            (r'Power\w*(?:Req|Res)', 'PowerDelivery'),
            (r'Status', 'ChargingStatus'),
            (r'Demand', 'CurrentDemand'),
        ]
        for pat, msg_type in msg_patterns:
            m = re.search(pat, content, re.IGNORECASE)
            if m:
                return msg_type
        return "General"


# ==================== 批量生成 ====================

def batch_generate(
    requirement_ids: List[str],
    mode: str = "single",
    skip_verify: bool = False,
    output_dir: str = str(DEFAULT_OUTPUT_DIR),
) -> List[Dict]:
    """批量生成测试用例"""
    store = ProtocolDataStore()
    store.load()

    pipeline = TestCaseGeneratorPipeline(store, output_dir)
    results = []

    if mode == "single":
        for rid in requirement_ids:
            result = pipeline.generate([rid], mode="single", skip_verify=skip_verify)
            results.append(result)
    else:
        # multi 模式：所有需求一起处理
        result = pipeline.generate(requirement_ids, mode="multi", skip_verify=skip_verify)
        results.append(result)

    return results


# ==================== CLI 入口 ====================

def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="ISO 15118-2 Test Case Generator (4-Agent Pipeline)")
    parser.add_argument(
        "--requirements", type=str,
        help="多个安全需求 ID，逗号分隔，如 V2G2-082,V2G2-086"
    )
    parser.add_argument("--mode", choices=["single", "multi"], default="single",
                        help="生成模式: single (每个需求单独生成) 或 multi (多需求合并为一个测试用例)")
    parser.add_argument("--skip-verify", action="store_true",
                        help="跳过 Verifier agent 步骤")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR),
                        help="测试用例输出目录")
    parser.add_argument("--protocol-json", default=str(PROTOCOL_JSON),
                        help="协议 JSON 文件路径")

    args = parser.parse_args()

    # 解析需求 ID 列表（支持逗号分隔和空格分隔）
    if args.requirements:
        # 支持逗号分隔: V2G2-082,V2G2-083
        raw_ids = args.requirements.replace(",", " ").split()
        requirement_ids = [r.strip() for r in raw_ids if r.strip()]
    else:
        requirement_ids = []

    if not requirement_ids:
        parser.error("必须提供 --requirements 参数")

    # multi 模式下验证需求数量
    if args.mode == "multi" and len(requirement_ids) < 1:
        parser.error("multi 模式至少需要一个需求 ID")

    store = ProtocolDataStore(args.protocol_json)
    store.load()

    pipeline = TestCaseGeneratorPipeline(store, args.output_dir)

    if args.mode == "single":
        for rid in requirement_ids:
            pipeline.generate([rid], mode="single", skip_verify=args.skip_verify)
    else:
        pipeline.generate(requirement_ids, mode="multi", skip_verify=args.skip_verify)


if __name__ == "__main__":
    main()
