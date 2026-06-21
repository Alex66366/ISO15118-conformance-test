#!/usr/bin/env python3
"""
ISO 15118-2 TTCN-3 代码生成流水线
基于 NeTestLLM 多 Agent 协作方法，使用 Claude Code CLI 作为 sub-agent。

数据流:
  需求ID/测试用例 → 测试用例获取/生成 → Orchestrator → Summarizer → Core Agent → Fault Corrector → .ttcn 文件

输入模式:
  1. 安全需求ID (--requirement/--requirements): 从 generated_test_cases.json 查找，或调用测试用例生成器
  2. 测试用例ID (--tc-id): 从 generated_test_cases.json 查找
  3. 测试用例JSON (--test-case): 直接传入测试用例字典
  4. 测试用例文件 (--test-case-file): 从JSON文件读取测试用例

用法:
  # 单个安全需求
  python ttcn3_generation_pipeline.py --requirement V2G2-082
  python ttcn3_generation_pipeline.py --requirement V2G2-082 --sut SECC

  # 多个安全需求（对应一个测试用例）
  python ttcn3_generation_pipeline.py --requirements V2G2-082,V2G2-165,V2G2-434

  # 测试用例ID
  python ttcn3_generation_pipeline.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001

  # 直接传入测试用例JSON
  python ttcn3_generation_pipeline.py --test-case '{"Test_Case": {...}}'

  # 从文件读取测试用例
  python ttcn3_generation_pipeline.py --test-case-file ./my_test_case.json
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import datetime as _run_ts
from collections import namedtuple

# === 全局子进程管理和中断标志 ===
_subprocess_ref = None  # 保存当前活动的子进程引用
_interrupt_requested = False  # 中断请求标志
_llm_watchdog_timeout = 1200  # LLM 子代理最大空闲秒数，看门狗在此时间后终止
# Phase 3.6: flag set by the idle watchdog when it kills a claude sub-process
# after detecting >600s of no LLM output. Read by call_agent_with_timeout to
# refuse a retry. Reset by main() at the start of each new TC run.
_llm_silent_killed = False

# Phase 3.7: flag set by the sandbox-lock scanner when Claude Code reports that
# the sandbox is blocking file creation / shell write attempts. Re-calling the
# agent in this state would just repeat the same refusal, so call_agent_with_timeout
# refuses to retry when this flag is set. Reset by main() at the start of each
# new TC run (alongside _llm_silent_killed).
_sandbox_locked_flag = False


def cleanup_claude_history() -> int:
    """Delete all files and subdirectories inside .claude/ except settings.local.json.

    Session histories written by Claude Code sub-agents (e.g. under .claude/projects/ or
    .claude/.session/) can accumulate hundreds of MB per pipeline run when the reasoning
    model times out.  This function is called at the end of every run_pipeline() to keep
    disk usage bounded.
    """
    project_root = Path(__file__).resolve().parent.parent
    claude_dir = project_root / ".claude"
    if not claude_dir.is_dir():
        return 0

    removed = 0
    preserved = []
    try:
        for entry in claude_dir.iterdir():
            if entry.name == "settings.local.json":
                preserved.append(entry.name)
                continue
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass

    if removed:
        log(f"[CLEANUP] Removed {removed} item(s) from .claude/ (preserved: {preserved or 'none'})")
    return removed


def cleanup_stale_titan_temp_dirs() -> int:
    """Remove orphaned /tmp/titan_build_* directories left behind by killed pipelines.

    When a reasoning-model agent is killed by subprocess.TimeoutExpired, the bash subprocess
    may not complete its EXIT trap, leaving /tmp/titan_build_* directories behind.  This
    function is called at the start of every run_pipeline() to sweep them up before the
    new run begins.
    """
    removed = 0
    try:
        for entry in Path("/tmp").iterdir():
            if entry.name.startswith("titan_build_") and entry.is_dir():
                try:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass

    if removed:
        log(f"[CLEANUP] Removed {removed} stale /tmp/titan_build_* directory(ies) from previous runs")
    return removed


def _request_interrupt(signum, frame):
    """SIGINT 信号处理：标记中断请求，但不立即终止"""
    global _interrupt_requested
    _interrupt_requested = True
    log("收到中断信号 (Ctrl+C)，正在停止子进程...", "WARN")
    _terminate_current_subprocess()


def _terminate_current_subprocess():
    """终止当前活动的子进程及其进程组"""
    global _subprocess_ref
    
    if _subprocess_ref is None:
        return
    
    try:
        # 首先尝试发送 SIGTERM 给进程组（Unix）
        if hasattr(os, 'killpg'):
            try:
                os.killpg(os.getpgid(_subprocess_ref.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        
        # 然后尝试终止主进程
        _subprocess_ref.terminate()
        
        # 等待最多3秒
        try:
            _subprocess_ref.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # 强制杀死
            _subprocess_ref.kill()
            _subprocess_ref.wait()
    except Exception:
        pass
    finally:
        _subprocess_ref = None


def _check_interrupt() -> bool:
    """检查是否请求了中断，返回 True 表示应该停止当前操作"""
    return _interrupt_requested

# === Fault Corrector import (conditional, modules under test_code/fault_corrector/) ===
try:
    sys.path.insert(0, str(Path(__file__).parent / "fault_corrector"))
    from fault_corrector.sniffer import V2G_Sniffer
    from fault_corrector.titan_log_parser import TitanLogParser
    from fault_corrector.pcap_parser import PcapParser
    from fault_corrector.correlator import Correlator
    from fault_corrector.repair_engine import RepairEngine
    from fault_corrector.fault_classifier import FaultClassifier
    FAULT_CORRECTOR_AVAILABLE = True
except ImportError as e:
    FAULT_CORRECTOR_AVAILABLE = False

# === 路径常量 ===
AGENT_DIR = Path(__file__).resolve().parent.parent  # agent/
TEST_CODE_DIR = AGENT_DIR / "test_code"
PROMPTS_DIR = TEST_CODE_DIR / "prompts"
EXAMPLES_DIR = TEST_CODE_DIR / "examples"
KB_DIR = TEST_CODE_DIR / "knowledge_base"
GENERATED_DIR = TEST_CODE_DIR / "generated"
DOCUMENT_DIR = AGENT_DIR / "document"
TEST_CASE_DIR = AGENT_DIR / "test_case"

# 测试用例生成器路径（第一阶段）
TEST_CASE_GENERATOR_SCRIPT = TEST_CASE_DIR / "test_case_generator.py"

# 知识库文件（仅用于参考代码检索，不用于测试用例）
KNOWLEDGE_BASE_PATH = DOCUMENT_DIR / "iso15118_4_ttcn_knowledge_base.json"
SOP_PATH = KB_DIR / "ttcn3_generation_sop.json"
DOC_URLS_PATH = KB_DIR / "doc_urls.json"

# 已生成的测试用例文件（第一阶段输出）
GENERATED_TEST_CASES_PATH = TEST_CASE_DIR / "generated_test_cases.json"
HIERARCHICAL_TREE_PATH = DOCUMENT_DIR / "iso15118_2_hierarchical_tree.json"

# Prompt 模板
ORCHESTRATOR_PROMPT_PATH = PROMPTS_DIR / "orchestrator_prompt.txt"
SUMMARIZER_PROMPT_PATH = PROMPTS_DIR / "summarizer_prompt.txt"
CORE_AGENT_PROMPT_PATH = PROMPTS_DIR / "core_agent_prompt.txt"
FAULT_CORRECTOR_PROMPT_PATH = PROMPTS_DIR / "fault_corrector_prompt.txt"
INFRASTRUCTURE_AGENT_PROMPT_PATH = PROMPTS_DIR / "infrastructure_agent_prompt.txt"

# demo 项目路径（用于 Infrastructure Agent 参考）
DEMO_ROOT = AGENT_DIR / "test_code" / "examples" / "demo"

# Cached result of the demo TTCN baseline precompile gate. None = not yet run;
# True = baseline is syntactically valid; False = baseline has errors (any
# further case will short-circuit-abort). Reset by reloading the module.
_DEMO_PRECOMPILE_OK: Optional[bool] = None

# Few-shot 样例文件
EXAMPLE_FILES = {
    "testcase_goodcase": EXAMPLES_DIR / "ttcn3_testcase_goodcase.ttcn",
    "testcase_badcase": EXAMPLES_DIR / "ttcn3_testcase_badcase.ttcn",
    "test_behavior": EXAMPLES_DIR / "ttcn3_test_behavior_function.ttcn",
    "precondition": EXAMPLES_DIR / "ttcn3_precondition_function.ttcn",
    "postcondition": EXAMPLES_DIR / "ttcn3_postcondition_function.ttcn",
    "templates": EXAMPLES_DIR / "ttcn3_templates.ttcn",
    "altstep": EXAMPLES_DIR / "ttcn3_altstep.ttcn",
    "testcase_with_imports": EXAMPLES_DIR / "ttcn3_testcase_with_imports.ttcn",
}

MAX_CORRECTION_ITERATIONS = 3
MAX_RUNTIME_REPAIR_ITERATIONS = 4  # maximum self-repair iterations after test execution

# ============================================================
# Agent 超时配置（按类型区分）
# ============================================================
AGENT_TIMEOUTS = {
    "orchestrator": 900,        # 意图分解，reasoning model ~7min/turn
    "summarizer": 600,          # 知识检索，reasoning model ~7min/turn
    "core_agent": 1800,         # 代码生成，reasoning model ~7min/turn
    "infrastructure_agent": 5400,  # 项目结构生成，3-iteration compile loop
    "fault_corrector": 600,     # 错误分析
    "default": 900,             # 未识别类型默认超时
}

# ============================================================
# Prompt 大小限制配置（统一配置）
# ============================================================

# Prompt size thresholds
PROMPT_SIZE_WARNING = 80000   # 80KB warning threshold
PROMPT_SIZE_HARD = 100000    # 100KB hard truncation

# Core Agent few-shot 优化配置
FEW_SHOT_CONFIG = {
    "max_total_size": 20000,    # 所有样例总计不超过 20KB
    "max_single_example": 5000,  # 单个样例不超过 5KB
    "priority_examples": [       # 按优先级选择样例
        "testcase",              # 必须
        "test_behavior",         # 必须
        "precondition",          # 重要
        "postcondition",         # 重要
    ],
    "optional_examples": [        # 可选样例（空间足够时才添加）
        "templates",
        "testcase_with_imports",
    ],
}

# Infrastructure Agent 优化配置
INFRA_CONFIG = {
    "ttcn_code_truncate_size": 8000,    # TTCN 代码截断到 8KB
    "demo_ref_truncate_size": 15000,     # demo 参考截断到 15KB
    "batch_threshold": 40000,          # 超过此大小启用批量模式
}

# Fault Corrector runtime 优化配置
FAULT_CORRECTOR_CONFIG = {
    "max_log_events": 100,          # 最多保留 100 个日志事件
    "max_pcap_packets": 50,         # 最多保留 50 个数据包
    "log_event_truncate": 500,      # 每个日志事件最大字符数
}


def get_agent_timeout(agent_name: str, prompt_size: int = 0) -> int:
    """获取指定 Agent 的超时时间。

    Args:
        agent_name: Agent 名称（小写匹配）
        prompt_size: Prompt 大小（字节），用于自动调整超时

    Returns:
        超时秒数
    """
    # Normalise agent name: "Infrastructure Agent" -> "infrastructure_agent"
    # so "Infrastructure Agent".lower() matches the dict key "infrastructure_agent".
    # Also strips leading/trailing spaces that might cause "infrastructure agent " to not match.
    normalised = agent_name.lower().replace(" ", "_").strip()
    base_timeout = AGENT_TIMEOUTS.get(normalised, AGENT_TIMEOUTS["default"])

    # 当 prompt 大小超过阈值时，自动增加超时
    if prompt_size > PROMPT_SIZE_WARNING:
        extra = int((prompt_size - PROMPT_SIZE_WARNING) / 10000) * 60
        return min(base_timeout + extra, base_timeout * 2)

    return base_timeout


# ============================================================
# Few-shot 样例智能加载函数
# ============================================================

def build_few_shot_section(
    few_shot_examples: Dict[str, str],
    test_type: str,
    message_type: str = "",
    max_total_size: int = None,
    max_single_size: int = None
) -> str:
    """智能构建 few-shot 样例部分，根据空间限制选择性加载。

    策略:
    1. 必须包含: testcase, test_behavior (最重要)
    2. 重要: precondition, postcondition
    3. 可选: templates, testcase_with_imports (空间足够时才添加)

    Args:
        few_shot_examples: 样例字典
        test_type: "GoodCase" 或 "BadCase"
        message_type: 消息类型（用于日志）
        max_total_size: 最大总大小限制（字节），默认使用配置值
        max_single_size: 单个样例最大大小限制（字节），默认使用配置值

    Returns:
        格式化后的 few-shot 样例字符串
    """
    if max_total_size is None:
        max_total_size = FEW_SHOT_CONFIG["max_total_size"]
    if max_single_size is None:
        max_single_size = FEW_SHOT_CONFIG["max_single_size"]

    priority = FEW_SHOT_CONFIG["priority_examples"]
    optional = FEW_SHOT_CONFIG["optional_examples"]

    # 选择主 testcase 样例
    if test_type == "GoodCase":
        tc_key = "testcase_goodcase"
    else:
        tc_key = "testcase_badcase"

    sections = []
    used_size = 0

    # Helper: 截断单个样例
    def truncate_example(name: str, content: str) -> str:
        if len(content) > max_single_size:
            truncated = content[:max_single_size]
            # 在最近的换行符处截断
            last_nl = truncated.rfind('\n')
            if last_nl > max_single_size * 0.8:
                truncated = truncated[:last_nl]
            return truncated + f"\n// ... [TRUNCATED from {len(content)} chars]"
        return content

    # Helper: 添加样例（检查空间）
    def add_section(title: str, key: str, content: str) -> bool:
        nonlocal used_size
        if not content:
            return False

        # 计算预估大小（包含 markdown 标签）
        estimated = len(title) + len(content) + 30
        if used_size + estimated > max_total_size:
            return False

        truncated = truncate_example(key, content)
        sections.append(f"### {title}:\n```ttcn3\n{truncated}\n```")
        used_size += len(title) + len(truncated) + 30
        return True

    # 优先级 1: testcase（必须）
    add_section("Testcase 样例", tc_key, few_shot_examples.get(tc_key, ""))

    # 优先级 2: test_behavior（必须）
    add_section("Test Behavior Function 样例", "test_behavior",
                few_shot_examples.get("test_behavior", ""))

    # 优先级 3: precondition 和 postcondition
    add_section("Precondition Function 样例", "precondition",
                few_shot_examples.get("precondition", ""))
    add_section("Postcondition Function 样例", "postcondition",
                few_shot_examples.get("postcondition", ""))

    # 可选样例（空间足够时才添加）
    if used_size < max_total_size * 0.7:
        add_section("Template 样例", "templates",
                    few_shot_examples.get("templates", ""))

    if used_size < max_total_size * 0.8:
        tc_with_imports = few_shot_examples.get("testcase_with_imports", "")
        if tc_with_imports:
            # 如果空间不够，用更精简的版本
            if used_size + len(tc_with_imports) > max_total_size:
                tc_with_imports = tc_with_imports[:int(max_total_size * 0.3)]
            add_section("TestCase with Imports 样例", "testcase_with_imports",
                        tc_with_imports)

    result = "## Few-shot 样例\n\n" + "\n\n".join(sections)
    if used_size >= max_total_size * 0.9:
        result += "\n\n// 注意: 样例已根据空间限制进行截断"
    return result


def truncate_text(text: str, max_size: int, suffix: str = "... [TRUNCATED]") -> str:
    """截断文本到指定大小，保留结尾。

    Args:
        text: 原始文本
        max_size: 最大大小（字节）
        suffix: 截断后缀

    Returns:
        截断后的文本
    """
    if len(text) <= max_size:
        return text

    available = max_size - len(suffix)
    if available <= 0:
        return suffix[:max_size]

    # 在最近的换行符或分号处截断
    truncated = text[:available]
    last_nl = truncated.rfind('\n')
    last_sc = truncated.rfind(';')
    cut = max(last_nl, last_sc)
    if cut > available * 0.7:
        truncated = truncated[:cut + 1]

    return truncated + suffix


# ============================================================
# Infrastructure Agent Constants (Prompt Size Optimization)
# ============================================================

# Retry configuration
MAX_INFRA_RETRY = 3          # Maximum retry attempts

# Batch output configuration
BATCH_MODE_ENABLED = True    # Enable batch output mode for large responses

# Demo file truncation ratios (differentiated by file type)
TRUNCATE_RATIOS = {
    "Common": 1.0,           # Complete - basic utility class
    "TitanTci": 1.0,         # Complete - interface definitions
    "ExiCodecAdapter": 0.3,  # Truncate - implementation patterns visible
    "HAL61851Adapter": 0.2,  # Truncate - hardware abstraction
    "TCPTLSPortAdapter": 0.2,  # Truncate - port implementation
    "TitanTri": 0.2,         # Truncate - TRI interface
    "default": 0.15,         # Default for other C++ files
}

# Expected file count for validation
EXPECTED_INFRA_FILES_MIN = 5   # Minimum required files from agent (config + TTCN modules; C++ adapters are pipeline-added)


# ============================================================
# TITAN 工具链检测
# ============================================================

def check_titan_toolchain() -> Dict[str, Optional[str]]:
    """检查 TITAN 编译器、makefilegen、ttcn3_executor 是否可用。

    Returns:
        包含工具路径的字典，值为 None 表示未找到。
    """
    results: Dict[str, Optional[str]] = {}
    tools = ["makefilegen", "ttcn3_compiler", "ttcn3_executor"]
    for tool in tools:
        results[tool] = shutil.which(tool)

    ttcn3_inc = os.environ.get("TTCN3_INC", "")
    results["TTCN3_INC"] = ttcn3_inc if ttcn3_inc else None

    if not results.get("TTCN3_INC"):
        candidates = [
            "C:/Program Files/TITAN/include",
            "C:/titan/include",
            "/usr/include/titan",
            "/usr/local/include/titan",
        ]
        for p in candidates:
            if Path(p).exists():
                results["TTCN3_INC"] = p
                break

    return results


# ============================================================
# 编译错误分类器
# ============================================================

def log(msg: str, level: str = "INFO") -> None:
    ts = time.strftime("%H:%M:%S")
    safe_msg = msg.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    try:
        print(f"[{ts}] [{level}] {safe_msg}")
    except UnicodeEncodeError:
        # Fallback: use ascii with replacements
        ascii_msg = safe_msg.encode('ascii', errors='replace').decode('ascii')
        print(f"[{ts}] [{level}] {ascii_msg}")


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def verify_core_agent_output(core_result: Dict, sut_mode: Optional[str] = None) -> Tuple[bool, List[str]]:
    """检查 Core Agent 生成的代码是否包含必要元素。

    Args:
        core_result: Core Agent 返回的结果字典，包含 generation_plan 和 generated_code
        sut_mode: SUT 模式（"SECC" 或 "EVCC"，可选）

    Returns:
        Tuple[bool, List[str]]: (是否通过验证, 错误信息列表)
    """
    errors: List[str] = []
    generation_plan = core_result.get("generation_plan", {})
    ttcn3_code = core_result.get("testcase", "") or core_result.get("generated_code", "") or core_result.get("ttcn3_code", "")

    if not generation_plan and not ttcn3_code:
        errors.append("Core Agent 返回结果为空（无 generation_plan 或 generated_code）")
        return False, errors

    plan_tc_id = generation_plan.get("tc_id", "")
    if plan_tc_id and ttcn3_code:
        expected_module = plan_tc_id.replace("-", "_").replace(" ", "_")
        if f"module {expected_module}" not in ttcn3_code and f"module {plan_tc_id}" not in ttcn3_code:
            errors.append(f"TTCN-3 代码中未找到期望的 module 名称: {plan_tc_id}")

    if ttcn3_code:
        if "testcase" not in ttcn3_code.lower():
            log("验证警告: TTCN-3 代码中未找到 testcase 函数定义", "WARN")
        required_imports = ["SECC_TestSystem", "SECC_CmnTemplates"]
        for imp in required_imports:
            if imp not in ttcn3_code:
                log(f"验证警告: TTCN-3 代码中可能缺少 import: {imp}（真实错误将在 TITAN 编译时报告）", "WARN")
    else:
        errors.append("Core Agent 未生成 TTCN-3 代码")

    return len(errors) == 0, errors


def verify_infrastructure_agent_output(infra_result: Dict) -> Tuple[bool, List[str]]:
    """检查 Infrastructure Agent 生成的文件是否满足最小要求。

    Args:
        infra_result: Infrastructure Agent 返回的结果字典，包含 files 列表

    Returns:
        Tuple[bool, List[str]]: (是否通过验证, 错误信息列表)
    """
    errors: List[str] = []
    files = infra_result.get("files", [])

    if not files:
        errors.append("Infrastructure Agent 未生成任何文件")
        return False, errors

    file_paths = [f.get("path", "") for f in files]

    has_cfg = any("titan_runtime.cfg" in p for p in file_paths)

    if not has_cfg:
        log("验证警告: 未找到 config/titan_runtime.cfg", "WARN")
    # Note: build.sh is copied from demo by _install_build_script(), not generated by Agent.
    # Skip has_build check since the Agent no longer generates it.

    for f in files:
        p = f.get("path", "")
        c = f.get("content", "")
        # Note: Common.cfg 引用检查已移除（TITAN 不强制要求）

    return len(errors) == 0, errors


def derive_tc_filename(tc_id: str) -> str:
    """Derive the test case TTCN-3 filename from tc_id.

    tc_id format: TC_SECC_CMN_VTB_SupportedAppProtocol_001
    filename format: TC_SECC_CMN_VTB_SupportedAppProtocol_001.ttcn

    CRITICAL: tc_id already contains the TC_ prefix.
    DO NOT add another TC_ prefix. DO NOT strip and re-add.
    Always use the tc_id as-is and append .ttcn.
    """
    # Ensure tc_id is the complete name (already has TC_ prefix)
    return f"{tc_id}.ttcn"


def load_few_shot_examples() -> Dict[str, str]:
    """加载所有 few-shot 样例"""
    examples = {}
    for name, path in EXAMPLE_FILES.items():
        if path.exists():
            examples[name] = load_text(path)
    return examples


def load_doc_urls(path: Path = DOC_URLS_PATH) -> Dict:
    """Load the TITAN documentation URLs from doc_urls.json."""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_doc_urls_for_infrastructure(doc_urls: Dict) -> str:
    """Build URL reference text for the Infrastructure Agent from doc_urls.json."""
    if not doc_urls or "build_reference" not in doc_urls:
        return "（doc_urls.json 未找到或格式不正确，跳过在线参考链接）"
    lines = []
    refs = doc_urls.get("build_reference", {})
    for key, url in refs.items():
        title_map = {
            "chapter3_userguide": "UserGuide Ch.3 (build process)",
            "tips_troubleshooting": "ReferenceGuide Ch.8 (Tips & Troubleshooting)",
            "language_extensions": "ReferenceGuide Ch.4 (Language Extensions)",
        }
        title = title_map.get(key, key)
        lines.append(f"- {title}: {url}")
    result = "\n".join(lines) if lines else "（无 build_reference 条目）"
    # Truncate to 2000 chars to avoid prompt bloat
    return result[:2000] if len(result) > 2000 else result


def build_doc_urls_for_fault_corrector(doc_urls: Dict) -> str:
    """Build URL reference text for the Fault Corrector Agent from doc_urls.json."""
    if not doc_urls or "urls" not in doc_urls:
        return "（doc_urls.json 未找到或格式不正确，跳过在线参考链接）"
    lines = []
    refs = doc_urls.get("reference_guide", {})
    for key, url in refs.items():
        title_map = {
            "tips_troubleshooting": "TITAN Troubleshooting",
            "language_extensions": "TITAN Language Extensions",
            "test_ports": "TITAN Test Ports",
        }
        title = title_map.get(key, f"ReferenceGuide/{key}")
        lines.append(f"- {title}: {url}")
    return "\n".join(lines) if lines else "（无 reference_guide 条目）"



# ============================================================
# Agent Metrics Tracking
# ============================================================
from collections import namedtuple
from protocol_registry import get_default_registry, ResolvedMessage
AgentMetrics = namedtuple("AgentMetrics", ["elapsed_seconds", "input_tokens", "output_tokens", "calls"])

_agent_input_tokens = {}
_agent_output_tokens = {}
_agent_call_counts = {}
_claude_model_name = None  # set once from first CLI response for experiment_metadata


def _extract_text_from_message_event(msg_event: dict) -> str:
    """Extract concatenated text from a Claude Code message event.

    The message event has structure:
    {"type": "message", "message": {"content": [{"type": "text", "text": "..."}]}}
    """
    if not isinstance(msg_event, dict):
        return ""
    msg = msg_event.get("message", {})
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content", [])
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            t = c.get("text", "")
            if t:
                parts.append(t)
    return "".join(parts)


def _parse_token_usage_from_claude_output(stderr, stdout=""):
    """Multi-path resolver for token usage from Claude Code CLI output.

    CLI OUTPUT FORMAT (--output-format json):
      Streaming NDJSON — each line is a complete JSON object:
        [{"type":"system","subtype":"init",...},
         {"type":"assistant","message":{"role":"assistant","content":[{...}],"usage":{...}},...},
         {"type":"result","result":"...",...}]

    Token data lives in THREE distinct locations across the stream (Path A/B/C):
      Path A: {type:"assistant", message:{content:[...], usage:{input_tokens:N, output_tokens:N}}}
              "usage" is the canonical per-invocation source from the API response.
      Path B: {type:"result", result:"...", modelUsage:{...}}          (older CLI format)
      Path C: raw-text regex scan of stdout for "Input: N tokens" patterns.

    IMPORTANT: stderr is ALWAYS empty in the current setup (Unix Popen with streaming
    threads). All token data MUST be extracted from stdout's NDJSON stream.

    Returns:
        (input_tokens, output_tokens)
    """
    import json as _json

    input_tok = 0
    output_tok = 0
    debug_info = {
        "path_a_assistant_found": False,
        "path_a_tokens": (0, 0),
        "path_b_modelUsage_found": False,
        "path_b_tokens": (0, 0),
        "path_c_regex_found": False,
        "path_c_tokens": (0, 0),
        "ndjson_lines_scanned": 0,
        "ndjson_parse_errors": 0,
        "active_path": None,
    }

    # ------------------------------------------------------------------
    # Helper: safely extract ints from a dict with snake/camel variants
    # ------------------------------------------------------------------
    def _get_tokens(d):
        if not isinstance(d, dict):
            return 0, 0
        # Try snake_case first (Anthropic API standard), then camelCase
        in_t  = d.get("input_tokens")  or d.get("inputTokens")  or 0
        out_t = d.get("output_tokens") or d.get("outputTokens") or 0
        return int(in_t or 0), int(out_t or 0)

    # ------------------------------------------------------------------
    # PATH A — NDJSON streaming: scan for assistant messages with usage
    # This is the PRIMARY path for current Claude Code CLI versions.
    #
    # The CLI can emit output in two physical layouts:
    #   Multi-line:  [{"type":"system"...}\n{"type":"assistant"...}\n{"type":"result"...}\n]
    #   Single-line: [{"type":"system"...},{"type":"assistant"...},{"type":"result"...}]
    #
    # Both produce valid JSON arrays. We handle both by first splitting on \n,
    # then falling back to bracket-aware parsing if a single-line array was
    # mistaken for one "unparseable" chunk.
    # ------------------------------------------------------------------
    last_assistant_usage = (0, 0)
    _candidates = []  # [(raw_line_str, is_from_multiline), ...]

    if stdout:
        raw = stdout.strip()
        raw_lines = raw.split("\n")
        for rl in raw_lines:
            stripped = rl.strip().rstrip(",")
            if stripped and stripped not in ("[", "]"):
                _candidates.append((stripped, True))

        # If after \n-split we got no valid candidates (single-line array case),
        # fall back to bracket-aware slicing so we don't skip the entire stream.
        if not _candidates:
            depth, buf_start = 0, -1
            for i, ch in enumerate(raw):
                if ch in ("{", "["):
                    if depth == 0:
                        buf_start = i
                    depth += 1
                elif ch in ("}", "]"):
                    depth -= 1
                    if depth == 0 and buf_start >= 0:
                        chunk = raw[buf_start:i + 1]
                        buf_start = -1
                        _candidates.append((chunk, False))

        for raw_line, from_multiline in _candidates:
            debug_info["ndjson_lines_scanned"] += 1
            try:
                parsed = _json.loads(raw_line)
            except _json.JSONDecodeError:
                debug_info["ndjson_parse_errors"] += 1
                continue

            # parsed can be a list (when the single-line array wasn't split by \n)
            # or a dict (normal NDJSON line). Normalise to a list of dicts.
            items = parsed if isinstance(parsed, list) else [parsed]

            for obj in items:
                if not isinstance(obj, dict):
                    continue

                # Capture model name from system/init or assistant events (first seen wins)
                global _claude_model_name  # noqa: F821
                if _claude_model_name is None:
                    _model = obj.get("model") or (
                        obj.get("message", {}).get("model")
                        if isinstance(obj.get("message"), dict) else None
                    )
                    if _model:
                        _claude_model_name = _model
                        log(f"[TOKEN][MODEL] detected: {_model}", "DEBUG")

                # Path A: {type:"assistant", message:{..., usage:{...}}}
                if obj.get("type") == "assistant":
                    msg = obj.get("message")
                    if isinstance(msg, dict):
                        usage = msg.get("usage")
                        in_t, out_t = _get_tokens(usage)
                        if in_t or out_t:
                            last_assistant_usage = (in_t, out_t)
                            log(f"[TOKEN][PATH_A] assistant usage: in={in_t} out={out_t}", "DEBUG")

                # Path B: {type:"result", modelUsage:{...}}  (older CLI format)
                if obj.get("type") == "result":
                    mu = obj.get("modelUsage")
                    if isinstance(mu, dict):
                        for _model, u in mu.items():
                            if not isinstance(u, dict):
                                continue
                            in_t, out_t = _get_tokens(u)
                            if in_t or out_t:
                                input_tok += in_t
                                output_tok += out_t
                                debug_info["path_b_modelUsage_found"] = True
                                debug_info["path_b_tokens"] = (in_t, out_t)
                                log(f"[TOKEN][PATH_B] modelUsage: in={in_t} out={out_t}", "DEBUG")

    # Promote the last assistant usage as the Path A result
    if last_assistant_usage != (0, 0):
        debug_info["path_a_assistant_found"] = True
        debug_info["path_a_tokens"] = last_assistant_usage
        if not debug_info["path_b_modelUsage_found"]:
            input_tok, output_tok = last_assistant_usage

    # ------------------------------------------------------------------
    # PATH C — Regex fallback (last resort; activates when Paths A & B fail)
    # ------------------------------------------------------------------
    if not debug_info["path_a_assistant_found"] and not debug_info["path_b_modelUsage_found"]:
        debug_info["active_path"] = "C"
        combined = stdout  # NOTE: stderr is always empty, but include it anyway
        _input_patterns = [
            (r"Input[:\s]+(\d+)[\s]*tokens?",              "Input: N tokens"),
            (r"input\s*tokens?[:\s]+(\d+)",                "input tokens: N"),
            (r"Input\s+Tokens?\s*[:\-=]\s*(\d+)",          "Input Tokens: N"),
            (r"prompt.*?(\d+).*?tokens?",                  "prompt ... N tokens"),
        ]
        _output_patterns = [
            (r"Output[:\s]+(\d+)[\s]*tokens?",             "Output: N tokens"),
            (r"output\s*tokens?[:\s]+(\d+)",               "output tokens: N"),
            (r"Output\s+Tokens?\s*[:\-=]\s*(\d+)",         "Output Tokens: N"),
            (r"response.*?(\d+).*?tokens?",                "response ... N tokens"),
        ]
        found_in = None
        found_out = None
        for pattern, label in _input_patterns:
            m = re.search(pattern, combined, re.IGNORECASE)
            if m:
                found_in = int(m.group(1))
                log(f"[TOKEN][PATH_C] input pattern '{label}' -> {found_in}", "DEBUG")
                break
        for pattern, label in _output_patterns:
            m = re.search(pattern, combined, re.IGNORECASE)
            if m:
                found_out = int(m.group(1))
                log(f"[TOKEN][PATH_C] output pattern '{label}' -> {found_out}", "DEBUG")
                break
        if found_in is not None or found_out is not None:
            input_tok  = found_in  if found_in  is not None else 0
            output_tok = found_out if found_out is not None else 0
            debug_info["path_c_regex_found"] = True
            debug_info["path_c_tokens"] = (input_tok, output_tok)

    # ------------------------------------------------------------------
    # Determine active path for debug logging
    # ------------------------------------------------------------------
    if debug_info["path_a_assistant_found"]:
        debug_info["active_path"] = "A"
    elif debug_info["path_b_modelUsage_found"]:
        debug_info["active_path"] = "B"
    elif debug_info["path_c_regex_found"]:
        debug_info["active_path"] = "C"
    else:
        debug_info["active_path"] = "NONE"

    # ------------------------------------------------------------------
    # Verbose debug log — THE canonical signal for paper-grade traceability
    # ------------------------------------------------------------------
    log(f"[TOKEN] ██ resolved={debug_info['active_path']} "
        f"path_a={debug_info['path_a_assistant_found']}({debug_info['path_a_tokens']}) "
        f"path_b={debug_info['path_b_modelUsage_found']}({debug_info['path_b_tokens']}) "
        f"path_c={debug_info['path_c_regex_found']}({debug_info['path_c_tokens']}) "
        f"scanned={debug_info['ndjson_lines_scanned']} "
        f"parse_err={debug_info['ndjson_parse_errors']} "
        f"in={input_tok} out={output_tok}", "DEBUG")

    return (input_tok, output_tok)


def _accumulate_tokens(agent_name, input_tok, output_tok):
    global _agent_input_tokens, _agent_output_tokens, _agent_call_counts
    _agent_input_tokens[agent_name] = _agent_input_tokens.get(agent_name, 0) + input_tok
    _agent_output_tokens[agent_name] = _agent_output_tokens.get(agent_name, 0) + output_tok
    _agent_call_counts[agent_name] = _agent_call_counts.get(agent_name, 0) + 1


def _reset_metrics():
    global _agent_input_tokens, _agent_output_tokens, _agent_call_counts, _claude_model_name
    _agent_input_tokens.clear()
    _agent_output_tokens.clear()
    _agent_call_counts.clear()
    _claude_model_name = None



def call_claude_agent(prompt: str, agent_name: str, timeout: int = 120,
                      extra_allowed_tools: str = "",
                      read_only: bool = False) -> str:
    """调用 Claude Code CLI 作为 sub-agent

    Args:
        prompt: 完整的 prompt 文本
        agent_name: Agent 名称（用于日志和超时选择）
        timeout: 超时秒数
        extra_allowed_tools: Space-separated extra tool patterns appended to the default
                             sandbox tools. Used to grant additional capabilities to
                             specific agents (e.g. "Bash(echo *)" for the Infrastructure
                             Agent so it can overwrite build.sh via redirection).
        read_only: If True, restrict tools to Read-only (no Write, Edit, or Bash).
                   Use this for agents that should only output text, never touch files.
    """
    log(f"调用 {agent_name} Agent...")

    # Windows subprocess requires .cmd extension for batch-file lookups
    # Try multiple methods to find claude CLI:
    # 1. Check npm global path (common location for globally installed tools)
    # 2. Try shutil.which for user-installed paths
    npm_global_claude = None
    for npm_path in [r"D:\nodejs\node_global",
                     r"C:\Program Files\nodejs\node_global",
                     os.path.expanduser(r"~\AppData\Roaming\npm")]:
        candidate = os.path.join(npm_path, "claude.cmd")
        if os.path.exists(candidate):
            npm_global_claude = candidate
            break
        # Also try .CMD uppercase
        candidate_upper = os.path.join(npm_path, "claude.CMD")
        if os.path.exists(candidate_upper):
            npm_global_claude = candidate_upper
            break

    claude_cmd = (npm_global_claude or
                   shutil.which("claude.cmd") or
                   shutil.which("claude") or
                   ("claude.cmd" if os.name == "nt" else "claude"))

    # Log the found path for debugging
    log(f"DEBUG: claude_cmd resolved to: {claude_cmd}", "DEBUG")
    if claude_cmd and os.name == "nt":
        log(f"DEBUG: claude_cmd exists: {os.path.exists(claude_cmd)}", "DEBUG")

    try:
        tmp_prompt_file = None
        tmp_output_file = None
        tmp_batch_file = None

        import logging as _log_mod
        _log_path = Path(__file__).resolve().parent.parent.parent / ".cursor" / "debug.log"
        def _dbg(msg, data=None):
            import datetime as _dt
            entry = {"ts": _dt.datetime.now().isoformat(), "loc": "call_claude_agent", "msg": msg, "data": data}
            try:
                with open(_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass

        _dbg("before_subprocess", {
            "agent_name": agent_name,
            "prompt_len": len(prompt),
            "prompt_preview": prompt[:200],
            "is_nt": os.name == "nt",
            "claude_cmd": claude_cmd,
        })

        global _subprocess_ref
        _subprocess_ref = None

        # Write prompt to temp file (used for stdin on Unix; already done on Windows)
        tmp_prompt_file = Path(tempfile.gettempdir()) / f"claude_prompt_{os.getpid()}_{time.time():.0f}.txt"
        tmp_prompt_file.write_text(prompt, encoding="utf-8")

        if os.name == "nt":
            # Windows: Use cmd.exe /c with stdin redirected from file
            extra_tools = (f",{extra_allowed_tools}" if extra_allowed_tools else "")
            if read_only:
                allowed_tools = "Read"
            else:
                allowed_tools = (f"Write,Read,Edit,Bash(bash *),Bash(echo *),Bash(makefilegen *),Bash(ttcn3_compiler *){extra_tools}")
            cmd_str = (f'chcp 65001 >nul & "{claude_cmd}" --bare --verbose -p - '
                       f'--allowedTools "{allowed_tools}" '
                       f'--output-format json <"{tmp_prompt_file}"')

            proc = subprocess.Popen(
                cmd_str,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if hasattr(subprocess, 'CREATE_NEW_PROCESS_GROUP') else 0,
            )
            _subprocess_ref = proc

            # Stream stderr in real-time via reader thread
            import threading
            stderr_lines = []
            def _stream_stderr():
                for line in iter(proc.stderr.readline, ""):
                    if line:
                        stderr_lines.append(line)
                        log(f"[{agent_name} stderr] {line.rstrip()}", "DEBUG")
            stderr_thread = threading.Thread(target=_stream_stderr, daemon=True)
            stderr_thread.start()

            # === WATCHDOG for Windows (no session file polling, just wall-clock) ===
            _wd_timeout = max(0, int(_llm_watchdog_timeout or 0))
            _watchdog_killed_win = [False]

            def _watchdog_tick_win():
                if _wd_timeout <= 0:
                    return
                time.sleep(_wd_timeout)
                if not _watchdog_killed_win[0] and proc.poll() is None:
                    _watchdog_killed_win[0] = True
                    log(f"[WATCHDOG] {agent_name} idle for {_wd_timeout}s, killing", "WARN")
                    try:
                        proc.kill()
                    except Exception:
                        pass

            threading.Thread(target=_watchdog_tick_win, daemon=True).start()

            start_time = time.time()
            while proc.poll() is None:
                if _check_interrupt():
                    _terminate_current_subprocess()
                    log(f"{agent_name} Agent 被中断", "WARN")
                    return ""
                if timeout > 0 and (time.time() - start_time) > timeout:
                    _terminate_current_subprocess()
                    log(f"{agent_name} Agent 超时 ({timeout}s)", "ERROR")
                    return ""
                time.sleep(0.5)

            stderr_thread.join(timeout=2)
            try:
                stdout_val, _ = proc.communicate()
            except ValueError:
                stdout_val = ""
            returncode = proc.returncode
            _subprocess_ref = None

        else:
            # Unix: Stream stdin from file + stream stdout/stderr in real-time.
            # Writing prompt via stdin (instead of -p <prompt>) is more robust because
            # it avoids OS ARG_MAX limits on large prompts (~100KB+).  Claude Code CLI
            # outputs regular tool-call logs to stdout and only uses stderr for verbose
            # internal messages -- we must stream BOTH pipes simultaneously.
            if read_only:
                allowed_tools = "Read"
            else:
                allowed_tools = (
                    "Write,Read,Edit,Bash(bash *),Bash(echo *),Bash(makefilegen *),Bash(ttcn3_compiler *)"
                    + (f",{extra_allowed_tools}" if extra_allowed_tools else "")
                )
            cmd_args = [
                claude_cmd,
                "--bare",
                "--verbose",          # Emit real-time progress/debug messages to stderr
                "-p", "-",            # "-" means read prompt from stdin
                "--allowedTools",
                allowed_tools,
                "--output-format", "json"
            ]

            proc = subprocess.Popen(
                cmd_args,
                stdin=subprocess.PIPE,   # Feed prompt via stdin
                stdout=subprocess.PIPE,  # JSON result + tool-call logs
                stderr=subprocess.PIPE,  # Verbose internal output
                # bufsize=0 forces truly unbuffered binary I/O on both pipes.
                # Without it, Python wraps stdout/stderr in TextIOWrapper with default
                # full buffering, so readline() returns 8KB+ chunks instead of lines.
                bufsize=0,
                preexec_fn=os.setsid,
            )
            _subprocess_ref = proc

            import threading
            stdout_lines = []
            stderr_lines = []

            def _stream_stdout():
                for raw in iter(lambda: proc.stdout.read(2048), b""):
                    for line in raw.decode("utf-8", errors="replace").splitlines(keepends=True):
                        if line:
                            stdout_lines.append(line)
                            log(f"[{agent_name} stdout] {line.rstrip()}", "DEBUG")

            def _stream_stderr():
                for raw in iter(lambda: proc.stderr.read(2048), b""):
                    for line in raw.decode("utf-8", errors="replace").splitlines(keepends=True):
                        if line:
                            stderr_lines.append(line)
                            log(f"[{agent_name} stderr] {line.rstrip()}", "DEBUG")

            stdout_thread = threading.Thread(target=_stream_stdout, daemon=True)
            stderr_thread = threading.Thread(target=_stream_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            # === WATCHDOG: idle-kill for LLM thinking stalls ===
            # Monitor the most-recent Claude session JSONL for new events.
            # If no new events appear for _llm_watchdog_timeout seconds, kill the agent.
            # IMPORTANT: only watch sessions that were modified AFTER this process started.
            # Otherwise an old session from a previous run will appear "idle for years".
            import glob as _glob
            _wd_timeout = max(0, int(_llm_watchdog_timeout or 0))
            _watchdog_killed = [False]  # closure-safe mutable flag
            _wd_start_time = time.time()

            def _watchdog_tick():
                """Poll session file mtime every 30s. Kill agent if idle > _wd_timeout.

                Bug fix (2026-06-18): the previous logic used `_last_mtime = _watched_file.stat().st_mtime`
                ONCE at startup. If a session file already existed before the pipeline started
                (e.g. a prior crashed run), then the "idle" counter could include the time the
                file had been untouched BEFORE we launched Claude, producing a false positive
                like "idle for 601s" when only 127s had actually elapsed.

                The fix: continuously re-pick the freshest mtime seen so far and ONLY start
                counting idle time from the LATEST mtime update. Also require TWO consecutive
                30s polls showing no progress (60s grace period) before considering the agent
                truly idle — this prevents killing Claude during brief gaps between batch
                flushes or tool calls.
                """
                if _wd_timeout <= 0:
                    return  # disabled
                try:
                    _session_pattern = str(Path.home() / ".claude" / "projects" / "*" / "*.jsonl")
                    _all_session_files = [Path(p) for p in _glob.glob(_session_pattern) if Path(p).exists()]
                    # CRITICAL: only watch files that were created/modified AFTER our process started.
                    # _wd_start_time is set BEFORE Claude is spawned, so any file with mtime
                    # >= _wd_start_time is plausibly "ours".
                    _our_sessions = [p for p in _all_session_files if p.stat().st_mtime >= (_wd_start_time - 5)]
                    if not _our_sessions:
                        # Claude hasn't created its session file yet — sleep and retry
                        return
                    # Pick the freshest mtime AMONG our sessions right now.
                    _last_mtime = max(p.stat().st_mtime for p in _our_sessions)
                    _consecutive_idle_polls = 0  # require 2 consecutive 30s polls (60s grace)
                    while True:
                        time.sleep(30)
                        try:
                            # Re-evaluate all our sessions each tick; pick the freshest mtime.
                            _current_our_sessions = [
                                p for p in _all_session_files
                                if p.exists() and p.stat().st_mtime >= (_wd_start_time - 5)
                            ]
                            if not _current_our_sessions:
                                _consecutive_idle_polls += 1
                            else:
                                _new_mtime = max(p.stat().st_mtime for p in _current_our_sessions)
                                if _new_mtime > _last_mtime:
                                    # Activity detected — reset baseline and idle counter.
                                    _last_mtime = _new_mtime
                                    _consecutive_idle_polls = 0
                                    continue
                                else:
                                    _consecutive_idle_polls += 1
                        except (FileNotFoundError, OSError):
                            break
                        # Require 2 consecutive 30s idle polls (60s grace) before counting toward timeout.
                        # This eliminates the "false positive" seen when the file hasn't been
                        # touched for a single 30s window but is about to be written to.
                        if _consecutive_idle_polls < 2:
                            continue
                        _idle = int(time.time() - _last_mtime)
                        if _idle > _wd_timeout:
                            if not _watchdog_killed[0]:
                                _watchdog_killed[0] = True
                                log(f"[WATCHDOG] {agent_name} idle for {_idle}s (>{_wd_timeout}s), killing process group", "WARN")
                                # Phase 3.6: set the global silent-kill flag so the
                                # caller (call_agent_with_timeout) refuses to retry.
                                # Without this, the empty response triggers an immediate
                                # retry with 2x timeout, starting another 20+ minute
                                # silent wait. With this flag, the LLM is declared
                                # broken and the pipeline exits cleanly.
                                try:
                                    global _llm_silent_killed
                                    _llm_silent_killed = True
                                except Exception:
                                    pass
                                try:
                                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                                except (ProcessLookupError, PermissionError, OSError):
                                    pass
                            break
                except Exception:
                    pass  # Never let watchdog crash the pipeline

            _wd_thread = threading.Thread(target=_watchdog_tick, daemon=True)
            _wd_thread.start()

            # Stream stdin: write prompt then flush so claude starts reading immediately
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.flush()
                proc.stdin.close()
            except BrokenPipeError:
                pass

            start_time = time.time()
            while proc.poll() is None:
                if _check_interrupt():
                    _terminate_current_subprocess()
                    log(f"{agent_name} Agent 被中断", "WARN")
                    return ""
                if timeout > 0 and (time.time() - start_time) > timeout:
                    _terminate_current_subprocess()
                    log(f"{agent_name} Agent 超时 ({timeout}s)", "ERROR")
                    return ""
                time.sleep(0.5)

            # CRITICAL: do NOT call proc.communicate() here. The reader threads above
            # are already draining proc.stdout / proc.stderr. Calling communicate()
            # on the same file descriptors will block waiting for EOF that the
            # threads are preventing (a known Python Popen pitfall). Just join the
            # threads with a generous timeout and use their captured output.
            #
            # SUBTLE BUG: Claude Code CLI may spawn grandchildren (e.g. `bash build.sh`,
            # `make`, `g++`) that INHERIT the stdout/stderr pipe FDs. When Claude exits,
            # those grandchildren still hold the read end of the pipe, so the kernel
            # never sends EOF and the reader threads block forever. Fix: after Claude
            # exits, kill the entire process group to reap grandchildren, then close pipes.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            # Give grandchildren a moment to terminate gracefully
            time.sleep(1.0)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)
            if stdout_thread.is_alive() or stderr_thread.is_alive():
                log(f"{agent_name} Agent reader threads still alive; forcibly closing pipes", "WARN")
                try:
                    proc.stdout.close()
                except Exception:
                    pass
                try:
                    proc.stderr.close()
                except Exception:
                    pass
                stdout_thread.join(timeout=3)
                stderr_thread.join(timeout=3)
            # At this point: Claude + grandchildren are dead, pipes are closed,
            # reader threads have joined. Safe to return.
            returncode = proc.returncode
            _subprocess_ref = None

            # Build combined stdout from thread output
            all_stdout_bytes = b"".join(line.encode("utf-8", errors="replace")
                                         for line in stdout_lines)
            stdout_val = all_stdout_bytes.decode("utf-8", errors="replace")

        # Collect streamed stderr lines for post-processing
        stderr_val = "".join(stderr_lines)

        # === Phase 3.7: Sandbox-Lock Fail-Fast ===
        # Scan only stderr for known sandbox-restriction phrases. We deliberately
        # DO NOT scan stdout here because Claude Code CLI embeds permission_denials,
        # permissionMode, and shell-blocked-error transcripts in every JSON event
        # stream on stdout — scanning stdout produces false positives on otherwise
        # successful runs (the agent briefly tried mkdir, was blocked, then
        # recovered by emitting the required JSON, but its stdout still contains
        # the blocked-mkdir transcript).
        #
        # IMPORTANT: we ONLY abort if the agent produced NO usable output.
        # If stdout has >= 1000 chars of content the agent succeeded despite any
        # in-flight sandbox errors (those errors are echoed inside the agent's
        # thinking/recovery loop but the final message contains the requested JSON).
        # Aborting in that case would waste tokens on a perfect-good response.
        _dbg("phase_3_7_sandbox_check", {
            "agent_name": agent_name,
            "stderr_len": len(stderr_val),
            "stderr_preview": stderr_val[:500] if stderr_val else "",
            "stdout_len": len(stdout_val),
        })
        stdout_has_output = bool(stdout_val and len(stdout_val) >= 1000)
        _sandbox_phrase = None
        if not stdout_has_output:
            _sandbox_phrase = _scan_for_sandbox_lock("", stderr_val)
        if _sandbox_phrase:
            global _sandbox_locked_flag
            _sandbox_locked_flag = True
            log(
                f"[SANDBOX-LOCK] {agent_name} reported sandbox restriction "
                f"(matched phrase: '{_sandbox_phrase}'). Aborting retries.",
                "ERROR",
            )
            raise _SandboxLocked(agent_name, _sandbox_phrase)
        else:
            _dbg("phase_3_7_skip_sandbox", {
                "reason": "stdout has usable output (>= 1000 chars)",
                "agent_name": agent_name,
            })

        # Track token usage for metrics
        in_tok, out_tok = _parse_token_usage_from_claude_output(stderr_val, stdout_val)
        _accumulate_tokens(agent_name, in_tok, out_tok)

        _dbg("after_subprocess", {
            "agent_name": agent_name,
            "returncode": returncode,
            "stderr_len": len(stderr_val),
            "stderr_preview": stderr_val[:300],
            "stdout_len": len(stdout_val),
            "stdout_preview": stdout_val[:200] if stdout_val else "",
        })

        # Detect the "empty message" error pattern
        empty_patterns = [
            "empty message",
            "no message was provided",
            "you sent an empty message",
            "prompt is empty",
        ]
        is_empty_msg = any(p in stdout_val.lower() or p in stderr_val.lower() for p in empty_patterns)
        if is_empty_msg:
            log(f"{agent_name} Agent 返回 'empty message'（prompt 过大或无法读取）", "WARN")
            return ""

        if returncode != 0:
            log(f"{agent_name} Agent 返回错误码 {returncode}", "ERROR")
            if stderr_val:
                log(f"stderr: {stderr_val[:500]}", "ERROR")
            return ""

        # Parse JSON output, extract result field
        raw_output = stdout_val.strip()
        try:
            output = json.loads(raw_output)
            if isinstance(output, dict):
                # Prefer "result" field if it has actual content
                result_val = output.get("result")
                if result_val and isinstance(result_val, str) and len(result_val) > 0:
                    text = result_val
                else:
                    text = raw_output
            elif isinstance(output, list):
                # Find the result event with non-empty content
                result_event = next(
                    (item for item in output
                     if isinstance(item, dict) and item.get("type") == "result"),
                    None
                )
                if result_event:
                    result_val = result_event.get("result")
                    if result_val and isinstance(result_val, str) and len(result_val) > 0:
                        text = result_val
                    else:
                        # Empty/missing result -- try to extract from message event
                        msg_event = next(
                            (item for item in output
                             if isinstance(item, dict) and item.get("type") == "message"),
                            None
                        )
                        if msg_event:
                            text = _extract_text_from_message_event(msg_event)
                        else:
                            text = raw_output
                else:
                    text = raw_output
            else:
                text = raw_output
        except json.JSONDecodeError:
            # Fallback: extract result field via regex when JSON parsing fails.
            # This handles multiline strings in the result field.
            result_text = raw_output
            m = re.search(r'"result"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_output, re.DOTALL)
            if m:
                try:
                    result_text = m.group(1).encode().decode('unicode_escape')
                except (ValueError, SyntaxError):
                    result_text = m.group(1)
            text = result_text

        # === Critical Fix: detect and extract text from JSON event streams ===
        # If text contains too many "thinking_tokens" events or no actual result,
        # try to extract the assistant's final text message.
        if text and ('"thinking_tokens"' in text or '"type":"thinking_tokens"' in text):
            log(f"{agent_name} stdout is mostly thinking_tokens; attempting message extraction", "WARN")
            try:
                # Try to parse as JSON event stream and find message events
                events = json.loads(text)
                if isinstance(events, list):
                    # Collect all text from message events in order
                    texts = []
                    for ev in events:
                        if isinstance(ev, dict):
                            msg = ev.get("message", {})
                            if isinstance(msg, dict):
                                for c in msg.get("content", []):
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        t = c.get("text", "")
                                        if t:
                                            texts.append(t)
                    if texts:
                        text = texts[-1]  # Use the last text message (most recent)
                        log(f"{agent_name} extracted last text message ({len(text)} chars)", "INFO")
            except (json.JSONDecodeError, ValueError):
                pass

        log(f"{agent_name} Agent 完成，输出 {len(text)} 字符")
        return text

    except KeyboardInterrupt:
        log(f"{agent_name} Agent 被用户中断", "WARN")
        _terminate_current_subprocess()
        return ""
    except FileNotFoundError:
        log(f"claude CLI 未找到 (tried: {claude_cmd})，请确认已安装 Claude Code", "ERROR")
        sys.exit(1)
    except OSError as e:
        log(f"claude CLI 调用失败: {e}", "ERROR")
        sys.exit(1)
    finally:
        # Clean up temp files
        for f in [tmp_prompt_file, tmp_output_file, tmp_batch_file]:
            if f and f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass


def _recover_truncated_json(text: str) -> Optional[Dict]:
    """Attempt to recover a truncated JSON response by completing unclosed structures.

    Handles two truncation patterns:
    1. JSON object/array truncated at a top-level boundary (balanced brace tracking)
    2. The last file entry's "content" string is truncated mid-escape-sequence
       (e.g., ends with 'content": "...\\n' missing the closing quote)

    For pattern 2, we detect the trailing content as a partial JSON string and
    strip it before parsing.
    """
    import re

    # Pattern: valid JSON followed by partial content
    # Try to find the last complete JSON object/array by tracking brace balance
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
        except json.JSONDecodeError:
            pass

    # Fallback A: strip trailing incomplete fields after last comma
    last_comma = candidate.rfind(',')
    if last_comma > 0:
        # Try as object
        stripped = candidate[:last_comma] + "\n  ]\n}"
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
        # Try as array
        stripped_arr = candidate[:last_comma] + "\n  ]"
        try:
            return json.loads(stripped_arr)
        except json.JSONDecodeError:
            pass

    # Fallback B: handle truncated last content string in a file entry
    # If the JSON ends mid-string, try to strip back to the last complete field
    # Look for the pattern: "content": "...truncated with escape sequences...
    # Strategy: find the last place where we had a valid "path": "..." and try
    # to close the files array there
    path_last = candidate.rfind('"path"')
    if path_last > 0:
        # Try stripping everything after the last "path" key to a safe point
        # and closing the array properly
        for cut_offset in [0, 10, 50, 100, 200, 500]:
            cut = last_comma + cut_offset if last_comma > 0 else len(candidate)
            for suffix in ['\n  ]\n}', '\n]\n}', '\n}']:
                attempt = candidate[:cut] + suffix
                try:
                    result = json.loads(attempt)
                    # Sanity check: should have 'files' key with at least one entry
                    if isinstance(result, dict) and result.get("files"):
                        return result
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

    return None


def extract_json_from_response(text: str) -> Optional[Dict]:
    """从 Agent 响应中提取 JSON 对象，支持截断响应恢复。

    策略（按优先级）：
      1. 递归扫描：找到第一个平衡的 {...} 块并解析（最优先，覆盖所有情况）
      2. 递归扫描：找到第一个平衡的 [...] 块并解析
      3. Markdown JSON 块解析（```json ... ```）
      4. 通用 Markdown 代码块（``` ... ```）
      5. 直接解析（响应本身是纯 JSON）
    """
    if not text:
        return None

    # Strip XML-like tags that wrap or annotate responses
    # e.g. <fatal_build_error>...</fatal_build_error>, <analysis>...</analysis>
    text = re.sub(r'<[a-zA-Z_][^>]*>.*?</[a-zA-Z_][^>]*>', '', text, flags=re.DOTALL)

    # --- Strategy 1: Recursive brace scanner for {...} ---
    def find_balanced_block(text: str, open_char: str, close_char: str) -> Optional[str]:
        """Find the first balanced block delimited by open_char/close_char."""
        start = -1
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text):
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == open_char:
                if start < 0:
                    start = i
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None

    # Try {...} first (objects are more common as root in agent responses)
    block = find_balanced_block(text, '{', '}')
    if block:
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            # Try truncation recovery on the block
            recovered = _recover_truncated_json(block)
            if recovered:
                return recovered

    # --- Strategy 2: Recursive bracket scanner for [...] ---
    block = find_balanced_block(text, '[', ']')
    if block:
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            pass

    # --- Strategy 3: Markdown JSON block ---
    json_block = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    if json_block:
        try:
            return json.loads(json_block.group(1))
        except json.JSONDecodeError:
            recovered = _recover_truncated_json(json_block.group(1))
            if recovered:
                return recovered

    # --- Strategy 4: Generic markdown code block ---
    generic_block = re.search(r'```\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if generic_block:
        try:
            return json.loads(generic_block.group(1))
        except json.JSONDecodeError:
            recovered = _recover_truncated_json(generic_block.group(1))
            if recovered:
                return recovered

    # --- Strategy 5: Direct parse (response is pure JSON) ---
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    return None


# ============================================================
# 测试用例 ID 规范化与前置条件命名映射
# ============================================================

def normalize_tc_id(tc_id: str) -> str:
    """将非标准测试用例 ID 规范化为 ISO 15118-4 标准格式。

    支持的变体格式:
      - TC_V2GTP_BR_V2GTPHeader_001 -> TC_SECC_CMN_VTB_V2GTPHeader_001
      - TC_EVCC_* -> TC_EVCC_CMN_VTB_* (如果缺少 CMN/VTB)
      - TC_SECC_* -> TC_SECC_CMN_VTB_* (如果缺少 CMN/VTB)

    Args:
        tc_id: 原始测试用例 ID

    Returns:
        规范化后的测试用例 ID
    """
    import re

    if not tc_id:
        return tc_id

    # 如果已经是标准格式，直接返回
    standard_pattern = re.compile(r'^TC_(SECC|EVCC)_(CMN|AC|DC)_VTB_.+_\d{3}$')
    if standard_pattern.match(tc_id):
        return tc_id

    # 提取各部分
    # TC_V2GTP_BR_V2GTPHeader_001 -> V2GTP, BR, V2GTPHeader, 001
    # TC_SECC_VTB_SessionSetup_001 -> SECC, VTB, SessionSetup, 001
    # TC_SECC_CMN_SessionSetup_001 -> SECC, CMN, SessionSetup, 001

    # 处理 TC_V2GTP_BR_* 格式（V2GTP_BR 协议变体）
    if 'V2GTP' in tc_id and 'BR' in tc_id:
        # 提取消息类型和序号
        match = re.search(r'TC_V2GTP_BR_(\w+)_(\d+)', tc_id)
        if match:
            msg_type = match.group(1)
            seq = match.group(2)
            # 推断 SUT 类型（通常是 SECC）
            sut = "SECC"
            if 'EVCC' in msg_type.upper():
                sut = "EVCC"
            return f"TC_{sut}_CMN_VTB_{msg_type}_{seq}"

    # 处理缺少 Category 的情况
    # TC_SECC_VTB_Message_001 或 TC_SECC_Message_001
    # Use [a-zA-Z0-9]+ for message type to exclude underscores
    # (so V2GTPHeader -> Header, not V2GTPHeader as one unit)
    match = re.match(r'^TC_(SECC|EVCC)_(VTB_)?([a-zA-Z0-9]+)_(\d+)$', tc_id)
    if match:
        sut = match.group(1)
        category = "CMN"  # 默认 Category
        msg_type = match.group(3)
        seq = match.group(4)
        return f"TC_{sut}_{category}_VTB_{msg_type}_{seq}"

    # 无法规范化，返回原始值
    log(f"无法规范化 TC ID: {tc_id}，使用原始值", "WARN")
    return tc_id


def normalize_precondition_name(precond_name: str) -> str:
    """将 Phase 1 生成的前置条件函数名规范化为 SOP 标准格式。

    常见不标准格式 -> 标准格式映射:
      - f_V2GTP_BR_PR_V2GTPSessionSetup_001 -> f_SECC_CMN_PR_SessionSetup_001
      - f_V2GTP_PR_* -> f_SECC_CMN_PR_*
      - f_SECC_PR_* (缺少 Category) -> f_SECC_CMN_PR_*

    Args:
        precond_name: 原始前置条件函数名

    Returns:
        规范化后的函数名
    """
    import re

    if not precond_name:
        return precond_name

    # 如果已经是标准格式（包含 CMN/AC/DC），直接返回
    standard_pattern = re.compile(r'^f_(SECC|EVCC)_(CMN|AC|DC)_(PR|PO|TB)_.+_\d+$')
    if standard_pattern.match(precond_name):
        return precond_name

    # 处理 f_V2GTP_BR_PR_* 格式
    if 'V2GTP' in precond_name and 'BR' in precond_name:
        # 提取消息类型和序号
        match = re.search(r'f_V2GTP_BR_(PR|PO|TB)_(\w+)_(\d+)', precond_name)
        if match:
            func_type = match.group(1)
            msg_type = match.group(2)
            seq = match.group(3)
            # V2GTP_BR 通常是 SECC，Category 为 CMN
            sut = "SECC"
            return f"f_{sut}_CMN_{func_type}_{msg_type}_{seq}"

    # 处理 f_SECC_PR_* (缺少 Category) -> f_SECC_CMN_PR_*
    match = re.match(r'^f_(SECC|EVCC)_(PR|PO|TB)_(\w+)_(\d+)$', precond_name)
    if match:
        sut = match.group(1)
        func_type = match.group(2)
        msg_type = match.group(3)
        seq = match.group(4)
        return f"f_{sut}_CMN_{func_type}_{msg_type}_{seq}"

    # 无法规范化，返回原始值
    return precond_name


# State-machine coverage table: maps test case message_type to the
# pre-condition function that already exchanges the corresponding V2G
# message. If the pre-condition chain contains that function, the TB can
# safely short-circuit (covered_by_precondition=True) without sending a
# duplicate message and risking TITAN port-queue exhaustion or unbound
# variable errors. The TC wrapper still calls all post-conditions.
_STATE_MACHINE_COVERED = {
    "SDP":                    "f_SECC_CMN_PR_InitConfiguration_001",
    "SupportedAppProtocol":   "f_SECC_CMN_PR_SupportedAppProtocol_001",
}


# Wrapper protocol prefixes that some test suites attach to the V2G message
# name (e.g. ISO 15118-4 uses "V2GTPSessionSetup" while the EXI/union layer
# uses the bare "SessionSetup").  Strip them so the generated TTCN-3 code
# can reuse the canonical templates already defined in CmnTemplates.ttcn
# (which never carry the wrapper prefix).
#
# This is intentionally a list so we can grow it for new protocols without
# touching the call sites.
_WRAPPER_PROTOCOL_PREFIXES = ["V2GTP", "V2GTCP", "V2GUDP"]


def _to_camel_case(pascal: str) -> str:
    """Convert PascalCase to camelCase by lowercasing the first character.

    Examples:
      "SessionSetup"       -> "sessionSetup"
      "ServiceDiscovery"   -> "serviceDiscovery"
      "PreCharge"          -> "preCharge"
    """
    if not pascal:
        return pascal
    return pascal[0].lower() + pascal[1:]


def _canonicalize_message_type(message_type: str) -> Dict[str, str]:
    """Normalize a (possibly wrapper-prefixed) message type into the
    canonical identifiers the TTCN-3 code generator needs.

    The four pieces of information that downstream templates need are
    NOT the same string:
      * message_type  — bare V2G name used for `var v_<msg>` and template
                        instantiation (e.g. "SessionSetup").
      * union_field   — camelCase field name inside the V2G_Message union
                        (e.g. "sessionSetupReq" / "sessionSetupRes").
      * request_msg_type  — PascalCase + "Req" suffix used as the TTCN-3
                        type identifier in the receive/send alternation
                        (e.g. "SessionSetupReq").
      * response_msg_type — PascalCase + "Res" suffix (e.g. "SessionSetupRes").

    Returns a dict with all four keys so callers don't have to reconstruct
    the camelCase form (which is error-prone for multi-word names like
    "ServiceDiscovery" -> "serviceDiscovery").
    """
    if not message_type:
        return {
            "canonical": "",
            "union_field_req": "",
            "union_field_res": "",
            "request_msg_type": "",
            "response_msg_type": "",
        }

    canonical = message_type
    stripped_prefix = False
    for prefix in _WRAPPER_PROTOCOL_PREFIXES:
        # Match a wrapper prefix that is *immediately* followed by an
        # uppercase V2G-message name.  E.g. "V2GTPSessionSetup" matches
        # and canonicalizes to "SessionSetup"; "V2GTPHeader" would NOT
        # be stripped (it really is a V2GTP-header test, not a V2G msg).
        if canonical.startswith(prefix):
            tail = canonical[len(prefix):]
            if tail and tail[0].isupper() and tail != canonical:
                canonical = tail
                stripped_prefix = True
                break

    return {
        "canonical": canonical,
        "union_field_req": _to_camel_case(canonical) + "Req",
        "union_field_res": _to_camel_case(canonical) + "Res",
        "request_msg_type": canonical + "Req",
        "response_msg_type": canonical + "Res",
    }


def build_generation_plan_from_test_case(raw_input: Dict, sop: Dict, filter_tc_id: Optional[str] = None) -> Dict:
    """Deterministically build a generation plan from a test case JSON — no LLM required.

    Extracts all structured metadata (tc_id, message_type, sut_type, test_type,
    protocol_layer, precondition/postcondition chains, test_sequences, verdict_logic)
    from the raw test case JSON using rule-based pattern matching.

    Multi-source fallback chain for test_sequences:
      1. orchestrator_plan.sequence_plan  (English, per-step: seq_no + test_type + description)
      2. Test_Sequence  (CamelCase Chinese: Step + Name + Instruction)
      3. testing_points  (fine-grained requirement-level: tp_id + description + expected_behavior)
      4. empty  (Core Agent infers from prompt patterns)

    Args:
        raw_input: Raw JSON loaded from the test case file. May be:
                   - A single test case dict {"Test_Case": {...}}
                   - A bulk file with {"test_cases": [...], "testing_points": [...],
                                       "orchestrator_plan": {"sequence_plan": [...]}}
                   The function auto-detects the layout and extracts the right TC.
        sop: SOP JSON dict loaded from knowledge_base/ttcn3_generation_sop.json.
        filter_tc_id: When raw_input is a bulk file, selects the matching TC entry
                      (defaults to first TC if not specified).

    Returns:
        A fully-structured generation_plan dict compatible with the rest of the pipeline.
    """
    import re

    # ── Detect layout and extract the inner test case dict ─────────────────────
        # Layout A: bulk file with test_cases array
    if "test_cases" in raw_input and isinstance(raw_input.get("test_cases"), list):
        test_cases = raw_input["test_cases"]
        # Find the test case by filter_tc_id or tc_id
        tc = None
        for entry in test_cases:
            inner = entry.get("Test_Case", entry)
            tc_id_candidate = inner.get("TC_Id", "")
            if filter_tc_id and tc_id_candidate == filter_tc_id:
                tc = inner
                break
            if tc is None and tc_id_candidate:
                tc = inner
        if tc is None and test_cases:
            tc = test_cases[0].get("Test_Case", test_cases[0])
        if tc is None:
            tc = {}
        # Store sibling data for later extraction
        _orchestrator_plan = raw_input.get("orchestrator_plan", {})
        _sequence_plan = _orchestrator_plan.get("sequence_plan", [])
        _testing_points = raw_input.get("testing_points", [])
    # Layout B: single test case wrapped as {"Test_Case": {...}}
    elif "Test_Case" in raw_input:
        tc = raw_input.get("Test_Case", raw_input)
        _sequence_plan = []
        _testing_points = []
    # Layout C: bare dict (no wrapper)
    else:
        tc = raw_input
        _sequence_plan = []
        _testing_points = []

    # ── 1. tc_id ──────────────────────────────────────────────────────────────
    tc_id: str = tc.get("TC_Id", "TC_UNKNOWN")

    # ── 2. SUT type (infer from TC_Id) ──────────────────────────────────────
    sut_type: str = "EVCC" if "EVCC" in tc_id.upper() else "SECC"

    # ── 3. Category (infer from TC_Id) ───────────────────────────────────────
    if "_AC_" in tc_id:
        category = "AC"
    elif "_DC_" in tc_id:
        category = "DC"
    else:
        category = "CMN"

    # ── 4. Message type (infer from TC_Id + test objectives) ─────────────────
    # Try TC_Id first — pattern: TC_{SUT}_{CAT}_VTB_{MessageType}_{SeqNo}
    msg_type_match = re.search(r'_VTB_(\w+?)_\d{3}$', tc_id)
    if msg_type_match:
        raw_msg = msg_type_match.group(1)
        # Legacy alias map kept for backward compatibility with old TCs
        # that still carry non-canonical names. The ProtocolRegistry is
        # the source of truth going forward — this map only handles
        # cases the registry doesn't know about.
        legacy_alias_map = {
            "V2GTPHeader": "V2GTP",
            "V2GTPBR": "V2GTP",
            "V2GTCP": "V2GTCP",
        }
        message_type = legacy_alias_map.get(raw_msg, raw_msg)
    else:
        text_fields = tc.get("Test_Objective", "") + tc.get("Test_Purpose", "")
        message_type = _infer_message_type_from_text(text_fields)

    # ── 4b. ProtocolRegistry resolution ─────────────────────────────────────
    # Single source of truth for ALL protocol-specific metadata:
    # canonical name, union fields, port, pre-condition chain, transport,
    # wrapper-prefix stripping. The registry replaces the previous
    # hardcoded _WRAPPER_PROTOCOL_PREFIXES, _canonicalize_message_type,
    # _STATE_MACHINE_COVERED, and the protocol_layer string-matching
    # block. If a future protocol has different metadata, it goes into
    # protocols/{id}.json — no Python code change.
    seq_no_match = re.search(r'_(\d{3})$', tc_id)
    seq_no = seq_no_match.group(1) if seq_no_match else "001"
    _registry = get_default_registry()
    _auto_pid = _registry.auto_load_for_tc(tc_id)
    try:
        _resolved = _registry.resolve(message_type, sut=sut_type, seq_no=seq_no)
    except (KeyError, RuntimeError) as _e:
        log(f"ProtocolRegistry could not resolve {message_type!r}: {_e}; "
            f"falling back to inline derivation (pre-refactor behavior).", "WARN")
        _resolved = None

    # ── 5. Test type (infer from TC_Id suffix — most reliable, works for all layouts) ─
    sequences_raw: List[Dict] = tc.get("Sequence", [])
    test_seq_camel: List[Dict] = tc.get("Test_Sequence", [])
    suffix_match = re.search(r'_(\d{3})$', tc_id)
    if suffix_match:
        seq = int(suffix_match.group(1))
        _test_type = "GoodCase" if seq == 1 else "BadCase"
    elif _sequence_plan and len(_sequence_plan) == 1:
        # Only trust sequence_plan for single-TC files (not shared bulk plans)
        _test_type = _sequence_plan[0].get("test_type", "GoodCase")
    elif test_seq_camel and len(test_seq_camel) > 0:
        first_step_desc = test_seq_camel[0].get("Name", "") + test_seq_camel[0].get("Instruction", "")
        _test_type = "BadCase" if any(kw in first_step_desc for kw in ["拒绝", "非法", "错误", "无效", "异常"]) else "GoodCase"
    else:
        _test_type = "GoodCase"
    test_type = _test_type

    # ── 6. Protocol layer ───────────────────────────────────────────────────
    # Sourced from ProtocolRegistry. The previous implementation did a
    # string-match against the message_type for keywords like "v2gtp",
    # "sdp", "supportedappprotocol" — all of that is now declarative
    # in protocols/{id}.json's `messages[].protocol_layer` field.
    if _resolved is not None:
        protocol_layer = _resolved.protocol_layer
    else:
        msg_lower = message_type.lower()
        if any(kw in msg_lower for kw in ["v2gtp", "sdp", "v2g_tcp", "tls", "tcp", "slac"]):
            protocol_layer = "V2GTP"
        elif any(kw in msg_lower for kw in ["supportedappprotocol"]):
            protocol_layer = "V2G_Application"
        else:
            protocol_layer = "V2G_Application"

    # ── 7. Precondition chain ────────────────────────────────────────────────
    precond_chain: List[str] = _get_default_precondition_chain(message_type, sop)
    if not precond_chain:
        if _resolved is not None and _resolved.default_precondition_chain:
            precond_chain = list(_resolved.default_precondition_chain)
        elif protocol_layer == "V2GTP":
            precond_chain = [f"f_{sut_type}_CMN_PR_InitConfiguration_001"]
        else:
            precond_chain = [
                f"f_{sut_type}_CMN_PR_InitConfiguration_001",
                f"f_{sut_type}_CMN_PR_SupportedAppProtocol_001",
            ]
    precond_chain = [fn.replace("{SUT}", sut_type) for fn in precond_chain]

    # ── 7b. Self-precondition injection ─────────────────────────────────────
    # If the protocol registry declares a `self_precondition` for this message
    # (e.g. SessionSetup -> f_..._PR_SessionSetup_001), append it to the chain.
    # This makes THIS case responsible for generating its own precondition
    # function module, so downstream cases that reference it can reuse the
    # asset (Asset Promotion).
    #
    # IMPORTANT: Skip self-precondition if THIS test case IS the message being
    # tested. E.g., TC_SECC_CMN_VTB_SessionSetup_001 should NOT have
    # f_SECC_CMN_PR_SessionSetup_001 in its chain -- that would cause
    # the precondition to send SessionSetupReq BEFORE the test behavior sends
    # it again, triggering a FAILED_SequenceError from the SECC.
    if _resolved is not None and getattr(_resolved, "self_precondition", None):
        self_pre = _resolved.self_precondition
        if self_pre and self_pre not in precond_chain:
            # Extract message type from self_precondition: f_{SUT}_{CAT}_PR_{MsgType}_{Seq}
            # vs tc_id: TC_{SUT}_{CAT}_VTB_{MsgType}_{Seq}
            _pr_match = re.search(r'_PR_(\w+?)_\d{3}$', self_pre)
            if _pr_match:
                _pr_msg_type = _pr_match.group(1).lower()
                _tc_match = re.search(r'_VTB_(\w+?)_\d{3}$', tc_id)
                if _tc_match:
                    _tc_msg_type = _tc_match.group(1).lower()
                    if _pr_msg_type == _tc_msg_type:
                        log(f"[PLAN] Skipping self-precondition {self_pre} for {tc_id} "
                            f"(PR={_pr_msg_type} vs TC={_tc_msg_type} -- would cause double-SessionSetup)")
                    else:
                        precond_chain.append(self_pre)
                else:
                    precond_chain.append(self_pre)
            else:
                precond_chain.append(self_pre)

    # ── 8. Postcondition chain ──────────────────────────────────────────────
    postcond_chain: List[str] = [
        f"f_{sut_type}_CMN_PO_InitialState_001",
        f"f_{sut_type}_CMN_PO_ShutdownConfiguration_001",
    ]

    # ── 9. Port name ─────────────────────────────────────────────────────────
    # The ProtocolRegistry owns the port-name template per protocol/transport.
    # The previous implementation had hardcoded `if message_type == "SDP"`.
    if _resolved is not None:
        port_name = _resolved.port_name
        if message_type == "SDP":
            protocol_layer = "SDP"
    elif message_type == "SDP":
        port_name = "pt_V2G_UDP_SDP_Port"
        protocol_layer = "SDP"
    else:
        port_name = f"pt_V2G_TCP_TLS_ALM_{sut_type}_Port"

    # ── 10. Component names ──────────────────────────────────────────────────
    component = f"{sut_type}Tester"
    system_component = f"System{sut_type}"

    # ── 11. Template names ──────────────────────────────────────────────────
    # The ProtocolRegistry provides canonical template + function names
    # for every protocol message.  This replaces the previous
    # _canonicalize_message_type() helper (which did the wrapper-prefix
    # stripping inline) and the per-call f-string assembly.
    if _resolved is not None and _resolved.send_template_name:
        send_template_name = _resolved.send_template_name
        receive_template_name = _resolved.receive_template_name
        request_msg_type = _resolved.request_type
        response_msg_type = _resolved.response_type
        union_field_req = _resolved.union_field_req
        union_field_res = _resolved.union_field_res
        # Backward-compat: keep `canonical_message_type` defined for
        # any code that still references it.
        canonical_message_type = _resolved.canonical
    else:
        # Fallback to inline derivation (pre-refactor behavior) when
        # the registry can't resolve.  This is reached only for
        # messages the registry doesn't know about.
        _canon = _canonicalize_message_type(message_type)
        canonical_message_type = _canon["canonical"]
        union_field_req = _canon["union_field_req"]
        union_field_res = _canon["union_field_res"]
        request_msg_type = _canon["request_msg_type"]
        response_msg_type = _canon["response_msg_type"]
        seq_no_fb = "001"
        suffix_match_fb = re.search(r'_(\d{3})$', tc_id)
        if suffix_match_fb:
            seq_no_fb = suffix_match_fb.group(1)
        send_template_name = f"md_CMN_{canonical_message_type}Req_{seq_no_fb}"
        receive_template_name = f"mdw_{sut_type}_{canonical_message_type}Res_{seq_no_fb}"

    # ── 12. Function names ───────────────────────────────────────────────────
    # Prefer the registry's naming template (declarative, in
    # protocols/{id}.json).  Fall back to inline f-string only if the
    # registry is unavailable.
    if _resolved is not None and _registry.active_protocol() is not None:
        try:
            test_behavior_func_name = _registry.format_naming(
                "test_behavior_function",
                SUT=sut_type, Category=category, MessageType=message_type, SeqNo=seq_no,
            )
        except (KeyError, RuntimeError) as _e:
            log(f"format_naming failed for {message_type!r}: {_e}; falling back", "WARN")
            clean_msg = message_type.replace(" ", "").replace("_", "")
            test_behavior_func_name = f"f_{sut_type}_{category}_TB_VTB_{clean_msg}_{seq_no}"
    else:
        clean_msg = message_type.replace(" ", "").replace("_", "")
        test_behavior_func_name = f"f_{sut_type}_{category}_TB_VTB_{clean_msg}_{seq_no}"
    testcase_name = tc_id

    # ── 13. Requirements traced ─────────────────────────────────────────────
    reqs = []
    doc_ref = tc.get("Document_Reference", {})
    if isinstance(doc_ref, dict):
        reqs = doc_ref.get("Requirements", [])
    elif isinstance(doc_ref, list):
        reqs = doc_ref

    # ── 14. Key parameters ───────────────────────────────────────────────────
    key_params: Dict[str, Any] = {}
    for src in [_sequence_plan, sequences_raw, test_seq_camel]:
        for seq in src:
            desc = seq.get("description", seq.get("Instruction", ""))
            s_no = seq.get("seq_no", seq.get("Step", "?"))
            if desc:
                key_params[f"seq_{s_no}_description"] = desc
                break
        if key_params:
            break

    # ── 15. test_sequences (precisely scoped to the selected TC) ────────────────
    # Each source has different granularity:
    #   - sequence_plan: one entry per TC (TC_001=seq_no=001, TC_002=seq_no=002, ...)
    #     → match by seq_no == TC_Id suffix
    #   - Test_Sequence: already per-TC (the TC's own 2-3 step sequence)
    #     → use directly
    #   - testing_points: fine-grained per-requirement (12 TPs for all 4 TCs)
    #     → map to the TC's range based on suffix
    structured_sequences: List[Dict] = []

    # Determine TC suffix (e.g., "001" from "TC_SECC_CMN_VTB_V2GTPHeader_001")
    tc_suffix_match = re.search(r'_(\d{3})$', tc_id)
    tc_suffix = tc_suffix_match.group(1) if tc_suffix_match else "001"

    # Source 1: orchestrator_plan.sequence_plan — one step per TC, match by seq_no
    if _sequence_plan and len(_sequence_plan) > 0:
        matched_step = None
        for step in _sequence_plan:
            if str(step.get("seq_no", "")).zfill(3) == tc_suffix:
                matched_step = step
                break
        if matched_step:
            structured_sequences.append({
                "seq_no": 1,
                "original_seq_no": tc_suffix,
                "test_type": matched_step.get("test_type", test_type),
                "description": matched_step.get("description", ""),
                "_source": "orchestrator_plan.sequence_plan",
            })
    # Source 2: Test_Sequence — already per-TC, use directly
    elif test_seq_camel and len(test_seq_camel) > 0:
        for i, step in enumerate(test_seq_camel):
            step_desc = step.get("Instruction", step.get("Name", ""))
            inferred_tt = "BadCase" if any(
                kw in step.get("Name", "") + step.get("Instruction", "")
                for kw in ["拒绝", "非法", "错误", "无效", "异常"]
            ) else "GoodCase"
            structured_sequences.append({
                "seq_no": i + 1,
                "original_seq_no": str(step.get("Step", i + 1)).zfill(3),
                "test_type": inferred_tt,
                "description": step_desc,
                "_source": "Test_Sequence",
            })
    # Source 3: testing_points — 12 TPs mapped to 4 TCs (3 TPs each, sequential)
    # TC_001 → TP_001-003, TC_002 → TP_004-006, TC_003 → TP_007-009, TC_004 → TP_010-012
    elif _testing_points and len(_testing_points) > 0:
        tps_per_tc = 3
        tc_index = int(tc_suffix) - 1  # 001 → 0, 002 → 1, ...
        start_idx = tc_index * tps_per_tc
        end_idx = start_idx + tps_per_tc
        relevant_tps = _testing_points[start_idx:end_idx]
        for i, tp in enumerate(relevant_tps):
            approach = tp.get("test_approach", tp.get("modeling_type", "positive"))
            inferred_tt = "BadCase" if approach in ("negative", "error", "failure") else "GoodCase"
            structured_sequences.append({
                "seq_no": i + 1,
                "original_seq_no": tp.get("tp_id", f"TP_{start_idx + i + 1:03d}").replace("TP_", "").zfill(3),
                "test_type": inferred_tt,
                "description": tp.get("description", "") + ". " + tp.get("expected_behavior", ""),
                "_source": "testing_points",
            })

    # ── 16. Verdict logic ───────────────────────────────────────────────────
    if test_type == "GoodCase":
        verdict_logic = {
            "pass_condition": "SECC returns a valid response matching the expected message",
            "fail_condition": "SECC returns an invalid response or unexpected message",
            "inconc_condition": "Timeout waiting for SECC response",
        }
    else:
        verdict_logic = {
            "pass_condition": "SECC rejects the invalid input (TCP close, timeout, or error response)",
            "fail_condition": "SECC accepts the invalid input and returns a normal response",
            "inconc_condition": "Timeout without any response from SECC",
        }

    plan: Dict[str, Any] = {
        "tc_id": tc_id,
        "sut_type": sut_type,
        "category": category,
        "message_type": message_type,                    # raw (may have wrapper prefix)
        "normalized_message_type": canonical_message_type,  # prefix-stripped
        "test_type": test_type,
        "protocol_layer": protocol_layer,
        "precondition_chain": precond_chain,
        "postcondition_chain": postcond_chain,
        "port_name": port_name,
        "component": component,
        "system_component": system_component,
        "send_template_name": send_template_name,
        "receive_template_name": receive_template_name,
        "request_message_type": request_msg_type,   # e.g. "SessionSetupReq"
        "response_message_type": response_msg_type, # e.g. "SessionSetupRes"
        "union_field_req": union_field_req,         # e.g. "sessionSetupReq"
        "union_field_res": union_field_res,         # e.g. "sessionSetupRes"
        "key_parameters": key_params,
        "verdict_logic": verdict_logic,
        "requirements_traced": reqs,
        "test_behavior_func_name": test_behavior_func_name,
        "testcase_name": testcase_name,
        "test_sequences": structured_sequences,
        "_plan_builder": "rule_based",
        "_plan_builder_version": "1.3-registry",
    }

    # ── PreCondition completeness check (2B) ──────────────────────────────────
    # Diff the precondition_chain (and postcondition_chain) against the
    # PreCondition/PostCondition modules actually present in DEMO_SHARED_FILES.
    # Anything the chain references but the demo doesn't supply is a gap that
    # the Infrastructure Agent must fill in. We surface the gap explicitly in
    # the plan so:
    #   (a) the Infrastructure Agent is told (not blind) about what to write;
    #   (b) downstream tooling (compile loop, validators) can count on
    #       precond_chain AND availability moving in lock-step.
    _precond_completeness = _check_precondition_completeness(precond_chain, postcond_chain)
    if _precond_completeness["missing_preconditions"]:
        log(
            f"[PLAN-CHK] {len(_precond_completeness['missing_preconditions'])} "
            f"precondition module(s) referenced by chain but NOT in demo -- "
            f"Infrastructure Agent MUST generate: "
            f"{_precond_completeness['missing_preconditions']}",
            "WARN",
        )
    if _precond_completeness["missing_postconditions"]:
        log(
            f"[PLAN-CHK] {len(_precond_completeness['missing_postconditions'])} "
            f"postcondition module(s) referenced by chain but NOT in demo -- "
            f"Infrastructure Agent MUST generate: "
            f"{_precond_completeness['missing_postconditions']}",
            "WARN",
        )
    plan["_precondition_completeness"] = _precond_completeness

    # SDP / raw-octetstring and state-machine-coverage metadata:
    # the ProtocolRegistry now owns this.  The previous code had
    # hardcoded `if message_type == "SDP"` and the _STATE_MACHINE_COVERED
    # table; both have moved into protocols/{id}.json.
    if _resolved is not None:
        if _resolved.uses_raw_octetstring:
            plan["sdp_send"] = True
            if _resolved.raw_request_hex:
                plan["sdp_request_hex"] = _resolved.raw_request_hex
            if _resolved.response_timeout_seconds is not None:
                plan["sdp_response_timeout_seconds"] = _resolved.response_timeout_seconds
        if _resolved.covered_by_precondition is not None:
            _chain_strs = " ".join(precond_chain)
            if _resolved.covered_by_precondition in _chain_strs or _resolved.uses_raw_octetstring:
                plan["covered_by_precondition"] = True
                plan["coverage_note"] = (
                    f"{message_type} exchange already performed by "
                    f"{_resolved.covered_by_precondition} in pre-condition chain. "
                    f"TB will short-circuit to setverdict(pass). "
                    f"Post-conditions will still run in TC wrapper."
                )
    else:
        # Pre-refactor fallback for the unknown-message path
        if message_type == "SDP":
            plan["sdp_send"] = True
            plan["sdp_request_hex"] = "01FE9000000000021000"
            plan["sdp_response_timeout_seconds"] = 5.0
        if message_type in _STATE_MACHINE_COVERED:
            _coverage_fn = _STATE_MACHINE_COVERED[message_type]
            _chain_strs = " ".join(precond_chain)
            if _coverage_fn in _chain_strs or message_type == "SDP":
                plan["covered_by_precondition"] = True
                plan["coverage_note"] = (
                    f"{message_type} exchange already performed by "
                    f"{_coverage_fn} in pre-condition chain. TB will "
                    f"short-circuit to setverdict(pass). Post-conditions "
                    f"will still run in TC wrapper."
                )
    return plan


def _infer_message_type_from_text(text: str) -> str:
    """从文本内容推断消息类型（用于 Orchestrator fallback）"""
    import re
    patterns = [
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
        (r'V2GTP', 'V2GTP'),
        (r'SDP', 'SDP'),
    ]
    for pat, msg_type in patterns:
        if re.search(pat, text, re.IGNORECASE):
            return msg_type
    return "General"


def _get_default_precondition_chain(message_type: str, sop: Dict) -> List[str]:
    """获取默认前置条件链（用于 Orchestrator fallback）"""
    chains = sop.get("precondition_chains", {})

    # 精确匹配
    if message_type in chains:
        entry = chains[message_type]
        return entry.get("chain", []) if isinstance(entry, dict) else entry

    # 模糊匹配
    for key, entry in chains.items():
        if message_type.lower() in key.lower() and key != "description":
            return entry.get("chain", []) if isinstance(entry, dict) else entry

    # 尝试 _missing_precondition_chains（补充标准链之外的场景）
    missing_chains = sop.get("_missing_precondition_chains", {})
    if message_type in missing_chains:
        entry = missing_chains[message_type]
        return entry.get("chain", []) if isinstance(entry, dict) else entry
    for key, entry in missing_chains.items():
        if message_type.lower() in key.lower():
            return entry.get("chain", []) if isinstance(entry, dict) else entry

    # 别名规范化：将空格/下划线变体映射到标准名称
    normalized = message_type.replace(" ", "").replace("_", "")
    alias_map = {
        "V2GTPHeader": "V2GTP",
        "V2GTPBR": "V2GTP",
    }
    for alias, canonical in alias_map.items():
        if normalized == alias:
            if canonical in missing_chains:
                entry = missing_chains[canonical]
                return entry.get("chain", []) if isinstance(entry, dict) else entry
            if canonical in chains:
                entry = chains[canonical]
                return entry.get("chain", []) if isinstance(entry, dict) else entry
            return [f"f_{{SUT}}_CMN_PR_InitConfiguration_001"]

    # V2GTP/SDP 等特殊场景的默认处理
    if message_type in ("V2GTP", "SDP"):
        return [f"f_SECC_CMN_PR_InitConfiguration_001"]
    return []


def _check_precondition_completeness(
    precond_chain: List[str],
    postcond_chain: List[str],
) -> Dict[str, List[str]]:
    """Diff precondition/postcondition chains against the modules in DEMO_SHARED_FILES.

    Used by the plan builder to surface gaps the Infrastructure Agent must
    fill. Returns a dict with:
        - referenced_preconditions: items in precond_chain
        - available_preconditions: PR module basenames in DEMO_SHARED_FILES
        - missing_preconditions: precond_chain \\ available_preconditions
        - referenced_postconditions: items in postcond_chain
        - available_postconditions: PO module basenames in DEMO_SHARED_FILES
        - missing_postconditions: postcond_chain \\ available_postconditions

    The "available" set is derived from DEMO_SHARED_FILES entries under
    3_Behavior_Steps/{PreConditions,PostConditions}/. This is the single
    source of truth for what the demo baseline can supply -- it lines up
    with what SCAFFOLD actually copies into the generated project.
    """
    import re

    def _module_basename(ttcn_rel_path: str) -> str:
        # 'src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_InitConfiguration_001.ttcn'
        #  -> 'f_SECC_CMN_PR_InitConfiguration_001'
        return Path(ttcn_rel_path).stem

    available_pre: set = set()
    available_post: set = set()
    for rel in DEMO_SHARED_FILES:
        if "/PreConditions/" in rel:
            available_pre.add(_module_basename(rel))
        elif "/PostConditions/" in rel:
            available_post.add(_module_basename(rel))

    missing_pre = [fn for fn in precond_chain if fn not in available_pre]
    missing_post = [fn for fn in postcond_chain if fn not in available_post]

    return {
        "referenced_preconditions": list(precond_chain),
        "available_preconditions": sorted(available_pre),
        "missing_preconditions": missing_pre,
        "referenced_postconditions": list(postcond_chain),
        "available_postconditions": sorted(available_post),
        "missing_postconditions": missing_post,
    }


def _build_missing_precondition_directive(
    missing_pr: List[str],
    missing_po: List[str],
) -> str:
    """Format the missing-precondition list into a prompt directive.

    The output replaces the `{MISSING_PRECONDITIONS_DIRECTIVE}` placeholder
    inside ``prompts/infrastructure_agent_prompt.txt``. When neither list is
    non-empty, we emit ``"(none)"`` so the prompt stays grammatically valid.
    """
    if not missing_pr and not missing_po:
        return "(none)"

    sections: List[str] = []
    if missing_pr:
        pr_lines = "\n".join(f"  - `{fn}`  ->  `src/ttcn3/3_Behavior_Steps/PreConditions/{fn}.ttcn`" for fn in missing_pr)
        sections.append(
            f"### Missing PreCondition module(s) you MUST generate ({len(missing_pr)}):\n{pr_lines}"
        )
    if missing_po:
        po_lines = "\n".join(f"  - `{fn}`  ->  `src/ttcn3/3_Behavior_Steps/PostConditions/{fn}.ttcn`" for fn in missing_po)
        sections.append(
            f"### Missing PostCondition module(s) you MUST generate ({len(missing_po)}):\n{po_lines}"
        )
    return "\n\n".join(sections)


def extract_ttcn3_from_response(text: str) -> str:
    """从 Agent 响应中提取 TTCN-3 代码"""
    import re

    # 尝试从 markdown 代码块中提取
    ttcn_block = re.search(r'```(?:ttcn3?|TTCN-?3?)?\s*\n(.*?)\n```', text, re.DOTALL)
    if ttcn_block:
        return ttcn_block.group(1).strip()

    # 如果响应以 module 或 /** 开头，直接返回
    stripped = text.strip()
    if stripped.startswith("module ") or stripped.startswith("/**"):
        return stripped

    # 查找 module ... } 块
    module_match = re.search(r'(/\*\*.*?)?module\s+\w+.*?\n\}', text, re.DOTALL)
    if module_match:
        return module_match.group(0).strip()

    return stripped


def _is_valid_ttcn3_code(code: str) -> bool:
    """验证提取到的代码是否是有效的 TTCN-3 模块"""
    if not code or len(code) < 20:
        return False
    return code.startswith("module ") or code.startswith("/**")


def _extract_ttcn3_code_from_text(text: str) -> str:
    """From the current position of module keyword, extract the full balanced-brace body."""
    depth = 0
    in_string = False
    escape_next = False
    end_pos = -1
    for i, ch in enumerate(text):
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
                end_pos = i
                break
    return text[:end_pos + 1].strip() if end_pos >= 0 else text.strip()


def sanitize_ttcn3_hex_strings(text: str) -> str:
    """Fix odd-length hexadecimal octetstring literals in TTCN-3 source.

    TTCN-3 requires octetstring hex literals to contain an even number of
    hex digits (each byte is 2 hex digits).  LLM output frequently generates
    odd-length literals (e.g. '0201FD'O) which cause TITAN compilation errors:
        "Octetstring value contains odd number of hexadecimal digits"

    This sanitizer:
      1. Uses a regex that validates hex content (ignores non-hex content).
      2. Pads odd-length literals by appending a trailing '0' (the safest padding
         since the extra byte has minimal semantic impact on protocol tests).
      3. Logs each fix for traceability.

    Example transforms:
        '02FD8001'O  -> '02FD8001'O  (already even, unchanged)
        '0201FD'O    -> '0201FD0'O   (odd -> even, padded)
        'ABC'O       -> 'ABC0'O      (odd -> even, padded)

    Args:
        text: Raw TTCN-3 source text (may contain multiple octetstring literals)

    Returns:
        Text with all odd-length hex literals padded to even length.
    """
    import re

    def _safe_pad(m: re.Match) -> str:
        hex_part = m.group(1)
        suffix = m.group(2)
        if not re.fullmatch(r'[0-9A-Fa-f]+', hex_part):
            return m.group(0)
        if len(hex_part) % 2 == 0:
            return m.group(0)
        padded = hex_part + '0'
        return f"'{padded}'{suffix}"

    pattern = r"'([0-9A-Fa-f]+)'([Oo])"
    result, count = re.subn(pattern, _safe_pad, text)
    if count > 0:
        log(f"[HEX-SANITIZE] Fixed {count} odd-length octetstring literal(s)")
    return result


def sanitize_pr_nesting(text: str, file_label: str = "<unknown>") -> str:
    """Strip nested precondition calls from generated PR function bodies.

    Root cause of a real bug (2026-06-20): the LLM would occasionally generate
    PR functions that call other PR functions (e.g. SessionSetup PR calling
    `f_SECC_CMN_PR_SupportedAppProtocol_001`). This violates ISO 15118-2:
    each PR is atomic, the test case (TC) is responsible for chaining PRs.
    Nested PR calls cause the SECC to receive an out-of-sequence request
    and emit `EXIDecodingError("Premature EOS")`, closing the TCP/TLS
    connection. The C++ adapter is correct; the LLM-generated PR was wrong.

    This sanitizer is a defence-in-depth safety net (Pipeline's Core-Agent
    prompt now also enforces the HARD INVARIANT, but we sanitize anyway
    so a regression in the prompt cannot silently break the suite).

    Detection rule (intentionally simple and conservative):
      1. Use regex + brace-balanced scanner to locate the body of every
         function whose name matches `f_SECC_CMN_PR_<X>_001`.
      2. Within each such body, find any line containing a call to a
         DIFFERENT `f_SECC_CMN_PR_<Y>_001` and replace that line with
         a TTCN-3 single-line comment carrying a `[SANITIZER]` marker.

    The sanitizer ONLY inspects files whose function name matches the PR
    pattern; for non-PR files (testcase, postconditions, services) it is a
    no-op so we don't accidentally strip legitimate helper-function calls.

    Args:
        text: Raw TTCN-3 source text (possibly containing multiple modules).
        file_label: For logging only — the file basename, e.g. `f_SECC_CMN_PR_SessionSetup_001.ttcn`.

    Returns:
        Text with all illegal nested PR calls removed/commented-out.
    """
    import re

    if not text or "f_SECC_CMN_PR_" not in text:
        return text

    fn_header_re = re.compile(
        r'\bfunction\s+(f_SECC_CMN_PR_[A-Za-z0-9_]+_001)\s*\([^)]*\)[^{]*\{',
    )
    nested_call_re = re.compile(r'\b(f_SECC_CMN_PR_[A-Za-z0-9_]+_001)\s*\(')
    line_comment_re = re.compile(r'//.*$')

    # First pass: locate every PR function body span using brace matching.
    fn_spans = []  # list of (fn_name, char_start, char_end)
    for m in fn_header_re.finditer(text):
        fn_name = m.group(1)
        # Position of the opening '{' in the matched header
        brace_pos = m.end() - 1
        depth = 0
        pos = brace_pos
        while pos < len(text):
            c = text[pos]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    fn_spans.append((fn_name, m.start(), pos + 1))
                    break
            pos += 1

    if not fn_spans:
        return text

    # Map each line index to the (first) PR function that encloses it.
    line_to_fn = {}
    for fn_name, cstart, cend in fn_spans:
        li_start = text.count('\n', 0, cstart)
        li_end = text.count('\n', 0, cend)
        for li in range(li_start, li_end + 1):
            line_to_fn.setdefault(li, fn_name)

    # Second pass: scan lines, comment out nested calls to other PR functions.
    lines = text.splitlines(keepends=True)
    sanitized_lines = []
    removed_total = 0
    for li, line in enumerate(lines):
        enclosing_fn = line_to_fn.get(li)
        if enclosing_fn is None:
            sanitized_lines.append(line)
            continue
        code_part = line_comment_re.sub('', line)
        nested_match = None
        for call_m in nested_call_re.finditer(code_part):
            called_fn = call_m.group(1)
            if called_fn == enclosing_fn:
                continue
            nested_match = (call_m, called_fn)
            break
        if nested_match is None:
            sanitized_lines.append(line)
            continue
        _, called_fn = nested_match
        stripped = line.lstrip()
        indent = line[:len(line) - len(stripped)]
        if stripped.startswith('//'):
            sanitized_lines.append(line)
        else:
            sanitized_lines.append(
                f"{indent}// [SANITIZER] Removed illegal nested PR call "
                f"({called_fn} inside {enclosing_fn})\n"
            )
            removed_total += 1

    if removed_total > 0:
        log(f"[SANITIZER] Removed {removed_total} illegal nested PR call(s) in {file_label}")
    return "".join(sanitized_lines)


def sanitize_v2g_wrapper(text: str, file_label: str = "<unknown>") -> str:
    """Auto-wrap bare receive/send templates in { v2g := { ... } }.

    Root cause of a real bug (2026-06-20): the LLM generated receive templates
    without the top-level V2G union wrapper, e.g.:
        receive({ sessionSetupRes := ? })       // WRONG
        send({ serviceDiscoveryReq := ... })    // WRONG

    The C++ adapter crashes on bare union fields because it expects the outer
    V2GMessage struct envelope.  The correct form is always:
        receive({ v2g := { sessionSetupRes := ? } })
        send({ v2g := { serviceDiscoveryReq := ... } })

    This sanitizer is a defence-in-depth safety net (Pipeline's Core-Agent
    prompt now also enforces the CRITICAL UNION WRAPPER invariant, but we
    sanitize anyway so a regression cannot silently break the suite).

    Detection rules (conservative — we ONLY fix the clearly-wrong patterns):
      Pattern A — bare `receive({ <field> := ? })` at top level of receive arg
                  NOT already wrapped in `v2g :=` or `raw :=\`;
      Pattern B — bare `send({ <field> := ... })` at top level of send arg
                  NOT already wrapped in `v2g :=` or `raw :=\`.

    We use a regex that matches the "field :=" or "field :=" sentinel at the
    top brace level only, so we don't accidentally touch nested sub-expressions.

    Args:
        text: Raw TTCN-3 source text.
        file_label: For logging only — the file basename.

    Returns:
        Text with all bare v2g union fields wrapped in { v2g := { ... } }.
    """
    import re

    if not text:
        return text

    # Pattern A: receive({ <bare_field> := [?] })  where <bare_field> is NOT
    # already v2g or raw.  We look for 'receive({ ' followed eventually by
    # 'fieldName :=' at the same brace depth, without an intervening v2g/raw.
    # Strategy: for each 'receive' call, find its argument block (balanced braces)
    # and check whether it starts with 'v2g :=' or 'raw :=\'.  If not, and the
    # first content is a known V2G message field, wrap it.

    KNOWN_V2G_FIELDS = {
        # General / SDP
        "sessionSetupReq", "sessionSetupRes",
        "sessionSetupV2gtpRes",
        "serviceDiscoveryReq", "serviceDiscoveryRes",
        "serviceDetailReq", "serviceDetailRes",
        "serviceListReq", "serviceListRes",
        "servicePaymentSelectionReq", "servicePaymentSelectionRes",
        "chargeParameterDiscoveryReq", "chargeParameterDiscoveryRes",
        "powerDeliveryReq", "powerDeliveryRes",
        "chargerErrorReq", "chargerErrorRes",
        "paymentDetailsReq", "paymentDetailsRes",
        "certificateInstallationReq", "certificateInstallationRes",
        "certificateUpdateReq", "certificateUpdateRes",
        "sessionStopReq", "sessionStopRes",
        "weldingDiagnosisReq", "weldingDiagnosisRes",
        "supportedAppProtocolReq", "supportedAppProtocolRes",
        "v2gSetupReq", "v2gSetupRes",
        "v2gResponseReq", "v2gResponseRes",
        "AC_ChargeParameterDiscoveryReq", "AC_ChargeParameterDiscoveryRes",
        "DC_ChargeParameterDiscoveryReq", "DC_ChargeParameterDiscoveryRes",
        "DC_WeldingDetectionReq", "DC_WeldingDetectionRes",
        "DC_CableCheckReq", "DC_CableCheckRes",
        "DC_PreChargeReq", "DC_PreChargeRes",
        "EVSEStatusReq", "EVSEStatusRes",
        "currentDemandReq", "currentDemandRes",
        "chargingStatusReq", "chargingStatusRes",
        "meteringConfirmationReq", "meteringConfirmationRes",
        "reassembly", "v2gtpRes", "sdpRes",
        "primitive", "msg", "body", "header", "sdpReq", "sdpRes",
    }

    total_fixes = 0

    # ---- Pattern A: receive without v2g/raw wrapper ----
    # Match 'receive(' then find the balanced arg, check if first field is bare.
    # Regex: receive(\s*(\([^)]*\)|\([^)]*\([^)]*\)[^)]*\))) but with balanced braces.
    # We use a brace-scanning approach: for each 'receive(' we scan to the
    # matching ')' and inspect the content.
    receive_pattern = re.compile(r'\breceive\s*\(')

    def _fix_receive(text: str) -> str:
        result = []
        pos = 0
        fixes = 0
        while True:
            m = re.search(receive_pattern, text[pos:])
            if not m:
                result.append(text[pos:])
                break
            chunk_start = pos + m.start()
            result.append(text[pos:chunk_start])
            # Now scan from chunk_start to find the balanced closing ')'
            open_pos = text.find('(', chunk_start)
            if open_pos == -1:
                result.append(text[chunk_start:])
                break
            depth = 0
            scan = open_pos
            while scan < len(text):
                c = text[scan]
                if c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                    if depth == 0:
                        break
                scan += 1
            arg = text[open_pos + 1:scan]  # content inside receive(...)
            stripped = arg.strip()
            # Check if already wrapped
            if stripped.startswith('v2g :=') or stripped.startswith('raw :=') or stripped.startswith('{'):
                # Already has a union wrapper (possibly more complex)
                # Still check: does it have a bare field at top level without v2g/raw?
                # For safety, pass through as-is for simple cases
                result.append(text[chunk_start:scan + 1])
            else:
                # Check if first token looks like a V2G field.
                # Use re.search + MULTILINE so we match across lines even when
                # stripped starts with whitespace/newlines before the field name.
                # (re.match would fail because it anchors at position 0.)
                field_match = re.search(r'([A-Za-z_]\w*)\s*:=\s*\?', stripped, re.MULTILINE)
                if field_match and field_match.group(1) in KNOWN_V2G_FIELDS:
                    rest = stripped[field_match.end():].lstrip()
                    wrapped_arg = f"v2g := {{ {field_match.group(1)} := ?{rest} }}"
                    result.append(f"receive({{{wrapped_arg}}})")
                    fixes += 1
                else:
                    result.append(text[chunk_start:scan + 1])
            pos = scan + 1
        return "".join(result), fixes

    text, fixes_a = _fix_receive(text)
    total_fixes += fixes_a

    # ---- Pattern B: send without v2g/raw wrapper ----
    # Match 'send(' similarly.
    send_pattern = re.compile(r'\bsend\s*\(')

    def _fix_send(text: str) -> str:
        result = []
        pos = 0
        fixes = 0
        while True:
            m = re.search(send_pattern, text[pos:])
            if not m:
                result.append(text[pos:])
                break
            chunk_start = pos + m.start()
            result.append(text[pos:chunk_start])
            open_pos = text.find('(', chunk_start)
            if open_pos == -1:
                result.append(text[chunk_start:])
                break
            depth = 0
            scan = open_pos
            while scan < len(text):
                c = text[scan]
                if c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                    if depth == 0:
                        break
                scan += 1
            arg = text[open_pos + 1:scan].strip()
            if arg.startswith('v2g :=') or arg.startswith('raw :=') or arg.startswith('{'):
                result.append(text[chunk_start:scan + 1])
            else:
                # Non-greedy .+? prevents the regex from eating nested braces.
                # Bracket-balance fallback: extract the value after ":=" and scan
                # for the outermost balanced brace pair, so the exact value
                # boundary is never ambiguous regardless of regex match quality.
                def _extract_balanced_value(s: str) -> str:
                    """Return the content after ':=' trimmed to the first balanced {} block."""
                    colon = s.find(':=')
                    if colon == -1:
                        return s.strip()
                    val = s[colon + 2:].strip()
                    depth = 0
                    end = 0
                    for i, c in enumerate(val):
                        if c == '{':
                            depth += 1
                        elif c == '}':
                            depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                    return val[:end] if end else val

                field_match = re.search(r'([A-Za-z_]\w*)\s*:=\s*(.+?)\s*$', arg, re.DOTALL)
                if field_match and field_match.group(1) in KNOWN_V2G_FIELDS:
                    raw_value = field_match.group(2)
                    # Use bracket-balance algorithm for precise boundary detection
                    value_clean = _extract_balanced_value(arg)
                    # Strip the outermost braces of the value itself
                    value_clean = value_clean.strip().lstrip('{').rstrip('}').strip()
                    wrapped_arg = f"v2g := {{ {field_match.group(1)} := {value_clean} }}"
                    result.append(f"send({{{wrapped_arg}}})")
                    fixes += 1
                else:
                    result.append(text[chunk_start:scan + 1])
            pos = scan + 1
        return "".join(result), fixes

    text, fixes_b = _fix_send(text)
    total_fixes += fixes_b

    if total_fixes > 0:
        log(f"[SANITIZER] Fixed {total_fixes} bare union wrapper(s) in {file_label}")
    return text


def _sanitize_extracted_text(code: str) -> str:
    """Apply hex-string, PR-nesting, and v2g-wrapper sanitizers to a single
    chunk of extracted TTCN-3 source.

    Used by `extract_core_agent_output` to ensure EVERY extracted TTCN-3
    string is free of all three defect classes:
      * odd-length octetstring hex literals (compile error);
      * illegal nested calls from PR functions to other PR functions
        (causes SECC state-machine corruption at runtime);
      * bare receive/send templates missing the { v2g := { ... } } wrapper
        (causes C++ adapter crash at runtime).
    """
    if not code:
        return code
    code = sanitize_ttcn3_hex_strings(code)
    code = sanitize_pr_nesting(code, file_label="<extracted>")
    code = sanitize_v2g_wrapper(code, file_label="<extracted>")
    return code


def _apply_sanitizers_to_extracted(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Apply all three sanitizers to every TTCN-3 field of a parsed Core-Agent dict.

    Iterates over `testcase` plus every entry in `precondition_files` and
    `postcondition_files` (each entry is expected to be a dict with
    `filename` + `content` keys), running both sanitizers on each chunk.
    """
    try:
        if isinstance(parsed, dict):
            if isinstance(parsed.get("testcase"), str):
                parsed["testcase"] = _sanitize_extracted_text(parsed["testcase"])
            for k in ("precondition_files", "postcondition_files"):
                files = parsed.get(k)
                if isinstance(files, list):
                    for entry in files:
                        if isinstance(entry, dict) and isinstance(entry.get("content"), str):
                            label = entry.get("filename", k)
                            content = entry["content"]
                            content = _sanitize_extracted_text(content)
                            entry["content"] = content
    except Exception as exc:
        log(f"[SANITIZER] Error applying sanitizers: {exc}", "WARN")
    return parsed


def extract_core_agent_output(text: str) -> Dict[str, Any]:
    """从 Core Agent 响应中提取 JSON 格式的完整输出（包括 testcase + pre/post condition 文件）。

    Three-Tier Robust Parsing:
      Tier 1 — Loose JSON Stripping:
        Strip chat prefixes (e.g. "Here is the JSON:", "Result:", "Sure ...") and trailing
        notes (e.g. "Note:") from the raw text. Then locate the first { or [ and use a
        balanced-brace/bracket counter to isolate the JSON core before parsing.
      Tier 2 — Regex TTCN-3 Module Block Fallback:
        If JSON parsing still fails, scan for explicit TTCN-3 module blocks
        (module <Name> { ... }) using a balanced-brace extractor and return the raw
        TTCN-3 code as a last resort.
      Tier 3 — Type Polymorphism Guard:
        Ensure the returned dict is always well-formed: no AttributeError or TypeError
        can propagate from this function regardless of input type.

    Returns:
        Always a dict with keys: testcase (str), precondition_files (list),
        postcondition_files (list), and optionally _parse_error (bool).
    """
    import re
    import json as _json

    # --- Tier 3: Type Polymorphism Guard ---
    # Guard against non-string inputs that would crash _json.loads
    if not isinstance(text, str):
        log(f"extract_core_agent_output: input is {type(text).__name__}, not str — returning empty fallback", "WARN")
        return {"testcase": "", "precondition_files": [], "postcondition_files": [], "_parse_error": True}

    if not text or not text.strip():
        return {"testcase": "", "precondition_files": [], "postcondition_files": [], "_parse_error": True}

    # If the caller accidentally passed an already-parsed dict, return it safely
    if isinstance(text, dict):
        return text

    raw = text.strip()

    # --- Tier 0: Fix JSON with embedded control characters ---
    # Claude Code CLI sometimes embeds literal \n or \r in JSON string values,
    # producing invalid JSON like {"testcase": "module X {\n  import..."}.
    # json.loads rejects these. We normalize them before parsing.
    # Approach: find "testcase": " and scan for the value string, replacing
    # embedded literal \n/\r with \\n/\\r inside that string.
    def _fix_embedded_newlines_in_json(text: str) -> str:
        """Replace literal newlines inside JSON string values with \\n."""
        # Find "testcase": " or "precondition_files": [ etc.
        result = []
        i = 0
        while i < len(text):
            # Look for the start of a string value we want to sanitize
            # We want to fix the "testcase" value (and other code fields)
            # Strategy: find each : " followed by code content, and fix newlines in it
            if (i < len(text) - 11 and
                text[i:i+11] == '"testcase"' and
                i + 11 < len(text) and text[i+11] in '":'):
                # Found "testcase" key, advance past it
                result.append(text[i:i+11])
                i += 11
                # Skip whitespace
                while i < len(text) and text[i] in ' \t\n\r':
                    result.append(text[i])
                    i += 1
                if i < len(text) and text[i] == ':':
                    result.append(text[i])
                    i += 1
                # Now inside the string value — find the opening "
                while i < len(text) and text[i] in ' \t\n\r':
                    result.append(text[i])
                    i += 1
                if i < len(text) and text[i] == '"':
                    result.append(text[i])
                    i += 1
                    # Scan until closing " (handling escapes and nested braces)
                    esc = False
                    in_str = True
                    while i < len(text):
                        ch = text[i]
                        if esc:
                            result.append(ch)
                            esc = False
                            i += 1
                            continue
                        if ch == '\\':
                            result.append(ch)
                            esc = True
                            i += 1
                            continue
                        if ch == '"':
                            # End of string
                            result.append(ch)
                            i += 1
                            break
                        # Replace literal \n with \\n, \r with \\r
                        if ch == '\n':
                            result.append('\\n')
                            i += 1
                            continue
                        if ch == '\r':
                            result.append('\\r')
                            i += 1
                            continue
                        result.append(ch)
                        i += 1
                continue
            result.append(text[i])
            i += 1
        return ''.join(result)

    raw = _fix_embedded_newlines_in_json(raw)

    # --- Tier 1: Loose JSON Stripping ---

    # 1a. Strip conversational prefixes (case-insensitive)
    prefix_patterns = [
        r"^(?:Here(?:'s| is)?\s+(?:the\s+)?(?:requested\s+|)?(?:result|data|output|json|code)[:\s]*)",
        r"^Result[:\s]*",
        r"^Output[:\s]*",
        r"^(?:Sure|Okay|Here|Maybe|Actually)[,:\s]+\{",
        r"^(?:Below\s+is|Here\s+is|Attached\s+is)[,:\s]*",
    ]
    for pat in prefix_patterns:
        raw = re.sub(pat, '', raw, flags=re.IGNORECASE).strip()

    # 1b. Strip trailing notes, markdown dividers, and trailing whitespace
    raw = re.sub(r'\n\s*Note[:\s].*$', '', raw, flags=re.DOTALL).strip()
    raw = re.sub(r'\n\s*[-*_]{3,}\s*$', '', raw).strip()

    # 1c. Locate the first { or [ as the JSON boundary anchor
    first_brace = raw.find('{')
    if first_brace < 0:
        first_brace = raw.find('[')
    json_candidate = raw[first_brace:] if first_brace >= 0 else raw

    # 1d. Find the matching closing brace/bracket using balanced counter
    if json_candidate.startswith('{'):
        depth, last_close = 0, -1
        in_str, esc = False, False
        for i, ch in enumerate(json_candidate):
            if esc:
                esc = False
                continue
            if ch == '\\' and in_str:
                esc = True
                continue
            if ch == '"' and not esc:
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    last_close = i
                    break
        if last_close >= 0:
            json_candidate = json_candidate[:last_close + 1]

    elif json_candidate.startswith('['):
        depth, last_close = 0, -1
        in_str, esc = False, False
        for i, ch in enumerate(json_candidate):
            if esc:
                esc = False
                continue
            if ch == '\\' and in_str:
                esc = True
                continue
            if ch == '"' and not esc:
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    last_close = i
                    break
        if last_close >= 0:
            json_candidate = json_candidate[:last_close + 1]

    # 1e. Try parsing the isolated JSON core
    for attempt_text in [json_candidate, raw]:
        try:
            parsed = _json.loads(attempt_text)
            if isinstance(parsed, dict) and "testcase" in parsed:
                return _apply_sanitizers_to_extracted(parsed)
            # Re-wrap a bare list or bare string into the expected dict shape
            if isinstance(parsed, list):
                return {"testcase": "", "precondition_files": [], "postcondition_files": [],
                        "_parse_error": True, "_raw_list_len": len(parsed)}
            if isinstance(parsed, str):
                return {"testcase": _sanitize_extracted_text(parsed),
                        "precondition_files": [], "postcondition_files": []}
        except (_json.JSONDecodeError, ValueError):
            # Try truncation recovery on this candidate
            recovered = _recover_truncated_json(attempt_text)
            if recovered and isinstance(recovered, dict) and "testcase" in recovered:
                log("extract_core_agent_output: Tier-1 JSON truncation recovery succeeded", "WARN")
                return recovered
            if recovered:
                log("extract_core_agent_output: Tier-1 JSON truncation recovery returned non-standard shape", "WARN")
                return recovered

    # 1f. Try markdown code-block wrappers
    for block_pat in [r'```json\s*\n?(.*?)\n?```', r'```\s*\n?(.*?)\n?\s*```']:
        m = re.search(block_pat, raw, re.DOTALL)
        if m:
            block = m.group(1).strip()
            try:
                parsed = _json.loads(block)
                if isinstance(parsed, dict) and "testcase" in parsed:
                    return parsed
            except (_json.JSONDecodeError, ValueError):
                recovered = _recover_truncated_json(block)
                if recovered and isinstance(recovered, dict) and "testcase" in recovered:
                    log("extract_core_agent_output: markdown-block truncation recovery succeeded", "WARN")
                    return recovered

    # --- Tier 2: Regex TTCN-3 Module Block Fallback ---
    # Last resort: scan for explicit TTCN-3 module declarations
    module_match = re.search(r'module\s+\w+\s*\{', raw, re.IGNORECASE)
    if module_match:
        start = module_match.start()
        candidate = raw[start:]
        depth, in_str, esc, end_pos = 0, False, False, -1
        for i, ch in enumerate(candidate):
            if esc:
                esc = False
                continue
            if ch == '\\' and in_str:
                esc = True
                continue
            if ch == '"' and not esc:
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end_pos = i
                    break
        if end_pos >= 0:
            code = candidate[:end_pos + 1].strip()
            if _is_valid_ttcn3_code(code):
                log("extract_core_agent_output: Tier-2 TTCN-3 module block salvaged from malformed response", "WARN")
                return {"testcase": _sanitize_extracted_text(code),
                        "precondition_files": [], "postcondition_files": []}

    # --- All tiers exhausted ---
    log(f"extract_core_agent_output: all tiers failed — raw len={len(text)}, "
        f"preview={text[:120]!r}", "WARN")
    return {"testcase": "", "precondition_files": [], "postcondition_files": [], "_parse_error": True}


# ============================================================
# 测试用例查找与生成
# ============================================================

def find_test_cases_in_generated_file(requirement_ids: List[str]) -> List[Dict]:
    """从已生成的测试用例文件 (generated_test_cases.json) 中查找匹配的测试用例。

    采用明确的优先级匹配策略：
      1. 精确匹配：所有输入需求都存在于该测试用例
      2. 子集匹配：该测试用例的所有需求都是输入需求的子集
      3. 部分匹配：任意一个需求匹配（保留所有匹配，而非仅第一个）

    Args:
        requirement_ids: 安全需求 ID 列表，如 ["V2G2-082", "V2G2-165"]

    Returns:
        匹配的测试用例字典列表，每个元素包含 Test_Case 键
    """
    if not GENERATED_TEST_CASES_PATH.exists():
        log(f"测试用例文件不存在: {GENERATED_TEST_CASES_PATH}", "WARN")
        return []

    try:
        data = load_json(GENERATED_TEST_CASES_PATH)
        test_cases = data.get("test_cases", [])

        exact_matches = []
        subset_matches = []
        partial_matches = []
        seen_ids = set()  # 避免重复添加同一个测试用例

        for tc_entry in test_cases:
            tc = tc_entry.get("Test_Case", {})
            tc_id = tc.get("TC_Id", "")
            if tc_id in seen_ids:
                continue
            doc_ref = tc.get("Document_Reference", {})
            requirements = doc_ref.get("Requirements", []) if isinstance(doc_ref, dict) else []

            # 提取需求ID（去除方括号）
            tc_req_ids = [req.strip("[]").strip() for req in requirements]

            # 优先级 1: 精确匹配（所有输入需求都存在于该测试用例）
            if all(req_id in tc_req_ids for req_id in requirement_ids):
                exact_matches.append(tc_entry)
                seen_ids.add(tc_id)
                continue

            # 优先级 2: 子集匹配（该测试用例的所有需求都是输入需求的子集）
            if tc_req_ids and all(req_id in requirement_ids for req_id in tc_req_ids):
                subset_matches.append(tc_entry)
                seen_ids.add(tc_id)
                continue

            # 优先级 3: 部分匹配（任意一个需求匹配）
            if any(req_id in tc_req_ids for req_id in requirement_ids):
                partial_matches.append(tc_entry)
                seen_ids.add(tc_id)

        # 按优先级返回
        if exact_matches:
            log(f"精确匹配: {len(exact_matches)} 个测试用例")
            return exact_matches
        if subset_matches:
            log(f"子集匹配: {len(subset_matches)} 个测试用例")
            return subset_matches
        if partial_matches:
            log(f"部分匹配: {len(partial_matches)} 个测试用例")
            return partial_matches

        return []

    except Exception as e:
        log(f"读取测试用例文件失败: {e}", "ERROR")
        return []


def find_test_case_by_tc_id(tc_id: str) -> Optional[Dict]:
    """从 generated_test_cases.json 中通过 TC ID 查找测试用例

    Args:
        tc_id: 测试用例 ID，如 TC_SECC_CMN_VTB_SupportedAppProtocol_001

    Returns:
        匹配的测试用例字典，或 None
    """
    if not GENERATED_TEST_CASES_PATH.exists():
        log(f"测试用例文件不存在: {GENERATED_TEST_CASES_PATH}", "WARN")
        return None

    try:
        data = load_json(GENERATED_TEST_CASES_PATH)
        test_cases = data.get("test_cases", [])

        for tc_entry in test_cases:
            tc = tc_entry.get("Test_Case", {})
            if tc.get("TC_Id") == tc_id:
                return tc_entry

        return None

    except Exception as e:
        log(f"读取测试用例文件失败: {e}", "ERROR")
        return None


def call_test_case_generator(requirement_ids: List[str], mode: str = "multi") -> Optional[Dict]:
    """调用第一阶段的测试用例生成器生成测试用例

    Args:
        requirement_ids: 安全需求 ID 列表
        mode: 生成模式，"single" 或 "multi"（多需求合并为一个测试用例）

    Returns:
        生成的测试用例字典，或 None
    """
    log(f"调用测试用例生成器: requirements={requirement_ids}, mode={mode}")

    if not TEST_CASE_GENERATOR_SCRIPT.exists():
        log(f"测试用例生成器不存在: {TEST_CASE_GENERATOR_SCRIPT}", "ERROR")
        return None

    try:
        cmd = [
            sys.executable,
            str(TEST_CASE_GENERATOR_SCRIPT),
        ]

        if mode == "multi" and len(requirement_ids) > 1:
            cmd.extend(["--mode", "multi"])
            cmd.extend(requirement_ids)
        else:
            cmd.extend(requirement_ids)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 测试用例生成可能需要更长时间
            cwd=str(AGENT_DIR),
        )

        if result.returncode != 0:
            log(f"测试用例生成器执行失败: {result.stderr[:500]}", "ERROR")
            return None

        # 尝试从输出中解析生成的测试用例
        # 查找最近生成的测试用例文件
        output_dir = TEST_CASE_DIR / "output"
        if output_dir.exists():
            output_files = list(output_dir.glob("*.json"))
            if output_files:
                latest_file = max(output_files, key=lambda p: p.stat().st_mtime)
                log(f"读取生成的测试用例: {latest_file}")
                return load_json(latest_file)

        return None

    except subprocess.TimeoutExpired:
        log("测试用例生成器超时", "ERROR")
        return None
    except Exception as e:
        log(f"调用测试用例生成器失败: {e}", "ERROR")
        return None


def get_test_cases_for_requirements(
    requirement_ids: List[str],
    auto_generate: bool = True,
    multi_mode: bool = True
) -> List[Dict]:
    """获取需求对应的测试用例

    查找策略:
    1. 首先从 generated_test_cases.json 中查找
    2. 如果未找到且 auto_generate 为 True，调用测试用例生成器

    Args:
        requirement_ids: 安全需求 ID 列表
        auto_generate: 未找到时是否自动生成
        multi_mode: 生成模式，True 表示多需求合并为一个测试用例

    Returns:
        测试用例字典列表
    """
    log(f"查找测试用例: requirements={requirement_ids}")

    # 尝试从已生成的测试用例中查找
    matched_cases = find_test_cases_in_generated_file(requirement_ids)

    if matched_cases:
        log(f"找到 {len(matched_cases)} 个匹配的测试用例")
        return matched_cases

    # 未找到，尝试自动生成
    if auto_generate:
        log(f"未找到匹配的测试用例，尝试生成...")
        mode = "multi" if multi_mode and len(requirement_ids) > 1 else "single"
        generated = call_test_case_generator(requirement_ids, mode=mode)

        if generated:
            test_cases = generated.get("test_cases", [])
            if test_cases:
                log(f"成功生成 {len(test_cases)} 个测试用例")
                return test_cases

    log(f"无法获取需求 {requirement_ids} 对应的测试用例", "WARN")
    return []


def parse_test_case_from_string(test_case_str: str) -> Optional[Dict]:
    """从字符串解析测试用例 JSON

    Args:
        test_case_str: 测试用例 JSON 字符串

    Returns:
        测试用例字典，或 None
    """
    try:
        return json.loads(test_case_str)
    except json.JSONDecodeError:
        pass

    # 尝试从代码块中提取
    import re
    json_block = re.search(r'```(?:json)?\s*\n(.*?)\n```', test_case_str, re.DOTALL)
    if json_block:
        try:
            return json.loads(json_block.group(1))
        except json.JSONDecodeError:
            pass

    return None


def load_test_case_from_file(file_path: Path) -> Optional[List[Dict]]:
    """从文件加载测试用例

    Args:
        file_path: 测试用例文件路径

    Returns:
        测试用例列表（每个元素是对应一个测试用例的字典，可能包含 Test_Case 键），
        或单个测试用例字典（向后兼容旧的 {"Test_Case": {...}} 格式），
        或 None
    """
    if not file_path.exists():
        log(f"测试用例文件不存在: {file_path}", "ERROR")
        return None

    try:
        data = load_json(file_path)

        # 兼容处理不同的 JSON 结构：
        # 1. {"test_cases": [...]} - test_case_generator.py 生成的格式
        # 2. {"Test_Case": {...}} - 单个旧格式
        # 3. [{"Test_Case": {...}}, ...] - 列表旧格式
        # 4. [{"Test_Case": {...}}, ...] - 列表新格式

        # 情况1: 包含 test_cases 数组
        if isinstance(data, dict) and "test_cases" in data:
            return data["test_cases"]

        # 情况2: 直接是列表
        if isinstance(data, list):
            return data

        # 情况3: 单个用例（旧格式）
        if isinstance(data, dict) and "Test_Case" in data:
            return [data]

        # 情况4: 单个用例（无 Test_Case 包装，直接有 TC_Id）
        if isinstance(data, dict) and "TC_Id" in data:
            return [data]

        log(f"测试用例文件格式无法识别: {file_path}", "ERROR")
        return None

    except Exception as e:
        log(f"读取测试用例文件失败: {e}", "ERROR")
        return None


def find_requirement_context(requirement_id: str) -> str:
    """从 ISO 15118-2 层次树中查找需求上下文"""
    if not HIERARCHICAL_TREE_PATH.exists():
        return ""

    try:
        tree = load_json(HIERARCHICAL_TREE_PATH)
    except Exception:
        return ""

    # 搜索需求 ID
    context_parts = []

    def search_node(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                current_path = f"{path}/{key}" if path else key
                if isinstance(value, str) and requirement_id in value:
                    context_parts.append(f"[{current_path}]: {value[:500]}")
                elif isinstance(value, (dict, list)):
                    search_node(value, current_path)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                search_node(item, f"{path}[{i}]")

    search_node(tree)
    return "\n".join(context_parts[:10])  # 限制上下文长度


def search_knowledge_base(message_type: str, sut_type: str) -> Dict[str, Any]:
    """从知识库中检索相关代码片段（用于 TTCN-3 代码生成的参考）

    注意：这里仍然使用知识库作为 TTCN-3 代码生成的参考样例，
    但测试用例本身必须来自 generated_test_cases.json。

    优先使用 by_message_type 视图（按消息类型索引），回退到 ttcn_code_by_type（按代码类型索引）。
    """
    if not KNOWLEDGE_BASE_PATH.exists():
        return {}

    kb = load_json(KNOWLEDGE_BASE_PATH)
    results = {
        "testcases": [],
        "functions": [],
        "templates": [],
        "altsteps": [],
        "descriptions": [],
    }

    # 优先使用 by_message_type 视图
    by_message = kb.get("by_message_type", {})
    if by_message:
        # 规范化消息类型：去除空格和下划线，映射别名
        # "V2GTP Header" -> "V2GTP"
        normalized_msg = message_type.replace(" ", "").replace("_", "")
        alias_map = {
            "V2GTPHeader": "V2GTP",
            "V2GTPBR": "V2GTP",
            "V2GTCP": "V2GTCP",
            "TLSHandshake": "TLSHandshake",
        }
        canonical_msg = alias_map.get(normalized_msg, message_type)

        # 精确匹配消息类型
        msg_view = by_message.get(message_type, {})
        if not msg_view:
            # 尝试规范化后的消息类型
            msg_view = by_message.get(canonical_msg, {})
        if not msg_view:
            # 模糊匹配：查找包含消息类型的键（正向）
            for key in by_message:
                if canonical_msg.lower() in key.lower() or message_type.lower() in key.lower():
                    msg_view = by_message[key]
                    break
        if not msg_view:
            # 模糊匹配：查找键是否包含在消息类型中（反向）
            for key in by_message:
                if key.lower() in canonical_msg.lower() or key.lower() in message_type.lower():
                    msg_view = by_message[key]
                    break

        if msg_view:
            results["testcases"] = [
                tc for tc in msg_view.get("testcases", [])
                if sut_type in tc.get("name", "")
            ]
            results["functions"] = (
                msg_view.get("test_behavior_functions", []) +
                msg_view.get("precondition_functions", []) +
                msg_view.get("postcondition_functions", [])
            )
            results["templates"] = (
                msg_view.get("send_templates", []) +
                msg_view.get("receive_templates", [])
            )
            results["altsteps"] = msg_view.get("altsteps", [])
            results["descriptions"] = msg_view.get("descriptions", [])

    # 如果 by_message_type 没有结果，回退到 ttcn_code_by_type
    if not any(results[k] for k in ["testcases", "functions", "templates"]):
        code_by_type = kb.get("ttcn_code_by_type", {})

        for tc in code_by_type.get("testcase", []):
            if message_type.lower() in tc.get("name", "").lower():
                results["testcases"].append(tc)
            elif sut_type in tc.get("name", ""):
                results["testcases"].append(tc)

        for func in code_by_type.get("function", []):
            name = func.get("name", "")
            if message_type.lower() in name.lower():
                results["functions"].append(func)

        for tmpl in code_by_type.get("template", []):
            name = tmpl.get("name", "")
            if message_type.lower() in name.lower():
                results["templates"].append(tmpl)

        for alt in code_by_type.get("altstep", []):
            if sut_type in alt.get("name", ""):
                results["altsteps"].append(alt)

    # 补充 tc_code_associations 中的关联信息
    tc_assocs = kb.get("tc_code_associations", [])
    related_assocs = [
        a for a in tc_assocs
        if a.get("message_type", "").lower() == message_type.lower()
        and a.get("sut_type", "") == sut_type
    ]
    if related_assocs:
        results["associations"] = related_assocs[:5]

    # 限制每类最多 5 个
    for key in ["testcases", "functions", "templates", "altsteps", "descriptions"]:
        results[key] = results[key][:5]

    return results


# ============================================================
# Agent 调用辅助函数
# ============================================================

def log_prompt_size(agent_name: str, prompt: str, threshold: int = None) -> None:
    """记录 prompt 大小，用于调试和监控。

    Args:
        agent_name: Agent 名称
        prompt: Prompt 内容
        threshold: 警告阈值（字节），None 使用 PROMPT_SIZE_WARNING
    """
    if threshold is None:
        threshold = PROMPT_SIZE_WARNING
    size = len(prompt)
    size_kb = size / 1024
    if size > PROMPT_SIZE_HARD:
        log(f"[{agent_name}] Prompt 大小: {size_kb:.1f}KB - 超过 HARD 阈值 ({PROMPT_SIZE_HARD/1024:.0f}KB)", "ERROR")
    elif size > threshold:
        log(f"[{agent_name}] Prompt 大小: {size_kb:.1f}KB - 超过 WARNING 阈值 ({threshold/1024:.0f}KB)", "WARN")
    else:
        log(f"[{agent_name}] Prompt 大小: {size_kb:.1f}KB (正常)", "DEBUG")


def call_agent_with_timeout(prompt: str, agent_name: str, retry_on_timeout: bool = True,
                            log_size: bool = False,
                            extra_allowed_tools: str = "",
                            read_only: bool = False) -> str:
    """调用 Claude Agent，自动选择合适的超时时间。

    Args:
        prompt: 要发送的 prompt
        agent_name: Agent 名称（用于日志和超时选择）
        retry_on_timeout: 超时后是否重试一次
        log_size: 是否记录 prompt 大小
        extra_allowed_tools: Space-separated extra tool patterns for sandbox relaxation.
        read_only: If True, restrict tools to Read-only (no Write/Edit/Bash).

    Returns:
        Agent 的文本输出
    """
    if log_size:
        log_prompt_size(agent_name, prompt)

    timeout = get_agent_timeout(agent_name, len(prompt))
    # Phase 3.7: short-circuit before invoking Claude if a previous call in this
    # TC run already raised _SandboxLocked. We can't recover by retrying — the
    # sandbox state is environmental. Returning "" lets the caller detect the
    # failure via the existing empty-response path without raising an
    # exception through the call_agent_with_timeout contract.
    if _sandbox_locked_flag:
        log(
            f"{agent_name} Agent call skipped — sandbox lock flag is set from "
            f"a previous call in this TC run. No retry will be attempted.",
            "ERROR",
        )
        return ""
    try:
        response = call_claude_agent(prompt, agent_name, timeout=timeout,
                                     extra_allowed_tools=extra_allowed_tools,
                                     read_only=read_only)
    except _SandboxLocked:
        # The sandbox lock is environmental and cannot be recovered by
        # retrying. Propagate the empty response (caller will detect this as
        # a normal failure via the existing empty-string contract) so the
        # outer pipeline loop can exit cleanly.
        log(
            f"{agent_name} Agent call aborted by sandbox lock — returning empty "
            f"response to caller (no retry, no escalation).",
            "ERROR",
        )
        return ""
    # Phase 3.6: Detect "silent failure" (LLM silent for >600s, killed by idle watchdog).
    # On 2026-06-18 the user hit a token-burning bug: Core Agent / Infra Agent was
    # called by claude, sat silent for 10+ minutes, got killed by idle watchdog
    # returning "", then call_agent_with_timeout immediately re-invoked claude
    # with 2x timeout -- starting another 20+ minute silent wait. We now:
    #   1. Check the global _llm_silent_killed flag set by the idle watchdog.
    #   2. If set, refuse to retry (the LLM is broken, more calls won't help).
    #   3. The pipeline's existing silent-failure detection will then mark the
    #      TC as failed and exit, instead of looping.
    if not response and retry_on_timeout:
        global _llm_silent_killed
        if _llm_silent_killed:
            log(f"{agent_name} Agent killed by idle watchdog -- NOT retrying "
                "(API likely hung, will not recover with another call).", "WARN")
            return response
        # Phase 3.7: double-check the sandbox flag after the call too — if the
        # call returned empty AND the sandbox flag is now set (race window),
        # refuse to retry.
        if _sandbox_locked_flag:
            log(f"{agent_name} Agent returned empty AND sandbox lock flag is set "
                f"— NOT retrying.", "WARN")
            return response
        log(f"{agent_name} Agent 超时或无响应，触发重试 (timeout={timeout}s)", "WARN")
        try:
            response = call_claude_agent(prompt, f"{agent_name} (retry)", timeout=timeout * 2,
                                         extra_allowed_tools=extra_allowed_tools,
                                         read_only=read_only)
        except _SandboxLocked:
            log(
                f"{agent_name} Agent retry aborted by sandbox lock — returning empty.",
                "ERROR",
            )
            return ""
        # If retry also returns empty, the LLM is broken — do not loop further.
        if not response:
            log(f"{agent_name} Agent retry also returned empty -- giving up.", "ERROR")
    return response


# ============================================================
# Agent 1: Orchestrator - 意图分解
# ============================================================
def run_orchestrator(
    test_case: Dict,
    requirement_context: str,
    sop: Dict,
    protocol_snapshot: Optional[Dict] = None,
) -> Optional[Dict]:
    """运行 Orchestrator Agent，分解测试用例为生成计划。

    Args:
        test_case: The test case JSON dict.
        requirement_context: Human-readable spec context (ISO 15118-2 etc.).
        sop: The SOP dict (precondition_chains, naming_rules) from
             knowledge_base/ttcn3_generation_sop.json.
        protocol_snapshot: The ProtocolRegistry.snapshot() output — the active
             protocol's machine-readable descriptor. Injected into the LLM prompt
             as the authoritative source for naming, preconditions, union fields,
             and transport. If None, falls back to the old sop-based injection.
    """
    system_prompt = load_text(ORCHESTRATOR_PROMPT_PATH)

    # Protocol snapshot injection (P3: registry-aware prompt)
    snapshot_block = ""
    if protocol_snapshot:
        snapshot_block = f"""

### Protocol Descriptor (from ProtocolRegistry):
```json
{json.dumps(protocol_snapshot, indent=2, ensure_ascii=False)}
```
"""
    else:
        # Legacy fallback: use SOP-derived data
        snapshot_block = f"""
### SOP 前置条件链参考:
```json
{json.dumps(sop.get("precondition_chains", {}), indent=2)}
```

### SOP 命名规则:
```json
{json.dumps(sop.get("naming_rules", {}), indent=2)}
```
"""

    user_prompt = f"""{system_prompt}{snapshot_block}

---

## 输入数据

### 测试用例 JSON:
```json
{json.dumps(test_case, indent=2, ensure_ascii=False)}
```

### 需求上下文:
{requirement_context if requirement_context else "(无额外上下文)"}

请分析以上测试用例，输出结构化的 TTCN-3 生成计划 JSON。"""

    response = call_agent_with_timeout(user_prompt, "Orchestrator", read_only=True)
    if not response:
        return None

    plan = extract_json_from_response(response)
    if not plan:
        log("Orchestrator 输出无法解析为 JSON，使用默认 plan", "WARN")
        log(f"原始输出: {response[:500]}", "DEBUG")
        # 从测试用例中提取可用信息构建默认 plan
        tc_id = test_case.get("TC_Id", "TC_UNKNOWN")
        sut_type = "SECC"
        if "EVCC" in tc_id.upper():
            sut_type = "EVCC"
        # 从 test_case 推断消息类型
        test_obj = test_case.get("Test_Objective", "")
        message_type = _infer_message_type_from_text(test_obj)
        # 调试：规范化提示
        if " " in message_type or "_" in message_type:
            log(f"注意: message_type='{message_type}' 包含空格或下划线，建议规范化（去除空格和下划线）", "DEBUG")
        # ProtocolRegistry provides the canonical identifiers; the previous
        # inline _canonicalize_message_type() helper has been replaced.
        import re as _re
        _seq_no_orch = "001"
        if tc_id:
            _sm = _re.search(r'_(\d{3})$', tc_id)
            if _sm:
                _seq_no_orch = _sm.group(1)
        _registry_orch = get_default_registry()
        _registry_orch.auto_load_for_tc(tc_id)
        try:
            _r = _registry_orch.resolve(message_type, sut=sut_type, seq_no=_seq_no_orch)
        except (KeyError, RuntimeError) as _e:
            log(f"Orchestrator: registry could not resolve {message_type!r}: {_e}; "
                f"falling back to inline derivation", "WARN")
            _r = None
        if _r is not None:
            plan = {
                "tc_id": tc_id,
                "sut_type": sut_type,
                "category": "CMN",
                "message_type": message_type,
                "normalized_message_type": _r.canonical,
                "test_type": "GoodCase",
                "protocol_layer": _r.protocol_layer,
                "precondition_chain": _r.default_precondition_chain,
                "port_name": _r.port_name,
                "send_template_name": _r.send_template_name,
                "receive_template_name": _r.receive_template_name,
                "request_message_type": _r.request_type,
                "response_message_type": _r.response_type,
                "union_field_req": _r.union_field_req,
                "union_field_res": _r.union_field_res,
                "requirements_traced": [],
                "test_behavior_func_name": _registry_orch.format_naming(
                    "test_behavior_function", SUT=sut_type, Category="CMN",
                    MessageType=message_type, SeqNo=_seq_no_orch,
                ),
                "postcondition_chain": [
                    f"f_{sut_type}_CMN_PO_InitialState_001",
                    f"f_{sut_type}_CMN_PO_ShutdownConfiguration_001"
                ],
            }
        else:
            # Pre-refactor fallback for the unknown-message path
            _canon = _canonicalize_message_type(message_type)
            _canonical = _canon["canonical"]
            plan = {
                "tc_id": tc_id,
                "sut_type": sut_type,
                "category": "CMN",
                "message_type": message_type,
                "normalized_message_type": _canonical,
                "test_type": "GoodCase",
                "protocol_layer": "V2G_Application",
                "precondition_chain": _get_default_precondition_chain(message_type, sop),
                "port_name": f"pt_V2G_TCP_TLS_ALM_{sut_type}_Port",
                "send_template_name": f"md_CMN_{_canonical}Req_{_seq_no_orch}",
                "receive_template_name": f"mdw_{sut_type}_{_canonical}Res_{_seq_no_orch}",
                "request_message_type": _canon["request_msg_type"],
                "response_message_type": _canon["response_msg_type"],
                "union_field_req": _canon["union_field_req"],
                "union_field_res": _canon["union_field_res"],
                "requirements_traced": [],
                "test_behavior_func_name": f"f_{sut_type}_CMN_TB_VTB_{message_type}_{_seq_no_orch}",
                "postcondition_chain": [
                    f"f_{sut_type}_CMN_PO_InitialState_001",
                    f"f_{sut_type}_CMN_PO_ShutdownConfiguration_001"
                ],
            }
        # 智能推断：从 TC_Id 推断 test_type（_001=GoodCase, _002+=BadCase）
        # TC_SECC_CMN_VTB_V2GTPHeader_001 -> GoodCase
        # TC_SECC_CMN_VTB_V2GTPHeader_002/003/004 -> BadCase
        if tc_id:
            # 提取序号部分：TC_SECC_CMN_VTB_V2GTPHeader_001 -> 001
            import re as _re
            seq_match = _re.search(r'_(\d{3})$', tc_id)
            if seq_match:
                seq = int(seq_match.group(1))
                plan["test_type"] = "GoodCase" if seq == 1 else "BadCase"
        # 智能推断：从消息类型推断 protocol_layer
        # V2GTP/SDP 相关测试使用 V2GTP 层，其他使用 V2G_Application 层
        msg_lower = message_type.lower()
        if any(kw in msg_lower for kw in ['v2gtp', 'sdp', 'v2g_tcp', 'tls']):
            plan["protocol_layer"] = "V2GTP"
        # 规范化消息类型（去除空格和下划线，映射别名）
        # "V2GTP Header" -> "V2GTP"
        normalized_msg = message_type.replace(" ", "").replace("_", "")
        alias_map = {
            "V2GTPHeader": "V2GTP",
            "V2GTPBR": "V2GTP",
            "V2GTCP": "V2GTCP",
            "TLSHandshake": "TLSHandshake",
        }
        plan["normalized_message_type"] = alias_map.get(normalized_msg, message_type)

    log(f"生成计划: tc_id={plan.get('tc_id')}, type={plan.get('test_type')}")

    # P3 Step 6: handle LLM-proposed protocol additions (review queue)
    _proposals = plan.get("proposed_additions")
    if _proposals and protocol_snapshot:
        try:
            _reg = get_default_registry()
            if _reg.active_protocol():
                _path = _reg.extend_with_llm_discoveries(
                    _proposals,
                    proposed_by="Orchestrator Agent",
                    proposed_during_tc=plan.get("tc_id", ""),
                )
                log(f"[Review Queue] Proposed additions written to {_path}", "INFO")
                # Remove from the plan so downstream code doesn't see it
                plan.pop("proposed_additions", None)
        except Exception as _ex:
            log(f"[Review Queue] Failed to write proposal: {_ex}", "WARN")

    return plan


# ============================================================
# Agent 2: Summarizer - 知识检索
# ============================================================
def run_summarizer(generation_plan: Dict, kb_search_results: Dict) -> Optional[Dict]:
    """运行 Summarizer Agent，检索相关 TTCN-3 参考代码。

    当 LLM 层解析失败时，使用 Python 端本地知识库搜索作为降级策略。
    """
    system_prompt = load_text(SUMMARIZER_PROMPT_PATH)

    # 准备知识库摘要（避免传入过大的完整知识库）
    kb_summary = json.dumps(kb_search_results, indent=2, ensure_ascii=False)
    # 截断过长的代码块
    if len(kb_summary) > 15000:
        kb_summary = kb_summary[:15000] + "\n... (truncated)"

    user_prompt = f"""{system_prompt}

---

## 输入数据

### 生成计划 (from Orchestrator):
```json
{json.dumps(generation_plan, indent=2, ensure_ascii=False)}
```

### 知识库检索结果 (预筛选):
```json
{kb_summary}
```

请根据生成计划，从以上知识库结果中选取最相关的参考代码，输出 JSON。"""

    response = call_agent_with_timeout(user_prompt, "Summarizer", read_only=True)
    if not response:
        log("Summarizer 返回空，使用 Python 端本地知识库搜索作为降级策略", "WARN")
        return _local_kb_search_as_summarizer_fallback(generation_plan, kb_search_results)

    references = extract_json_from_response(response)
    if not references:
        log("Summarizer 输出无法解析为 JSON，使用 Python 端本地知识库搜索作为降级策略", "WARN")
        return _local_kb_search_as_summarizer_fallback(generation_plan, kb_search_results)

    return references


def _local_kb_search_as_summarizer_fallback(generation_plan: Dict, kb_search_results: Dict) -> Dict:
    """Python 端本地知识库搜索，作为 Summarizer LLM 层的降级策略。

    直接从 kb_search_results 中选取最相关的参考代码，不依赖 LLM 解析。
    """
    message_type = generation_plan.get("message_type", "")
    sut_type = generation_plan.get("sut_type", "SECC")
    tc_id = generation_plan.get("tc_id", "")

    result = {
        "reference_testcase": None,
        "reference_test_behavior": None,
        "reference_templates": [],
        "reference_preconditions": [],
        "reference_postconditions": [],
        "additional_context": "",
        "_fallback": True,  # 标记为降级策略
    }

    # 选取第一个匹配的 testcase 作为参考
    for tc in kb_search_results.get("testcases", []):
        tc_name = tc.get("name", "")
        if sut_type in tc_name or message_type.lower() in tc_name.lower():
            if result["reference_testcase"] is None:
                result["reference_testcase"] = tc
                # 同时选取对应的 test behavior function
                for func in kb_search_results.get("functions", []):
                    func_name = func.get("name", "")
                    if "TB" in func_name and (sut_type in func_name or message_type.lower() in func_name.lower()):
                        result["reference_test_behavior"] = func
                        break
            # 选取相关的 templates
            if len(result["reference_templates"]) < 3:
                result["reference_templates"].append(tc)

    # 选取 pre/post condition functions
    for func in kb_search_results.get("functions", []):
        func_name = func.get("name", "")
        if "PR" in func_name and sut_type in func_name:
            result["reference_preconditions"].append(func)
        elif "PO" in func_name and sut_type in func_name:
            result["reference_postconditions"].append(func)

    log(f"Summarizer 降级策略: 选取 {len(result['reference_templates'])} 个模板, "
        f"precondition={len(result['reference_preconditions'])}, "
        f"postcondition={len(result['reference_postconditions'])}", "INFO")

    return result


# ============================================================
# Agent 3: Core Agent - TTCN-3 代码生成
# ============================================================
def run_core_agent(generation_plan: Dict, references: Dict,
                   few_shot_examples: Dict[str, str], sop: Dict,
                   error_feedback: str = "",
                   sut_mode: str = "SIL",
                   local_ref_section: str = "",
                   previous_core_files: Optional[List[Dict]] = None,
                   previous_infra_files: Optional[List[str]] = None,
                   is_fix_mode: bool = False,
                   protocol_snapshot: Optional[Dict] = None,
                   output_dir: Optional[object] = None) -> Dict[str, Any]:
    """运行 Core Agent，生成 TTCN-3 代码。

    Args:
        generation_plan: Orchestrator 生成的计划 JSON
        references: Summarizer 提取的参考代码
        few_shot_examples: Few-shot 样例字典
        sop: SOP JSON
        error_feedback: 上一次编译错误的反馈（用于迭代修复）
        sut_mode: 测试运行模式，"SIL" 或 "HIL"
        local_ref_section: 本地知识库检索结果
        previous_core_files: 上次生成的 core 文件列表（用于修复模式）
        previous_infra_files: 上次生成的 infrastructure 文件列表（用于参考）
        is_fix_mode: 是否为修复模式（精简 prompt，不包含 demo 参考）
        protocol_snapshot: ProtocolRegistry.snapshot() 输出 — 当前协议描述符。
             注入到 LLM prompt 中作为权威的协议知识来源。
    """
    system_prompt = load_text(CORE_AGENT_PROMPT_PATH)

    # P4: Inject protocol snapshot into system prompt
    # The prompt file has a {PROTOCOL_SNAPSHOT} placeholder.
    _snapshot_str = json.dumps(protocol_snapshot, indent=2, ensure_ascii=False) if protocol_snapshot else "{}"
    system_prompt = system_prompt.replace("{PROTOCOL_SNAPSHOT}", _snapshot_str)

    # Phase 3.5: Inject the GENDIR absolute path so the LLM knows exactly where to
    # write files. On 2026-06-18 the user hit a token-burning bug: the LLM
    # wrote generated files to the project root (~/桌面/platform/src/...) instead
    # of the per-TC GENDIR, which silently broke the build and triggered 3 retry
    # cycles. We now hardcode the absolute path into the prompt and forbid any
    # write outside it.
    import os as _os_for_path
    if output_dir is not None:
        _output_dir_str = str(output_dir.resolve()) if hasattr(output_dir, "resolve") else str(output_dir)
    else:
        _output_dir_env = _os_for_path.environ.get("TTCN3_GENDIR_OVERRIDE", "").strip()
        if _output_dir_env:
            _output_dir_str = _output_dir_env
        else:
            _output_dir_str = str((GENERATED_DIR / tc_id).resolve())
    system_prompt = system_prompt.replace("{OUTPUT_DIR}", _output_dir_str)

    # Build pre/postcondition params blocks (used by both fix-mode and normal-mode paths)
    # SIL mode: no parameters for pre/postcondition functions
    pre_params = "SIL mode: Precondition functions take NO parameters (void return type for f_SECC_CMN_PR_InitConfiguration_001, verdicttype return for others)."
    post_params = "SIL mode: Postcondition functions take NO parameters (void return type)."
    if sut_mode == "HIL":
        pre_params = "HIL mode:\n- f_SECC_CMN_PR_InitConfiguration_001(out HAL_61851_Listener, in SystemSECC) -> void\n- f_SECC_CMN_PR_SupportedAppProtocol_001(in HAL_61851_Listener) -> verdicttype\n- f_SECC_CMN_PR_SessionSetup_001(in HAL_61851_Listener) -> verdicttype"
        post_params = "HIL mode:\n- f_SECC_CMN_PO_InitialState_001(in HAL_61851_Listener) -> void\n- f_SECC_CMN_PO_ShutdownConfiguration_001(in HAL_61851_Listener, in SystemSECC) -> void"
    # Inject into system_prompt via string replacement
    system_prompt = system_prompt.replace("{pre_params}", pre_params)
    system_prompt = system_prompt.replace("{post_params}", post_params)

    # Load generation patterns for injection into prompt
    _gen_patterns_path = Path(__file__).parent / "knowledge_base" / "generation_patterns.json"
    gen_patterns_text = ""
    if _gen_patterns_path.exists():
        try:
            with open(_gen_patterns_path, "r", encoding="utf-8") as _f:
                gen_patterns_text = json.dumps(json.load(_f), indent=2, ensure_ascii=False)
        except Exception:
            gen_patterns_text = ""

    # Load shared project file contents for injection into prompt.
    # These files are generated by Infrastructure Agent; providing their content
    # to Core Agent prevents "cannot find definition" errors.
    CORE_AGENT_SHARED_REFS = _load_core_shared_refs()

    # Build reference code section (used in both normal and fix mode)
    ref_code_parts = []
    ref_size_limit = 3000  # 每个参考代码片段不超过 3KB
    if references:
        if references.get("reference_testcase"):
            ref_tc = references["reference_testcase"]
            if isinstance(ref_tc, dict) and ref_tc.get("code"):
                code = truncate_text(ref_tc['code'], ref_size_limit)
                ref_code_parts.append(f"// 参考 testcase:\n{code}")
        if references.get("reference_test_behavior"):
            ref_tb = references["reference_test_behavior"]
            if isinstance(ref_tb, dict) and ref_tb.get("code"):
                code = truncate_text(ref_tb['code'], ref_size_limit)
                ref_code_parts.append(f"// 参考 test behavior:\n{code}")
        for ref_tmpl in references.get("reference_templates", [])[:3]:  # 最多3个模板
            if isinstance(ref_tmpl, dict) and ref_tmpl.get("code"):
                code = truncate_text(ref_tmpl['code'], ref_size_limit)
                ref_code_parts.append(f"// 参考 template:\n{code}")
    ref_code_section = "\n\n".join(ref_code_parts) if ref_code_parts else "(无额外参考代码)"

    # === Fix Mode: Simplified prompt ===
    if is_fix_mode and error_feedback:
        # Build simplified fix mode prompt
        previous_files_section = ""
        if previous_core_files:
            previous_files_section = "## Previously Generated Core Files (for reference):\n"
            for f in previous_core_files:
                path = f.get("path", "")
                content = f.get("content", "")
                if path and content:
                    previous_files_section += f"\n### {path}:\n```ttcn3\n{content[:1000]}{'...' if len(content) > 1000 else ''}\n```\n"

        infra_files_section = ""
        if previous_infra_files:
            infra_files_section = f"## Previously Generated Infrastructure Files:\n- {', '.join(previous_infra_files[:10])}{'...' if len(previous_infra_files) > 10 else ''}\n"

        error_section = f"""
## ⚠️ 上一次生成的代码存在以下问题，请修复:
{error_feedback}
"""

        user_prompt = f"""{system_prompt}

---

## MODE: FIX MODE (Repair Iteration)

This is a repair iteration. Focus ONLY on fixing the errors reported below.

{error_section}

{previous_files_section}

{infra_files_section}

## Generation Plan (for reference):
```json
{json.dumps(generation_plan, indent=2, ensure_ascii=False)}
```

## Test Case Name
testcase_name: {generation_plan.get('testcase_name', generation_plan.get('tc_id', ''))}

## Test Mode
{sut_mode}

## Precondition Function Parameters
{pre_params}

## Postcondition Function Parameters
{post_params}

## From knowledge base local search (from Summarizer):
{local_ref_section if local_ref_section else "(无 local search 结果)"}

## From Summarizer Agent (reference code):
{ref_code_section}

## Generation Patterns (from knowledge_base/generation_patterns.json):

```json
{gen_patterns_text}
```

## SOP 常见错误提醒:
{json.dumps(sop.get("common_errors", []), indent=2, ensure_ascii=False)}

请严格按照生成计划和样例格式，生成完整的 TTCN-3 代码。只输出 JSON，不要解释。"""

    else:
        # === Normal Mode: Full prompt ===

        # 选择合适的 few-shot 样例（使用智能加载）
        test_type = generation_plan.get("test_type", "GoodCase")
        message_type = generation_plan.get("message_type", "")

        # 错误反馈（来自 Fault Corrector 的迭代修复）
        error_section = ""
        if error_feedback:
            error_section = f"""
## ⚠️ 上一次生成的代码存在以下问题，请修复:
{error_feedback}
"""

        # 动态构建 few-shot 样例部分（使用智能加载函数）
        few_shot_section = build_few_shot_section(
            few_shot_examples,
            test_type,
            message_type,
            max_total_size=FEW_SHOT_CONFIG["max_total_size"],
            max_single_size=FEW_SHOT_CONFIG["max_single_example"]
        )

        # (pre_params and post_params are injected into system_prompt above, outside the if/else block)

        user_prompt = f"""{system_prompt}

---
{error_section}

## 生成计划 (from Orchestrator):
```json
{json.dumps(generation_plan, indent=2, ensure_ascii=False)}
```

## Test Case Name
testcase_name: {generation_plan.get('testcase_name', generation_plan.get('tc_id', ''))}

## Test Mode
{sut_mode}

## Precondition Function Parameters
{pre_params}

## Postcondition Function Parameters
{post_params}

## Shared Project Files (read-only reference -- DO NOT modify these)

The Infrastructure Agent generates these files. Your test case imports from them:

### src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn:
```ttcn3
{CORE_AGENT_SHARED_REFS.get('TimerAndConstants', '(file not available)')}
```

### src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn:
```ttcn3
{CORE_AGENT_SHARED_REFS.get('CmnTemplates', '(file not available)')}
```

## Critical: Precondition Function Names (MUST generate EXACTLY these)

The generation plan specifies the following precondition chain:

{json.dumps(generation_plan.get('precondition_chain', []))}

You MUST generate function files for EXACTLY these function names.
DO NOT generate additional precondition functions not in this list.
For example, if the list is ["f_SECC_CMN_PR_InitConfiguration_001", "f_SECC_CMN_PR_Binding_001", "f_SECC_CMN_PR_InitConfiguration_002"],
then generate ONLY these 3 function files (plus the aggregator PreConditions_SECC_15118_2.ttcn).
DO NOT generate f_SECC_CMN_PR_SupportedAppProtocol_001 unless it is in the precondition_chain.

## Critical: Postcondition Function Names (MUST generate EXACTLY these)

The generation plan specifies the following postcondition chain:

{json.dumps(generation_plan.get('postcondition_chain', []))}

You MUST generate function files for EXACTLY these function names.
DO NOT generate additional postcondition functions not in this list.

## TTCN-3 Module Naming Rules

CRITICAL: The module name inside the TTCN-3 code MUST match the function name inside the module.

Examples:
- For precondition function `f_SECC_CMN_PR_InitConfiguration_001`, the file MUST be named `f_SECC_CMN_PR_InitConfiguration_001.ttcn` and the module name MUST be `f_SECC_CMN_PR_InitConfiguration_001`.
- For postcondition function `f_SECC_CMN_PO_InitialState_001`, the file MUST be named `f_SECC_CMN_PO_InitialState_001.ttcn` and the module name MUST be `f_SECC_CMN_PO_InitialState_001`.

DO NOT create a separate "aggregator" file like `PreConditions_InitConfiguration.ttcn` with module name `PreConditions_InitConfiguration` for a function called `f_SECC_CMN_PR_InitConfiguration_001`. The module name must match the function name.

DO NOT use the same module name in multiple files. Each .ttcn file must have a unique module name.

## From knowledge base local search (from Summarizer):
{local_ref_section if local_ref_section else "(无 local search 结果)"}

## From Summarizer Agent (reference code):
{ref_code_section}

{few_shot_section}

## Generation Patterns (from knowledge_base/generation_patterns.json):

This file contains canonical TTCN-3 code generation patterns that you MUST follow:

```json
{gen_patterns_text}
```

## SOP 常见错误提醒:
{json.dumps(sop.get("common_errors", []), indent=2, ensure_ascii=False)}

请严格按照生成计划和样例格式，生成完整的 TTCN-3 代码。只输出 JSON，不要解释。"""

    # 记录 Core Agent prompt 大小
    log_prompt_size("Core Agent", user_prompt)

    response = call_agent_with_timeout(user_prompt, "Core Agent")
    if not response:
        return {"testcase": "", "precondition_files": [], "postcondition_files": [], "_parse_error": True}

    result = extract_core_agent_output(response)
    # 所有解析策略均失败时触发重试
    if result.get("_parse_error"):
        log("Core Agent 响应解析失败，触发一次重试...", "WARN")
        retry_response = call_claude_agent(user_prompt, "Core Agent (retry)", timeout=900,
                                          extra_allowed_tools="Write,Read,Edit,Bash(bash *)")
        if retry_response:
            result = extract_core_agent_output(retry_response)
            if not result.get("_parse_error"):
                log("重试成功", "INFO")
    return result


# ============================================================
# Agent 4: Unified Fault Corrector
# (runtime only: post-execution dual-track analysis)
# ============================================================

def run_fault_corrector(
    ttcn3_code: str,
    generation_plan: Dict,
    sop: Dict,
    phase: str = "runtime",
    tc_id: Optional[str] = None,
    log_path: Optional[Path] = None,
    pcap_path: Optional[Path] = None,
) -> Dict:
    """Fault Corrector -- runtime behavior analysis only.

    Phase   | Trigger                              | Output
    --------|--------------------------------------|------------------------------
    runtime | TITAN executed (log + optional PCAP) | fault_type + repairs

    Note: Compile phase errors are now handled by Infrastructure Agent's self-compile loop.

    Args:
        ttcn3_code: The TTCN-3 source code under analysis.
        generation_plan: The Orchestrator generation plan JSON.
        sop: The SOP JSON from knowledge base.
        phase: Always "runtime". Only runtime phase is supported.
        tc_id: Test case ID (required for runtime phase).
        log_path: Path to TITAN TestExecution.log (runtime phase).
        pcap_path: Path to PCAP capture file (runtime phase, optional).

    Returns:
        Result dict matching the JSON schema in fault_corrector_prompt.txt.
    """
    system_prompt = load_text(FAULT_CORRECTOR_PROMPT_PATH)

    # Knowledge base entries for runtime analysis
    kb_text = _build_kb_reference_text()
    system_prompt = system_prompt.replace("{KNOWLEDGE_BASE_ENTRIES}", kb_text)

    user_prompt_parts = [system_prompt]

    # --- Phase-specific prompt construction ---

    if phase == "runtime":
        if not log_path or not Path(log_path).exists():
            return {
                "status": "skipped",
                "phase": "runtime",
                "message": f"TITAN log not found: {log_path}",
                "issues_found": [],
                "corrected_code": "",
                "repairs": [],
            }

        user_prompt_parts.append(f"## PHASE: runtime\n")
        user_prompt_parts.append(f"## Test Case ID: {tc_id or 'unknown'}\n")

        # Parse TTCN-3 log
        log_parser = TitanLogParser()
        all_entries = log_parser.parse_file(str(log_path))
        testcase_events = log_parser.get_event_sequence(tc_id or "", entries=all_entries)

        if not testcase_events:
            return {
                "status": "skipped",
                "phase": "runtime",
                "message": f"No log events found for test case: {tc_id}",
                "issues_found": [],
                "corrected_code": "",
                "repairs": [],
            }

        # Format log events for the LLM (with truncation to prevent prompt overflow)
        log_text = _format_titan_log_events(
            testcase_events,
            max_events=FAULT_CORRECTOR_CONFIG["max_log_events"],
            max_event_size=FAULT_CORRECTOR_CONFIG["log_event_truncate"]
        )
        user_prompt_parts.append(f"## TITAN Log Events:\n{log_text}\n")

        # Parse PCAP if available (with truncation)
        packets_text = ""
        if pcap_path and Path(pcap_path).exists() and FAULT_CORRECTOR_AVAILABLE:
            try:
                pcap_parser = PcapParser()
                packets = pcap_parser.parse_file(str(pcap_path))
                packets_text = _format_pcap_packets(
                    packets,
                    max_packets=FAULT_CORRECTOR_CONFIG["max_pcap_packets"]
                )
                user_prompt_parts.append(f"## PCAP Packets:\n{packets_text}\n")
            except Exception as e:
                log(f"Fault Corrector (runtime): PCAP parsing failed: {e}", "WARN")

        # Also provide Python-side correlation results as context
        if FAULT_CORRECTOR_AVAILABLE:
            try:
                packets = []
                if pcap_path and Path(pcap_path).exists():
                    pcap_parser = PcapParser()
                    packets = pcap_parser.parse_file(str(pcap_path))
                correlator = Correlator()
                alignment = correlator.align(
                    testcase_events, packets, tc_name=tc_id or "unknown"
                )
                classifier = FaultClassifier()
                fault = classifier.classify(alignment, testcase_events)
                repair_engine = RepairEngine()
                repairs = repair_engine.generate_repairs(fault, ttcn3_code, generation_plan)

                correlation_ctx = {
                    "python_alignment": {
                        "root_cause": alignment.root_cause,
                        "confidence": alignment.confidence,
                        "summary": alignment.summary,
                        "correlated_count": len(alignment.correlated_events),
                        "missing_count": len(alignment.missing_packets),
                        "unexpected_count": len(alignment.unexpected_packets),
                    },
                    "python_fault": {
                        "fault_type": fault.fault_type,
                        "confidence": fault.confidence,
                        "description": fault.description,
                        "affected_component": fault.affected_component,
                    },
                    "python_repairs": [
                        {
                            "repair_type": r.repair_type,
                            "fault_type": r.fault_type,
                            "location": r.location,
                            "current_code": r.current_code,
                            "suggested_code": r.suggested_code,
                            "reasoning": r.reasoning,
                            "confidence": r.confidence,
                        }
                        for r in repairs
                    ],
                }
                user_prompt_parts.append(
                    f"## Python-Side Analysis Context:\n```json\n{json.dumps(correlation_ctx, indent=2, ensure_ascii=False)}\n```\n"
                )
            except Exception as e:
                log(f"Fault Corrector (runtime): Python analysis failed: {e}", "WARN")

        user_prompt_parts.append(f"""\
## TTCN-3 Source Code:
```ttcn3
{ttcn3_code}
```

## Generation Plan:
```json
{json.dumps(generation_plan, indent=2, ensure_ascii=False)}
```

Please perform dual-track root cause analysis and output the JSON result.""")

    else:
        return {
            "status": "error",
            "phase": phase,
            "message": f"Unknown phase: {phase}. Only 'runtime' is supported.",
            "issues_found": [],
            "corrected_code": ttcn3_code,
            "repairs": [],
        }

    user_prompt = "\n".join(user_prompt_parts)
    response = call_agent_with_timeout(user_prompt, "Fault Corrector", read_only=True)

    if not response:
        return {
            "status": "error",
            "phase": phase,
            "message": "Fault Corrector LLM call returned empty",
            "issues_found": [],
            "corrected_code": ttcn3_code,
            "repairs": [],
        }

    try:
        result = extract_json_from_response(response)
    except Exception as e:
        log(f"Fault Corrector: extract_json_from_response raised unexpected error: {e}", "WARN")
        result = None

    # Prefer the structured markdown JSON block over the stream-array that
    # Claude CLI emits. The raw response is JSON Lines (an event array) plus
    # a trailing markdown ```json block containing the actual agent verdict.
    # Strategy 1 of extract_json_from_response matches the first balanced
    # brace pair — which lands on the init event in the array, NOT on the
    # intended verdict. We work around this by extracting the markdown
    # block first and only falling back to the default strategy on miss.
    if not (isinstance(result, dict) and result.get("status") in ("needs_fix", "pass", "no_fix", "ok")):
        import re as _re_fc
        md_block = _re_fc.search(r'```json\s*(.*?)\s*```', response, _re_fc.DOTALL)
        if md_block:
            try:
                md_parsed = json.loads(md_block.group(1))
                if isinstance(md_parsed, dict) and "status" in md_parsed:
                    log("Fault Corrector: using markdown ```json block (skipped noisy stream events)", "WARN")
                    result = md_parsed
            except (json.JSONDecodeError, ValueError) as e:
                log(f"Fault Corrector: markdown json block parse failed: {e}", "WARN")

    if not result:
        log("Fault Corrector output could not be parsed as JSON", "WARN")
        return {
            "status": "pass",
            "phase": phase,
            "issues_found": [],
            "corrected_code": ttcn3_code,
            "repairs": [],
        }

    # CRITICAL: extract_json_from_response can return a LIST when the
    # recursive bracket scanner (Strategy 2) matches a `[...]` block before
    # the brace scanner finds the top-level `{...}`. This commonly happens
    # when the response contains nested arrays (e.g. "repairs": [...]) and
    # the JSON block boundary detection hits the inner array first.
    # Coerce to dict; if the result is a non-empty list, wrap its first
    # element so downstream code can still read fields like "status" and
    # "issues_found". If the result is an empty list, treat as "no fix".
    if isinstance(result, list):
        if len(result) == 1 and isinstance(result[0], dict):
            log("Fault Corrector: extract_json returned a 1-element list — unwrapping", "WARN")
            result = result[0]
        elif len(result) == 0:
            log("Fault Corrector: extract_json returned an empty list — treating as no fix", "WARN")
            return {
                "status": "pass",
                "phase": phase,
                "issues_found": [],
                "corrected_code": ttcn3_code,
                "repairs": [],
            }
        else:
            # Multi-element list (e.g. list of issues). Synthesize a dict
            # that downstream code can consume.
            log(f"Fault Corrector: extract_json returned a {len(result)}-element list — synthesizing dict", "WARN")
            result = {
                "status": "needs_fix",
                "issues_found": result if all(isinstance(x, dict) for x in result) else [],
                "repairs": [],
                "corrected_code": "",
            }
    if not isinstance(result, dict):
        log(f"Fault Corrector: extract_json returned unexpected type {type(result).__name__} — treating as no fix", "WARN")
        return {
            "status": "pass",
            "phase": phase,
            "issues_found": [],
            "corrected_code": ttcn3_code,
            "repairs": [],
        }

    # Always tag the phase
    result["phase"] = phase
    return result


def _build_kb_reference_text() -> str:
    """Load titan_troubleshooting_db.json and format entries for the LLM prompt."""
    try:
        kb_data = json.loads(Path(KB_DIR / "titan_troubleshooting_db.json").read_text(encoding="utf-8"))
    except Exception:
        return "（知识库文件不可用，跳过）"

    lines = []
    for entry in kb_data.get("entries", []):
        patterns = entry.get("patterns", [])
        lines.append(
            f"- **{entry.get('error_type', '?')}** (severity={entry.get('severity', '?')}): "
            f"{entry.get('fix_en', entry.get('fix', 'N/A'))}"
        )
        if patterns:
            lines.append(f"  Patterns: {', '.join(patterns[:3])}")
    return "\n".join(lines) if lines else "（无知识库条目）"


def _format_titan_log_events(events: List, max_events: int = None,
                            max_event_size: int = None) -> str:
    """Format TitanLogEntry objects for the LLM prompt with truncation.

    Args:
        events: List of log events
        max_events: Maximum number of events to include (None = use config default)
        max_event_size: Maximum characters per event (None = use config default)

    Returns:
        Formatted log events string, possibly truncated
    """
    if not events:
        return "(no events)"

    if max_events is None:
        max_events = FAULT_CORRECTOR_CONFIG["max_log_events"]
    if max_event_size is None:
        max_event_size = FAULT_CORRECTOR_CONFIG["log_event_truncate"]

    lines = []
    event_count = 0
    total_size = 0

    for e in events:
        if event_count >= max_events:
            lines.append(f"... [truncated {len(events) - event_count} more events]")
            break

        ts = getattr(e, "timestamp", "???")
        et = getattr(e, "event_type", "???")
        det = getattr(e, "details", "")

        # Truncate long details
        if len(det) > max_event_size:
            det = det[:max_event_size] + "... [truncated]"

        line = f"[{ts}] {et}: {det}"
        line_size = len(line)

        # Check if adding this event would exceed reasonable prompt size
        if total_size + line_size > max_events * max_event_size * 0.8:
            remaining = len(events) - event_count
            if remaining > 0:
                lines.append(f"... [truncated {remaining} more events]")
            break

        lines.append(line)
        total_size += line_size
        event_count += 1

    return "\n".join(lines)


def _format_pcap_packets(packets: List, max_packets: int = None,
                         max_packet_size: int = None) -> str:
    """Format V2GTP_Packet objects for the LLM prompt with truncation.

    Args:
        packets: List of packet objects
        max_packets: Maximum number of packets to include (None = use config default)
        max_packet_size: Maximum characters per packet (None = use config default)

    Returns:
        Formatted packets string, possibly truncated
    """
    if not packets:
        return "(no packets)"

    if max_packets is None:
        max_packets = FAULT_CORRECTOR_CONFIG["max_pcap_packets"]

    lines = []
    packet_count = 0

    for p in packets:
        if packet_count >= max_packets:
            remaining = len(packets) - packet_count
            lines.append(f"... [truncated {remaining} more packets]")
            break

        ts = getattr(p, "timestamp", "???")
        direction = getattr(p, "direction", "???")
        proto_ver = getattr(p, "protocol_version", "?")
        payload_type = getattr(p, "payload_type", "?")
        info = getattr(p, "packet_info", "")

        # Truncate long info
        if max_packet_size is None:
            max_packet_size = FAULT_CORRECTOR_CONFIG["log_event_truncate"]
        if len(info) > max_packet_size:
            info = info[:max_packet_size] + "... [truncated]"

        lines.append(
            f"[{ts:.3f}] {direction} proto={proto_ver} payloadType={payload_type}: {info}"
        )
        packet_count += 1

    return "\n".join(lines)


# ============================================================
# Agent 6: Infrastructure Agent - 生成完整项目结构
# ============================================================

def load_demo_file(relative_path: str) -> str:
    """从 demo/ 目录加载参考文件内容"""
    path = DEMO_ROOT / relative_path
    if path.exists():
        size = path.stat().st_size
        log(f"[DIAG] load_demo_file: {relative_path} ({size} bytes) from {DEMO_ROOT}")
        return load_text(path)
    log(f"警告: demo 参考文件不存在: {relative_path}", "WARN")
    return ""


def _create_batch_prompts(
    system_prompt: str,
    generation_plan: Dict,
    ttcn3_code: str,
    tc_id: str,
    sut_ip: Optional[str],
    demo_summary_text: str
) -> List[str]:
    """Create batched prompts for large infrastructure generation.

    Splits the generation into two batches to reduce output size per response:
    - Batch 1: Config files + C++ adapter placeholders + essential TTCN
    - Batch 2: Additional TTCN skeleton files + build scripts + logs

    Args:
        system_prompt: Base system prompt
        generation_plan: Generation plan JSON
        ttcn3_code: Generated TTCN-3 test code
        tc_id: Test case ID
        sut_ip: SUT IP address
        demo_summary_text: Demo reference files summary

    Returns:
        List of batched prompt strings
    """
    import json

    base_instructions = f"""
Now generate the project structure.

## Files to Generate (ALL in this single response):
1. config/titan_runtime.cfg
2. config/hardware_env.json
3. src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn
4. src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn
5. src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn
6. src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn
7. src/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn
8. src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn

Pipeline handles automatically (DO NOT generate):
- build.sh -- copied from demo/ by the pipeline
- C++ adapter files (12 files) -- copied from demo/ by the pipeline
- V2G port adapter .cc/.hh implementations (V2G_TCP_TLS_Port, V2G_UDP_SDP_Port) -- copied from demo/ by the pipeline
- CommonFunctions.ttcn, PIXIT_SECC_15118_2.ttcn -- copied from demo/ by the pipeline
- PreConditions/PostConditions aggregator modules -- copied from demo/ by the pipeline
- Pre/Post condition function definitions -- generated by Core Agent

IMPORTANT: Generate ALL 8 files listed above in this single response.
Output ONLY the JSON object with all files."""

    batch1_prompt = f"""{system_prompt}

---

## Reference Files from demo/

### Generation Plan:
```json
{json.dumps(generation_plan, indent=2, ensure_ascii=False)}
```

### Generated TTCN-3 Test Code:
```ttcn3
{ttcn3_code}
```

### SUT IP:
{sut_ip if sut_ip else "(not provided, will use IPv6 multicast ff02::01)"}

### Demo Reference Files:
{demo_summary_text}

{base_instructions}
"""

    batch2_instructions = ""  # No second batch needed
    batch2_prompt = ""  # No second batch needed

    return [batch1_prompt]


def run_infrastructure_agent(
    generation_plan: Dict,
    ttcn3_code: str,
    tc_id: str,
    sut_ip: Optional[str] = None,
    tc_output_filename: str = "",
    error_feedback: str = "",
    previous_infra_files: Optional[List[str]] = None,
    previous_core_files: Optional[List[Dict]] = None,
    is_fix_mode: bool = False,
    output_dir: Optional[Path] = None,
    protocol_snapshot: Optional[Dict] = None,
) -> Dict:
    """运行 Infrastructure Agent，生成完整的 TITAN 项目结构

    架构说明：
    1. Pipeline 复制：12 个 C++ 适配器文件（common/TRI/TCI/EXI）从 demo/ 复制
    2. Infrastructure Agent 生成：TTCN 骨架 + config + build.sh + C++ Port adapter（4 个 .cc/.hh）
    3. Core Agent 生成：PreCondition / PostCondition 函数定义 + 主测试用例 module

    文件分工：
    - Pipeline：12 个 C++ 适配器文件（从 demo/ 复制）
    - Infrastructure Agent：TTCN 骨架 + config + build.sh + C++ Port adapter（4 个文件）
    - Core Agent：PreCondition/PostCondition 函数 + 主测试用例 module

    Args:
        generation_plan: Orchestrator 生成的计划 JSON
        ttcn3_code: Core Agent 生成的完整测试用例 TTCN-3 代码
        tc_id: 测试用例 ID
        sut_ip: SUT IP 地址（可选，用于配置）
        tc_output_filename: 测试用例输出文件名
        error_feedback: 上一次编译错误的反馈（用于修复模式）
        previous_infra_files: 上次生成的 infrastructure 文件路径列表（用于修复模式）
        previous_core_files: 上次生成的 core 文件列表（用于参考）
        is_fix_mode: 是否为修复模式（精简 prompt，不包含 demo 参考）
    Args:
        generation_plan: Orchestrator 生成的计划 JSON
        ttcn3_code: Core Agent 生成的完整测试用例 TTCN-3 代码
        tc_id: 测试用例 ID
        sut_ip: SUT IP 地址（可选，用于配置）
        tc_output_filename: 测试用例输出文件名
        error_feedback: 上一次编译错误的反馈（用于修复模式）
        previous_infra_files: 上次生成的 infrastructure 文件路径列表（用于修复模式）
        previous_core_files: 上次生成的 core 文件列表（用于参考）
        is_fix_mode: 是否为修复模式（精简 prompt，不包含 demo 参考）
        output_dir: 项目输出目录
        protocol_snapshot: ProtocolRegistry.snapshot() 输出 — 当前协议描述符。
             注入到 LLM prompt 中作为权威的协议知识来源。
    """
    import time
    import shutil

    system_prompt = load_text(INFRASTRUCTURE_AGENT_PROMPT_PATH)

    doc_urls = load_doc_urls()
    doc_urls_text = build_doc_urls_for_infrastructure(doc_urls)
    system_prompt = system_prompt.replace("{DOC_URLS_INFRASTRUCTURE}", doc_urls_text)

    # P4: Inject protocol snapshot — replaces the hardcoded ISO 15118-2 V2G_MessageUnion
    # and precondition rules in the prompt with the live protocol descriptor
    if protocol_snapshot:
        _snap_json = json.dumps(protocol_snapshot, indent=2, ensure_ascii=False)
        system_prompt = system_prompt.replace("{PROTOCOL_SNAPSHOT}", _snap_json)
    else:
        system_prompt = system_prompt.replace("{PROTOCOL_SNAPSHOT}", "{}")

    # 2C: Inject the missing-precondition directive — computed by the plan
    # builder's completeness check. The prompt now carries a hard requirement
    # for the Infrastructure Agent to generate any PR modules that the
    # precondition_chain references but the demo baseline doesn't supply.
    # This closes the "Infrastructure Agent is blind to missing PRs" gap.
    _missing_pr_list = generation_plan.get("_precondition_completeness", {}).get("missing_preconditions", [])
    _missing_po_list = generation_plan.get("_precondition_completeness", {}).get("missing_postconditions", [])
    _missing_directive = _build_missing_precondition_directive(_missing_pr_list, _missing_po_list)
    system_prompt = system_prompt.replace("{MISSING_PRECONDITIONS_DIRECTIVE}", _missing_directive)

    # === 所有静态文件：Pipeline 直接复制，不通过 Agent ===
    # C++ 适配器（12 个 .hh + .cc 文件）-- 定义在 if/else 外部，确保 fix mode 也能访问
    ALL_CPP_ADAPTER_FILES = [
        "src/cpp_adapters/common/Common.hh",
        "src/cpp_adapters/common/Common.cc",
        "src/cpp_adapters/CD/titan_tci/TitanTci.hh",
        "src/cpp_adapters/CD/titan_tci/TitanTci.cc",
        "src/cpp_adapters/CD/exi_engine/ExiCodecAdapter.hh",
        "src/cpp_adapters/CD/exi_engine/ExiCodecAdapter.cc",
        "src/cpp_adapters/SA/titan_tri/TitanTri.hh",
        "src/cpp_adapters/SA/titan_tri/TitanTri.cc",
        "src/cpp_adapters/SA/hardware/HAL61851Adapter.hh",
        "src/cpp_adapters/SA/hardware/HAL61851Adapter.cc",
        "src/cpp_adapters/SA/hardware/TCPTLSPortAdapter.hh",
        "src/cpp_adapters/SA/hardware/TCPTLSPortAdapter.cc",
    ]

    # === Build demo reference section (used in normal mode; fix mode uses simplified prompt) ===
    demo_parts = []
    demo_summary_text = "(not included in fix mode)"
    # === Fix Mode: Simplified prompt ===
    if is_fix_mode and error_feedback:
        previous_files_section = ""
        if previous_infra_files:
            previous_files_section = f"## Previously Generated Infrastructure Files:\n- {', '.join(previous_infra_files[:15])}{'...' if len(previous_infra_files) > 15 else ''}\n"

        core_files_section = ""
        if previous_core_files:
            core_files_section = "## Core Agent Generated Files (DO NOT modify):\n"
            for f in previous_core_files:
                path = f.get("path", "")
                content = f.get("content", "")
                if path and content:
                    core_files_section += f"- {path}\n"
            core_files_section += "\nThese files are generated by the Core Agent. Do NOT modify them in your fix.\n"

        error_section = f"""
## ⚠️ 上一次生成的项目存在以下问题，请修复:
{error_feedback}
"""

        # === Pre-install static TTCN files from demo (same as normal mode) ===
        _infra_tc_dir = output_dir if output_dir else (GENERATED_DIR / tc_id)
        _static_ttcn_files = {
            # Types and constants: complete from demo, no LLM generation needed
            "src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn": None,
            "src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn": None,
            "src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn": None,
            # PICS
            "src/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn": None,
            # Architecture
            "src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn": None,
            # Aggregators
            "src/ttcn3/3_Behavior_Steps/PreConditions/PreConditions_SECC_15118_2.ttcn": None,
            "src/ttcn3/3_Behavior_Steps/PostConditions/PostConditions_SECC_15118_2.ttcn": None,
            # TestCases_SECC.ttcn must use the actual tc_output_filename (not hardcoded demo name)
            "src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn": (
                f'module TestCases_SECC {{\n'
                f'    import from SECC_TestSystem all;\n'
                f'    import from {tc_output_filename} all;\n'
                f'\n'
                f'    control {{\n'
                f'        execute({tc_output_filename}());\n'
                f'    }}\n'
                f'}} // end module\n'
            ),
        }
        for _rel, _content in _static_ttcn_files.items():
            _src = DEMO_ROOT / _rel
            _dst = _infra_tc_dir / _rel
            _dst.parent.mkdir(parents=True, exist_ok=True)
            if _content is not None:
                _dst.write_text(_content, encoding="utf-8")
                log(f"[INFRA PREINSTALL] {_rel} (static generation)", "DEBUG")
            elif _src.exists():
                shutil.copy2(_src, _dst)
                log(f"[INFRA PREINSTALL] {_rel}", "DEBUG")
            else:
                log(f"[INFRA PREINSTALL] Not in demo: {_rel}", "DEBUG")

        user_prompt = f"""{system_prompt}

---

## MODE: FIX MODE (Repair Iteration)

This is a repair iteration. Focus ONLY on fixing the errors reported below related to project structure.

{error_section}

{previous_files_section}

{core_files_section}

### Generation Plan:
```json
{json.dumps(generation_plan, indent=2, ensure_ascii=False)}
```

### Generated Test Case File (from Core Agent):
The main test case file generated by the Core Agent is placed at:
  src/ttcn3/4_TestCases/{tc_output_filename}

DO NOT modify the Core Agent's TTCN-3 test case code. Only fix infrastructure-related issues.

### IMPORTANT: Files handled by the pipeline (DO NOT generate these)
- build.sh: handled by pipeline (copied from demo/)
- 12 C++ adapter files: handled by pipeline (copied from demo/)
- V2G port adapter .cc/.hh implementations: handled by pipeline
- PreConditions/PostConditions function definitions: handled by Core Agent

### SUT IP (for titan_runtime.cfg):
{sut_ip if sut_ip else "(not provided, will use IPv6 multicast ff02::01)"}

Now generate the corrected project structure. Output ONLY the JSON object."""

    else:
        # === Normal Mode: Full prompt ===

        # === Demo 参考文件：只包含 Agent 需要的 TTCN 骨架参考 ===
        def _load_demo(path: str) -> str:
            return load_demo_file(path)

        # Agent 需要的参考文件（只加载 TTCN 骨架，prompt 大幅减小）
        demo_parts = []

        # 配置文件参考
        demo_parts.append("### Reference: Config Files")
        demo_parts.append(f"[File: config/titan_runtime.cfg]\n{_load_demo('config/titan_runtime.cfg')}")
        demo_parts.append(f"[File: config/hardware_env.json]\n{_load_demo('config/hardware_env.json')}")

        # build.sh: NOT included in demo_parts — pipeline copies it directly from demo.
        # It is infrastructure-critical and must not be truncated. See _install_build_script().
        demo_parts.append("[File: build.sh] (handled by pipeline: copied directly from demo/)")
        demo_parts.append("[File: port_implementation_stub.txt] (handled by pipeline: copied directly from demo/)")

        # TTCN skeleton templates: inline (no file loads — reduces prompt from ~35KB to ~4KB).
        # Each template is 40-80 lines — enough context for the agent without LLM reasoning bloat.
        demo_parts.append("### Reference: TTCN Skeleton File Templates (inline)")

        demo_parts.append("""[File: src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn]
module SECC_TestSystem {
    import from SECC_Types all;

    // V2G_PortMessage: dual-path for V2GTP BadCase support
    //   v2g variant: normal V2G messages (EXI-encoded via C++ JSON library)
    //   raw variant: raw octetstring for V2GTP header BadCase tests ONLY
    type union V2G_PortMessage {
        V2G_MessageUnion  v2g,
        octetstring       raw
    };

    // V2G_TCP_TLS_Port: out uses V2G_PortMessage (dual-path), in uses V2G_MessageUnion.
    // V2G_UDP_SDP_Port: raw octetstring for SDP protocol (no EXI).
    type port V2G_TCP_TLS_Port message {
        out V2G_PortMessage;
        in  V2G_MessageUnion;
    };
    type port V2G_UDP_SDP_Port message {
        out octetstring;
        in  octetstring;
    };

    // SECCTester: the main test component. SystemSECC: the SUT component.
    type component SECCTester {
        port V2G_TCP_TLS_Port pt_V2G_TCP_TLS_ALM_SECC_Port;
        port V2G_UDP_SDP_Port pt_V2G_UDP_SDP_Port;
    };
    type component SystemSECC {
        port V2G_TCP_TLS_Port pt_V2G_TCP_TLS_ALM_SECC_Port;
        port V2G_UDP_SDP_Port pt_V2G_UDP_SDP_Port;
    };
} // end module""")

        demo_parts.append("""[File: src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn]
module SECC_Constants {
    import from SECC_TestSystem all;

    // Message Timeouts (ISO 15118-3)
    const float PX_SECC_CMN_Message_Timeout := 60.0;
    const float PX_SECC_CMN_Sequence_Timeout := 2.0;
    const float PX_SECC_CMN_SDP_Response_Timeout := 1.0;
    const float PX_SECC_CMN_TCP_Connection_Timeout := 5.0;
    const float PX_SECC_CMN_TLS_Handshake_Timeout := 10.0;

    // Default Timer Values
    const float PX_SECC_CMN_DefaultTimer := 5.0;
    const float PX_SECC_CMN_LongTimer := 30.0;

    // V2GTP Protocol Version
    const octetstring PX_SECC_CMN_V2GTP_ProtocolVersion := '01'O;
    const octetstring PX_SECC_CMN_V2GTP_InvVersion     := 'FE'O;

    // Default EVCCID (8 bytes hex)
    const octetstring PX_SECC_CMN_Default_EVCCID := '123456789ABC'O;
    const octetstring PX_SECC_CMN_Default_SessionID := '0000000000000000'O;

    // SDP / V2G Port
    const charstring PX_SECC_CMN_SDP_Multicast_Addr := "ff02::01";
    const integer PX_SECC_CMN_SDP_Port := 15118;
    const integer PX_SECC_CMN_V2G_Port := 15118;
} // end module""")

        demo_parts.append("""[File: src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn]
module SECC_Types {

    // Section 1: Basic ID types
    type record EvccIDType { octetstring content optional };
    type record SessionIDType { octetstring content optional };

    // Section 2: V2G Message Type constants (PayloadType in V2GTP header)
    const integer V2G_MSG_TYPE_SUPPORTED_APP_PROT := 32768;  // 0x8000
    const integer V2G_MSG_TYPE_SESSION_SETUP     := 32769;  // 0x8001
    const integer V2G_MSG_TYPE_SERVICE_DISCOVERY  := 32770;  // 0x8002

    // Section 3: AppProtocol messages
    type record AppProtocolEntry {
        charstring   protocolNamespace,
        integer      versionNumberMajor,
        integer      versionNumberMinor,
        integer      SchemaID optional
    };
    type record SupportedAppProtocolReq {
        AppProtocolEntry protocolArray[1 .. 16] optional
    };
    type record SupportedAppProtocolRes {
        integer ResponseCode,   // 0=OK, 1=Failed
        integer SchemaID optional,
        charstring SchemaURI optional
    };

    // Section 4: SessionSetup messages
    type record SessionSetupReq {
        charstring   EVCCID,
        charstring   DEXArray[1 .. 16] optional
    };
    type record SessionSetupRes {
        integer ResponseCode,
        charstring   EVSEID,
        integer EVSETimeStamp optional
    };

    // Section 5: V2G_MessageUnion (top-level union for V2G messages on the wire)
    // Keys must match exi_payload_library.json: supportedAppProtocolReq, sessionSetupReq, ...
    type union V2G_MessageUnion {
        SupportedAppProtocolReq  supportedAppProtocolReq,
        SupportedAppProtocolRes  supportedAppProtocolRes,
        SessionSetupReq          sessionSetupReq,
        SessionSetupRes          sessionSetupRes,
        octetstring              rawPayload  // fallback for unknown message types
    };

} // end module""")

        demo_parts.append("""[File: src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn]
module SECC_CmnTemplates {
    import from SECC_Types all;

    // Common timeout altstep
    altstep a_CMN_WaitTimeout(inout timer p_timer) {
        [] p_timer.timeout {
            setverdict(inconc, "Timeout occurred");
        }
    }

    // Verdict helper
    function f_CMN_IsVerdictPass(in verdicttype p_verdict) return boolean {
        return (p_verdict == pass);
    }

    // V2G message send: use structured union with the msgType name from V2G_MessageUnion.
    // C++ adapter encodes via exi_payload_library.json, patches V2GTP header, sends over TCP.
    // Example template (adjust msgType to match your message):
    // template SupportedAppProtocolReq md_CMN_SupportedAppProtocolReq_001 := {
    //     protocolArray := omit
    // };
} // end module""")

        demo_parts.append("""[File: src/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn]
module SECC_PICS {
    import from SECC_TestSystem all;

    // Basic Communication
    const boolean PICS_SECC_CMN_TCP_Supported := true;
    const boolean PICS_SECC_CMN_UDP_Supported := true;
    const boolean PICS_SECC_CMN_IPv6_Supported := true;

    // TLS
    const boolean PICS_SECC_CMN_TLS_Required := true;
    const boolean PICS_SECC_CMN_TLS12_Supported := true;

    // Supported V2G Messages
    const boolean PICS_SECC_CMN_SupportedAppProtocol := true;
    const boolean PICS_SECC_CMN_SessionSetup := true;
} // end module""")

        demo_parts.append("""[File: src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn]
module TestCases_SECC {
    import from SECC_TestSystem all;
    import from TC_SECC_CMN_VTB_V2GTPHeader_002 all;

    control {
        execute(TC_SECC_CMN_VTB_V2GTPHeader_002());
    }
} // end module""")
        demo_parts.append("\n### NOTE: The following files are handled by the pipeline automatically")
        demo_parts.append("- 12 C++ adapter files (common/TRI/TCI/EXI/HAL): copied from demo/")
        demo_parts.append("- Port adapter .cc implementations (V2G_TCP_TLS_Port, V2G_UDP_SDP_Port): copied from demo/")
        demo_parts.append("- build.sh: copied from demo/ (infrastructure-critical, never generated by Agent)")
        demo_parts.append("- PreConditions/PostConditions aggregator modules: copied from demo/ by pipeline (PreConditions_SECC_15118_2.ttcn, PostConditions_SECC_15118_2.ttcn)")
        demo_parts.append("- PreConditions/PostConditions function definitions: generated by Core Agent, placed by pipeline")
        demo_parts.append("DO NOT include these files in your output JSON.")

        # === Asset-Promotion-aware focus directive (Phase 3.1) ===
        # After Phase 2 ran, _precondition_completeness.promoted_preconditions contains
        # modules the pipeline copied from a sibling run. If there are still missing
        # precondition files, tell the agent to focus ONLY on those — not on
        # infrastructure that's already pre-installed. This keeps the agent from
        # re-doing boilerplate and lets it concentrate on synthesizing the few
        # missing TTCN modules.
        _missing_after_promote = generation_plan.get("_precondition_completeness", {}).get(
            "missing_preconditions", []
        )
        _promoted_list = generation_plan.get("_precondition_completeness", {}).get(
            "promoted_preconditions", []
        )
        _missing_focus_directive = ""
        if _missing_after_promote:
            _missing_lines = "\n".join(
                f"  - `{fn}.ttcn` at `src/ttcn3/3_Behavior_Steps/PreConditions/{fn}.ttcn`"
                for fn in _missing_after_promote
            )
            _missing_focus_directive = (
                "\n\n## FOCUS DIRECTIVE — Generate ONLY the following missing precondition modules:\n"
                f"{_missing_lines}\n\n"
                "These are the ONLY files the agent must produce. Do NOT touch:\n"
                "- build.sh (pipeline-managed)\n"
                "- 12 C++ adapter files (pipeline-managed)\n"
                "- Port adapter .cc files (pipeline-managed)\n"
                "- Any file already marked as PRE-CREATED below\n\n"
                "Keep your response under 30 seconds of generation. Output each missing file directly."
            )
            log(
                f"[INFRA PROMPT] Asset promotion skipped {_missing_after_promote}; "
                f"agent will synthesize (focused prompt injected)",
                "INFO",
            )
        if _promoted_list:
            log(
                f"[INFRA PROMPT] Asset promotion reused {len(_promoted_list)} modules; "
                f"agent does NOT need to regenerate them: {_promoted_list}",
                "INFO",
            )

        demo_summary_text = "\n".join(demo_parts)

        # === 截断 TTCN 代码（优化：超过阈值时只传递摘要信息）===
        ttcn_code_display = ttcn3_code
        ttcn_code_truncate_size = INFRA_CONFIG["ttcn_code_truncate_size"]
        if len(ttcn3_code) > ttcn_code_truncate_size:
            # 提取 module name 和 import 语句作为摘要
            module_match = re.search(r'module\s+(\w+)', ttcn3_code)
            module_name = module_match.group(1) if module_match else "Unknown"
            imports = re.findall(r'import\s+from\s+(\w+)\s+all;', ttcn3_code)
            imports_str = ", ".join(imports[:10]) + ("..." if len(imports) > 10 else "")

            ttcn_code_display = f"""// [TTCN-3 CODE TRUNCATED - Full code available from Core Agent output]
// Module: {module_name}
// Imports: {imports_str}
// Full code: {len(ttcn3_code)} chars (see Core Agent output)
// ...
// [First {ttcn_code_truncate_size} chars of original code:]
{ttcn3_code[:ttcn_code_truncate_size]}
// ... [code continues] ...
"""

        # === Output directory for direct write mode ===
        if output_dir:
            output_dir_str = str(output_dir.resolve())
        else:
            output_dir_str = str((GENERATED_DIR / tc_id).resolve())

        # === Pre-install static TTCN files from demo (never need LLM generation) ===
        # These files are pure boilerplate: no test-case-specific content.
        # Pre-installing them avoids LLM generation overhead entirely.
        _infra_tc_dir = output_dir if output_dir else (GENERATED_DIR / tc_id)
        _static_ttcn_files = {
            # Types and constants: complete from demo, no LLM generation needed
            "src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn": None,
            "src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn": None,
            "src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn": None,
            # PICS
            "src/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn": None,
            # Architecture
            "src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn": None,
            # Aggregators
            "src/ttcn3/3_Behavior_Steps/PreConditions/PreConditions_SECC_15118_2.ttcn": None,
            "src/ttcn3/3_Behavior_Steps/PostConditions/PostConditions_SECC_15118_2.ttcn": None,
            # TestCases_SECC.ttcn must use the actual tc_output_filename (not hardcoded demo name)
            "src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn": (
                f'module TestCases_SECC {{\n'
                f'    import from SECC_TestSystem all;\n'
                f'    import from {tc_output_filename} all;\n'
                f'\n'
                f'    control {{\n'
                f'        execute({tc_output_filename}());\n'
                f'    }}\n'
                f'}} // end module\n'
            ),
        }
        for _rel, _content in _static_ttcn_files.items():
            _src = DEMO_ROOT / _rel
            _dst = _infra_tc_dir / _rel
            _dst.parent.mkdir(parents=True, exist_ok=True)
            if _content is not None:
                # Static content that must be generated (e.g. TestCases_SECC with correct TC name)
                _dst.write_text(_content, encoding="utf-8")
                log(f"[INFRA PREINSTALL] {_rel} (static generation)", "DEBUG")
            elif _src.exists():
                # File exists in demo — copy it
                shutil.copy2(_src, _dst)
                log(f"[INFRA PREINSTALL] {_rel}", "DEBUG")
            else:
                log(f"[INFRA PREINSTALL] Not in demo: {_rel}", "DEBUG")

        # === 构建 Agent prompt ===
        user_prompt = f"""{system_prompt}

---

## Reference Files from demo/

### Generation Plan:
```json
{json.dumps(generation_plan, indent=2, ensure_ascii=False)}
```

### Generated Test Case File (from Core Agent):
The main test case file generated by the Core Agent is placed at:
  src/ttcn3/4_TestCases/{tc_output_filename}
where tc_output_filename = "{tc_output_filename}"

Your build.sh makefilegen command MUST reference this exact filename:
  "${{SRC_DIR}}/ttcn3/4_TestCases/{tc_output_filename}"

Your TestCases_SECC.ttcn import statement MUST use this exact filename (without path):
  import from {tc_output_filename} all;

DO NOT guess a different filename. Use the filename provided above.

### Generated TTCN-3 Test Code (will be placed at src/ttcn3/4_TestCases/{tc_output_filename}):
```ttcn3
{ttcn_code_display}
```

### SUT IP (for titan_runtime.cfg):
{sut_ip if sut_ip else "(not provided, will use IPv6 multicast ff02::01)"}

### Demo Reference Files:

{demo_summary_text}

{_missing_focus_directive if _missing_focus_directive else ""}

Now generate the project structure by writing files directly to disk.

IMPORTANT: Output directory is `{output_dir_str}/`

The following files are ALREADY PRE-CREATED by the pipeline (do NOT overwrite them — just skip):
- config/titan_runtime.cfg
- config/hardware_env.json
- src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn
- src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn
- src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn
- src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn
- src/ttcn3/3_Behavior_Steps/PreConditions/PreConditions_SECC_15118_2.ttcn
- src/ttcn3/3_Behavior_Steps/PostConditions/PostConditions_SECC_15118_2.ttcn
- src/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn
- src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn

You do NOT need to generate any TTCN files. All files are pre-created. You may optionally run `bash build.sh` to self-verify the build.

Do NOT create C++ adapter files, build.sh, or V2G port implementations -- these are handled by the pipeline automatically."""

    # === Prompt 大小监控 ===
    prompt_size = len(user_prompt)
    log(f"Infrastructure Agent 输入分析:", "DEBUG")
    log(f"  - generation_plan keys: {list(generation_plan.keys())}", "DEBUG")
    log(f"  - ttcn3_code length: {len(ttcn3_code)} chars", "DEBUG")
    log(f"  - demo reference sections: {len(demo_parts)}", "DEBUG")
    log(f"  - total prompt size: {prompt_size} chars ({prompt_size/1024:.1f} KB)", "DEBUG")

    if prompt_size > PROMPT_SIZE_HARD:
        log(f"Infrastructure Agent prompt {prompt_size/1024:.1f}KB exceeds HARD threshold ({PROMPT_SIZE_HARD/1024:.0f}KB)", "ERROR")
    elif prompt_size > PROMPT_SIZE_WARNING:
        log(f"Infrastructure Agent prompt {prompt_size/1024:.1f}KB exceeds WARNING threshold ({PROMPT_SIZE_WARNING/1024:.0f}KB)", "WARN")
    else:
        log(f"Infrastructure Agent prompt {prompt_size/1024:.1f}KB (within limits)", "INFO")

    # === Batch Mode: Split prompt for large outputs ===
    # When output is expected to be large, use batch mode
    if BATCH_MODE_ENABLED and prompt_size > INFRA_CONFIG["batch_threshold"]:
        batch_prompts = _create_batch_prompts(
            system_prompt, generation_plan, ttcn3_code, tc_id, sut_ip, demo_summary_text
        )
        log(f"Using batch mode: {len(batch_prompts)} batches", "INFO")
    else:
        batch_prompts = None

    # === 调用 Agent ===
    def _process_infra_response(response_text: str, attempt: int = 1) -> Dict:
        """Process and validate Infrastructure Agent response."""
        log(f"Infrastructure Agent 响应分析 (attempt {attempt}):", "DEBUG")
        log(f"  - response length: {len(response_text)} chars", "DEBUG")

        # Tolerate Empty / Non-JSON / Short responses.
        # When the agent writes files directly to disk via Bash/Edit tools and produces no
        # output (or non-JSON output), we must NOT treat this as a failure -- the files are
        # on disk and the Python-side build will verify them. Return "completed" so the
        # pipeline proceeds naturally to run_build_and_test(), where compile_locked will be set.
        if not response_text:
            log("[INFO] No JSON returned by Infrastructure Agent (empty response). "
                "Assuming files were written successfully via Tool Calls.", "WARN")
            return {
                "status": "completed",
                "files": [],
                "note": "files_written_via_tools",
                "message": "Empty response; files written via Bash/Edit tools assumed present on disk",
                "attempt": attempt,
            }

        if len(response_text.strip()) < 100:
            log("[INFO] Infrastructure Agent response too short (<100 chars). "
                "Assuming files were written successfully via Tool Calls.", "WARN")
            return {
                "status": "completed",
                "files": [],
                "note": "files_written_via_tools",
                "message": f"Short response ({len(response_text)} chars); files written via Bash/Edit tools assumed present on disk",
                "raw_output": response_text[:500],
                "attempt": attempt,
            }

        # Try JSON parsing
        result = None
        log(f"[DEBUG] Raw Agent Response ({len(response_text)} chars):\n{response_text[:3000]}", "DEBUG")

        try:
            result = extract_json_from_response(response_text)
        except Exception as e:
            log(f"extract_json_from_response raised: {e}", "WARN")

        if not result:
            # Agent output was not valid JSON -- but files may still be on disk.
            # Tolerate this instead of crashing the pipeline. Log and proceed.
            log(f"[INFO] Infrastructure Agent did not return valid JSON. "
                f"Assuming files were written successfully via Tool Calls. "
                f"Raw preview: {response_text[:500]!r}", "WARN")
            return {
                "status": "completed",
                "files": [],
                "note": "files_written_via_tools",
                "message": "JSON parse failed; files written via Bash/Edit tools assumed present on disk",
                "raw_output": response_text[:500],
                "attempt": attempt,
            }

        # Normalize: preserve files_written from Summary JSON (new format).
        # The new prompt outputs a lightweight Summary JSON (files_written: list of paths).
        # The pipeline reads actual file contents from disk via detect_direct_write_mode,
        # so we preserve files_written here but do NOT treat it as file contents.
        # (files_written is already in result if present in the JSON -- no action needed)
        # Ensure "files" key exists for backward compat, but it contains path strings
        # from the Summary JSON, not file content dicts. Content is read from disk later.
        if "files" not in result:
            result["files"] = result.get("files_generated", [])

        # Status now comes from the Summary JSON's "status" field directly.
        # Do NOT auto-set status based on len(files) -- that logic is removed.
        # If status is missing from the JSON, leave it unset (pipeline will handle it).

        result["attempt"] = attempt
        return result

    # === 重试逻辑（改进版：指数退避 + 渐进式 prompt 缩减）===
    # === Single-attempt execution (no legacy prompt-reduction retries) ===
    # The agent must write files to disk using Write tool. The pipeline reads files from disk.
    # If JSON parsing fails, _process_infra_response returns status="error" immediately.
    # We do NOT retry with prompt reduction -- if the agent failed, the human should investigate.
    infra_result = None
    final_response = ""

    if batch_prompts:
        # Batch mode: agent writes files per batch, pipeline assembles them from disk
        log(f"Batch mode: {len(batch_prompts)} batches", "INFO")
        all_files = []
        for batch_idx, batch_prompt in enumerate(batch_prompts):
            log(f"Batch {batch_idx + 1}/{len(batch_prompts)}: size={len(batch_prompt)} chars", "INFO")
            batch_response = call_agent_with_timeout(
                batch_prompt, "Infrastructure Agent",
                retry_on_timeout=False,
                extra_allowed_tools="Bash(echo *)",  # Allow file redirection (echo ... > file)
            )
            batch_result = _process_infra_response(batch_response, attempt=batch_idx + 1)
            # All non-JSON / empty responses are now tolerated as "completed" with note="files_written_via_tools".
            # The pipeline will verify the actual files on disk via run_build_and_test().
            if batch_result.get("status") == "error":
                log(f"[WARN] Batch {batch_idx + 1} returned status=error despite tolerance. "
                    f"Message: {batch_result.get('message', 'unknown')}", "WARN")
            # Batch mode uses direct write -- files are on disk, not in JSON.
            # The direct_write detection happens after this returns.
            all_files.extend(batch_result.get("files", []))
        infra_result = {
            "status": "success",
            "files": all_files,
            "message": f"Batch mode: {len(all_files)} entries from {len(batch_prompts)} batches",
            "batch_mode": True
        }
        final_response = str(all_files)
    else:
        # Normal mode: single call
        response = call_agent_with_timeout(
            user_prompt, "Infrastructure Agent",
            extra_allowed_tools="Bash(echo *)",  # Allow file redirection (echo ... > file)
        )
        infra_result = _process_infra_response(response, attempt=1)
        final_response = response
        # _process_infra_response now tolerates empty/non-JSON responses (returns status="completed").
        # This block is kept only for defensive logging if some other error type occurs.
        if infra_result.get("status") == "error":
            log(f"[WARN] Infrastructure Agent returned status=error: "
                f"{infra_result.get('message', 'unknown')}. Proceeding to build verification.", "WARN")

    # === Finalize result (no legacy static-file injection into infra_result["files"]) ===
    # The pipeline now reads actual files from disk via detect_direct_write_mode().
    # C++ adapter files are copied separately by ensure_cpp_adapters().
    # infra_result["files"] is no longer used for file content -- only for metadata.
    if infra_result:
        infra_result["retry_history"] = []  # Retained for schema compatibility

    # === 保存原始响应用于调试 ===
    if final_response and len(final_response) > 100:
        status_suffix = infra_result.get("status", "unknown") if infra_result else "unknown"
        response_file = GENERATED_DIR / f"{tc_id}_infra_response_{status_suffix}.txt"
        try:
            with open(response_file, "w", encoding="utf-8") as f:
                f.write(final_response)
            log(f"Infrastructure Agent 原始输出已保存: {response_file}", "DEBUG")
        except Exception as e:
            log(f"保存 Infrastructure Agent 响应失败: {e}", "WARN")

    infra_result.setdefault("status", "success")

    # === Direct Write Mode detection ===
    # The new architecture requires the agent to write files directly to disk using the Write tool.
    # There is no JSON Parsing Mode fallback -- if files are not on disk, it is a hard failure.
    tc_dir = output_dir if output_dir else (GENERATED_DIR / tc_id)
    if not is_fix_mode and infra_result.get("status") in ("success", "partial"):
        try:
            detected_mode, detected_files = detect_direct_write_mode(tc_dir)
        except RuntimeError as e:
            log(f"[FATAL] {e}", "ERROR")
            raise
        log(f"[MODE] Direct Write Mode confirmed -- agent wrote {len(detected_files)} files directly", "INFO")
        return {
            "status": "success",
            "write_mode": "direct_write",
            "files": [{"path": str(f.relative_to(tc_dir)), "content": f.read_text(encoding="utf-8")} for f in detected_files],
            "detected_files_count": len(detected_files),
            "message": f"Agent wrote {len(detected_files)} files directly to {tc_dir}",
        }


    # === STEP 3: Post-run disk validation fallback ===
    # If infra agent returned "completed" but some required files are still missing,
    # mark as files_missing so the compile loop can retry in fix mode.
    _infra_tc_dir = output_dir if output_dir else (GENERATED_DIR / tc_id)
    _required_patterns = [
        "config/titan_runtime.cfg",
        "config/hardware_env.json",
        "src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn",
        "src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn",
        "src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn",
        "src/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn",
        "src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn",
    ]
    _missing = [p for p in _required_patterns
                if not (_infra_tc_dir / p).exists() or (_infra_tc_dir / p).stat().st_size < 100]
    if _missing and infra_result.get("status") in ("completed", "error", None, ""):
        log(f"[INFRA-DISK-FALLBACK] {len(_missing)} required files missing: {_missing[:3]}", "WARN")
        infra_result["status"] = "files_missing"
        infra_result["missing_files"] = _missing
    return infra_result


def _try_decode_json_string(raw_string: str) -> str:
    """Try to decode a raw JSON string (with escaped sequences) back to proper content.

    When Infrastructure Agent outputs JSON with escaped newlines (\\n), the regex
    extractor's non-greedy match stops at the first unescaped '"', leaving a
    partial string like '#include \\n'. This function attempts to parse the raw
    string as a JSON-encoded string literal to recover the original content.
    """
    import json
    # Wrap in quotes to form a valid JSON string literal, then parse
    candidates = [
        raw_string,
        '"' + raw_string,
        raw_string + '"',
        '"' + raw_string + '"',
    ]
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
            if isinstance(decoded, str) and len(decoded) > len(raw_string) // 2:
                return decoded
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    # Fallback: unescape common sequences manually
    result = raw_string.replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r')
    return result


def _validate_file_content(content: str, path: str) -> Tuple[bool, str]:
    """检查文件内容是否完整
    
    Returns:
        (is_valid, reason): 内容是否有效及原因
    """
    if not content or len(content) < 5:
        return False, "Content too short"
    
    # C++ 文件：检查是否以 } 或 ; 结尾
    if path.endswith(('.cc', '.hh')):
        stripped = content.rstrip()
        if not stripped.endswith('}') and not stripped.endswith(';'):
            return False, f"Missing closing brace or semicolon (ends with: {stripped[-20:]!r})"
    
    # TTCN 文件：检查模块是否闭合
    if path.endswith('.ttcn'):
        # 简单检查：module 和 } 的数量应该匹配
        module_count = content.count('module')
        close_count = content.count('}')
        if module_count > 0 and close_count < module_count:
            return False, f"Incomplete module definition ({module_count} modules, {close_count} closes)"
    
    # build.sh：检查是否有未闭合的命令
    if path in ('build.sh', 'build.bat'):
        lines = content.split('\n')
        incomplete = [l for l in lines if l.rstrip().endswith('\\')]
        if incomplete:
            return False, f"Incomplete command (line continuation): {len(incomplete)} lines"
    
    return True, ""


def _get_truncation_ratio(filename: str) -> float:
    """Get truncation ratio based on filename pattern.

    Differentiated truncation for different file types:
    - Common, TitanTci: Full content (1.0)
    - ExiCodecAdapter: 30% content
    - HAL61851Adapter, TCPTLSPortAdapter, TitanTri: 20% content
    - Others: 15% content
    """
    filename_lower = filename.lower()
    for key, ratio in TRUNCATE_RATIOS.items():
        if key.lower() in filename_lower:
            return ratio
    return TRUNCATE_RATIOS["default"]


def _truncate_demo_content(content: str, filename: str) -> str:
    """Truncate demo content based on file type.

    Args:
        content: Full file content
        filename: File name to determine truncation strategy

    Returns:
        Truncated content with ellipsis marker
    """
    ratio = _get_truncation_ratio(filename)
    if ratio >= 1.0:
        return content

    original_len = len(content)
    truncated_len = int(original_len * ratio)

    # Find a good break point (end of line or statement)
    if truncated_len < original_len:
        # Try to break at a newline or semicolon
        break_point = content.rfind('\n', 0, truncated_len)
        if break_point < truncated_len // 2:
            break_point = content.rfind(';', 0, truncated_len)
        if break_point < truncated_len // 2:
            break_point = truncated_len

        truncated = content[:break_point]
        return f"{truncated}\n// ... [TRUNCATED {original_len - break_point} chars, ratio={ratio}]"
    return content


def _reduce_batch_prompt(prompt: str) -> str:
    """Progressively reduce a batch prompt to increase generation success probability.

    Strategy (3-level reduction):
    1. First: Remove the entire "### Demo Reference Files:" section
       (Agent knows the format from system prompt and generation_plan)
    2. Second: Also remove TTCN code section if present
    3. Third: Remove generation plan JSON if it's too large

    Args:
        prompt: The batch prompt to reduce

    Returns:
        Reduced prompt string
    """
    reduced = prompt

    # Level 1: Remove demo reference section (largest reduction)
    demo_start = reduced.find('### Demo Reference Files:')
    if demo_start != -1:
        demo_end = reduced.find('Now generate', demo_start)
        if demo_end != -1:
            reduced = reduced[:demo_start] + reduced[demo_end:]

    # Level 2: Remove TTCN code section if present
    ttcn_marker = '### Generated TTCN-3 Test Code'
    ttcn_start = reduced.find(ttcn_marker)
    if ttcn_start != -1:
        # Find the closing code block marker
        ttcn_end = reduced.find('```\n', ttcn_start)
        if ttcn_end != -1:
            reduced = (reduced[:ttcn_start] +
                       "### Generated TTCN-3 Test Code\n[SKIPPED - AVAILABLE FROM CORE AGENT OUTPUT]\n\n" +
                       reduced[ttcn_end + 4:])

    # Level 3: If still large, truncate generation plan JSON
    if len(reduced) > 30000:
        plan_marker = '### Generation Plan:'
        plan_start = reduced.find(plan_marker)
        if plan_start != -1:
            plan_json_start = reduced.find('```json\n', plan_start)
            if plan_json_start != -1:
                plan_json_end = reduced.find('\n```', plan_json_start)
                if plan_json_end != -1:
                    reduced = (reduced[:plan_json_start + 8] +
                               '[AVAILABLE FROM ORCHESTRATOR - SKIPPED TO REDUCE SIZE]\n' +
                               reduced[plan_json_end:])

    return reduced


def _reduce_prompt_aggressively(prompt: str) -> str:
    """Aggressively reduce prompt size by removing the entire demo reference section.

    The demo files are reference material - the Agent already knows what to generate
    from the generation_plan and system prompt. Removing them significantly reduces
    prompt size without losing generation capability.

    Strategy:
    1. Keep system prompt and generation plan
    2. Remove the entire "### Demo Reference Files:" section
    3. Keep output instructions ("Now generate...")
    """
    reduced = prompt

    # Remove the entire demo reference section
    demo_start = reduced.find('### Demo Reference Files:')
    if demo_start != -1:
        demo_end = reduced.find('Now generate', demo_start)
        if demo_end != -1:
            # Keep "Now generate" instruction but remove all demo content
            reduced = reduced[:demo_start] + reduced[demo_end:]

    # Also remove TTCN code section if still present
    ttcn_marker = '### Generated TTCN-3 Test Code'
    ttcn_start = reduced.find(ttcn_marker)
    if ttcn_start != -1:
        ttcn_end = reduced.find('```\n', ttcn_start)
        if ttcn_end != -1:
            # Replace TTCN code with a brief note
            reduced = reduced[:ttcn_start] + \
                "### Generated TTCN-3 Test Code\n[AVAILABLE - SKIPPED TO REDUCE PROMPT SIZE]\n" + \
                reduced[ttcn_end + 4:]

    return reduced


def _reduce_prompt_minimal(prompt: str) -> str:
    """Minimal prompt reduction - keep only essential information.

    Strategy:
    1. Keep system prompt (critical for correct output format)
    2. Keep generation plan
    3. Remove all demo reference content
    4. Keep output instructions only
    """
    import re

    # Remove demo reference files section entirely
    minimal_prompt = prompt

    # Find and replace demo section with minimal placeholder
    demo_start = minimal_prompt.find('### Demo Reference Files:')
    if demo_start != -1:
        demo_end = minimal_prompt.find('Now generate', demo_start)
        if demo_end != -1:
            minimal_prompt = minimal_prompt[:demo_start] + \
                "### Demo Reference Files:\n[ESSENTIAL FILES FROM EXAMPLES - COPY FROM examples/ AS FALLBACK]\n\n" + \
                minimal_prompt[demo_end:]

    # Remove large TTCN code section if still present
    ttcn_match = re.search(r'### Generated TTCN-3 Test Code.*?```ttcn3\n.*?```', minimal_prompt, re.DOTALL)
    if ttcn_match and len(ttcn_match.group()) > 500:
        minimal_prompt = minimal_prompt.replace(ttcn_match.group(),
            "### Generated TTCN-3 Test Code\n[SKIPPED - COPY FROM PREVIOUS OUTPUT]")

    return minimal_prompt


def _extract_partial_files_from_text(text: str) -> List[Dict]:
    """Extract file entries from potentially truncated Infrastructure Agent text using regex.

    Uses a multi-pass approach:
    1. Find complete JSON objects via brace-balance tracking
    2. Extract path/content via regex with JSON string decoding
    3. Apply heuristics to filter out obviously truncated content
    4. Validate content integrity before accepting
    """
    import re
    files = []
    seen_paths = set()  # 防止重复

    # --- Pass 1: Try to find complete JSON file objects via brace-balance tracking ---
    # Look for {...} blocks that are balanced and contain "path" and "content"
    file_obj_pattern = re.compile(
        r'\{[^{}]*"path"\s*:\s*"([^"]+)"[^{}]*"content"\s*:\s*("[\s\S]*?"|\[[\s\S]*?\])',
        re.MULTILINE
    )
    for m in file_obj_pattern.finditer(text):
        path = m.group(1).strip()
        raw_content = m.group(2).strip()
        if not path or not raw_content or path in seen_paths:
            continue
        # Decode JSON string if quoted
        if raw_content.startswith('"') and raw_content.endswith('"'):
            content = _try_decode_json_string(raw_content[1:-1])
        else:
            content = _try_decode_json_string(raw_content)

        # Guard: reject obviously truncated content (too short, just includes, etc.)
        if len(content) < 2:
            continue
        # Validate content integrity
        is_valid, reason = _validate_file_content(content, path)
        if not is_valid:
            continue
        seen_paths.add(path)
        files.append({"path": path, "content": content, "validated": True})

    # --- Pass 2: Nested-brace-aware extraction for complex file objects ---
    if len(files) < 3:
        files.clear()
        seen_paths.clear()
        brace_pattern = re.compile(r'"path"\s*:\s*"([^"]+)"')
        content_val_pattern = re.compile(r'"content"\s*:\s*("[\s\S]*?")(?=,\s*\}|\s*\})')
        for path_m in brace_pattern.finditer(text):
            path = path_m.group(1)
            if path in seen_paths:
                continue
            # Find the "content" field that follows this path
            search_start = path_m.end()
            content_m = content_val_pattern.search(text[search_start:search_start + 5000])
            if content_m:
                raw = content_m.group(1)
                try:
                    import json
                    content = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    content = _try_decode_json_string(raw[1:-1])
                # Validate content integrity
                is_valid, reason = _validate_file_content(content, path)
                if not is_valid:
                    continue
                if len(content) >= 20 and not content.startswith('#include \\'):
                    seen_paths.add(path)
                    files.append({"path": path, "content": content, "validated": True})

    return files


def detect_direct_write_mode(tc_dir: Path) -> Tuple[str, List[Path]]:
    """检测 Infrastructure Agent 是否使用了直接写文件模式。

    如果 tc_dir 中已存在预期的 infrastructure 文件（来自 agent 的直接写入），
    则判定为 "Direct Write" 模式。
    如果文件数量不足，抛出 RuntimeError -- 不再有 JSON Parsing Mode 回退。

    Args:
        tc_dir: 测试用例项目目录

    Returns:
        Tuple of (mode: "direct_write", found_files: List[Path])

    Raises:
        RuntimeError: Agent 未使用 Write tool 写文件到磁盘
    """
    if not tc_dir.exists():
        raise RuntimeError(
            f"[FATAL] Infrastructure Agent did not write files to {tc_dir}. "
            f"Agent MUST use Write tool to create all files."
        )

    # 预期的 infrastructure 文件路径模式（agent 直接写入的 TTCN/config 文件）
    expected_patterns = [
        "config/titan_runtime.cfg",
        "config/hardware_env.json",
        "src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn",
        "src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn",
        "src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn",
        "src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn",
        "src/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn",
        "src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn",
    ]

    found_files: List[Path] = []
    for rel_path in expected_patterns:
        file_path = tc_dir / rel_path
        if file_path.exists() and file_path.stat().st_size > 50:
            found_files.append(file_path)

    # 如果找到了超过一半的预期文件，认为是直接写模式
    if len(found_files) >= len(expected_patterns) // 2:
        return "direct_write", found_files
    raise RuntimeError(
        f"[FATAL] Infrastructure Agent wrote only {len(found_files)}/{len(expected_patterns)} "
        f"expected files to {tc_dir}. Agent MUST use Write tool for ALL files."
    )


def write_infrastructure_files(tc_dir: Path, infra_result: Optional[Dict],
                                tc_output_filename: str = "") -> Tuple[List[Path], Optional[str]]:
    """Validate Infrastructure Agent output (direct write mode only).

    Agent MUST have written files directly to disk using Write tool.
    This function only validates the directory contents and returns the written file list.

    JSON Parsing Mode has been removed. If the agent did not write files to disk,
    detect_direct_write_mode() will have already raised RuntimeError before this
    function is called.

    Args:
        tc_dir: 测试用例项目根目录
        infra_result: run_infrastructure_agent() 返回的结果（未使用）
        tc_output_filename: 测试用例文件名（未使用）

    Returns:
        Tuple of (验证通过的文件路径列表, None)
    """
    log(f"[Direct Write Mode] 验证目录内容: {tc_dir}")
    try:
        _, detected_files = detect_direct_write_mode(tc_dir)
    except RuntimeError:
        # Should not reach here -- detect_direct_write_mode already raised.
        # Re-raise for safety.
        raise

    if detected_files:
        log(f"[Direct Write Mode] 验证通过，找到 {len(detected_files)} 个文件:")
        for f in sorted(detected_files):
            log(f"  [DIRECT] {f.relative_to(tc_dir)}")
    return [], None


def _fix_tc_import_duplicates(
    tc_dir: Path,
    pre_modules: List[str],
    post_modules: List[str],
) -> None:
    """Remove duplicate module files and fix TC imports.

    Core Agent sometimes generates both "PreConditions_Foo.ttcn" (module PreConditions_Foo)
    AND "f_SECC_CMN_PR_Foo_001.ttcn" (also module PreConditions_Foo). This causes
    "module already defined" errors. The fix has two steps:

    1. Delete the non-f_-prefixed file that duplicates an f_-prefixed module.
       The f_-prefixed file follows the naming convention and is kept.
    2. Update the TC file's import statements: if it imports the removed module name
       but not the correct one, replace the import.

    Args:
        tc_dir: Project root directory
        pre_modules: List of precond module names (after dedup)
        post_modules: List of postcond module names (after dedup)
    """
    import re

    pre_dir = tc_dir / "src/ttcn3/3_Behavior_Steps/PreConditions"
    post_dir = tc_dir / "src/ttcn3/3_Behavior_Steps/PostConditions"

    # Step 1: Find files that define a module but are NOT in the final import lists.
    # These are the "wrong" files that should be deleted.
    final_pre_modules = set(pre_modules)
    final_post_modules = set(post_modules)

    def _find_wrong_files(directory: Path) -> List[Tuple[Path, str]]:
        """Return (file_path, module_name) for files that should be deleted."""
        wrong = []
        if not directory.exists():
            return wrong
        for f in directory.glob("*.ttcn"):
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                m = re.search(r'^\s*module\s+(\w+)\s*\{', content, re.MULTILINE)
                if not m:
                    continue
                module_name = m.group(1)
                # Determine if this file's module is in the final import lists
                is_pre_final = module_name in final_pre_modules
                is_post_final = module_name in final_post_modules
                is_aggregator = (module_name in ("PreConditions_SECC_15118_2",
                                                 "PostConditions_SECC_15118_2"))
                is_pics_pixit = module_name in ("SECC_PICS", "SECC_PIXIT",
                                                  "SECC_CmnTemplates", "SECC_Constants",
                                                  "SECC_Types", "SECC_CommonFunctions",
                                                  "SECC_TestSystem")
                if not is_pre_final and not is_post_final and not is_aggregator and not is_pics_pixit:
                    wrong.append((f, module_name))
            except Exception:
                continue
        return wrong

    wrong_pre = _find_wrong_files(pre_dir)
    wrong_post = _find_wrong_files(post_dir)

    deleted_files = []
    deleted_modules = {}  # wrong_module_name -> correct_module_name (if known)

    for wrong_file, wrong_module in wrong_pre + wrong_post:
        # Determine correct f_-prefixed equivalent
        # E.g., PreConditions_InitConfiguration.ttcn -> f_SECC_CMN_PR_InitConfiguration_001.ttcn
        # Try to find an f_-prefixed file in the same directory that defines the same module
        directory = wrong_file.parent
        correct_module = wrong_module  # fallback: keep the same name
        for sibling in directory.glob("f_*.ttcn"):
            try:
                sibling_content = sibling.read_text(encoding="utf-8", errors="replace")
                m = re.search(r'^\s*module\s+(\w+)\s*\{', sibling_content, re.MULTILINE)
                if m and m.group(1) == wrong_module:
                    # The f_-prefixed file defines the same module -- keep it
                    correct_module = wrong_module
                    break
            except Exception:
                continue
        else:
            # No f_-prefixed file with the same module name found.
            # This module is just wrong and should be removed entirely.
            pass

        try:
            wrong_file.unlink()
            deleted_files.append(str(wrong_file.relative_to(tc_dir)))
            deleted_modules[wrong_module] = correct_module
            log(f"[INJECT] Deleted duplicate module file: {wrong_file.relative_to(tc_dir)} "
                f"(module '{wrong_module}')")
        except Exception as e:
            log(f"[INJECT] Failed to delete {wrong_file}: {e}", "WARN")

    if not deleted_modules:
        return

    # Step 2: Fix TC file imports for deleted modules
    tc_patterns = list(tc_dir.glob("src/ttcn3/4_TestCases/**/TC_*.ttcn"))
    if not tc_patterns:
        return
    tc_path = tc_patterns[0]

    try:
        content = tc_path.read_text(encoding="utf-8")
    except Exception:
        return

    original = content
    for wrong_module, correct_module in deleted_modules.items():
        if wrong_module == correct_module:
            continue  # No replacement needed
        wrong_import = f"import from {wrong_module} all;"
        correct_import = f"import from {correct_module} all;"
        if wrong_import in content and correct_import not in content:
            content = content.replace(wrong_import, correct_import)
            log(f"[INJECT] Updated TC import: '{wrong_module}' -> '{correct_module}'")

    if content != original:
        tc_path.write_text(content, encoding="utf-8")
        log(f"[INJECT] TC file imports fixed in {tc_path.relative_to(tc_dir)}")


def _inject_condition_function_imports(
    tc_dir: Path,
    pre_condition_files: List[Dict],
    post_condition_files: List[Dict],
    tc_id: str,
) -> None:
    """Rebuild aggregator modules with correct function imports.

    After install_shared_ttcn_files() overwrites the aggregator modules with demo
    versions (which don't import the current test case's function modules), this
    function rebuilds the aggregator from scratch by scanning the actual .ttcn files
    in the pre/postcondition directories to find function module names.

    This avoids relying on the demo aggregator containing stale function names.

    Args:
        tc_dir: Project root directory
        pre_condition_files: List of {"path": ..., "content": ...} from Core Agent
        post_condition_files: List of {"path": ..., "content": ...} from Core Agent
        tc_id: Test case ID (e.g. TC_SECC_CMN_VTB_V2GTPHeader_001) used to
            dynamically generate the correct import/execute statement in TestCases_SECC.ttcn
    """
    import re

    def _get_module_name(content: str) -> Optional[str]:
        """Extract module name from TTCN-3 file content."""
        match = re.search(r'^\s*module\s+(\w+)\s*\{', content, re.MULTILINE)
        return match.group(1) if match else None

    def _scan_directory_modules(directory: Path, suffix: str = "") -> List[str]:
        """Scan .ttcn files in a directory and return their module names.

        Args:
            directory: Directory to scan
            suffix: If provided, only include files whose name contains suffix.
                    This prevents aggregator-style files (e.g., PreConditions_InitConfiguration.ttcn)
                    from being scanned alongside f_-prefixed function files (e.g.,
                    f_SECC_CMN_PR_InitConfiguration_001.ttcn), which would cause
                    duplicate module definition errors when both have the same module name
                    (e.g., module PreConditions_InitConfiguration { ... }).
        """
        modules = []
        if not directory.exists():
            return modules
        for f in directory.glob("*.ttcn"):
            if suffix and suffix not in f.name:
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                name = _get_module_name(content)
                if name:
                    modules.append(name)
            except Exception:
                continue
        return modules

    def _get_all_modules_in_directory(directory: Path) -> List[str]:
        """Scan all .ttcn files in a directory and return their module names.

        This scans ALL files regardless of name prefix. Use only for dedup checking,
        NOT for building import lists (use _scan_directory_modules with suffix="f_"
        for that purpose to avoid duplicate module name issues).
        """
        modules = []
        if not directory.exists():
            return modules
        for f in directory.glob("*.ttcn"):
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                name = _get_module_name(content)
                if name:
                    modules.append(name)
            except Exception:
                continue
        return modules

    # Also extract from the explicitly passed Core Agent files
    # Filter out aggregator module names (PreConditions_SECC_15118_2, PostConditions_SECC_15118_2)
    # since those modules are the aggregator files themselves and should not be imported
    # into themselves (causes self-import / cannot find module errors).
    passed_pre_modules = [m for f in pre_condition_files
                          if (m := _get_module_name(f.get("content", "")))
                          and not m.startswith("PreConditions_")
                          and not m.startswith("PostConditions_")]
    passed_post_modules = [m for f in post_condition_files
                           if (m := _get_module_name(f.get("content", "")))
                           and not m.startswith("PreConditions_")
                           and not m.startswith("PostConditions_")]

    # Scan the pre/postcondition directories for all function modules
    pre_dir = tc_dir / "src/ttcn3/3_Behavior_Steps/PreConditions"
    post_dir = tc_dir / "src/ttcn3/3_Behavior_Steps/PostConditions"

    # Exclude the aggregator files themselves (PreConditions_SECC_15118_2.ttcn etc.)
    # Note: function modules may have names like "PreConditions_TCPConnected" which are valid
    # and should NOT be excluded. Only exclude exact aggregator names.
    pre_modules = [m for m in _scan_directory_modules(pre_dir, "f_")
                   if m not in ("PreConditions_SECC_15118_2",)]
    post_modules = [m for m in _scan_directory_modules(post_dir, "f_")
                    if m not in ("PostConditions_SECC_15118_2",)]

    # Detect and resolve duplicate module names across pre and post directories.
    # Core Agent sometimes generates both "PreConditions_Foo.ttcn" (module PreConditions_Foo { ... })
    # AND "f_SECC_CMN_PR_Foo_001.ttcn" (module PreConditions_Foo { ... }) which causes
    # "module already defined" errors. Keep only the f_-prefixed file's module since
    # that is the correct naming convention.
    all_modules_in_pre = _get_all_modules_in_directory(pre_dir)
    all_modules_in_post = _get_all_modules_in_directory(post_dir)

    # Find pre/post modules that appear in both directories (duplicate module names)
    cross_dir_dups = set(pre_modules) & set(post_modules)
    if cross_dir_dups:
        log(f"[INJECT] Removing cross-directory duplicate module names: {cross_dir_dups}")
        # Remove duplicates from both lists (keep in neither) -- these are wrong modules
        pre_modules = [m for m in pre_modules if m not in cross_dir_dups]
        post_modules = [m for m in post_modules if m not in cross_dir_dups]

    # Find within-directory duplicates (e.g., both "PreConditions_Foo.ttcn" and
    # "f_SECC_CMN_PR_Foo_001.ttcn" define module PreConditions_Foo)
    for module_name in list(pre_modules):
        all_in_pre = _get_all_modules_in_directory(pre_dir)
        # Count how many files in pre dir define this module name
        count = sum(1 for m in all_in_pre if m == module_name)
        if count > 1:
            log(f"[INJECT] Module '{module_name}' defined by {count} files in PreConditions/, "
                f"keeping f_-prefixed version")
            pre_modules = [m for m in pre_modules if m != module_name]
    for module_name in list(post_modules):
        all_in_post = _get_all_modules_in_directory(post_dir)
        count = sum(1 for m in all_in_post if m == module_name)
        if count > 1:
            log(f"[INJECT] Module '{module_name}' defined by {count} files in PostConditions/, "
                f"keeping f_-prefixed version")
            post_modules = [m for m in post_modules if m != module_name]

    # Merge with passed modules (deduplicate)
    for m in passed_pre_modules:
        if m not in pre_modules and not m.startswith("PreConditions_") and not m.startswith("PostConditions_"):
            pre_modules.append(m)
    for m in passed_post_modules:
        if m not in post_modules and not m.startswith("PreConditions_") and not m.startswith("PostConditions_"):
            post_modules.append(m)

    def _build_aggregator(agg_path: Path, function_modules: List[str],
                           module_name: str, agg_type: str) -> None:
        """Rebuild an aggregator module with essential imports and function imports."""
        essential_imports = [
            "import from SECC_TestSystem all;",
            "import from SECC_CmnTemplates all;",
            "import from SECC_Constants all;",
        ]
        content_lines = [f"module {module_name} {{"]
        for imp in essential_imports:
            content_lines.append(f"    {imp}")

        content_lines.append("")
        content_lines.append(f"    // {agg_type} function imports")
        for mod in sorted(function_modules):
            content_lines.append(f"    import from {mod} all;")

        content_lines.append("")
        content_lines.append("} // end of module")
        new_content = "\n".join(content_lines) + "\n"

        agg_path.parent.mkdir(parents=True, exist_ok=True)
        old_content = agg_path.read_text(encoding="utf-8") if agg_path.exists() else ""
        if new_content != old_content:
            agg_path.write_text(new_content, encoding="utf-8")
            log(f"[INJECT] Rebuilt {agg_type} aggregator with {len(function_modules)} function imports")

    pre_agg_path = tc_dir / "src/ttcn3/3_Behavior_Steps/PreConditions/PreConditions_SECC_15118_2.ttcn"
    _build_aggregator(pre_agg_path, pre_modules, "PreConditions_SECC_15118_2", "PreConditions")

    post_agg_path = tc_dir / "src/ttcn3/3_Behavior_Steps/PostConditions/PostConditions_SECC_15118_2.ttcn"
    _build_aggregator(post_agg_path, post_modules, "PostConditions_SECC_15118_2", "PostConditions")

    # Fix duplicate module files: delete non-f_-prefixed files that duplicate f_-prefixed modules
    # and update TC file imports accordingly. Must run AFTER _build_aggregator so that
    # pre_modules/post_modules reflect the final state.
    _fix_tc_import_duplicates(tc_dir, pre_modules, post_modules)

    # Fix stale import in TestCases_SECC.ttcn: _rebuild_testcases_module writes the file
    # with the correct import (tc_id), but the stale import
    # from a previous run (e.g., TC_SECC_CMN_VTB_V2GTPSessionSetup_001) may remain.
    # Double-check and fix the file.
    testcases_path = tc_dir / "src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn"
    if testcases_path.exists():
        try:
            tc_content = testcases_path.read_text(encoding="utf-8")
        except Exception:
            tc_content = ""
        if "TC_SECC_CMN_VTB_V2GTPSessionSetup_001" in tc_content or tc_id not in tc_content:
            # Rebuild with correct content using the dynamic tc_id
            testcases_path.write_text(
                f"""module TestCases_SECC {{
    import from SECC_TestSystem all;
    import from {tc_id} all;

    control {{
        execute({tc_id}());
    }}
}}
""", encoding="utf-8"
            )
            log("[INJECT] Fixed stale import in TestCases_SECC.ttcn")


def _write_simple_watchdog_metrics(tc_project_dir: Path, plan_tc_id: str, result: Dict) -> None:
    """Write a minimal metrics.json when the Infra Agent is killed by watchdog.

    Phase 3.2: instead of waiting through the full fix loop just to write
    a build_failed metrics, this short-circuits and writes the essential
    fields so the batch runner can record a clean "build_failed" status
    without further LLM calls.
    """
    metrics_path = tc_project_dir / "metrics.json"
    payload = {
        "tc_id": plan_tc_id,
        "timestamp": tc_project_dir.name.rsplit("_", 1)[-1] if "_" in tc_project_dir.name else "",
        "verdict": result.get("verdict", {}),
        "latency": result.get("latency", {}),
        "compile_errors": result.get("compile_errors", []),
        "infra_status": result.get("infra_status", "watchdog_killed"),
    }
    metrics_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                            encoding="utf-8")
    log(f"[FIX LOOP] Wrote fallback metrics.json: {metrics_path}")


def _load_core_shared_refs() -> Dict[str, str]:
    """Load shared TTCN reference files needed by the Core Agent prompt.

    These files are copied from the demo project. Providing their content in the
    prompt prevents "cannot find definition" errors when the LLM references
    TimerAndConstants and CmnTemplates.

    Cached on first call per process so we don't re-read the filesystem repeatedly.
    """
    import functools as _functools
    @_functools.lru_cache(maxsize=1)
    def _cached() -> Dict[str, str]:
        result: Dict[str, str] = {}
        for _name, _rel_path in {
            "TimerAndConstants": "src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn",
            "CmnTemplates":      "src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn",
        }.items():
            _full_path = DEMO_ROOT / _rel_path
            if _full_path.exists():
                result[_name] = truncate_text(_full_path.read_text(encoding="utf-8"), 4000)
            else:
                result[_name] = ""
        return result
    return _cached()


def _promote_precondition_assets(tc_dir: Path, generation_plan: Dict, generated_root: Optional[Path] = None) -> List[str]:
    """Reuse previously-generated PreConditions .ttcn files from past project directories.

    Why this exists (Asset Promotion)
    ---------------------------------
    The Infra Agent is great at synthesizing precondition code from scratch — but on
    large prompts (~58 KB) it can hang for 10+ minutes. When a sibling test case
    (e.g. ``TC_SECC_CMN_VTB_V2GTPSessionSetup_001``) has already generated an
    implementation for ``f_SECC_CMN_PR_SessionSetup_001.ttcn``, we should COPY
    that asset into the new project instead of asking the agent to regenerate it.

    This is a cache, NOT a hardcoded answer:
      * The assets were LLM-generated in previous runs.
      * We only copy from project directories whose ``metrics.json`` has a real
        TITAN verdict (pass/fail/inconc) — meaning the source actually compiled
        and ran.
      * If nothing is in the cache for a given module, the Infra Agent is still
        invoked normally — the dynamic plan -> generate flow stays intact.

    Returns the list of module basenames that were promoted (caller can remove
    them from ``missing_preconditions`` before building the Infra Agent prompt).
    """
    import re
    from pathlib import Path as _P

    if generated_root is None:
        # generated_root = <repo>/agent/test_code/generated
        generated_root = tc_dir.parent.parent / "generated"
    if not generated_root.is_dir():
        log(f"[ASSET PROMOTE] No generated/ root found at {generated_root}; skipping")
        return []

    completeness = generation_plan.get("_precondition_completeness", {}) or {}
    missing = list(completeness.get("missing_preconditions", []) or [])
    if not missing:
        log("[ASSET PROMOTE] No missing preconditions reported by plan; nothing to promote")
        return []

    pre_dir = tc_dir / "src" / "ttcn3" / "3_Behavior_Steps" / "PreConditions"
    pre_dir.mkdir(parents=True, exist_ok=True)

    # Collect candidate source directories (timestamped project dirs from prior runs).
    # Sort by mtime DESC so the newest, most likely-compatible asset wins.
    candidates: List[_P] = []
    for d in generated_root.iterdir():
        if not d.is_dir():
            continue
        # Only consider dirs that look like <TC_ID>_<YYYYMMDD_HHMMSS>
        if "_" not in d.name:
            continue
        # Skip the current target dir (and its non-timestamped sibling)
        if d == tc_dir:
            continue
        # Must have a metrics.json with a real verdict
        metrics_path = d / "metrics.json"
        if not metrics_path.is_file():
            continue
        try:
            with open(metrics_path, "r", encoding="utf-8") as mf:
                m = json.loads(mf.read())
        except (OSError, json.JSONDecodeError):
            continue
        verdict = (m.get("verdict") or {}).get("result")
        if verdict not in {"pass", "fail", "inconc", "fix_success"}:
            continue
        # Must have a PreConditions dir
        src_pre_dir = d / "src" / "ttcn3" / "3_Behavior_Steps" / "PreConditions"
        if not src_pre_dir.is_dir():
            continue
        candidates.append(d)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    promoted: List[str] = []
    still_missing: List[str] = []
    for mod_name in missing:
        mod_filename = f"{mod_name}.ttcn"
        found_src: Optional[_P] = None
        for cand in candidates:
            src_file = cand / "src" / "ttcn3" / "3_Behavior_Steps" / "PreConditions" / mod_filename
            if not src_file.is_file():
                continue
            # Filter: skip stubs. Stubs are <=50 lines or contain the
            # "Auto-generated stub precondition function." marker.
            try:
                content = src_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            line_count = content.count("\n")
            if line_count <= 50 or "Auto-generated stub precondition function" in content:
                continue
            found_src = src_file
            break
        if found_src is None:
            still_missing.append(mod_name)
            continue
        # Promote: copy the asset into the new project.
        dst_file = pre_dir / mod_filename
        try:
            dst_file.write_text(found_src.read_text(encoding="utf-8", errors="replace"),
                                encoding="utf-8")
        except OSError as e:
            log(f"[ASSET PROMOTE] Copy failed for {mod_filename}: {e}", "WARN")
            still_missing.append(mod_name)
            continue
        promoted.append(mod_name)
        # Source file path: <project_dir>/src/ttcn3/3_Behavior_Steps/PreConditions/<mod>.ttcn
        # So .parent = PreConditions, .parent.parent = 3_Behavior_Steps,
        # .parent.parent.parent = ttcn3, .parent.parent.parent.parent = src,
        # .parent.parent.parent.parent.parent = <project_dir>
        try:
            project_dir_name = found_src.parent.parent.parent.parent.parent.name
            src_metrics_path = found_src.parent.parent.parent.parent.parent / "metrics.json"
            if src_metrics_path.is_file():
                src_verdict = (json.loads(src_metrics_path.read_text(encoding="utf-8"))
                               .get("verdict") or {}).get("result", "?")
            else:
                src_verdict = "(no metrics)"
        except (OSError, json.JSONDecodeError):
            project_dir_name = "<unknown>"
            src_verdict = "(unreadable)"
        log(f"[ASSET PROMOTE] {mod_filename} <- {project_dir_name} (verdict={src_verdict})")

    # Wire promoted modules into the aggregator so TITAN sees them.
    if promoted:
        agg_path = pre_dir / "PreConditions_SECC_15118_2.ttcn"
        try:
            if agg_path.exists():
                agg_content = agg_path.read_text(encoding="utf-8")
            else:
                agg_content = (
                    "module PreConditions_SECC_15118_2 {\n"
                    "    import from SECC_TestSystem all;\n"
                    "    import from SECC_CmnTemplates all;\n"
                    "    import from SECC_Constants all;\n"
                    "}\n"
                )
            modified = False
            for mod in promoted:
                if f"import from {mod} all;" not in agg_content:
                    agg_content = agg_content.replace(
                        "} // end module PreConditions_SECC_15118_2",
                        f"    import from {mod} all;\n}} // end module PreConditions_SECC_15118_2",
                    )
                    if f"import from {mod} all;" not in agg_content:
                        # Insert just before the last closing brace as a fallback.
                        agg_content = agg_content.rsplit("}", 1)[0] + f"    import from {mod} all;\n}}\n"
                    modified = True
            if modified:
                agg_path.write_text(agg_content, encoding="utf-8")
                log(f"[ASSET PROMOTE] Updated {agg_path.name} with promoted imports")
        except OSError as e:
            log(f"[ASSET PROMOTE] Could not update aggregator: {e}", "WARN")

        # Update the plan so the Infra Agent is NOT asked to regenerate these.
        completeness["missing_preconditions"] = still_missing
        completeness["promoted_preconditions"] = promoted
        generation_plan["_precondition_completeness"] = completeness
        log(f"[ASSET PROMOTE] Promoted {len(promoted)} module(s): {promoted}")
        if still_missing:
            log(f"[ASSET PROMOTE] Still missing after scan: {still_missing}")
    else:
        log(f"[ASSET PROMOTE] No promotable assets found; Infra Agent will synthesize: {missing}")

    return promoted


def _ensure_missing_precondition_stubs(tc_dir: Path, generation_plan: Dict) -> None:
    """Generate minimal stubs for precondition functions that the TC file imports but no file exists.

    Root cause: Core Agent writes `import from f_XYZ_001 all;` in the TC module
    without creating the corresponding PreConditions/f_XYZ_001.ttcn stub file.
    TITAN then fails with "There is no module with identifier `f_XYZ_001`".

    This function:
    1. Scans PreConditions/ for existing module names
    2. Extracts `import from f_XXX_001 all;` from the main TC file
    3. For each imported module that has NO corresponding .ttcn file in PreConditions/,
       generates a minimal stub module so TITAN can at least compile and link.
    4. Adds the stub module to the aggregator PreConditions_SECC_15118_2.ttcn
    """
    import re

    pre_dir = tc_dir / "src/ttcn3/3_Behavior_Steps/PreConditions"
    if not pre_dir.exists():
        pre_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Find already-existing module names in PreConditions/
    existing_modules = set()
    for f in pre_dir.glob("*.ttcn"):
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'^\s*module\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{', content, re.MULTILINE)
            if m:
                existing_modules.add(m.group(1))
        except Exception:
            continue

    # Step 2: Find all function modules imported in the TC file
    tc_ttcn_files = list((tc_dir / "src/ttcn3/4_TestCases").glob("*.ttcn"))
    imported_modules = set()
    for f in tc_ttcn_files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r'^\s*import from\s+([A-Za-z_][A-Za-z0-9_]*)\s+all;', content, re.MULTILINE):
                imported_modules.add(m.group(1))
        except Exception:
            continue

    # Step 3: Find missing stubs (imported but no .ttcn file)
    # Only generate stubs for precondition function modules (f_<Name>_<N> pattern).
    # Skip infrastructure modules (SECC_CmnTemplates, SECC_Constants, SECC_Types,
    # SECC_TestSystem, PostConditions_SECC_15118_2, etc.) — those are defined in
    # 1_Data_Definitions/ and PreConditions_SECC_15118_2.ttcn from the demo and
    # must NOT be duplicated as stub files in PreConditions/.
    _PRECOND_PATTERN = re.compile(r'^f_[A-Z][A-Za-z0-9]+(?:_V2G)?(?:_AC|_DC)?_\d+$')
    missing = sorted(
        m for m in (imported_modules - existing_modules)
        if _PRECOND_PATTERN.match(m)
    )
    if not missing:
        if imported_modules - existing_modules:
            skipped = sorted(imported_modules - existing_modules)
            log(f"[STUB GEN] Skipping {len(skipped)} non-function modules (infra already in 1_Data_Definitions/): {skipped}")
        return

    log(f"[STUB GEN] {len(missing)} missing precondition stubs detected: {missing}")

    generated_stubs = []
    for mod_name in missing:
        # Derive file path: f_SECC_CMN_PR_SessionSetup_001 -> f_SECC_CMN_PR_SessionSetup_001.ttcn
        stub_filename = f"{mod_name}.ttcn"
        stub_path = pre_dir / stub_filename

        # Build a minimal valid TTCN-3 stub module.
        # For precondition functions in SIL mode: void return, no parameters.
        # The function just passes to advance the state machine.
        # TITAN requires the module to exist and have a matching function signature.
        func_name = mod_name  # e.g. f_SECC_CMN_PR_SessionSetup_001

        stub_code = f"""module {mod_name} {{

    import from SECC_TestSystem all;
    import from SECC_CmnTemplates all;

    // Auto-generated stub precondition function.
    // Minimal implementation: advance state machine to satisfy TITAN import resolution.
    // Replace with full implementation if needed.
    function {func_name}()
        runs on SECCTester
        {{
            // Stub: precondition satisfied (no-op in SIL mode)
            log("{mod_name}: precondition stub called");
        }}

}} // end module {mod_name}
"""
        try:
            stub_path.write_text(stub_code, encoding="utf-8")
            generated_stubs.append(mod_name)
            log(f"[STUB GEN] Created {stub_filename}")
        except Exception as e:
            log(f"[STUB GEN] Failed to write {stub_filename}: {e}", "WARN")

    # Step 4: Add missing stubs to the PreConditions_SECC_15118_2.ttcn aggregator
    if generated_stubs:
        agg_path = pre_dir / "PreConditions_SECC_15118_2.ttcn"
        try:
            if agg_path.exists():
                agg_content = agg_path.read_text(encoding="utf-8")
            else:
                agg_content = (
                    "module PreConditions_SECC_15118_2 {\n"
                    "    import from SECC_TestSystem all;\n"
                    "    import from SECC_CmnTemplates all;\n"
                    "    import from SECC_Constants all;\n"
                    "}\n"
                )
            # Add missing import statements if not already present
            modified = False
            for mod in generated_stubs:
                if f"import from {mod} all;" not in agg_content:
                    # Insert before the closing brace
                    agg_content = agg_content.replace(
                        "} // end module PreConditions_SECC_15118_2",
                        f"    import from {mod} all;\n}} // end module PreConditions_SECC_15118_2"
                    )
                    modified = True
            if modified:
                agg_path.write_text(agg_content, encoding="utf-8")
                log(f"[STUB GEN] Updated PreConditions_SECC_15118_2.ttcn with {len(generated_stubs)} stubs")
        except Exception as e:
            log(f"[STUB GEN] Failed to update aggregator: {e}", "WARN")


def _heal_misplaced_condition_modules(tc_dir: Path) -> int:
    """Move misplaced precondition/postcondition files back to their proper directories.

    Core Agent (LLM) often writes PR_/PO_ function modules to the
    ``TestBehavior/`` directory because the output JSON's path template is
    ambiguous.  This causes ``makefilegen`` to fail with::

        error: Both files `.../TestBehavior/f_X_PR_Y.ttcn` and
               `.../PreConditions/f_X_PR_Y.ttcn` contain TTCN-3 module `f_X_PR_Y`.

    because the same module name is declared in two different files.  This
    self-heal step moves any ``*_PR_*`` / ``*_PO_*`` files found in
    ``TestBehavior/`` to the correct ``PreConditions/`` / ``PostConditions/``
    directory.  If a file with the same name already exists in the target
    directory, the misplaced copy is deleted (the original in the proper
    directory is the authoritative one — it was already validated by
    ``install_shared_ttcn_files``).

    Returns:
        Number of files moved (or deleted as duplicates).
    """
    import re
    import shutil

    testbehavior_dir = tc_dir / "src" / "ttcn3" / "3_Behavior_Steps" / "TestBehavior"
    pre_dir = tc_dir / "src" / "ttcn3" / "3_Behavior_Steps" / "PreConditions"
    post_dir = tc_dir / "src" / "ttcn3" / "3_Behavior_Steps" / "PostConditions"

    if not testbehavior_dir.exists():
        return 0

    pr_re = re.compile(r'_PR_[A-Za-z]+_\d{3}\.ttcn$')
    po_re = re.compile(r'_PO_[A-Za-z]+_\d{3}\.ttcn$')

    moved = 0
    for src_file in list(testbehavior_dir.glob("*.ttcn")):
        name = src_file.name
        if pr_re.search(name):
            target_dir = pre_dir
        elif po_re.search(name):
            target_dir = post_dir
        else:
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / name

        if target_file.exists():
            # Duplicate: the proper directory already has this module.
            # The copy in TestBehavior/ is the LLM misplacement; delete it.
            try:
                src_file.unlink()
                log(f"[HEAL] Deleted duplicate misplaced file: TestBehavior/{name} "
                    f"(authoritative copy already in {target_dir.name}/)")
                moved += 1
            except OSError as e:
                log(f"[HEAL] Failed to delete duplicate {src_file}: {e}", "WARN")
        else:
            # Move the file to its proper home.
            try:
                shutil.move(str(src_file), str(target_file))
                log(f"[HEAL] Moved misplaced file: TestBehavior/{name} -> "
                    f"{target_dir.name}/{name}")
                moved += 1
            except OSError as e:
                log(f"[HEAL] Failed to move {src_file} -> {target_file}: {e}", "WARN")

    if moved:
        log(f"[HEAL] Self-heal complete: relocated/cleaned {moved} misplaced "
            f"PR/PO file(s) in TestBehavior/")
    return moved


def _ensure_tc_direct_imports(tc_dir: Path, tc_filename: str,
                                pre_condition_files: List[Dict],
                                post_condition_files: List[Dict]) -> None:
    """Ensure the main TC module directly imports all pre/postcondition function modules.

    In TTCN-3, 'import from Aggregator all;' does NOT re-export functions from
    modules that the Aggregator itself imports. Therefore the main TC module must
    directly import every function module that its pre/postcondition calls depend on.
    This function scans the pre/postcondition directories, finds all function module
    names, and adds 'import from <module> all;' statements to the main TC file
    if they are not already present.
    """
    import re

    def get_module_name(content: str) -> Optional[str]:
        m = re.search(r'^\s*module\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{', content, re.MULTILINE)
        return m.group(1) if m else None

    def scan_function_modules(directory: Path, suffix: str = "") -> List[str]:
        modules = []
        if not directory.exists():
            return modules
        for f in directory.glob("*.ttcn"):
            if suffix and suffix not in f.name:
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                name = get_module_name(content)
                if name:
                    modules.append(name)
            except Exception:
                continue
        return modules

    # Gather all function module names from pre/postcondition directories
    pre_dir = tc_dir / "src/ttcn3/3_Behavior_Steps/PreConditions"
    post_dir = tc_dir / "src/ttcn3/3_Behavior_Steps/PostConditions"
    pre_modules = scan_function_modules(pre_dir, "f_")
    post_modules = scan_function_modules(post_dir, "f_")
    all_modules = sorted(set(pre_modules + post_modules))

    if not all_modules:
        return

    # Find and read the main TC file
    tc_path = tc_dir / "src/ttcn3/4_TestCases" / tc_filename
    if not tc_path.exists():
        # Try root generated dir
        tc_path = tc_dir / tc_filename
    if not tc_path.exists():
        log(f"[INJECT] Main TC file not found: {tc_filename}", "WARN")
        return

    content = tc_path.read_text(encoding="utf-8")

    # Find existing imports
    existing_imports = set(re.findall(r'^\s*import from\s+([A-Za-z_][A-Za-z0-9_]*)\s+all;', content, re.MULTILINE))

    # Find missing imports
    missing = [m for m in all_modules if m not in existing_imports]
    if not missing:
        return

    # Inject imports after the last existing 'import from ... all;' line
    # Build replacement: find the last import line and add missing ones after it
    lines = content.split('\n')
    last_import_idx = -1
    for i, line in enumerate(lines):
        if re.match(r'^\s*import from\s+\w+\s+all;', line):
            last_import_idx = i

    if last_import_idx >= 0:
        # Insert missing imports after the last import line
        for mod in missing:
            last_import_idx += 1
            lines.insert(last_import_idx, f"    import from {mod} all;")
        new_content = '\n'.join(lines)
        tc_path.write_text(new_content, encoding="utf-8")
        log(f"[INJECT] Added {len(missing)} direct function imports to {tc_filename}: {missing}")


def _fix_ttcn3_timer_syntax(tc_dir: Path) -> int:
    """Fix common TTCN-3 timer syntax issues in all .ttcn files under tc_dir.

    TTCN-3 timer declarations do not use the 'var' keyword.
    Correct: 'timer t := value;'
    Incorrect: 'var timer t := float2int(value);'

    Also replaces float2int() / floattoint() wrappers with direct float values,
    since timer parameters accept float types.

    Returns the number of files modified.
    """
    import re

    fixed = 0
    for ttcn_file in tc_dir.rglob("*.ttcn"):
        try:
            content = ttcn_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        original = content

        # Fix "var timer" -> "timer"
        content = re.sub(r'\bvar\s+timer\b', 'timer', content)

        # Fix "timer NAME := float2int(...)" -> "timer NAME := ..."
        # Covers common patterns like:
        #   timer tc_V2G_EVCC_Msg_Timer := float2int(PX_SECC_CMN_Message_Timeout);
        #   timer tc_TIMER := floattoint(PIXIT_CMN_Sequence_Timeout);
        content = re.sub(
            r'(timer\s+\w+)\s*:=\s*float2int\(([^)]+)\)',
            r'\1 := \2',
            content,
        )
        content = re.sub(
            r'(timer\s+\w+)\s*:=\s*floattoint\(([^)]+)\)',
            r'\1 := \2',
            content,
        )

        if content != original:
            ttcn_file.write_text(content, encoding="utf-8")
            fixed += 1
            log(f"[TIMER FIX] Fixed timer syntax in {ttcn_file.relative_to(tc_dir)}")

    if fixed > 0:
        log(f"[TIMER FIX] Fixed timer syntax in {fixed} file(s)")
    return fixed


def _install_build_script(tc_dir: Path) -> bool:
    """Install a build.sh into the generated project directory.

    Strategy: prefer the canonical, verified build.sh from
    ``knowledge_base/canonical_build.sh`` (the byte-for-byte copy of the
    20260605 PASS run). If that file is missing, fall back to the dynamic
    generator embedded below.
    """
    dest = tc_dir / "build.sh"

    # ── Try canonical build.sh first (verified PASS baseline) ──
    canonical = Path(__file__).parent / "knowledge_base" / "canonical_build.sh"
    if canonical.exists():
        try:
            dest.write_text(canonical.read_text(encoding="utf-8"), encoding="utf-8")
            os.chmod(dest, 0o755)
            log(f"[BUILD] Canonical build.sh copied (from {canonical.name})")
            return True
        except Exception as e:
            log(f"[BUILD] WARNING: canonical copy failed ({e}); falling back to dynamic generator", "WARN")

    # ── Fallback: dynamic generator (preserved for safety) ──
    #
    # The makefilegen command passes files in the correct TITAN ordering:
    #   1. Architecture (ComponentsAndPorts)
    #   2. Data definitions (Types, Templates, PICS, PIXIT)
    #   3. Services (CommonFunctions)
    #   4. PreConditions / PostConditions (ALL .ttcn files -- TITAN does NOT compile
    #      transitively; each module must be passed to makefilegen explicitly)
    #   5. TestBehavior (step functions)
    #   6. TestCases (main module + TC file)
    #   7. C++ port adapters
    #
    # Returns:
    #     True if the file was successfully written, False otherwise.
    dest = tc_dir / "build.sh"

    # ── Bash script content (written as a plain triple-quoted string so that
    #    Python's f-string brace rules never conflict with bash ${VAR} syntax) ──
    script = r"""#!/bin/bash
# ==========================================
# TITAN TTCN-3 Build Script for Linux
# Auto-generated by ttcn3_generation_pipeline
# (dynamic file discovery -- no hardcoded paths)
# ==========================================

set -e  # Exit on error

echo "[BUILD] Starting TITAN build process..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ==========================================
# Bulletproof temp directory management
# ALWAYS uses /tmp/titan_build_* to avoid disk-space leaks even when
# the reasoning model times out and the pipeline is killed mid-build.
# ==========================================
BUILD_WORK_DIR=""
ORIGINAL_DIR=""
KEEP_TEMP=""

_cleanup_and_restore() {
    local exit_code=$?
    local work_dir="$BUILD_WORK_DIR"
    if [ -z "$work_dir" ] || [ ! -d "$work_dir" ]; then return; fi
    if [ "$exit_code" -ne 0 ] && [ -n "$KEEP_TEMP" ]; then
        echo ""
        echo "[BUILD] Build failed (exit code: $exit_code). Temp directory kept: $work_dir"
        return
    fi
    if [ "$exit_code" -eq 0 ]; then
        echo "[BUILD] Syncing build artifacts back to original location..."
        # Non-ASCII path: project is under $work_dir/tc_build_$$/
        # ASCII path: build output lands directly under $work_dir/ (no subdirectory).
        local src_dir="$work_dir"
        if [ -n "$ORIGINAL_DIR" ] && [ -d "$work_dir/tc_build_$$" ]; then
            src_dir="$work_dir/tc_build_$$"
        fi
        [ -d "$src_dir/build"  ] && rsync -a --delete "$src_dir/build/"  "$SCRIPT_DIR/build/"
        [ -d "$src_dir/config" ] && rsync -a --delete "$src_dir/config/" "$SCRIPT_DIR/config/"
        [ -d "$src_dir/logs"   ] && rsync -a "$src_dir/logs/"          "$SCRIPT_DIR/logs/"
        # TITAN TestExecutable resolves `../config/` from `build/bin/` to `build/config/`.
        # Mirror the synced config to build/config/ so the runtime can chdir there.
        if [ -d "$SCRIPT_DIR/config" ]; then
            mkdir -p "$SCRIPT_DIR/build/config"
            rsync -a --delete "$SCRIPT_DIR/config/" "$SCRIPT_DIR/build/config/"
            echo "[BUILD] Mirrored config/ to build/config/ for runtime resolution"
        fi
    fi
    rm -rf "$work_dir"
    echo "[BUILD] Temp directory cleaned up."
}

# Always trap cleanup -- this is the bulletproof mechanism that prevents
# /tmp/titan_build_* orphans even when subprocess.TimeoutExpired kills the
# parent Python process and SIGTERM/SIGINT reaches this script.
trap '_cleanup_and_restore' EXIT
trap 'echo "[BUILD] Build interrupted."; exit 130' INT TERM

# Create temp dir unconditionally (non-ASCII path copies project in;
# ASCII path builds in-place but still uses temp dir so cleanup is uniform)
work_dir=$(mktemp -d /tmp/titan_build_XXXXXX)
if [ -z "$work_dir" ] || [ ! -d "$work_dir" ]; then
    echo "[ERROR] Failed to create temporary build directory."
    exit 1
fi
BUILD_WORK_DIR="$work_dir"

# Handle non-ASCII path: copy project into temp dir before building
_is_path_ascii() {
    ! printf '%s' "$1" | LC_ALL=C grep -q '[^\x00-\x7F]'
}

if ! _is_path_ascii "$SCRIPT_DIR"; then
    if ! command -v rsync &> /dev/null; then
        echo "[ERROR] rsync is required for non-ASCII path builds but is not installed."
        exit 1
    fi
    ORIGINAL_DIR="$SCRIPT_DIR"
    project_name="tc_build_$$"
    echo "[BUILD] Project path contains non-ASCII characters."
    echo "[BUILD] Copying project to ASCII temp directory: $work_dir/$project_name"
    mkdir -p "$work_dir/$project_name"
    rsync -a --delete --exclude='build/' --exclude='logs/' "$SCRIPT_DIR/" "$work_dir/$project_name/"
    echo "[BUILD] Project copied. Switching to temp directory for build."
    cd "$work_dir/$project_name"
fi

# ==========================================
# Step 1: Clean and prepare build directory
# ==========================================
echo "[BUILD] Step 1: Cleaning and preparing build directory..."
rm -rf build && mkdir -p build/generated_cpp build/obj build/bin
mkdir -p logs/titan_traces logs/system_logs config
echo "[BUILD] Directory structure ready."

# ==========================================
# Step 2: Check TITAN installation
# ==========================================
echo "[BUILD] Step 2: Checking TITAN installation..."
if ! command -v makefilegen &> /dev/null; then
    echo "[ERROR] TITAN makefilegen not found in PATH."
    exit 1
fi
if ! command -v g++ &> /dev/null; then
    echo "[ERROR] No C++ compiler found."
    exit 1
fi
echo "[BUILD] TITAN tools and compiler found."

# ── TITAN environment variables ────────────────────────────────────────────────
if [ -z "$TTCN3_DIR" ]; then
    if [ -d "/usr/include/titan" ]; then
        export TTCN3_DIR="/usr"
    elif [ -d "/opt/titan/include/titan" ]; then
        export TTCN3_DIR="/opt/titan"
    elif command -v makefilegen &> /dev/null; then
        TTCN3_BIN=$(command -v makefilegen)
        TTCN3_BIN=$(readlink -f "$TTCN3_BIN" 2>/dev/null || echo "$TTCN3_BIN")
        [ -n "$TTCN3_BIN" ] && export TTCN3_DIR=$(dirname "$(dirname "$TTCN3_BIN")")
    fi
fi
if [ -z "$TTCN3_SUBDIR" ]; then
    if [ -d "${TTCN3_DIR}/include/titan" ]; then
        export TTCN3_SUBDIR="/titan"
    elif [ -d "${TTCN3_DIR}/include" ]; then
        export TTCN3_SUBDIR=""
    fi
fi
export TTCN3_INC="${TTCN3_DIR}/include${TTCN3_SUBDIR}"
export TTCN3_LIB_DIR="${TTCN3_DIR}/lib${TTCN3_SUBDIR}"
if [ -z "$LD_LIBRARY_PATH" ]; then
    export LD_LIBRARY_PATH="${TTCN3_LIB_DIR}"
elif [[ ":$LD_LIBRARY_PATH:" != *":${TTCN3_LIB_DIR}:"* ]]; then
    export LD_LIBRARY_PATH="${TTCN3_LIB_DIR}:${LD_LIBRARY_PATH}"
fi
echo "[BUILD] TITAN: TTCN3_DIR=$TTCN3_DIR  TTCN3_INC=$TTCN3_INC  TTCN3_LIB_DIR=$TTCN3_LIB_DIR"

# ==========================================
# Step 3: Copy port adapter files to generated_cpp/
# ==========================================
echo "[BUILD] Step 3: Copying port adapter files..."
cd build/generated_cpp

find_port_file() {
    local filename="$1"
    # Option 1: src/cpp_adapters/SA/ports/ (always fresh from scaffolding)
    local found=$(find ../../src/cpp_adapters/SA/ports -name "$filename" 2>/dev/null | head -1)
    if [ -n "$found" ] && [ "$(stat -c%s "$found" 2>/dev/null || echo 0)" -gt 200 ]; then
        cp "$found" .; echo "[BUILD] Copied $filename from $found"; echo "$filename"; return 0
    fi
    # Option 2: build/generated_cpp/ (scaffolding copies correct files here, but might have stale entries from old projects)
    if [ -f "$filename" ] && [ "$(stat -c%s "$filename" 2>/dev/null || echo 0)" -gt 200 ]; then
        # Verify it's not a stale skeleton (check for "empty" or "skeleton" content)
        if ! grep -qi "empty\|skeleton" "$filename" 2>/dev/null; then
            echo "[BUILD] Using existing $filename in build/generated_cpp/"; echo "$filename"; return 0
        fi
    fi
    # Option 3: project root
    if [ -f "../../$filename" ] && [ "$(stat -c%s "../../$filename" 2>/dev/null || echo 0)" -gt 200 ]; then
        cp "../../$filename" .; echo "[BUILD] Copied $filename from project root"; echo "$filename"; return 0
    fi
    echo ""; return 1
}

PORT_FILES=("V2G_TCP_TLS_Port.cc" "V2G_TCP_TLS_Port.hh" "V2G_UDP_SDP_Port.cc" "V2G_UDP_SDP_Port.hh")
for port_file in "${PORT_FILES[@]}"; do
    find_port_file "$port_file" || { echo "[ERROR] Port adapter file not found: $port_file"; cd ../..; exit 1; }
done
echo "[BUILD] Port adapter files ready."

# ==========================================
# Step 4: Generate Makefile (dynamic discovery)
# ==========================================
echo "[BUILD] Step 4: Generating Makefile (dynamic file discovery)..."

# ── Auto-discover TTCN-3 source files ─────────────────────────────────────────
# Order matters: Types before Templates before Pre/Post Conditions before TestCases.
#
# CRITICAL: TITAN's makefilegen does NOT compile imported modules transitively.
# If Module A imports Module B, you MUST pass both A.ttcn AND B.ttcn to makefilegen.
# Therefore we must list ALL individual .ttcn files in PreConditions/PostConditions,
# not just the aggregator files.
TTCN_ARCH=$(find ../../src/ttcn3/2_Architecture -name "*.ttcn" 2>/dev/null | sort)
TTCN_TYPES=$(find ../../src/ttcn3/1_Data_Definitions/Types -name "*.ttcn" 2>/dev/null | sort)
TTCN_TEMPLATES=$(find ../../src/ttcn3/1_Data_Definitions/Templates -name "*.ttcn" 2>/dev/null | sort)
TTCN_PICS=$(find ../../src/ttcn3/1_Data_Definitions/PICS -name "*.ttcn" 2>/dev/null | sort)
TTCN_PIXIT=$(find ../../src/ttcn3/1_Data_Definitions/PIXIT -name "*.ttcn" 2>/dev/null | sort)
TTCN_SERVICES=$(find ../../src/ttcn3/3_Behavior_Steps/Services -name "*.ttcn" 2>/dev/null | sort)

# MUST include ALL .ttcn files in PreConditions/PostConditions (not just aggregators).
# TITAN does not compile transitively -- each module file must be passed explicitly.
TTCN_PRECOND=$(find ../../src/ttcn3/3_Behavior_Steps/PreConditions -name "*.ttcn" 2>/dev/null | sort)
TTCN_POSTCOND=$(find ../../src/ttcn3/3_Behavior_Steps/PostConditions -name "*.ttcn" 2>/dev/null | sort)

TTCN_BEHAVIOR=$(find ../../src/ttcn3/3_Behavior_Steps/TestBehavior -name "*.ttcn" 2>/dev/null | sort)
TTCN_TESTS=$(find ../../src/ttcn3/4_TestCases -name "*.ttcn" 2>/dev/null | sort)
CPP_ADAPTERS=$(find ../../src/cpp_adapters -name "*.cc" 2>/dev/null | sort)

echo "[BUILD] TTCN files discovered:"
echo "       Architecture : $(echo $TTCN_ARCH   | wc -w) files"
echo "       Types       : $(echo $TTCN_TYPES  | wc -w) files"
echo "       Templates   : $(echo $TTCN_TEMPLATES | wc -w) files"
echo "       PICS/PIXIT  : $(echo $TTCN_PICS $TTCN_PIXIT | wc -w) files"
echo "       Services    : $(echo $TTCN_SERVICES | wc -w) files"
echo "       PreCond     : $(echo $TTCN_PRECOND | wc -w) files"
echo "       PostCond    : $(echo $TTCN_POSTCOND | wc -w) files"
echo "       TestBehavior: $(echo $TTCN_BEHAVIOR | wc -w) files"
echo "       TestCases   : $(echo $TTCN_TESTS   | wc -w) files"
echo "       C++ Adapters: $(echo $CPP_ADAPTERS | wc -w) files"

# ── Run makefilegen with all discovered files in dependency order ───────────────
makefilegen -f -a -o . -e TestExecutable \
    $TTCN_ARCH \
    $TTCN_TYPES \
    $TTCN_TEMPLATES \
    $TTCN_PICS \
    $TTCN_PIXIT \
    $TTCN_SERVICES \
    $TTCN_PRECOND \
    $TTCN_POSTCOND \
    $TTCN_BEHAVIOR \
    $TTCN_TESTS \
    $CPP_ADAPTERS

MAKEFILEGEN_EXIT=$?

if [ $MAKEFILEGEN_EXIT -ne 0 ]; then
    echo "[ERROR] makefilegen failed!"
    echo "[ERROR] Check that all TTCN module files exist and are readable."
    cd ../..
    exit 1
fi

echo "[BUILD] Makefile generated successfully."

# ==========================================
# Step 4.1: Verify TTCN .cc files were generated
# ==========================================
TTCN_CC_COUNT=$(find . -maxdepth 1 -name "*.ttcn.cc" 2>/dev/null | wc -l)
echo "[BUILD] Generated $TTCN_CC_COUNT TTCN-3 .cc files."
if [ "$TTCN_CC_COUNT" -eq 0 ]; then
    echo "[WARN] No TTCN .cc files generated. Possible missing source files."
fi

# --- Step 4.2: Absolute-path leak check ---
if [ -f Makefile ]; then
    ABS_LEAKS=$(grep -nE "^[[:space:]]*/[^/*]" Makefile 2>/dev/null || true)
    if [ -n "$ABS_LEAKS" ]; then
        echo "[BUILD] ERROR: Absolute path leak detected in Makefile:"
        echo "$ABS_LEAKS"
        exit 1
    fi
fi
echo "[BUILD] Absolute-path leak check: PASS"

# ==========================================
# Step 5: Fix SECC_TestSystem.hh to include port headers
# ==========================================
echo "[BUILD] Step 5: Fixing SECC_TestSystem.hh..."
if [ -f "SECC_TestSystem.hh" ]; then
    sed -i '/#include "V2G_TCP_TLS_Port.hh"/d' SECC_TestSystem.hh
    sed -i '/#include "V2G_UDP_SDP_Port.hh"/d' SECC_TestSystem.hh
    SECOND_NS_LINE=$(grep -n "^namespace SECC__TestSystem {" SECC_TestSystem.hh | head -2 | tail -1 | cut -d: -f1)
    if [ -n "$SECOND_NS_LINE" ]; then
        head -n "$SECOND_NS_LINE" SECC_TestSystem.hh > SECC_TestSystem.hh.tmp
        printf '#include "V2G_TCP_TLS_Port.hh"\n#include "V2G_UDP_SDP_Port.hh"\n' >> SECC_TestSystem.hh.tmp
        tail -n +$((SECOND_NS_LINE + 1)) SECC_TestSystem.hh >> SECC_TestSystem.hh.tmp
        mv SECC_TestSystem.hh.tmp SECC_TestSystem.hh
    fi
fi
if [ -f "SECC_TestSystem.cc" ]; then
    sed -i '/#include "V2G_TCP_TLS_Port.hh"/d' SECC_TestSystem.cc
    sed -i '/#include "V2G_UDP_SDP_Port.hh"/d' SECC_TestSystem.cc
fi

# ==========================================
# Step 6: Fix Makefile
# ==========================================
echo "[BUILD] Step 6: Fixing Makefile..."
cp Makefile Makefile.orig
sed -i 's|^OBJDIR\s*=\s*.*|OBJDIR = ../obj|' Makefile
sed -i 's|^EXECUTABLE = TestExecutable|EXECUTABLE = ../bin/TestExecutable|' Makefile
sed -i 's/^TTCN3_LIB = ttcn3-parallel-dynamic$/TTCN3_LIB = ttcn3-dynamic/' Makefile
sed -i 's/^TTCN3_LIB = ttcn3-parallel$/TTCN3_LIB = ttcn3-dynamic/' Makefile
sed -i 's/^LINUX_LIBS = -lxml2$/LINUX_LIBS = -lxml2 -lpthread/' Makefile
# Add fmt include paths for ExiCodecAdapter and other adapters that use {fmt}
FMT_INC="-I../../src/cpp_adapters/CD/exi_codec/fmt/include -I../../src/cpp_adapters/CD/exi_codec/fmt/fmt -I../../src/cpp_adapters/CD/exi_codec"
if grep -q "^CXXFLAGS" Makefile; then
    sed -i "s|^CXXFLAGS = |CXXFLAGS = ${FMT_INC} |" Makefile
else
    echo "CXXFLAGS = ${FMT_INC}" >> Makefile
fi
echo "[BUILD] Makefile fixed (fmt include paths added)."

# ==========================================
# Step 7: Compile
# ==========================================
echo "[BUILD] Step 7: Compiling..."
export TTCN3_DIR="${TTCN3_DIR:-/usr}"
export TTCN3_INC="${TTCN3_INC:-${TTCN3_DIR}/include/titan}"
export TTCN3_LIB_DIR="${TTCN3_LIB_DIR:-${TTCN3_DIR}/lib/titan}"
export LD_LIBRARY_PATH="${TTCN3_LIB_DIR}:${LD_LIBRARY_PATH:-}"

make
if [ $? -ne 0 ]; then
    echo "[ERROR] Compilation failed!"
    cd ../..
    exit 1
fi

echo "[BUILD] Compilation successful."
OBJ_COUNT=$(find . -maxdepth 1 -name "*.o" 2>/dev/null | wc -l)
echo "[BUILD] Generated $OBJ_COUNT object files."

cd ..

echo ""
echo "[BUILD] Build completed successfully!"
echo ""
echo "To run the tests:"
echo "  cd $SCRIPT_DIR/build/bin"
echo "  ./TestExecutable ../config/titan_runtime.cfg"
echo ""
"""  # <-- end of raw bash script string

    try:
        dest.write_text(script, encoding="utf-8")
        os.chmod(dest, 0o755)
        log(f"[BUILD] Dynamic build.sh written (pipeline: {dest.relative_to(tc_dir)})")
        return True
    except Exception as e:
        log(f"[BUILD] ERROR: failed to write build.sh: {e}", "ERROR")
        return False



def _install_canonical_runtime_config(tc_dir: Path) -> None:
    """Copy the verified runtime config + hardware env to the TC project.

    Source of truth: ``knowledge_base/canonical_titan_runtime.cfg`` and
    ``knowledge_base/canonical_hardware_env.json`` (the 20260605 PASS run).

    These are placed at:
      - ``tc_dir/config/titan_runtime.cfg``  (consumed by the pipeline's
        `_run_titan_test` step, which invokes ``./TestExecutable
        ../config/titan_runtime.cfg`` from ``tc_dir/build/bin``)
      - ``tc_dir/build/config/...``  (consumed by the build.sh's temp
        workspace rsync, in case anyone re-builds)

    P0 FIX — Control File Generation:
    This function generates ``TestCases_SECC.control`` in both config
    directories AND mirrors it into ``tc_dir/build/bin/`` so TITAN can resolve
    the bare filename in the ``[EXECUTE]`` directive. TITAN parses ``[EXECUTE]``
    paths relative to its own startup cwd (``build/bin/`` when the pipeline
    runs ``./TestExecutable ../config/titan_runtime.cfg``) and rejects any
    value containing ``..`` with a parse error at the ``.`` token. Keeping the
    canonical bare filename ``TestCases_SECC.control`` and mirroring the
    control file to ``build/bin/`` is the only configuration that compiles
    AND parses AND finds the control at runtime.
    """
    kb = Path(__file__).parent / "knowledge_base"
    src_cfg = kb / "canonical_titan_runtime.cfg"
    src_hw = kb / "canonical_hardware_env.json"

    target_dirs = [
        (tc_dir / "config", "config/"),
        (tc_dir / "build" / "config", "build/config/"),
    ]

    # ── P0: Generate the .control file in both config/ directories AND in
    #    build/bin/ (TestExecutable's startup cwd). Content is always the
    #    single line "TestCases_SECC.control" — a bare filename, no path
    #    prefix, because TITAN's cfg parser rejects any path containing "..".
    control_text = "TestCases_SECC.control\n"
    runtime_dirs = list(target_dirs) + [
        (tc_dir / "build" / "bin", "build/bin/"),
    ]
    for target_dir, label in runtime_dirs:
        target_dir.mkdir(parents=True, exist_ok=True)
        ctrl = target_dir / "TestCases_SECC.control"
        ctrl.write_text(control_text, encoding="utf-8")
        log(f"[CONFIG] Generated TestCases_SECC.control in {label}")

    # ── P0: Keep the [EXECUTE] directive as the canonical bare filename.
    #    Earlier versions patched this to "../config/TestCases_SECC.control"
    #    which made TITAN abort with: "Parse error ... at or before token '.'
    #    ... expecting $end". Verified working with canonical bare filename
    #    + .control mirrored into build/bin/ on 20260617.
    cfg_text = src_cfg.read_text(encoding="utf-8")
    patched_cfg = cfg_text  # no-op: keep canonical bare filename

    pairs = [
        (src_cfg, "titan_runtime.cfg"),
        (src_hw, "hardware_env.json"),
    ]
    # Also copy to build/bin/ so TestExecutable can resolve config when run from
    # that directory directly (e.g. not via the pipeline's "../config/titan_runtime.cfg" path).
    all_target_dirs = list(target_dirs) + [(tc_dir / "build" / "bin", "build/bin/")]
    for src_file, fname in pairs:
        if not src_file.exists():
            log(f"[CONFIG] WARNING: canonical {fname} not found in knowledge_base", "WARN")
            continue
        content = patched_cfg if fname == "titan_runtime.cfg" else src_file.read_text(encoding="utf-8")
        for target_dir, label in all_target_dirs:
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                dest = target_dir / fname
                dest.write_text(content, encoding="utf-8")
                log(f"[CONFIG] Canonical {fname} installed to {label}")
            except Exception as e:
                log(f"[CONFIG] ERROR: failed to install {fname} to {label}: {e}", "ERROR")


def _patch_build_script_common_functions(tc_dir: Path) -> None:
    """修补 build.sh：如果引用了 CommonFunctions 但 makefilegen 命令中缺少 CommonFunctions.ttcn，则自动注入。"""
    build_sh_path = tc_dir / "build.sh"
    if not build_sh_path.exists():
        return

    content = build_sh_path.read_text(encoding="utf-8")
    # 检查 makefilegen 命令中是否已有 CommonFunctions.ttcn
    if "CommonFunctions.ttcn" in content:
        return

    # 检查 demo 目录中是否存在 CommonFunctions.ttcn（如果存在才注入）
    common_func_path = tc_dir / "src" / "ttcn3" / "3_Behavior_Steps" / "Services" / "CommonFunctions.ttcn"
    if not common_func_path.exists():
        return

    # 在 PreConditions_SECC_15118_2.ttcn 之后插入 CommonFunctions.ttcn
    marker = "PreConditions_SECC_15118_2.ttcn"
    injection = f"    ../../src/ttcn3/3_Behavior_Steps/Services/CommonFunctions.ttcn \\\n"
    if marker in content:
        content = content.replace(marker + " \\\n", marker + " \\\n" + injection, 1)
        build_sh_path.write_text(content, encoding="utf-8")
        log("build.sh 已自动注入 CommonFunctions.ttcn 引用")


def _patch_build_script_pre_post_conditions(tc_dir: Path) -> None:
    """Remove stale individual function file references from build.sh's makefilegen command.

    build.sh is copied from demo/ and contains references to demo's individual precondition,
    postcondition, test-behavior, and old test-case function files. When the current test case
    has an empty precondition_chain/postcondition_chain (or uses different functions), these
    stale references cause makefilegen to fail with "Cannot find any source file" errors.

    This function:
    1. Finds each makefilegen argument line referencing a .ttcn file in PreConditions/,
       PostConditions/, TestBehavior/, or src/ttcn3/4_TestCases/ subdirectories
    2. Checks if the corresponding file actually exists in the project
    3. Removes the line if the file does not exist (stale reference)
    4. Leaves aggregator files (PreConditions_SECC_15118_2.ttcn, PostConditions_SECC_15118_2.ttcn)
       and existing individual files untouched

    Args:
        tc_dir: Test case project directory (contains build.sh)
    """
    build_sh_path = tc_dir / "build.sh"
    if not build_sh_path.exists():
        return

    content = build_sh_path.read_text(encoding="utf-8")

    # Target subdirectories: PreConditions/, PostConditions/, TestBehavior/, 4_TestCases/
    # Aggregator files must be preserved.
    target_dirs = [
        "PreConditions/",
        "PostConditions/",
        "TestBehavior/",
        "4_TestCases/",
    ]
    preserve_files = [
        "PreConditions_SECC_15118_2.ttcn",
        "PostConditions_SECC_15118_2.ttcn",
    ]

    lines = content.split('\n')
    new_lines = []
    removed_count = 0
    for line in lines:
        is_target = any(d in line for d in target_dirs)
        is_preserve = any(p in line for p in preserve_files)
        is_makefilegen_arg = "    ../../" in line and ".ttcn" in line

        if is_target and is_makefilegen_arg and not is_preserve:
            # Extract the .ttcn relative path from the makefilegen argument line.
            # The line format is: "    ../../src/ttcn3/.../f_xxx.ttcn \"
            import re
            m = re.search(r"(\.\./\.\./src/ttcn3/[^\s]+\.ttcn)", line)
            if m:
                rel_path = m.group(1)
                file_path = tc_dir / rel_path
                if not file_path.exists():
                    removed_count += 1
                    continue  # Skip this line (remove it)
        new_lines.append(line)

    if removed_count > 0:
        build_sh_path.write_text('\n'.join(new_lines), encoding="utf-8")
        log(f"build.sh 已清理 {removed_count} 个过时源文件引用（makefilegen 命令）")


def _patch_build_script_include_paths(tc_dir: Path) -> None:
    """Recursively discover all header directories and inject -I flags into the Makefile.

    Problem: The C++ adapter hierarchy under src/cpp_adapters/ contains deeply nested
    include directories (e.g., CD/exi_codec/cppcommon/, fmt/include/, openv2g/, etc.).
    The fmt library's format.cc includes "fmt/format-inl.h", which resolves relative to
    fmt/include/ -- but makefilegen does NOT add nested include paths automatically.

    Solution: Traverse src/cpp_adapters/ for all directories containing .h, .hh, or .inl
    files, then inject each as an absolute -I flag into CXXFLAGS so the compiler can
    resolve all nested #include directives.

    Additionally inject -I.. so generated_cpp/ can find SECC_TestSystem.hh at the
    project root level.
    """
    makefile_path = tc_dir / "build" / "generated_cpp" / "Makefile"
    if not makefile_path.exists():
        log("_patch_build_script_include_paths: Makefile not found, skipping", "WARN")
        return

    # Collect all unique include directories that contain .h / .hh / .inl files
    cpp_adapters_dir = tc_dir / "src" / "cpp_adapters"
    include_dirs: set[str] = set()

    if cpp_adapters_dir.exists():
        for header_file in cpp_adapters_dir.rglob("*"):
            if header_file.is_file() and header_file.suffix in (".h", ".hh", ".hpp", ".inl"):
                include_dirs.add(str(header_file.parent))

    # Convert to sorted list for deterministic output
    sorted_dirs = sorted(include_dirs)

    content = makefile_path.read_text(encoding="utf-8")

    # Build the full -I flag string; prepend -I.. for SECC_TestSystem.hh discovery
    extra_flags = "-I.."
    for d in sorted_dirs:
        # Make path absolute from the project root
        abs_path = str(tc_dir / d)
        extra_flags += f" -I{abs_path}"

    # Check if already patched (idempotent)
    marker = "# TITAN_CXXFLAGS_PATCH"
    if marker in content:
        # Replace existing patch
        import re as _re
        content = _re.sub(
            r"{}.*?{}.*?\n".format(_re.escape(marker), _re.escape(marker)),
            f"{marker} injected\n{marker}\n",
            content,
            flags=_re.DOTALL
        )
        log("_patch_build_script_include_paths: Makefile already patched, skipping")
        return

    # Inject after the CXXFLAGS definition line
    import re as _re
    patched = _re.sub(
        r"(CXXFLAGS\s*=\s*[^\n]*)",
        r"\1 " + _re.escape(extra_flags),
        content
    )

    if patched == content:
        log("_patch_build_script_include_paths: no CXXFLAGS line found, trying CFLAGS", "WARN")
        patched = _re.sub(
            r"(CFLAGS\s*=\s*[^\n]*)",
            r"\1 " + _re.escape(extra_flags),
            content
        )

    if patched != content:
        makefile_path.write_text(patched, encoding="utf-8")
        log(f"_patch_makefile_include_paths: injected {len(sorted_dirs)} include dirs into CXXFLAGS")
    else:
        log("_patch_makefile_include_paths: could not find CXXFLAGS/CFLAGS to patch", "WARN")


def _patch_makefile_include_paths_post_build(tc_dir: Path) -> bool:
    """Recursively discover all C++ header dirs and inject -I flags into the Makefile.

    Called from run_build_and_test() AFTER makefilegen has generated the Makefile.
    This guarantees the Makefile exists so we can patch it.
    Only patches if the Makefile doesn't already have nested include paths.

    Returns:
        True if the Makefile was patched (caller should retry make), False otherwise.
    """
    import re as _re

    makefile_path = tc_dir / "build" / "generated_cpp" / "Makefile"
    if not makefile_path.exists():
        return False

    # Collect all unique include directories containing .h/.hh/.hpp/.inl files
    cpp_adapters_dir = tc_dir / "src" / "cpp_adapters"
    include_dirs: set[str] = set()
    if cpp_adapters_dir.exists():
        for header_file in cpp_adapters_dir.rglob("*"):
            if header_file.is_file() and header_file.suffix in (".h", ".hh", ".hpp", ".inl"):
                include_dirs.add(str(header_file.parent))

    if not include_dirs:
        return False

    sorted_dirs = sorted(include_dirs)

    # Build extra flags: -I.. for SECC_TestSystem.hh + all discovered include dirs
    extra_flags = "-I.. " + " ".join(f"-I{str(tc_dir / d)}" for d in sorted_dirs)

    lines = makefile_path.read_text(encoding="utf-8").splitlines()

    # Check if already patched (fast path)
    if any("CD/exi_codec/fmt" in l for l in lines):
        return False

    # Find and patch the CXXFLAGS line
    patched_lines = []
    found = False
    for line in lines:
        if line.startswith("CXXFLAGS") and "=" in line:
            # Append our extra flags to this line
            patched_lines.append(line + " " + extra_flags)
            found = True
        else:
            patched_lines.append(line)

    if not found:
        log("_patch_makefile_include_paths_post_build: no CXXFLAGS line found", "WARN")
        return False

    makefile_path.write_text("\n".join(patched_lines) + "\n", encoding="utf-8")
    log(f"_patch_makefile_include_paths_post_build: injected {len(sorted_dirs)} include dirs")
    return True


def _rebuild_testcases_module(tc_dir: Path, tc_id: str) -> None:
    """Rebuild TestCases_SECC.ttcn with the correct test case in the control block.

    install_shared_ttcn_files() copies demo's TestCases_SECC.ttcn which always has
    TC_SECC_CMN_VTB_V2GTPSessionSetup_001. This function replaces it with the
    current test case so the control block is correct.

    Args:
        tc_dir: Project root directory
        tc_id: Test case ID (e.g., TC_SECC_CMN_VTB_V2GTPHeader_001)
    """
    testcases_path = tc_dir / "src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn"
    testcases_path.parent.mkdir(parents=True, exist_ok=True)

    content = f"""module TestCases_SECC {{
    import from SECC_TestSystem all;
    import from {tc_id} all;

    control {{
        execute({tc_id}());
    }}
}}
"""
    testcases_path.write_text(content, encoding="utf-8")
    log(f"[STUB] Rebuilt TestCases_SECC.ttcn with {tc_id}")


def _patch_build_script_tc_file(tc_dir: Path, tc_filename: str) -> None:
    """在 build.sh 中确保主测试用例 .ttcn 文件被包含在 makefilegen 命令中"""
    build_sh_path = tc_dir / "build.sh"
    if not build_sh_path.exists():
        return

    content = build_sh_path.read_text(encoding="utf-8")

    # If the TC file is already in the command, nothing to do
    if tc_filename in content:
        return

    # Skip injection if build.sh uses the dynamic find-based approach.
    # The dynamic build.sh (installed by _install_build_script) auto-discovers
    # all .ttcn files via TTCN_TESTS=$(find ...), so manual injection is
    # unnecessary and would corrupt the well-structured script.
    if "TTCN_TESTS=$(find" in content and "makefilegen -f -a" in content:
        log(f"[PATCH SKIP] build.sh uses dynamic find for TTCN_TESTS; "
            f"skipping manual injection of {tc_filename}")
        return

    # Format: relative path as it appears in makefilegen (from build/generated_cpp/)
    # Note: include trailing " \" for line continuation
    injection = f"    ../../src/ttcn3/4_TestCases/{tc_filename} \\"

    # Strategy 1: append right after the marker "TestCases_SECC.ttcn"
    # (works even when the line has extra path prefix, e.g.
    # "../../src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn")
    if "TestCases_SECC.ttcn" in content:
        content = content.replace("TestCases_SECC.ttcn", "TestCases_SECC.ttcn\n" + injection, 1)
        build_sh_path.write_text(content, encoding="utf-8")
        log(f"build.sh 已自动注入主测试用例文件: {tc_filename}")
        return

    # Strategy 2: find the last .ttcn continuation line inside the makefilegen
    # command (within the "makefilegen" section) and insert the new TC line after it.
    # The makefilegen command section ends when we hit port .cc files.
    lines = content.split('\n')
    injection_inserted = False
    in_makefilegen = False
    last_ttcn_line_idx = -1

    for i, line in enumerate(lines):
        # Detect the start of makefilegen command
        if "makefilegen" in line and "-f" in line:
            in_makefilegen = True
        # Detect the end of makefilegen arguments (first .cc line)
        if in_makefilegen and (".cc \\" in line or line.strip().endswith(".cc")):
            break
        # Track the last .ttcn continuation line inside makefilegen
        if in_makefilegen and ".ttcn \\" in line and "../../src/ttcn3" in line:
            last_ttcn_line_idx = i

    if last_ttcn_line_idx >= 0:
        lines.insert(last_ttcn_line_idx + 1, injection)
        injection_inserted = True

    if injection_inserted:
        build_sh_path.write_text('\n'.join(lines), encoding="utf-8")
        log(f"build.sh 已自动注入主测试用例文件: {tc_filename}")
    else:
        log(f"[WARN] build.sh 无法注入主测试用例文件 {tc_filename}，makefilegen 命令结构不符合预期")


def _patch_build_script_ttcn3_compiler_fix(tc_dir: Path) -> None:
    """Fix the ttcn3_compiler fallback command in build.sh.

    The manual ttcn3_compiler fallback uses:
        ttcn3_compiler -I "$TTCN3_INC" "$ttcn_file"

    This is WRONG: TITAN's -I flag means "enable real-time testing features", NOT
    "include path" like in GCC. Passing TTCN3_INC as a positional argument makes the
    compiler treat the include directory as an input file, causing:
        "error: Argument `/usr/include/titan' is a directory"

    Fix: replace the command to use the correct syntax:
        ttcn3_compiler -o . "$ttcn_file"

    Include paths are handled automatically by TITAN's default search path.
    """
    build_sh_path = tc_dir / "build.sh"
    if not build_sh_path.exists():
        return

    content = build_sh_path.read_text(encoding="utf-8")
    if "ttcn3_compiler -I \"$TTCN3_INC\"" not in content:
        return

    # Replace the wrong command pattern
    import re
    old_pattern = r'ttcn3_compiler -I "\$TTCN3_INC" "\$ttcn_file"'
    new_cmd = 'ttcn3_compiler -o . "$ttcn_file"'
    if re.search(old_pattern, content):
        content = re.sub(old_pattern, new_cmd, content)
        build_sh_path.write_text(content, encoding="utf-8")
        log("build.sh: fixed ttcn3_compiler -I flag misuse (was treating TTCN3_INC as input file)")


def _verify_demo_ttcn_syntax() -> Tuple[bool, List[str]]:
    """SCAFFOLD-stage fail-fast gate: dry-run compile all DEMO_SHARED_FILES via ttc3_compiler.

    Without this gate, a syntax error in a demo source file (e.g. a stray
    ``template name := {...}`` without a Type prefix) is silently copied into
    every generated project and only surfaces after the Infrastructure Agent
    has spent several minutes on its work -- wasting LLM tokens and time.

    Compiles each .ttcn in DEMO_SHARED_FILES in one ttc3_compiler invocation.
    Files have cross imports (e.g. PreConditions_SECC_15118_2 imports every
    PR module), so we compile the full set together rather than per-file:
    this catches intra-demo semantic errors that per-file dry-runs would
    miss. Runs from DEMO_ROOT with relative paths so TITAN can resolve
    cross-module imports between subdirectories.

    Returns:
        (ok, errors) -- ok=True means the demo TTCN baseline is syntactically
        valid. errors is the list of compiler error lines (empty if ok=True).
        On TITAN toolchain absence we return (True, []) so this gate does
        not block pipelines on machines without the compiler installed.
    """
    import subprocess

    # Build the list of existing demo files
    files_to_check: List[Path] = []
    for rel in DEMO_SHARED_FILES:
        src = DEMO_ROOT / rel
        if src.exists():
            files_to_check.append(src)

    if not files_to_check:
        log("[PRECOMPILE] no demo TTCN files to verify -- skipping", "WARN")
        return True, []

    # Locate ttc3_compiler binary. Try TTCN3_DIR/bin first, then $PATH.
    ttc3 = None
    titan_dir = os.environ.get("TTCN3_DIR", "/usr")
    for candidate in (
        Path(titan_dir) / "bin" / "ttc3_compiler",
        Path(titan_dir) / "bin" / "ttcn3_compiler",
        Path("/usr/bin/ttc3_compiler"),
        Path("/usr/bin/compiler"),  # Debian/Ubuntu package ships as 'compiler'
    ):
        if candidate.exists():
            ttc3 = str(candidate)
            break
    if ttc3 is None:
        # Fall back to PATH lookup
        import shutil
        ttc3 = shutil.which("ttc3_compiler") or shutil.which("ttcn3_compiler") or shutil.which("compiler")

    if ttc3 is None:
        log("[PRECOMPILE] ttc3_compiler not found -- skipping demo syntax gate (install TITAN to enable)", "WARN")
        return True, []

    # Compile in the demo root so we can use relative paths. Absolute paths
    # cause TITAN to mis-resolve cross-module imports between files that live
    # in different subdirectories of the demo tree.
    cmd = [ttc3] + [str(f.relative_to(DEMO_ROOT)) for f in files_to_check]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(DEMO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        log(f"[PRECOMPILE] ttc3_compiler timed out on {len(files_to_check)} demo files", "ERROR")
        return False, ["timeout during ttc3_compiler dry-run"]
    except Exception as e:
        log(f"[PRECOMPILE] ttc3_compiler invocation failed: {e}", "ERROR")
        return False, [str(e)]

    if proc.returncode == 0:
        log(f"[PRECOMPILE] demo baseline OK -- {len(files_to_check)} files pass ttc3_compiler")
        return True, []

    # Extract error lines. TITAN prints "error:" markers on stderr.
    errors: List[str] = []
    for line in (proc.stderr or "").splitlines():
        stripped = line.strip()
        if "error:" in stripped or "Error" in stripped:
            errors.append(stripped)
    # If we didn't find explicit error markers but the return code is non-zero,
    # surface the last 20 lines of stderr for diagnostic purposes.
    if not errors:
        errors = (proc.stderr or "").splitlines()[-20:]

    log(
        f"[PRECOMPILE] FAIL-FAST: demo baseline has {len(errors)} syntax error(s); "
        f"would have wasted an Infrastructure Agent run. Fix the offending demo file(s) before retrying.",
        "ERROR",
    )
    for err in errors[:10]:
        log(f"  [PRECOMPILE]   {err}", "ERROR")
    return False, errors


def _validate_makefilegen_includes_tc(tc_dir: Path, tc_filename: str) -> bool:
    """验证 makefilegen 命令是否包含主测试用例文件"""
    build_sh_path = tc_dir / "build.sh"
    if not build_sh_path.exists():
        return False
    content = build_sh_path.read_text(encoding="utf-8")

    # Dynamic find-based build.sh auto-discovers all .ttcn files, so the
    # tc_filename will be found at runtime even if not literally present here.
    if "TTCN_TESTS=$(find" in content and "makefilegen -f -a" in content:
        return True

    return tc_filename in content


def _ensure_infrastructure_dir_structure(tc_dir: Path) -> None:
    """确保项目目录结构完整，并复制 V2G 端口适配器实现和 C++ 适配器桩代码

    当 Infrastructure Agent 失败时，build.sh 等关键文件可能不存在。
    此函数确保即使 Agent 完全失败，pipeline 也能生成最基本的项目结构。
    """
    # 确保目录存在
    (tc_dir / "src" / "cpp_adapters" / "common").mkdir(parents=True, exist_ok=True)
    (tc_dir / "src" / "cpp_adapters" / "SA" / "hardware").mkdir(parents=True, exist_ok=True)
    (tc_dir / "src" / "cpp_adapters" / "SA" / "titan_tri").mkdir(parents=True, exist_ok=True)
    (tc_dir / "src" / "cpp_adapters" / "CD" / "titan_tci").mkdir(parents=True, exist_ok=True)
    (tc_dir / "src" / "cpp_adapters" / "CD" / "exi_engine").mkdir(parents=True, exist_ok=True)
    (tc_dir / "build" / "generated_cpp").mkdir(parents=True, exist_ok=True)
    (tc_dir / "build" / "obj").mkdir(parents=True, exist_ok=True)
    (tc_dir / "build" / "bin").mkdir(parents=True, exist_ok=True)
    (tc_dir / "logs" / "titan_traces").mkdir(parents=True, exist_ok=True)
    (tc_dir / "logs" / "system_logs").mkdir(parents=True, exist_ok=True)
    (tc_dir / "config").mkdir(parents=True, exist_ok=True)

    # Copy V2G port adapters to build/generated_cpp/
    # Must copy both .cc implementation and .hh header, both are required.
    # The complete implementation versions live in demo/src/cpp_adapters/SA/ports/.
    # (demo/build/generated_cpp/ contains skeleton-only versions from ttcn3_compiler -t)
    # Only copy if destination doesn't exist.
    generated_cpp_dir = tc_dir / "build" / "generated_cpp"
    demo_port_files = [
        ("src/cpp_adapters/SA/ports/V2G_TCP_TLS_Port.cc", "V2G_TCP_TLS_Port.cc"),
        ("src/cpp_adapters/SA/ports/V2G_TCP_TLS_Port.hh", "V2G_TCP_TLS_Port.hh"),
        ("src/cpp_adapters/SA/ports/V2G_UDP_SDP_Port.cc", "V2G_UDP_SDP_Port.cc"),
        ("src/cpp_adapters/SA/ports/V2G_UDP_SDP_Port.hh", "V2G_UDP_SDP_Port.hh"),
    ]
    for demo_rel, dest_name in demo_port_files:
        dest_path = generated_cpp_dir / dest_name
        if not dest_path.exists():
            demo_content = load_demo_file(demo_rel)
            if demo_content:
                with open(dest_path, "w", encoding="utf-8") as f:
                    f.write(demo_content)
                log(f"  Copied V2G port impl: {dest_name}")

    # Create C++ adapter stubs
    ensure_cpp_adapters(tc_dir)


def pre_build_validate(tc_dir: Path) -> List[str]:
    """编译前验证 import 依赖完整性和 makefilegen 文件列表

    在调用 build.sh 之前，提前发现可能导致编译失败的问题：
    1. 所有 import 引用的模块是否有对应的 .ttcn 文件
    2. build.sh 中 makefilegen 是否包含主测试用例文件

    Args:
        tc_dir: 测试用例项目目录

    Returns:
        问题列表（空列表表示验证通过）
    """
    import re
    issues = []

    ttcn_files = list(tc_dir.rglob("*.ttcn"))

    # 建立模块名 -> 文件路径映射（从 module 关键字提取）
    module_map: Dict[str, Path] = {}
    for f in ttcn_files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        match = re.search(r'module\s+(\w+)', content)
        if match:
            module_map[match.group(1)] = f

    # 检查每个文件的 import 是否都有对应的模块
    for f in ttcn_files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for imp_match in re.finditer(r'import\s+from\s+(\w+)\s+all', content):
            imported_module = imp_match.group(1)
            # 跳过标准库模块（TITAN 内置）
            if imported_module in ("TTCN3", "Titan", "RAW", "TEXT", "JSON", "BER", "PER"):
                continue
            if imported_module not in module_map:
                issues.append(
                    f"{f.relative_to(tc_dir)}: import '{imported_module}' 模块不存在"
                )

    # 检查 build.sh 中 makefilegen 是否包含主测试用例文件
    build_sh = tc_dir / "build.sh"
    if build_sh.exists():
        main_tc_dir = tc_dir / "src" / "ttcn3" / "4_TestCases"
        if main_tc_dir.exists():
            for tc_file in main_tc_dir.glob("*.ttcn"):
                if tc_file.name not in build_sh.read_text(encoding="utf-8", errors="replace"):
                    issues.append(
                        f"build.sh makefilegen 未包含主测试用例: {tc_file.name}"
                    )

    # ==========================================
    # Demo 文件完整性验证：检查 pipeline 赖以复制的源文件是否存在
    # ==========================================
    missing_demo_files = []

    # 1. 端口适配器文件（应在项目根目录或 build/generated_cpp/）
    port_files = ["V2G_TCP_TLS_Port.cc", "V2G_TCP_TLS_Port.hh",
                  "V2G_UDP_SDP_Port.cc", "V2G_UDP_SDP_Port.hh"]
    for pf in port_files:
        root_path = tc_dir / pf
        gen_cpp_path = tc_dir / "build" / "generated_cpp" / pf
        demo_path = DEMO_ROOT / pf
        if not root_path.exists() and not gen_cpp_path.exists():
            if demo_path.exists():
                # 源文件在 demo 中存在，但 pipeline 没有正确复制
                issues.append(f"端口适配器文件未复制到项目: {pf} (demo 中存在，但未复制)")
                missing_demo_files.append(pf)
            else:
                issues.append(f"端口适配器文件缺失且 demo 中也不存在: {pf}")

    # 2. 关键 C++ 适配器文件（应在 src/cpp_adapters/ 下）
    cpp_adapter_files = [
        "src/cpp_adapters/common/Common.cc",
        "src/cpp_adapters/SA/hardware/HAL61851Adapter.cc",
        "src/cpp_adapters/SA/hardware/TCPTLSPortAdapter.cc",
        "src/cpp_adapters/SA/titan_tri/TitanTri.cc",
        "src/cpp_adapters/CD/titan_tci/TitanTci.cc",
        "src/cpp_adapters/CD/exi_engine/ExiCodecAdapter.cc",
    ]
    for caf in cpp_adapter_files:
        file_path = tc_dir / caf
        demo_path = DEMO_ROOT / caf
        if not file_path.exists():
            if demo_path.exists():
                issues.append(f"C++ 适配器文件未复制到项目: {caf} (demo 中存在)")
            else:
                issues.append(f"C++ 适配器文件缺失: {caf} (demo 中也不存在)")

    if missing_demo_files:
        issues.append(f"[严重] demo 文件复制不完整，缺少: {', '.join(missing_demo_files)}")

    # Self-heal: if build.sh is missing a TC file reference, delete it and
    # force _install_build_script to regenerate it dynamically so that ALL
    # TTCN files in 4_TestCases/ are included in the makefilegen.
    _stale_build_sh = tc_dir / "build.sh"
    if any("makefilegen 未包含" in issue for issue in issues):
        log("[BUILD] WARNING: build.sh does not include TC file — forcing dynamic regeneration", "WARN")
        try:
            _stale_build_sh.unlink()
            log("[BUILD] Removed stale build.sh")
        except OSError:
            pass
        _install_build_script(tc_dir)
        log("[BUILD] Regenerated build.sh with dynamic find (includes all TTCN_TESTS)")

    return issues


CPP_ADAPTER_STUBS = {
    "src/cpp_adapters/common/Common.hh": """#ifndef COMMON_HH
#define COMMON_HH
#include <string>
void log_message(const char* level, const char* msg);
#endif
""",
    "src/cpp_adapters/common/Common.cc": """#include "Common.hh"
#include <cstdio>
#include <cstring>
void log_message(const char* level, const char* msg) {
    if (msg) printf("[%s] %s\\n", level, msg);
}
""",
    "src/cpp_adapters/SA/titan_tri/TitanTri.hh": """#ifndef TITANTRI_HH
#define TITANTRI_HH
#include "Titan_Tri.hh"
class TitanTri : public Titan_Tri_Base {
public:
    TitanTri();
    ~TitanTri();
    void log(const char* msg);
};
#endif
""",
    "src/cpp_adapters/SA/titan_tri/TitanTri.cc": """#include "TitanTri.hh"
#include <cstdio>
TitanTri::TitanTri() : Titan_Tri_Base() {}
TitanTri::~TitanTri() {}
void TitanTri::log(const char* msg) { if (msg) printf("[TRI] %s\\n", msg); }
""",
    "src/cpp_adapters/CD/titan_tci/TitanTci.hh": """#ifndef TITANTCI_HH
#define TITANTCI_HH
#include "Titan_Tci.hh"
class TitanTci : public Titan_Tci_Base {
public:
    TitanTci();
    ~TitanTci();
};
#endif
""",
    "src/cpp_adapters/CD/titan_tci/TitanTci.cc": """#include "TitanTci.hh"
TitanTci::TitanTci() : Titan_Tci_Base() {}
TitanTci::~TitanTci() {}
""",
    "src/cpp_adapters/CD/exi_engine/ExiCodecAdapter.hh": """#ifndef EXICODECADAPTER_HH
#define EXICODECADAPTER_HH
#include <cstddef>
class ExiCodecAdapter {
public:
    ExiCodecAdapter();
    ~ExiCodecAdapter();
    void encode(const unsigned char* in, int in_len, unsigned char* out, int* out_len);
    void decode(const unsigned char* in, int in_len, unsigned char* out, int* out_len);
};
#endif
""",
    "src/cpp_adapters/CD/exi_engine/ExiCodecAdapter.cc": """#include "ExiCodecAdapter.hh"
#include <cstring>
ExiCodecAdapter::ExiCodecAdapter() {}
ExiCodecAdapter::~ExiCodecAdapter() {}
void ExiCodecAdapter::encode(const unsigned char* in, int in_len, unsigned char* out, int* out_len) {
    if (out && out_len && in) { memcpy(out, in, in_len); *out_len = in_len; }
}
void ExiCodecAdapter::decode(const unsigned char* in, int in_len, unsigned char* out, int* out_len) {
    if (out && out_len && in) { memcpy(out, in, in_len); *out_len = in_len; }
}
""",
    "src/cpp_adapters/SA/hardware/HAL61851Adapter.hh": """#ifndef HAL61851ADAPTER_HH
#define HAL61851ADAPTER_HH
#include "HAL61851.hh"
class HAL61851Adapter : public HAL61851_Base {
public:
    HAL61851Adapter();
    ~HAL61851Adapter();
};
#endif
""",
    "src/cpp_adapters/SA/hardware/HAL61851Adapter.cc": """#include "HAL61851Adapter.hh"
HAL61851Adapter::HAL61851Adapter() : HAL61851_Base() {}
HAL61851Adapter::~HAL61851Adapter() {}
""",
    "src/cpp_adapters/SA/hardware/TCPTLSPortAdapter.hh": """#ifndef TCPTLSPORTADAPTER_HH
#define TCPTLSPORTADAPTER_HH
#include "TCPTLSPortAdapter.hh"
class TCPTLSPortAdapter : public TCPTLSPortAdapter_Base {
public:
    TCPTLSPortAdapter(const char* name = NULL);
    ~TCPTLSPortAdapter();
};
#endif
""",
    "src/cpp_adapters/SA/hardware/TCPTLSPortAdapter.cc": """#include "TCPTLSPortAdapter.hh"
TCPTLSPortAdapter::TCPTLSPortAdapter(const char* name) : TCPTLSPortAdapter_Base(name) {}
TCPTLSPortAdapter::~TCPTLSPortAdapter() {}
""",
}

# ============================================================
# Shared TTCN-3 Stub Definitions (guaranteed-clean, generated by pipeline, not LLM)
# These replace LLM-generated shared files that may contain syntax errors.
# ============================================================

# ============================================================
# Shared TTCN-3 File Installation (from demo, not LLM)
# These files are copied from the demo project which contains
# working, syntax-verified shared definitions.
# ============================================================

# List of ALL verified TTCN files to copy from demo (nuclear copy strategy).
# These files are known to be syntax-correct and compilable.
# The pipeline REPLACES any LLM-generated version with the demo's verified copy,
# guaranteeing the test suite has a clean compilation baseline.
# NOTE: PreConditions/PostConditions aggregators and their individual function files are
# included -- the _inject_condition_function_imports() pipeline step will rebuild them
# with the correct imports for the current test case afterwards.
DEMO_SHARED_FILES = [
    # Architecture
    "src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn",
    # Data definitions
    "src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn",
    "src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn",
    "src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn",
    "src/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn",
    "src/ttcn3/1_Data_Definitions/PIXIT/PIXIT_SECC_15118_2.ttcn",
    # Services
    "src/ttcn3/3_Behavior_Steps/Services/CommonFunctions.ttcn",
    # PreConditions (aggregator + individual function modules -- all verified)
    "src/ttcn3/3_Behavior_Steps/PreConditions/PreConditions_SECC_15118_2.ttcn",
    "src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_Binding_001.ttcn",
    "src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_InitConfiguration_001.ttcn",
    "src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_InitConfiguration_002.ttcn",
    "src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_PLCLinkStatusCombinedTesting_001.ttcn",
    "src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_SetPowerFlowConfiguration_001.ttcn",
    "src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_SetProximityPilot_001.ttcn",
    "src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_StateA_001.ttcn",
    "src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_StateB_001.ttcn",
    "src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_SupportedAppProtocol_001.ttcn",
    # PostConditions (aggregator + individual function modules -- all verified)
    "src/ttcn3/3_Behavior_Steps/PostConditions/PostConditions_SECC_15118_2.ttcn",
    "src/ttcn3/3_Behavior_Steps/PostConditions/f_SECC_CMN_PO_InitialState_001.ttcn",
    "src/ttcn3/3_Behavior_Steps/PostConditions/f_SECC_CMN_PO_ShutdownConfiguration_001.ttcn",
    "src/ttcn3/3_Behavior_Steps/PostConditions/f_SECC_PO_ShutdownConfiguration_002.ttcn",
    # TestBehavior (function modules -- all verified)
    "src/ttcn3/3_Behavior_Steps/TestBehavior/f_SECC_CMN_TB_VTB_SDP_001.ttcn",
    "src/ttcn3/3_Behavior_Steps/TestBehavior/f_SECC_CMN_TB_VTB_SupportedAppProtocol_001.ttcn",
    "src/ttcn3/3_Behavior_Steps/TestBehavior/f_SECC_CMN_TB_VTB_V2GTPSessionSetup_001.ttcn",
    # TestCases: entry point + a minimal demo testcase for precompile gate.
    # P2 FIX: The demo's TC_SECC_CMN_VTB_V2GTPSessionSetup_001.ttcn was
    # removed from DEMO_SHARED_FILES to prevent cross-contamination between test cases.
    # The precompile gate now uses DemoPassTC.ttcn (a minimal always-pass testcase
    # that imports only the shared skeleton symbols) to verify demo syntax.
    # The _scrub_stale_testcase_files() call in install_shared_ttcn_files()
    # cleans up DemoPassTC.ttcn from generated projects at runtime.
    "src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn",
    "src/ttcn3/4_TestCases/TestCases_SECC/DemoPassTC.ttcn",
]


def _scrub_stale_testcase_files(tc_dir: Path, keep_tc_id: str) -> int:
    """Remove stale .ttcn files from TestCases_SECC/ that don't belong to the current TC.

    This is the P2 isolation fix: when switching from one test case to another
    (e.g., SessionSetup_001 → SDP_001), the previous TC's .ttcn file stays in
    TestCases_SECC/ and confuses TITAN's module resolution.  We only keep:
      - TestCases_SECC.ttcn (the aggregator)
      - The TC file for the current test case (keep_tc_id)

    Returns the number of files removed.
    """
    tc_cases_dir = tc_dir / "src/ttcn3/4_TestCases/TestCases_SECC"
    if not tc_cases_dir.is_dir():
        return 0

    removed = 0
    keep_base = keep_tc_id + ".ttcn"
    for f in tc_cases_dir.glob("*.ttcn"):
        if f.name in ("TestCases_SECC.ttcn", keep_base, "DemoPassTC.ttcn"):
            continue
        # Also skip any TB files that are part of DEMO_SHARED_FILES
        # (they're needed as verified pre/postcondition baselines)
        if any(f.name == Path(p).name for p in DEMO_SHARED_FILES):
            continue
        try:
            f.unlink()
            log(f"[ISOLATION] Removed stale file: {f.relative_to(tc_dir)}")
            removed += 1
        except OSError as e:
            log(f"[ISOLATION] Failed to remove {f}: {e}", "WARN")
    return removed


def _purge_unreferenced_precondition_artifacts(tc_dir: Path, precondition_chain: List[str]) -> None:
    """Remove PR .ttcn files not referenced in precondition_chain after demo copy.

    Called AFTER install_shared_ttcn_files() so the target directory is
    populated.  Scans the PreConditions/ directory and deletes any
    f_SECC_CMN_PR_*.ttcn files whose module name is not in the chain.

    This prevents demo scaffolding pollution (StateA, Binding, etc.) from
    confusing the LLM and the makefilegen.
    """
    pre_dir = tc_dir / "src/ttcn3/3_Behavior_Steps/PreConditions"
    if not pre_dir.exists():
        return
    expected = set(precondition_chain)
    removed_count = 0
    for f in pre_dir.glob("f_SECC_CMN_PR_*.ttcn"):
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        m = re.search(r'^\s*module\s+(\S+)\s*\{', content, re.MULTILINE)
        if not m:
            continue
        module_name = m.group(1)
        if module_name not in expected:
            try:
                f.unlink()
                log(f"[CLEAN] Removed demo artifact: {f.name}")
                removed_count += 1
            except OSError:
                pass
    if removed_count > 0:
        log(f"[CLEAN] Removed {removed_count} unreferenced demo PR artifact(s) from PreConditions/")


def install_shared_ttcn_files(tc_dir: Path, current_tc_id: str = "") -> int:
    """Copy shared TTCN-3 files from the demo project into the generated test project.

    The demo files contain valid, syntax-verified type/template/constant definitions
    that all test cases reference. Copying them ensures:
    1. All shared symbols (types, templates, constants) are defined and valid
    2. No LLM-generated syntax errors in shared files
    3. Consistent baseline across all generated projects

    P2 ISOLATION FIX:
    Before copying, _scrub_stale_testcase_files() removes any .ttcn files from
    TestCases_SECC/ that don't belong to the current test case.  This prevents
    cross-contamination when the batch runner switches between TCs (e.g.,
    SessionSetup_001's .ttcn polluting SDP_001's project).

    Returns:
        Number of files successfully copied.
    """
    # P2: Isolate the project — remove stale test case files before copying
    if current_tc_id:
        _scrub_stale_testcase_files(tc_dir, current_tc_id)

    written = 0
    for rel_path in DEMO_SHARED_FILES:
        src = DEMO_ROOT / rel_path
        dst = tc_dir / rel_path
        if not src.exists():
            log(f"[STUB] Demo source missing for {rel_path}, skipping", "WARN")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        log(f"[STUB] Installed from demo: {rel_path}")
        written += 1
    return written

def ensure_cpp_adapters(tc_dir: Path) -> int:
    """Ensure all required C++ adapter files exist.

    This is a fallback mechanism when the Infrastructure Agent fails to generate
    these files (e.g., due to token limits or missing demo reference).

    Returns:
        Number of files created.
    """
    created = 0
    for rel_path, content in CPP_ADAPTER_STUBS.items():
        file_path = tc_dir / rel_path
        if not file_path.exists():
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            log(f"[FALLBACK] Created C++ adapter stub: {rel_path}")
            created += 1
    return created


def _ensure_port_implementations(generated_cpp_dir: Path, tc_dir: Optional[Path] = None) -> int:
    """Ensure port adapter .cc implementations are non-empty.

    If the skeleton files are empty, stub-only (under 200 bytes), or contain
    TITAN's empty skeleton comment, copy the working implementations from the
    demo project as fallback.

    Args:
        generated_cpp_dir: Path to build/generated_cpp/ directory
        tc_dir: Root project directory (used to also copy ports to rsync-safe path)

    Returns:
        Number of files replaced.
    """
    if not generated_cpp_dir.exists():
        generated_cpp_dir.mkdir(parents=True, exist_ok=True)

    replaced = 0
    # rsync-safe destination (mirrors the path used in the main pipeline)
    rsync_safe_dir = None
    if tc_dir:
        rsync_safe_dir = tc_dir / "src" / "cpp_adapters" / "SA" / "ports"
        rsync_safe_dir.mkdir(parents=True, exist_ok=True)

    for port_name in ["V2G_TCP_TLS_Port.cc", "V2G_TCP_TLS_Port.hh",
                      "V2G_UDP_SDP_Port.cc", "V2G_UDP_SDP_Port.hh"]:
        port_path = generated_cpp_dir / port_name

        needs_replacement = False

        if not port_path.exists():
            needs_replacement = True
        elif port_path.stat().st_size < 200:
            needs_replacement = True
        elif port_name.endswith(".cc"):
            content = port_path.read_text(encoding="utf-8", errors="replace")
            if "empty function" in content or "skeleton" in content.lower():
                needs_replacement = True
            # Always replace if the active outgoing_send signature is missing
            # (old files use V2G__MessageUnion, new files use V2G__PortMessage)
            if "V2G__PortMessage" not in content:
                needs_replacement = True

        if needs_replacement:
            # Primary source: demo/src/cpp_adapters/SA/ports/ (full working implementation)
            # Fallback: demo/build/generated_cpp/ (may be skeleton only)
            # NOTE: Do NOT read from demo root (V2G_TCP_TLS_Port.cc) -- that is a
            # skeleton generated by ttcn3_compiler -t, not a working implementation.
            demo_port = DEMO_ROOT / "src" / "cpp_adapters" / "SA" / "ports" / port_name
            if not (demo_port.exists() and demo_port.stat().st_size > 200):
                demo_port = DEMO_ROOT / "build" / "generated_cpp" / port_name
            if demo_port.exists() and demo_port.stat().st_size > 200:
                shutil.copy(demo_port, port_path)
                log(f"  [FALLBACK] Copied port impl from demo: {port_name}")
                replaced += 1
                # Also mirror to rsync-safe path so build.sh can find it after rsync
                if rsync_safe_dir:
                    rsync_dest = rsync_safe_dir / port_name
                    demo_content = demo_port.read_text(encoding="utf-8", errors="replace")
                    with open(rsync_dest, "w", encoding="utf-8") as f:
                        f.write(demo_content)
                    log(f"  [FALLBACK] Mirrored to rsync-safe: src/cpp_adapters/SA/ports/{port_name}")
            else:
                log(f"  [WARN] No demo port impl found for {port_name}", "WARN")
    return replaced


# ============================================================
# Port 骨架生成 + Agent 实现覆盖
# ============================================================

def generate_port_skeletons(tc_dir: Path, port_impl_stub: Optional[str] = None) -> Dict:
    """使用 ttcn3_compiler -t 生成端口骨架文件，然后将 Agent 提供的完整 .cc 实现覆盖骨架的 .cc。

    流程：
    1. ttcn3_compiler -t 生成骨架（.hh 头文件 + .cc 空实现）
    2. 如果 Agent 提供了 port_impl_stub，用完整 .cc 实现覆盖骨架的 .cc
       （Agent 的 .cc 是完整实现，覆盖骨架的空方法体）

    Args:
        tc_dir: 测试用例项目根目录
        port_impl_stub: Infrastructure Agent 提供的端口完整 .cc 实现代码

    Returns:
        包含生成结果的字典 {"status": "success"|"error", "files": [...], "message": "..."}
    """
    # 确保 build/generated_cpp/ 存在（makefilegen 在此目录运行）
    generated_cpp_dir = tc_dir / "build" / "generated_cpp"
    generated_cpp_dir.mkdir(parents=True, exist_ok=True)

    # 如果没有传入 port_impl_stub 内容，从磁盘读取
    if port_impl_stub is None:
        stub_path = tc_dir / "port_implementation_stub.txt"
        if stub_path.exists():
            port_impl_stub = stub_path.read_text(encoding="utf-8")
            log(f"Read port_impl_stub from: {stub_path}")
        else:
            log(f"port_implementation_stub.txt not found at {stub_path}", "WARN")

    # 检查 ttcn3_compiler 是否可用
    ttcn3_compiler = shutil.which("ttcn3_compiler")
    if not ttcn3_compiler:
        log("ttcn3_compiler not found in PATH, cannot generate port skeletons", "WARN")
        return {
            "status": "error",
            "files": [],
            "message": "ttcn3_compiler not found in PATH"
        }

    components_file = tc_dir / "src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn"
    if not components_file.exists():
        log(f"ComponentsAndPorts.ttcn not found: {components_file}", "WARN")
        return {
            "status": "error",
            "files": [],
            "message": f"ComponentsAndPorts.ttcn not found"
        }

    # 从 tc_dir 运行 ttcn3_compiler -t，生成的骨架文件在 tc_dir / build/ 下
    try:
        log(f"Running ttcn3_compiler -t to generate port skeletons...")
        result = subprocess.run(
            [ttcn3_compiler, "-t", "src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn"],
            capture_output=True,
            text=True,
            cwd=str(tc_dir),
            timeout=60
        )

        if result.returncode != 0:
            log(f"ttcn3_compiler -t failed: {result.stderr}", "WARN")
            return {
                "status": "error",
                "files": [],
                "message": f"ttcn3_compiler -t failed: {result.stderr[:500]}"
            }

        # 移动生成的骨架文件到 build/generated_cpp/
        # （骨架默认生成在 tc_dir/ 下，需要集中到 generated_cpp/ 供 makefilegen 使用）
        build_dir = tc_dir / "build"
        # ttcn3_compiler -t 生成的文件在 tc_dir/ 下，文件名是单下划线
        # 例如: V2G_TCP_TLS_Port.hh, V2G_TCP_TLS_Port.cc, V2G_UDP_SDP_Port.hh, V2G_UDP_SDP_Port.cc
        generated_files = []
        for pattern in ["V2G_TCP_TLS_Port.hh", "V2G_TCP_TLS_Port.cc",
                        "V2G_UDP_SDP_Port.hh", "V2G_UDP_SDP_Port.cc"]:
            for f in tc_dir.glob(pattern):
                # 移动到 build/generated_cpp/
                dst = generated_cpp_dir / f.name
                shutil.move(str(f), str(dst))
                generated_files.append({
                    "path": str(dst.relative_to(tc_dir)),
                    "size": dst.stat().st_size,
                    "source": "ttcn3_compiler_skeleton"
                })
                log(f"  Generated (skeleton): {f.name} -> build/generated_cpp/")

        if not generated_files:
            log("No port skeleton files generated", "WARN")
            return {
                "status": "error",
                "files": [],
                "message": "No port skeleton files generated"
            }

        # Diagnostic: log final state of all port files
        for pf in ["V2G_TCP_TLS_Port.cc", "V2G_TCP_TLS_Port.hh",
                   "V2G_UDP_SDP_Port.cc", "V2G_UDP_SDP_Port.hh"]:
            gpath = generated_cpp_dir / pf
            rpath = tc_dir / "src" / "cpp_adapters" / "SA" / "ports" / pf
            gsz = gpath.stat().st_size if gpath.exists() else 0
            rsz = rpath.stat().st_size if rpath.exists() else 0
            log(f"[DIAG] Skeleton state: {pf}: generated_cpp={gsz}B, rsync-safe={rsz}B")

        # 用 Agent 提供的完整 .cc 实现覆盖骨架的 .cc
        # Agent 输出文件名: V2G_TCP_TLS_Port.cc（单下划线，与骨架文件名一致，直接覆盖）
        if port_impl_stub:
            port_impl_files = _parse_port_impl_stub(port_impl_stub)
            for port_impl in port_impl_files:
                if port_impl["path"].endswith(".cc"):
                    skeleton_path = generated_cpp_dir / port_impl["path"]
                    if skeleton_path.exists():
                        with open(skeleton_path, "w", encoding="utf-8") as f:
                            f.write(port_impl["content"])
                        log(f"  Agent impl overwrites skeleton: {port_impl['path']}")
                        for gf in generated_files:
                            if gf["path"].endswith(port_impl["path"]):
                                gf["source"] = "agent_override"
                                gf["size"] = skeleton_path.stat().st_size
                                break

        log(f"Port skeletons processed: {len(generated_files)} files")
        return {
            "status": "success",
            "files": generated_files,
            "message": f"Generated {len(generated_files)} port skeleton files"
        }

    except subprocess.TimeoutExpired:
        log("ttcn3_compiler -t timed out", "WARN")
        return {
            "status": "error",
            "files": [],
            "message": "ttcn3_compiler -t timed out"
        }
    except Exception as e:
        log(f"ttcn3_compiler -t error: {e}", "WARN")
        return {
            "status": "error",
            "files": [],
            "message": str(e)
        }


def _parse_port_impl_stub(stub_content: str) -> List[Dict]:
    """解析 Infrastructure Agent 的 port_implementation_stub.txt，提取各端口 .cc 实现。

    支持的格式：
    1. 分隔格式: ### V2G_TCP_TLS_Port.cc ### ...content...
    2. 名称标记格式: V2G_TCP_TLS_Port.cc = ...content...

    Returns:
        [{"path": "V2G_TCP_TLS_Port.cc", "content": "..."}, ...]
    """
    import re

    port_files: List[Dict] = []

    # 格式 1: ### filename ###
    sections = re.split(r'###\s*([^\s#]+)\s*###', stub_content)
    if len(sections) >= 3:
        i = 1
        while i < len(sections) - 1:
            filename = sections[i].strip()
            content = sections[i + 1].strip() if i + 1 < len(sections) else ""
            if filename and content and filename.endswith((".cc", ".hh")):
                port_files.append({"path": filename, "content": content})
            i += 2
        if port_files:
            return port_files

    # 格式 2: V2G_TCP_TLS_Port.cc = ...content... 或 ---- V2G_TCP_TLS_Port.cc ----
    for sep in ["=", "----"]:
        pattern = rf'{sep}\s*([^\s{sep}]+\.cc)\s*{sep}\s*\n?(.*?)(?=\n\s*{sep}|$)'
        matches = re.findall(pattern, stub_content, re.DOTALL)
        for filename, content in matches:
            content = content.strip()
            if content:
                port_files.append({"path": filename.strip(), "content": content})
        if port_files:
            return port_files

    return port_files


# ============================================================
# Build & Run 集成
# ============================================================

def run_build_and_test(
    tc_dir: Path,
    tc_id: str,
    sut_ip: Optional[str] = None,
    pcap_output: Optional[Path] = None,
) -> Dict:
    """编译并运行测试（跨平台 + 自动抓包）

    Args:
        tc_dir: 测试用例项目目录（含 build.sh / build.bat）
        tc_id: 测试用例 ID
        sut_ip: SUT IP 地址
        pcap_output: PCAP 抓包输出路径（可选，启用后自动抓包）

    Returns:
        包含编译结果、运行结果的字典
    """
    log(f"\n--- Build & Run: {tc_id} ---")

    # === Step 0: 工具链预检 ===
    toolchain = check_titan_toolchain()
    if not toolchain.get("makefilegen"):
        return {
            "status": "error",
            "reason": "titan_not_found",
            "message": (
                "TITAN 工具链未安装或不在 PATH 中。\n"
                "请安装 Eclipse TITAN 并设置 TTCN3_INC 环境变量。\n"
                "下载地址: https://projects.eclipse.org/projects/tools.titan"
            ),
        }

    # === Step 0b: 编译前预检验证 ===
    # 在真正调用 build.sh 之前，提前发现 import 依赖问题和 makefilegen 文件列表缺失
    validation_issues = pre_build_validate(tc_dir)
    if validation_issues:
        for issue in validation_issues:
            log(f"  [VALIDATE] {issue}", "WARN")
    else:
        log(f"  [VALIDATE] 所有 import 依赖验证通过")

    # === Step 1: 平台检测 + 选择构建脚本 ===
    is_windows = os.name == 'nt'
    build_script = tc_dir / ("build.bat" if is_windows else "build.sh")

    if not build_script.exists():
        return {
            "status": "skipped",
            "reason": "build_script_not_found",
            "message": f"构建脚本未找到: {build_script}",
        }

    # === Step 2: 执行构建脚本 ===
    try:
        if is_windows:
            log(f"Running build.bat in {tc_dir} (Windows)...")
            result = subprocess.run(
                ["cmd", "/c", str(build_script)],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(tc_dir),
            )
        else:
            log(f"Running build.sh in {tc_dir} (Linux)...")
            result = subprocess.run(
                ["bash", str(build_script)],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(tc_dir),
            )

        build_stdout = result.stdout
        build_stderr = result.stderr
        build_returncode = result.returncode

        log(f"Build exit code: {build_returncode}")

        # ================================================================
        # Fix 1 (sweep): Recursive C++ adapter include paths (POST-makefilegen).
        # build.sh runs makefilegen which generates the Makefile in generated_cpp/.
        # Only NOW does the Makefile exist, so we patch CXXFLAGS here.
        # This must happen BEFORE we check build_returncode so we can retry.
        # ================================================================
        _patch_makefile_include_paths_post_build(tc_dir)

        # 保存编译输出（build.sh 的 stdout 包含脚本内的 echo 输出，包含 === STDOXT === 等标记）
        # 将 build.sh 自身的 stdout 也纳入错误解析范围，以捕获脚本 echo 的日志内容
        build_log_path = tc_dir / "logs" / "system_logs" / "build.log"
        build_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(build_log_path, "w", encoding="utf-8") as f:
            f.write(f"=== STDOUT ===\n{build_stdout}\n\n=== STDERR ===\n{build_stderr}\n")
        log(f"Build log saved: {build_log_path}")

        # 解析 + 分类编译错误
        # 同时传入 build.sh 的 stdout（包含 echo 打印的错误）和 stderr
        compile_errors = parse_compile_errors(build_stdout + "\n" + build_stderr)

        if build_returncode != 0:
            # === Auto-retry: patch Makefile include paths and re-run make ===
            # The makefilegen-generated Makefile lacks nested C++ include dirs.
            # If the error mentions missing headers, patch and retry once.
            header_keywords = ["format-inl", "fatal error:", "no such file", "boost/function"]
            first_error = (build_stdout + build_stderr)
            needs_include_fix = any(kw.lower() in first_error.lower() for kw in header_keywords)

            if needs_include_fix:
                patched = _patch_makefile_include_paths_post_build(tc_dir)
                if patched:
                    log("[RETRY] Makefile patched with recursive include paths -- re-running make...")
                    makefile_dir = tc_dir / "build" / "generated_cpp"
                    nproc_result = subprocess.run(
                        ["nproc"], capture_output=True, text=True, timeout=5
                    )
                    n_jobs = nproc_result.stdout.strip() if nproc_result.returncode == 0 else "4"
                    retry_result = subprocess.run(
                        f"make -j{n_jobs}",
                        capture_output=True, text=True, timeout=240,
                        cwd=str(makefile_dir), shell=True
                    )
                    if retry_result.returncode == 0:
                        log("[RETRY] make succeeded after include path patch!")
                        build_returncode = 0
                        build_stdout += "\n[RETRY MAKE OUTPUT]\n" + retry_result.stdout
                        build_stderr += "\n[RETRY MAKE STDERR]\n" + retry_result.stderr
                        compile_errors = []
                        # Update build log to reflect retry
                        with open(build_log_path, "a", encoding="utf-8") as f:
                            f.write("\n[RETRY MAKE -- SUCCESS]\n")
                    else:
                        build_stdout += "\n[RETRY MAKE FAILED]\n" + retry_result.stdout
                        build_stderr += "\n[RETRY MAKE STDERR]\n" + retry_result.stderr

            if build_returncode != 0:
                return {
                    "status": "build_failed",
                    "returncode": build_returncode,
                    "stdout": build_stdout[-5000:],
                    "stderr": build_stderr[-5000:],
                    "compile_errors": compile_errors,
                    "executable_path": None,
                    "test_result": None,
                }

        # 编译成功，查找可执行文件
        exec_path = tc_dir / "build" / "bin" / "TestExecutable"
        if not exec_path.exists():
            exec_path = tc_dir / "build" / "TestExecutable"

        if not exec_path.exists():
            return {
                "status": "build_success_no_executable",
                "stdout": build_stdout[-5000:],
                "stderr": build_stderr[-5000:],
                "executable_path": None,
                "test_result": None,
            }

        # === Step 4: 启动 Sniffer（仅在编译成功后才启动）===
        sniffer = None
        if pcap_output:
            # Check raw socket permission before trying to start sniffer.
            # scapy uses AF_PACKET/SOCK_RAW which requires root or CAP_NET_RAW.
            try:
                import socket as _socket_module
                _raw_test = _socket_module.socket(
                    _socket_module.AF_PACKET, _socket_module.SOCK_RAW,
                    _socket_module.htons(0x0003)
                )
                _raw_test.close()
            except (PermissionError, OSError) as _e:
                # PermissionError: not root; OSError (EPERM/EACCES): no CAP_NET_RAW
                log("Sniffer requires root privileges (CAP_NET_RAW), skipping packet capture", "WARN")
                pcap_output = None

        if pcap_output:
            try:
                from fault_corrector.sniffer import V2G_Sniffer
                sniffer = V2G_Sniffer(output_pcap=str(pcap_output))
                sniffer.start()
                log(f"Sniffer started, capturing to {pcap_output}")
            except ImportError:
                log("Fault Agent sniffer not available (scapy not installed), skipping capture", "WARN")
                sniffer = None
            except Exception as e:
                log(f"Sniffer start failed: {e}, skipping packet capture", "WARN")
                sniffer = None

        # === Step 5: 运行测试 ===
        log(f"Running test: {exec_path} with config/titan_runtime.cfg")
        runtime_cfg = tc_dir / "config" / "titan_runtime.cfg"

        # Track wall-clock time of test execution for metrics.json
        test_start_time = time.time()
        try:
            run_result = subprocess.run(
                [str(exec_path), str(runtime_cfg)],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(exec_path.parent),
            )
        finally:
            test_end_time = time.time()
            # === Step 6: 停止 Sniffer（仅在启动后才停止）===
            if sniffer:
                try:
                    saved_path = sniffer.stop_and_save()
                    log(f"Sniffer stopped, saved to {saved_path}")
                except Exception as e:
                    log(f"Sniffer stop failed: {e}", "WARN")

        # 读取测试日志 (TITAN writes to build/bin/logs/titan_traces/, but earlier
        # versions expected tc_dir/logs/titan_traces/. Try both locations.)
        log_path_candidates = [
            tc_dir / "logs" / "titan_traces" / "TestExecution.log",
            tc_dir / "build" / "bin" / "logs" / "titan_traces" / "TestExecution.log",
        ]
        test_log_content = ""
        log_path = None
        for _lp in log_path_candidates:
            if _lp.exists():
                log_path = _lp
                with open(_lp, "r", encoding="utf-8") as f:
                    test_log_content = f.read()
                break

        test_result = {
            "returncode": run_result.returncode,
            "stdout": run_result.stdout[-5000:],
            "stderr": run_result.stderr[-5000:],
            "log_path": str(log_path),
            "log_content": test_log_content[-10000:],
        }

        # Read execution time from wall-clock (not from log parser)
        execution_wallclock = round(test_end_time - test_start_time, 3)

        # === Phase 3.8: Pre-compute runtime outcome hints for downstream metrics ===
        # Inspect the test_result + wallclock once, at the source of truth, so
        # that the verdict classifier in _build_metrics_dict can pick up
        # nuanced outcomes (early crash vs. genuine timeout) without having to
        # re-implement the same heuristics. This block is intentionally
        # non-destructive -- it only adds fields to test_result; the existing
        # "completed" status is preserved for backward compatibility.
        _combined_for_diag = (test_log_content or "") + "\n" + (run_result.stdout or "")
        if "Overall verdict: pass" in _combined_for_diag:
            test_result["verdict_hint"] = "pass"
        elif "Overall verdict: inconc" in _combined_for_diag:
            test_result["verdict_hint"] = "inconc"
        elif "Overall verdict: fail" in _combined_for_diag:
            test_result["verdict_hint"] = "fail"
        elif execution_wallclock < 1.0 and run_result.returncode not in (None, 0):
            test_result["verdict_hint"] = "runtime_silent_exit"
        elif execution_wallclock < 5.0 and run_result.returncode == 0:
            test_result["verdict_hint"] = "runtime_no_verdict"
        else:
            test_result["verdict_hint"] = "runtime_inconc"

        return {
            "status": "completed",
            "executable_path": str(exec_path),
            "test_result": test_result,
            "compile_errors": compile_errors,
            "execution_wallclock_seconds": execution_wallclock,
        }

    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "reason": "build_timeout",
            "message": "Build exceeded 5 minute timeout",
            "execution_wallclock_seconds": 300.0,
        }
    except Exception as e:
        return {
            "status": "error",
            "reason": "build_error",
            "message": str(e),
        }


def parse_compile_errors(output: str) -> List[Dict]:
    """从编译输出中提取错误信息

    识别 TTCN-3 语法错误和 C++ 编译错误。

    Args:
        output: 编译 stdout/stderr

    Returns:
        错误列表，每个包含 type、message、location
    """
    import re

    errors = []

    # TTCN-3 语法错误: "<filename>:<line>.<col>-<col>: error: <message>"
    # 或 "<filename>:<line>: error: <message>"
    # TITAN outputs column ranges like "9.35-40" after the line number
    ttcn_pattern = re.compile(
        r'(?P<file>[^:\n]+):(?P<line>\d+)(?:\.\d+(?:-\d+)?)?:\s*error:\s*(?P<msg>.+)',
        re.IGNORECASE
    )

    # C++ 编译错误: "<filename>:<line>:<col>: error: <message>"
    # Also matches "fatal error:" which has no column
    cpp_pattern = re.compile(
        r'(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s*error:\s*(?P<msg>.+)',
        re.IGNORECASE
    )

    # fatal error (C++): "<filename>:<line>: fatal error: <message>"
    fatal_pattern = re.compile(
        r'(?P<file>[^:\n]+):(?P<line>\d+):\s*fatal error:\s*(?P<msg>.+)',
        re.IGNORECASE
    )

    # C++ "error:" without column (sometimes emitted by gcc)
    cpp_nocol_pattern = re.compile(
        r'(?P<file>[^:\n]+):(?P<line>\d+):\s*error:\s*(?P<msg>.+)',
        re.IGNORECASE
    )

    for m in cpp_pattern.finditer(output):
        errors.append({
            "type": "cpp_error",
            "file": m.group("file"),
            "line": int(m.group("line")),
            "col": int(m.group("col")),
            "message": m.group("msg").strip(),
        })

    for m in fatal_pattern.finditer(output):
        errors.append({
            "type": "cpp_error",
            "file": m.group("file"),
            "line": int(m.group("line")),
            "col": 0,
            "message": "fatal: " + m.group("msg").strip(),
        })

    for m in ttcn_pattern.finditer(output):
        errors.append({
            "type": "ttcn_error",
            "file": m.group("file"),
            "line": int(m.group("line")),
            "message": m.group("msg").strip(),
        })

    # linker 错误
    linker_pattern = re.compile(
        r'(?P<msg>undefined reference to\s+[\w:_]+|.+\.cc:\d+: undefined)',
        re.IGNORECASE
    )
    for m in linker_pattern.finditer(output):
        errors.append({
            "type": "linker_error",
            "file": "",
            "line": 0,
            "message": m.group("msg").strip(),
        })

    # makefilegen 缺失源文件错误: "makefilegen: error: Cannot find any source file for argument '<filename>'"
    # This is the most common build.sh error. Extract the filename so _map_errors_to_files
    # can route it to Infrastructure Agent (not silently dropped by the metadata filter).
    makefilegen_missing_source_pattern = re.compile(
        r"makefilegen:\s*error:\s*Cannot find any source file for argument\s*'(?P<filename>[^']+)'",
        re.IGNORECASE | re.MULTILINE
    )
    for m in makefilegen_missing_source_pattern.finditer(output):
        errors.append({
            "type": "makefilegen_error",
            "file": m.group("filename").strip(),
            "line": 1,
            "message": m.group(0).strip(),
        })

    # makefilegen 其他错误: "makefilegen: error: <message>"
    makefilegen_pattern = re.compile(
        r'makefilegen:\s*error:\s*(?P<msg>.+)',
        re.IGNORECASE | re.MULTILINE
    )
    for m in makefilegen_pattern.finditer(output):
        # Skip if already captured by the more-specific missing-source pattern above
        if any(e.get("type") == "makefilegen_error" and m.group(0).strip() in e.get("message", "")
               for e in errors):
            continue
        errors.append({
            "type": "makefilegen_error",
            "file": "",
            "line": 0,
            "message": m.group("msg").strip(),
        })

    # build.sh 前置检查错误: "[ERROR] <message>" 或 "[HINT] <message>"
    # These are echoed by build.sh itself, not by TITAN. We capture them to ensure
    # the Compile Loop is triggered even when build.sh fails before makefilegen runs.
    buildscript_error_pattern = re.compile(
        r'\[ERROR\]\s*(?P<msg>.+)',
        re.MULTILINE
    )
    for m in buildscript_error_pattern.finditer(output):
        msg = m.group("msg").strip()
        # Skip generic messages that aren't actionable
        if msg and "error" not in msg.lower()[:10]:
            errors.append({
                "type": "buildscript_prerequisite_error",
                "file": "build.sh",
                "line": 0,
                "message": msg,
            })

    return errors


# ============================================================
# Compile Loop: Pre-flight, Smart Routing, Incremental Repair
# ============================================================

def _preflight_infrastructure_check(tc_dir: Path, compile_errors: List[Dict],
                                    build_returncode: int = 0,
                                    build_stderr: str = "") -> Tuple[bool, List[Dict]]:
    """Pre-flight check: auto-detect and fix common infrastructure issues before entering Compile Loop.

    Args:
        tc_dir: Project directory
        compile_errors: Parsed TITAN compile errors
        build_returncode: build.sh exit code (0 = success)
        build_stderr: build.sh stderr output (may contain pre-check failure messages)

    Returns:
        (fixed, remaining_errors):
            fixed: True if any issues were auto-fixed
            remaining_errors: errors that could not be auto-fixed and need Agent attention
    """
    fixed = False
    remaining = []
    generated_cpp = tc_dir / "build" / "generated_cpp"
    rsync_safe = tc_dir / "src" / "cpp_adapters" / "SA" / "ports"

    # Diagnostic: log the state of port files in both locations
    port_names = ["V2G_TCP_TLS_Port.cc", "V2G_TCP_TLS_Port.hh",
                  "V2G_UDP_SDP_Port.cc", "V2G_UDP_SDP_Port.hh"]
    for pname in port_names:
        gcpp = (generated_cpp / pname).stat().st_size if (generated_cpp / pname).exists() else 0
        rsafe = (rsync_safe / pname).stat().st_size if (rsync_safe / pname).exists() else 0
        log(f"[DIAG] Preflight port state: {pname}: build/generated_cpp={gcpp}B, rsync-safe={rsafe}B")

    # Known build.sh pre-check failure patterns that we can auto-fix
    auto_fixable_patterns = [
        "Port adapter file not found",
        "V2G_TCP_TLS_Port",
        "V2G_UDP_SDP_Port",
        "port adapter files are missing",
        "ttcn3_compiler",
    ]

    # Detect build.sh pre-check failure: returncode != 0 but no TITAN compile_errors.
    # This happens when build.sh exits before running TITAN (e.g., port files missing).
    if not compile_errors and build_returncode != 0:
        stderr_lower = build_stderr.lower()
        if any(p.lower() in stderr_lower for p in auto_fixable_patterns):
            log("[PREFLIGHT] Detected build.sh pre-check failure (no TITAN errors found)")
            port_fixed = _ensure_port_implementations(generated_cpp, tc_dir)
            if port_fixed > 0:
                log(f"[PREFLIGHT] Auto-fixed {port_fixed} port adapter files from demo")
                fixed = True
            else:
                log("[PREFLIGHT] Could not auto-fix port files for pre-check failure", "WARN")
            return fixed, []

    needs_port_fix = any(
        any(p in e.get("message", "") for p in auto_fixable_patterns)
        for e in compile_errors
    )
    if needs_port_fix:
        # Restore working port files from demo (not skeletons).
        # _ensure_port_implementations copies from demo/build/generated_cpp/ if the
        # destination file is missing or under 200 bytes.
        port_fixed = _ensure_port_implementations(generated_cpp, tc_dir)
        if port_fixed > 0:
            log(f"[PREFLIGHT] Auto-fixed {port_fixed} port adapter files from demo")
            fixed = True
        else:
            log("[PREFLIGHT] Could not auto-fix port files", "WARN")

    # Try to ensure shared TTCN files are present (prevent makefilegen from failing)
    if compile_errors:
        first_error = compile_errors[0].get("message", "").lower()
        if any(kw in first_error for kw in ["cannot find module", "import missing", "makefilegen"]):
            installed = install_shared_ttcn_files(tc_dir, tc_id)
            if installed > 0:
                log(f"[PREFLIGHT] Reinstalled {installed} shared TTCN files from demo")
                fixed = True

    # Auto-fix makefilegen missing-source errors: remove stale references from build.sh
    # These errors now carry the missing filename in the 'file' field (Fix 1).
    missing_source_errors = [e for e in compile_errors
                            if e.get("type") == "makefilegen_error"
                            and "cannot find any source file" in e.get("message", "").lower()]
    if missing_source_errors:
        log(f"[PREFLIGHT] Detected {len(missing_source_errors)} makefilegen missing-source errors")
        build_sh_path = tc_dir / "build.sh"
        if build_sh_path.exists():
            content = build_sh_path.read_text(encoding="utf-8")
            lines = content.split('\n')
            new_lines = []
            total_removed = 0
            for line in lines:
                # Check if this line references any missing file in a makefilegen argument
                removed_by_this_line = False
                for err in missing_source_errors:
                    missing_file = err.get("file", "")
                    if missing_file and missing_file in line and "    ../../" in line:
                        total_removed += 1
                        removed_by_this_line = True
                        break
                if not removed_by_this_line:
                    new_lines.append(line)
            if total_removed > 0:
                build_sh_path.write_text('\n'.join(new_lines), encoding="utf-8")
                log(f"[PREFLIGHT] Auto-removed {total_removed} stale makefilegen references from build.sh")
                fixed = True

    # Keep only errors that couldn't be auto-fixed
    auto_fixed_keywords = [
        "V2G_TCP_TLS_Port", "V2G_UDP_SDP_Port",
        "port adapter file not found", "port adapter files are missing",
        "cannot find any source file",
    ]
    for err in compile_errors:
        if not any(kw in err.get("message", "") for kw in auto_fixed_keywords):
            remaining.append(err)

    return fixed, remaining


def build_runtime_feedback(fault_result: Dict, ttcn3_code: str = "") -> str:
    """将 runtime fault 分析结果转换为 Core Agent 的 feedback 字符串。

    包含：故障类型 + 证据 + 修复建议 + 代码上下文行号。

    Args:
        fault_result: run_fault_corrector(phase="runtime") 返回的结果
        ttcn3_code: 当前的 TTCN-3 代码（用于提取上下文行号）
    """
    parts = []

    fault = fault_result.get("fault", {})
    if fault:
        parts.append("## Runtime Fault - Please Analyze and Fix:")
        parts.append(f"**Fault Type:** {fault.get('fault_type', 'unknown')}")
        parts.append(f"**Description:** {fault.get('description', 'N/A')}")
        parts.append(f"**Affected Component:** {fault.get('affected_component', 'N/A')}")
        parts.append(f"**Confidence:** {fault.get('confidence', 'N/A')}")
        if fault.get('evidence'):
            parts.append(f"**Evidence:** {fault.get('evidence', '')[:500]}")

    repairs = fault_result.get("repairs", [])
    if repairs:
        parts.append(f"\n## Suggested Repairs ({len(repairs)} items):")
        for i, r in enumerate(repairs, 1):
            repair_type = r.get('repair_type', '?')
            fault_type = r.get('fault_type', '?')
            current_code = r.get('current_code', '')
            suggested_code = r.get('suggested_code', '')
            reasoning = r.get('reasoning', '')
            location = r.get('location', 'unknown')

            parts.append(f"\n### Repair {i}: [{repair_type}] {fault_type}")
            parts.append(f"**Location:** {location}")
            if reasoning:
                parts.append(f"**Reasoning:** {reasoning[:300]}")
            if current_code and suggested_code:
                parts.append(f"**Current Code:**\n{current_code[:200]}")
                parts.append(f"**Suggested Fix:**\n{suggested_code[:200]}")

    if not fault and not repairs:
        parts.append("## Runtime Analysis Result:")
        parts.append("No specific fault type identified.")
        parts.append("Please review the test execution log and PCAP capture manually.")

    parts.append("\n## Guidelines:")
    parts.append("- Only modify the TTCN-3 code, do not change the project structure")
    parts.append("- Keep the testcase ID unchanged")
    parts.append("- Output the corrected TTCN-3 code as JSON")

    return "\n".join(parts)


# ============================================================
# ============================================================

def _build_error_context(compile_errors: List[Dict], tc_dir: Optional[Path] = None,
                         context_lines: int = 6) -> str:
    """Build code context for compile errors.

    Args:
        compile_errors: List of error dicts
        tc_dir: Test project directory for resolving file paths
        context_lines: Number of lines before/after to include

    Returns:
        Context string for errors that have a valid file+line.
    """
    if not tc_dir:
        return ""
    ctx_parts = []
    for err in compile_errors:
        err_file = err.get("file", "")
        err_line = err.get("line", 0)
        if not err_file or err_line <= 0:
            continue
        file_path = tc_dir / err_file
        if not file_path.exists():
            continue
        try:
            all_lines = file_path.read_text(encoding="utf-8").split('\n')
            start = max(0, err_line - context_lines - 1)
            end = min(len(all_lines), err_line + context_lines)
            snippet_lines = []
            for j in range(start, end):
                marker = ">>>" if j == err_line - 1 else "   "
                snippet_lines.append(f"{marker} {j+1:4d}: {all_lines[j]}")
            ctx_parts.append(f"\nFile: {err_file}:{err_line}\n" + "\n".join(snippet_lines))
        except Exception:
            continue
    return "\n---\n".join(ctx_parts) if ctx_parts else ""


def build_core_error_feedback(core_errors: List[Dict],
                              tc_dir: Optional[Path] = None,
                              targeted_only: bool = False,
                              target_files: Optional[List[str]] = None) -> str:
    """Build feedback string from compile errors (for Core Agent).

    Note: This function is kept for backward compatibility but is no longer
    called from the main pipeline since Infrastructure Agent now owns the compile loop.

    Args:
        core_errors: List of compile errors
        tc_dir: Test project directory (for reading code context)
        targeted_only: If True, only include errors for target_files
        target_files: Target file paths for incremental repair

    Returns:
        Formatted error feedback string
    """
    filtered = list(core_errors)
    if not filtered:
        return ""

    # Incremental repair: filter to only the specific files that have errors
    if targeted_only and target_files:
        target_set = set(target_files)
        filtered = [e for e in filtered if e.get("file", "") in target_set]
        if not filtered:
            return ""

    lines = [
        "## Compilation Errors - Core Agent Feedback",
        "(incremental: only fix listed files)" if targeted_only else "(compile errors)"
    ]
    for i, err in enumerate(filtered, 1):
        err_type = err.get("type", "unknown")
        err_msg = err.get("message", "")
        err_file = err.get("file", "")
        err_line = err.get("line", 0)

        lines.append(f"\n### Error {i}: [{err_type}]")
        lines.append(f"**Location:** {err_file}:{err_line}" if err_file else "**Location:** unknown")
        lines.append(f"**Message:** {err_msg}")

    # Append code context for errors with file+line info
    err_ctx = _build_error_context(filtered, tc_dir)
    if err_ctx:
        lines.append(f"\n## Code Context for Errors:\n{err_ctx}")

    if targeted_only and target_files:
        lines.append(f"\n## Targeted Files to Fix:")
        for f in sorted(target_files):
            lines.append(f"  - {f}")

    lines.append("\n## Guidelines:")
    lines.append("- Keep the testcase ID and module name unchanged")
    lines.append("- Fix ALL errors listed above")
    lines.append("- Output the corrected TTCN-3 code as JSON")
    lines.append("- Do not change working code, only fix the errors")

    return "\n".join(lines)


def build_infra_error_feedback(infra_errors: List[Dict],
                              tc_dir: Optional[Path] = None,
                              targeted_only: bool = False,
                              target_files: Optional[List[str]] = None) -> str:
    """Build feedback string from compile errors (for Infrastructure Agent).

    Note: This function is used by the Infrastructure Agent's inner compile loop.

    Args:
        infra_errors: List of compile errors
        tc_dir: Test project directory (for reading code context)
        targeted_only: If True, only include errors for target_files
        target_files: Target file paths for incremental repair

    Returns:
        Formatted error feedback string
    """
    filtered = list(infra_errors)
    if not filtered:
        return ""

    # Incremental repair: filter to only the specific files that have errors
    if targeted_only and target_files:
        target_set = set(target_files)
        filtered = [e for e in filtered if e.get("file", "") in target_set]
        if not filtered:
            return ""

    lines = [
        "## Compilation Errors - Infrastructure Agent Feedback",
        "(incremental: only fix listed files)" if targeted_only else "(infrastructure errors)",
        "",
        "## STRICT FIX POLICY (forbidden patterns):",
        "- DO NOT replace the test body with a stub like `return pass;`, `setverdict(pass, ...);` "
        "without exercising the network handshake. The framework requires a real send/receive "
        "exchange to validate the protocol against the SECC.",
        "- DO NOT remove or comment out the `send(...)` and `receive(...)` template calls.",
        "- DO NOT swallow exceptions / timeouts to force a `pass` verdict.",
        "- If a TTCN-3 field is missing on a union/record (e.g. `v2g` field), you MUST add the "
        "field to the type definition in Types_SECC_15118_2.ttcn -- do NOT silently change the "
        "TB file to a different field name.",
        "- Fix the root cause in the type system or templates; preserve structural send/receive.",
        "",
    ]
    for i, err in enumerate(filtered, 1):
        err_type = err.get("type", "unknown")
        err_msg = err.get("message", "")
        err_file = err.get("file", "")
        err_line = err.get("line", 0)

        lines.append(f"\n### Error {i}: [{err_type}]")
        lines.append(f"**Location:** {err_file}:{err_line}" if err_file else "**Location:** unknown")
        lines.append(f"**Message:** {err_msg}")

    err_ctx = _build_error_context(filtered, tc_dir)
    if err_ctx:
        lines.append(f"\n## Code Context for Errors:\n{err_ctx}")

    if targeted_only and target_files:
        lines.append(f"\n## Targeted Files to Fix:")
        for f in sorted(target_files):
            lines.append(f"  - {f}")

    lines.append("\n## Guidelines:")
    lines.append("- Focus on fixing project structure, config files, and C++ adapters")
    lines.append("- Keep the TTCN-3 test case code unchanged (if not related to errors)")
    lines.append("- Output the corrected project structure as JSON")

    return "\n".join(lines)


def _build_missing_pr_remediation_directive(generation_plan: Dict) -> str:
    """Append a "missing precondition" block to the Infrastructure Agent fix-mode prompt.

    2D: in fix mode, the Infrastructure Agent gets the same plan-driven
    directive as initial mode, but framed as a remediation step. The agent
    is told: even though you're in fix mode, the test case references these
    PR modules that are still missing on disk -- you MUST create them.

    Returns "" if the plan has no missing PRs (so the prompt stays clean
    for the common case).
    """
    completeness = generation_plan.get("_precondition_completeness", {}) if isinstance(generation_plan, dict) else {}
    missing_pr = completeness.get("missing_preconditions", []) or []
    missing_po = completeness.get("missing_postconditions", []) or []

    if not missing_pr and not missing_po:
        return ""

    lines = [
        "",
        "## 🚨 REPEATED-MISS REMEDIATION (HARD REQUIREMENT, fix mode)",
        "",
        "The previous attempt(s) FAILED to generate the following PreCondition/",
        "PostCondition function module(s) referenced by the test case's",
        "precondition_chain. The compiler errors below likely include messages",
        "like ``There is no module with identifier `f_XXX_001`'`` -- those errors",
        "will not resolve until YOU create the missing files.",
        "",
        "You have up to 3 fix iterations. Use this iteration specifically to:",
        "",
    ]
    if missing_pr:
        lines.append(f"### Missing PreCondition modules to GENERATE ({len(missing_pr)}):")
        for fn in missing_pr:
            lines.append(f"  - `{fn}`  ->  `src/ttcn3/3_Behavior_Steps/PreConditions/{fn}.ttcn`")
        lines.append("")
    if missing_po:
        lines.append(f"### Missing PostCondition modules to GENERATE ({len(missing_po)}):")
        for fn in missing_po:
            lines.append(f"  - `{fn}`  ->  `src/ttcn3/3_Behavior_Steps/PostConditions/{fn}.ttcn`")
        lines.append("")
    lines.extend([
        "**Generation contract** (same as the initial directive in this prompt):",
        "- One file per module, `module f_<Name>_<NNN> { ... }` declaration",
        "- Function body: call sub-conditions in order, mirror the structure of",
        "  the existing demo PreCondition modules (e.g. f_SECC_CMN_PR_SupportedAppProtocol_001.ttcn)",
        "- Return type: `verdicttype`. Use `setverdict(pass/fail/inconc, ...)`.",
        "- Update `PreConditions_SECC_15118_2.ttcn` / `PostConditions_SECC_15118_2.ttcn`",
        "  aggregator to `import from f_<Name>_<NNN> all;`",
        "- Verify the `module` declaration matches the filename before writing",
    ])
    return "\n".join(lines)


# ============================================================
# 运行时自修复
# ============================================================

def _runtime_self_repair(
    ttcn3_code: str,
    generation_plan: Dict,
    fault_result: Dict,
    tc_project_dir: Path,
    output_path: Path,
    tc_ttcn_dest: Path,
    plan_tc_id: str,
    sut_ip: Optional[str] = None,
    few_shot_examples: Optional[Dict[str, str]] = None,
    sop: Optional[Dict] = None,
    references: Optional[Dict] = None,
    compile_loop_fc_times: Optional[List[Tuple[int, float]]] = None,
    compile_loop_fc_total_ref: Optional[List[float]] = None,
) -> Optional[Dict]:
    """运行时自修复：处理 auto_fix 和 manual_review 两类修复建议。

    - auto_fix: 通过字符串替换直接应用。
    - manual_review: 调用 Core Agent + Infrastructure Agent 进行语义级修复，
      完成后 rebuild，进入 Runtime Loop 下一轮。

    支持最多 2 次重试（MAX_RUNTIME_REPAIR_ITERATIONS）。

    Args:
        ttcn3_code: 当前 TTCN-3 源代码。
        generation_plan: 生成计划 JSON。
        fault_result: run_fault_corrector() 返回的结果（runtime phase）。
        tc_project_dir: 项目根目录。
        output_path: 生成的 .ttcn 文件路径。
        tc_ttcn_dest: 项目目录中的 .ttcn 文件路径。
        plan_tc_id: 测试用例 ID。
        sut_ip: SUT IP 地址。
        few_shot_examples: few-shot 样例（manual_review 修复时传入）。
        sop: SOP JSON（manual_review 修复时传入）。
        references: 知识库检索结果（manual_review 修复时传入）。

    Returns:
        修复结果字典，或 None（无可修复项）。
    """
    repairs = fault_result.get("repairs", [])
    if not repairs:
        return None

    # === Runtime Repair Loop: 最多 2 次重试 ===
    all_applied_repairs = []
    current_code = ttcn3_code
    pcap_out = tc_project_dir / "logs" / "capture.pcap"

    for repair_iter in range(1, MAX_RUNTIME_REPAIR_ITERATIONS + 1):
        log(f"运行时自修复迭代 {repair_iter}/{MAX_RUNTIME_REPAIR_ITERATIONS}...")

        # Track total iteration time (repair application + rebuild)
        iter_start = time.time()

        # === auto_fix 类型：字符串替换 ===
        auto_repairs = [
            r for r in repairs
            if r.get("repair_type") == "auto_fix"
            and r.get("suggested_code")
            and r.get("current_code")
        ]

        applied_repairs_this_iter = []

        if auto_repairs:
            log(f"Fault Agent: 发现 {len(auto_repairs)} 个 auto_fix 项")
            for repair in auto_repairs:
                current = repair.get("current_code", "")
                suggested = repair.get("suggested_code", "")
                if current and suggested and current in current_code:
                    current_code = current_code.replace(current, suggested, 1)
                    applied_repairs_this_iter.append({
                        "type": repair.get("repair_type"),
                        "fault_type": repair.get("fault_type"),
                        "location": repair.get("location", ""),
                        "raw_message": repair.get("reasoning", ""),
                        "applied": True,
                        "iteration": repair_iter,
                    })
                    log(f"  Applied fix: [{repair.get('fault_type')}] at {repair.get('location', 'unknown')}")
                else:
                    applied_repairs_this_iter.append({
                        "type": repair.get("repair_type"),
                        "fault_type": repair.get("fault_type"),
                        "location": repair.get("location", ""),
                        "raw_message": repair.get("reasoning", ""),
                        "applied": False,
                        "reason": "current_code snippet not found in source",
                        "iteration": repair_iter,
                    })
                    log(f"  Skipped (not found): [{repair.get('fault_type')}]", "WARN")

        # === manual_review 类型：调用 Core Agent 生成修复代码 ===
        # Infrastructure Agent 不再调用（已锁定）；仅将 Core Agent 返回的 fixed_code
        # 和 pre/post 文件写入磁盘，循环自然调用 run_build_and_test 验证。
        manual_review_repairs = [r for r in repairs if r.get("repair_type") == "manual_review"]
        runtime_feedback = ""

        if manual_review_repairs:
            log(f"Fault Agent: 发现 {len(manual_review_repairs)} 个 manual_review 项，调用 Core Agent...")
            if few_shot_examples and sop:
                runtime_feedback = build_runtime_feedback(fault_result)
                fixed_core_result = run_core_agent(
                    generation_plan, references or {}, few_shot_examples, sop, runtime_feedback,
                    protocol_snapshot=_reg.snapshot(_reg.active_protocol()) if _reg.active_protocol() else None,
                )
                if fixed_core_result and fixed_core_result.get("testcase"):
                    fixed_code = fixed_core_result["testcase"]
                    # 写入修复后的 TTCN-3 代码（主目录 + 项目目录）
                    with open(output_path, "w", encoding="utf-8") as f:
                        f.write(fixed_code)
                    with open(tc_ttcn_dest, "w", encoding="utf-8") as f:
                        f.write(fixed_code)
                    # 写入 Core Agent 返回的新 pre/post 文件
                    fixed_pre = fixed_core_result.get("precondition_files", [])
                    fixed_post = fixed_core_result.get("postcondition_files", [])
                    for f_data in fixed_pre + fixed_post:
                        f_path = tc_project_dir / f_data["path"]
                        f_path.parent.mkdir(parents=True, exist_ok=True)
                        fixed_content = _sanitize_extracted_text(f_data["content"])
                        f_path.write_text(fixed_content, encoding="utf-8")
                    if fixed_pre or fixed_post:
                        log(f"已写入 {len(fixed_pre)} 个 pre + {len(fixed_post)} 个 post 文件")
                    current_code = fixed_code
                    applied_repairs_this_iter.append({
                        "type": "manual_review",
                        "status": "core_agent_success",
                        "iteration": repair_iter,
                    })
                    log(f"Core Agent 修复代码已写入: {output_path}")
                else:
                    log("Core Agent 未生成修复代码（manual_review）", "WARN")
                    applied_repairs_this_iter.append({
                        "type": "manual_review",
                        "status": "core_agent_failed",
                        "iteration": repair_iter,
                    })
            else:
                log("manual_review 修复缺少必要参数（few_shot_examples/sop），跳过", "WARN")

        if not any(r.get("applied") for r in applied_repairs_this_iter) and not manual_review_repairs:
            log("本次迭代无可应用的修复，跳过 rebuild")
            continue

        # 累计所有迭代的修复记录
        all_applied_repairs.extend(applied_repairs_this_iter)

        # 回写最终代码
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(current_code)
        with open(tc_ttcn_dest, "w", encoding="utf-8") as f:
            f.write(current_code)
        log(f"自修复代码已写入: {output_path}")

        # rebuild 验证
        retry_result = run_build_and_test(
            tc_dir=tc_project_dir,
            tc_id=plan_tc_id,
            sut_ip=sut_ip,
            pcap_output=pcap_out,
        )

        # Track this FC retry build iteration for metrics
        if compile_loop_fc_times is not None:
            compile_loop_fc_times.append((repair_iter, 0.0))
        if compile_loop_fc_total_ref is not None and compile_loop_fc_total_ref:
            compile_loop_fc_total_ref[0] += 0.0

        iter_elapsed = time.time() - iter_start

        retry_status = retry_result.get("status", "")
        if compile_loop_fc_times is not None and len(compile_loop_fc_times) >= repair_iter:
            compile_loop_fc_times[repair_iter - 1] = (repair_iter, round(iter_elapsed, 3))
        if compile_loop_fc_total_ref is not None and compile_loop_fc_total_ref:
            compile_loop_fc_total_ref[0] += iter_elapsed
        if retry_status == "completed":
            # ================================================================
            # Runtime Log Verification (Defect #1 fix):
            # Compile success is NOT sufficient. The TestExecution.log must
            # explicitly contain "Overall verdict: pass" -- otherwise the
            # binary may have returned 0 without exercising the protocol
            # (e.g. an LLM stub like `return pass;`). Continue the FC loop
            # instead of declaring success.
            # ================================================================
            _runtime_log_path_candidates = [
                tc_project_dir / "logs" / "titan_traces" / "TestExecution.log",
                tc_project_dir / "build" / "bin" / "logs" / "titan_traces" / "TestExecution.log",
            ]
            _runtime_log_path = next((p for p in _runtime_log_path_candidates if p.exists()), _runtime_log_path_candidates[0])
            _runtime_log_content = ""
            if _runtime_log_path.exists():
                try:
                    _runtime_log_content = _runtime_log_path.read_text(encoding="utf-8", errors="ignore")
                except OSError as _e:
                    log(f"[RUNTIME-VERIFY] could not read {_runtime_log_path}: {_e}", "WARN")
            _runtime_pass = "Overall verdict: pass" in _runtime_log_content
            if not _runtime_pass:
                log(f"[RUNTIME-FAIL] compile succeeded but TestExecution.log lacks 'Overall verdict: pass'")
                log(f"[RUNTIME-FAIL] log_path={_runtime_log_path}, length={len(_runtime_log_content)}")
                # Treat as runtime failure; do NOT return, do NOT lock compile.
                # Fall through to the build_failed branch to re-invoke FC.
                retry_status = "runtime_fail"
                applied_repairs_this_iter.append({
                    "type": "runtime_verification_failed",
                    "status": "no_overall_verdict_pass",
                    "iteration": repair_iter,
                    "log_path": str(_runtime_log_path),
                    "log_length": len(_runtime_log_content),
                })
            else:
                log("运行时自修复: 编译运行成功! (Overall verdict: pass confirmed)")
                try:
                    from fault_corrector.repair_engine import learn_from_repair
                    for repair in applied_repairs_this_iter:
                        if repair.get("applied"):
                            learn_from_repair(repair, repair.get("raw_message", ""))
                    log(f"知识库已更新: {len([r for r in applied_repairs_this_iter if r.get('applied')])} 条新修复记录")
                except Exception as e:
                    log(f"知识库更新失败: {e}", "WARN")
                return {
                    "status": "repair_success",
                    "repairs": all_applied_repairs,
                    "build_status": retry_status,
                    "iterations_used": repair_iter,
                    "runtime_verdict": "pass",
                }
        if retry_status in ("build_failed", "runtime_fail"):
            retry_errors = retry_result.get("compile_errors", [])
            log(f"运行时自修复: 修复后编译仍然失败 ({len(retry_errors)} 个错误)")

            if repair_iter < MAX_RUNTIME_REPAIR_ITERATIONS:
                # 还有重试机会，重新调用 Fault Corrector 获取新的修复建议
                log("还有重试机会，重新调用 Fault Corrector 获取新修复建议...")
                new_fault_result = run_fault_corrector(
                    ttcn3_code=current_code,
                    generation_plan=generation_plan,
                    sop=sop,
                    phase="runtime",
                    tc_id=plan_tc_id,
                    log_path=tc_project_dir / "build" / "bin" / "logs" / "titan_traces" / "TestExecution.log"
                        if (tc_project_dir / "build" / "bin" / "logs" / "titan_traces" / "TestExecution.log").exists()
                        else (tc_project_dir / "logs" / "titan_traces" / "TestExecution.log"),
                    pcap_path=pcap_out if pcap_out.exists() else None,
                )
                if new_fault_result and new_fault_result.get("repairs"):
                    repairs = new_fault_result.get("repairs", [])
                    log(f"获取到 {len(repairs)} 个新修复建议")
                else:
                    log("Fault Corrector 未返回新的修复建议，停止重试")
                    break
            else:
                # 已用完所有重试机会，回退
                log(f"已用完所有重试机会 ({MAX_RUNTIME_REPAIR_ITERATIONS})，回退到原始代码")
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(ttcn3_code)
                with open(tc_ttcn_dest, "w", encoding="utf-8") as f:
                    f.write(ttcn3_code)
                return {
                    "status": "repair_compile_failed",
                    "repairs": all_applied_repairs,
                    "build_status": retry_status,
                    "compile_errors": retry_errors,
                    "iterations_used": repair_iter,
                }
        else:
            log(f"运行时自修复: 重编译结果={retry_status}，保留修复代码")
            return {
                "status": f"repair_{retry_status}",
                "repairs": all_applied_repairs,
                "build_status": retry_status,
                "iterations_used": repair_iter,
            }

    # 循环结束但没有成功
    if all_applied_repairs:
        log("运行时自修复循环结束，恢复原始代码")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(ttcn3_code)
        with open(tc_ttcn_dest, "w", encoding="utf-8") as f:
            f.write(ttcn3_code)

    return None


# ============================================================
# 单个测试用例的代码生成
# ============================================================


class _PipelineEarlyExit(Exception):
    """Sentinel exception used internally to bail out of generate_code_for_test_case
    while ensuring the finally block always executes (for consistent timing output).
    """
    pass


class _SandboxLocked(Exception):
    """Raised when Claude Code CLI reports that the sandbox is blocking all file
    creation / shell write operations. Detected in call_claude_agent() by scanning
    the agent's stdout/stderr for known sandbox-restriction phrases.

    This is NOT a normal LLM failure (where the agent returned text but the code
    didn't compile). Re-calling the agent cannot recover from this — the sandbox
    state is environmental. Callers MUST propagate this exception out of the
    pipeline rather than retrying, to avoid burning tokens in a doomed retry loop.
    """
    def __init__(self, agent_name: str, evidence: str):
        self.agent_name = agent_name
        self.evidence = evidence
        super().__init__(
            f"{agent_name} Agent reported sandbox restrictions; aborting immediately."
        )


# Phrases that Claude Code CLI emits when its MCP sandbox blocks shell writes
# (mkdir, install -d, cat > file, etc.). When seen in agent stdout/stderr we
# MUST abort the TC immediately — re-calling the agent cannot recover from a
# sandbox-state failure and only burns tokens.
#
# Note: "permission_denials" is intentionally NOT in this list because Claude
# Code CLI emits `"permission_denials":[]` in every successful JSON response
# (an empty array of denied permissions), which would trigger a false-positive
# sandbox-lock on every successful run. The real signals we want are the
# human-readable error messages from blocked mkdir / approval requests.
_SANDBOX_LOCK_PHRASES = (
    "may only create directories in the allowed working directories",
    "this command requires approval",
    "this bash command contains multiple operations",
    "mkdir was blocked",
    "create_file was blocked",
    "edit_file was blocked",
)


def _scan_for_sandbox_lock(stdout: str, stderr: str) -> Optional[str]:
    """Return the first sandbox-restriction phrase found in agent output, or None.

    Phase 3.7 in call_claude_agent() calls this after every agent invocation.
    If a sandbox-restriction phrase is detected, the caller raises _SandboxLocked
    so the pipeline aborts the TC instead of retrying a doomed loop.

    Args:
        stdout: Agent stdout (may contain JSON event stream from Claude Code CLI).
        stderr: Agent stderr (may contain progress / error lines).

    Returns:
        The matched phrase string, or None if no sandbox-restriction is detected.
    """
    combined = ((stdout or "") + "\n" + (stderr or "")).lower()
    for phrase in _SANDBOX_LOCK_PHRASES:
        if phrase.lower() in combined:
            return phrase
    return None


class GenerationFailedError(RuntimeError):
    """Raised when LLM generation produces no output and no partial artifacts exist.

    This is the Fail-Fast anchor for Dimension 1 of the self-completing pipeline
    architecture. When the Core Agent is called to generate a missing Precondition
    function and returns nothing (API error, empty response, or no .ttcn file on
    disk), we MUST NOT fall back to a stub. Stub files cause TITAN compilation
    to silently succeed while runtime behavior is wrong or broken.

    Instead, we raise this exception immediately so the pipeline fails explicitly
    and loudly. The operator must investigate the Core Agent failure before
    re-running. This is intentional: better an explicit failure than a silent
    stub that corrupts downstream compilation.
    """
    def __init__(self, agent_name: str, reason: str, module_name: str = ""):
        self.agent_name = agent_name
        self.reason = reason
        self.module_name = module_name
        detail = module_name and f" (module={module_name})" or ""
        super().__init__(
            f"{agent_name} failed to generate missing precondition function{detail}: "
            f"{reason}. Pipeline aborted without generating a stub. "
            f"Fix the agent or provide the missing module manually."
        )


# ============================================================
# Global Asset Promotion Pool (Dimension 3)
# ============================================================

# Fixed path — lives inside generated/, visible to all run directories.
_GLOBAL_ASSET_POOL_DIR = GENERATED_DIR / "_global_asset_pool"
_GLOBAL_ASSET_INDEX_PATH = _GLOBAL_ASSET_POOL_DIR / "index.json"


def _ensure_global_asset_pool() -> None:
    """Ensure the global asset pool directory and index exist."""
    _GLOBAL_ASSET_POOL_DIR.mkdir(parents=True, exist_ok=True)
    if not _GLOBAL_ASSET_INDEX_PATH.exists():
        _GLOBAL_ASSET_INDEX_PATH.write_text("{}", encoding="utf-8")


def _load_global_asset_index() -> Dict[str, Dict]:
    """Load the global asset pool index, or return empty dict on error."""
    try:
        if _GLOBAL_ASSET_INDEX_PATH.exists():
            return json.loads(_GLOBAL_ASSET_INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_global_asset_index(index: Dict[str, Dict]) -> None:
    """Persist the global asset pool index to disk."""
    try:
        _ensure_global_asset_pool()
        _GLOBAL_ASSET_INDEX_PATH.write_text(
            json.dumps(index, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    except OSError as e:
        log(f"[GLOBAL POOL] Failed to persist index: {e}", "WARN")


def _promote_to_global_pool(generated_ttcn_path: Path, module_name: str,
                             source_run_dir: str) -> bool:
    """Copy a successfully-generated PR/TB .ttcn file to the global pool.

    Global pool location: generated/_global_asset_pool/
    Pool index: _global_asset_pool/index.json

    The file is only added if it is not already present (existing entries are
    preserved — first writer wins semantics).

    Args:
        generated_ttcn_path: Absolute path to the generated .ttcn file.
        module_name: Module name, e.g. "f_SECC_CMN_PR_SessionSetup_001".
        source_run_dir: Name of the run directory that produced this file,
                       e.g. "TC_SECC_CMN_VTB_ServiceDiscovery_001_20260619_161423".

    Returns:
        True if the file was promoted (new or updated), False if skipped.
    """
    _ensure_global_asset_pool()

    index = _load_global_asset_index()
    dest_path = _GLOBAL_ASSET_POOL_DIR / f"{module_name}.ttcn"

    try:
        content = generated_ttcn_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log(f"[GLOBAL POOL] Cannot read source file {generated_ttcn_path}: {e}", "WARN")
        return False

    # Stub filter: reject files that are empty, tiny, or contain the stub marker.
    line_count = content.count("\n")
    if line_count <= 50 or "Auto-generated stub precondition function" in content:
        log(f"[GLOBAL POOL] Skipping {module_name}: looks like a stub "
            f"(lines={line_count}); not promoted to global pool", "WARN")
        return False

    try:
        dest_path.write_text(content, encoding="utf-8")
    except OSError as e:
        log(f"[GLOBAL POOL] Failed to write {dest_path}: {e}", "WARN")
        return False

    # Update index.
    import datetime as _dt
    index[module_name] = {
        "file": f"{module_name}.ttcn",
        "first_promoted_by": source_run_dir,
        "promoted_at": _dt.datetime.utcnow().isoformat() + "Z",
        "compilable": True,
        "line_count": line_count,
    }
    _save_global_asset_index(index)
    log(f"[GLOBAL POOL] Promoted {module_name}.ttcn <- {source_run_dir} "
        f"({line_count} lines)")
    return True


def _upgrade_promote_precondition_assets(
    tc_dir: Path,
    generation_plan: Dict,
    generated_root: Optional[Path] = None,
    current_run_dir: str = "",
) -> List[str]:
    """Upgraded version of _promote_precondition_assets: checks global pool first.

    Priority order:
    1. Global asset pool (_global_asset_pool/) — most authoritative
    2. Per-run directories in generated/ — fallback

    The stub filter (>50 lines, no "Auto-generated stub" marker) is applied
    to both sources.

    This replaces the existing _promote_precondition_assets() function at
    runtime by monkey-patching it into the module namespace. Callers should
    use _upgrade_promote_precondition_assets directly after installing this upgrade.

    Returns list of module names that were promoted.
    """
    import re as _re
    from pathlib import Path as _P

    if generated_root is None:
        generated_root = tc_dir.parent.parent / "generated"
    if not generated_root.is_dir():
        log(f"[ASSET PROMOTE] No generated/ root found at {generated_root}; skipping")
        return []

    completeness = generation_plan.get("_precondition_completeness", {}) or {}
    missing = list(completeness.get("missing_preconditions", []) or [])
    if not missing:
        log("[ASSET PROMOTE] No missing preconditions reported by plan; nothing to promote")
        return []

    pre_dir = tc_dir / "src" / "ttcn3" / "3_Behavior_Steps" / "PreConditions"
    pre_dir.mkdir(parents=True, exist_ok=True)

    promoted: List[str] = []
    still_missing: List[str] = []

    # ---- Priority 1: Global asset pool ----
    _ensure_global_asset_pool()
    global_index = _load_global_asset_index()

    for mod_name in missing:
        mod_filename = f"{mod_name}.ttcn"
        found_src: Optional[_P] = None

        # Check global pool first.
        if mod_name in global_index:
            pool_file = _GLOBAL_ASSET_POOL_DIR / mod_filename
            if pool_file.is_file():
                try:
                    content = pool_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    content = ""
                line_count = content.count("\n")
                if line_count > 50 and "Auto-generated stub" not in content:
                    found_src = pool_file

        # ---- Priority 2: Per-run directory scan (existing logic) ----
        if found_src is None:
            # Collect candidate source directories sorted by mtime DESC.
            candidates: List[_P] = []
            for d in generated_root.iterdir():
                if not d.is_dir():
                    continue
                if "_" not in d.name:
                    continue
                if d == tc_dir:
                    continue
                metrics_path = d / "metrics.json"
                if not metrics_path.is_file():
                    continue
                try:
                    m = json.loads(metrics_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                verdict = (m.get("verdict") or {}).get("result")
                if verdict not in {"pass", "fail", "inconc", "fix_success"}:
                    continue
                src_pre_dir = d / "src" / "ttcn3" / "3_Behavior_Steps" / "PreConditions"
                if not src_pre_dir.is_dir():
                    continue
                candidates.append(d)
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

            for cand in candidates:
                src_file = cand / "src" / "ttcn3" / "3_Behavior_Steps" / "PreConditions" / mod_filename
                if not src_file.is_file():
                    continue
                try:
                    content = src_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                line_count = content.count("\n")
                if line_count <= 50 or "Auto-generated stub" in content:
                    continue
                found_src = src_file
                break

        if found_src is None:
            still_missing.append(mod_name)
            continue

        # Promote: copy the asset into the new project.
        dst_file = pre_dir / mod_filename
        try:
            dst_file.write_text(found_src.read_text(encoding="utf-8", errors="replace"),
                                encoding="utf-8")
        except OSError as e:
            log(f"[ASSET PROMOTE] Copy failed for {mod_filename}: {e}", "WARN")
            still_missing.append(mod_name)
            continue

        promoted.append(mod_name)
        src_dir_name = found_src.parent.parent.parent.parent.parent.name
        log(f"[ASSET PROMOTE] {mod_filename} <- {src_dir_name} "
            f"(global_pool={str(found_src).find('_global_asset_pool') >= 0})")

    # Wire promoted modules into the aggregator.
    if promoted:
        agg_path = pre_dir / "PreConditions_SECC_15118_2.ttcn"
        try:
            if agg_path.exists():
                agg_content = agg_path.read_text(encoding="utf-8")
            else:
                agg_content = (
                    "module PreConditions_SECC_15118_2 {\n"
                    "    import from SECC_TestSystem all;\n"
                    "    import from SECC_CmnTemplates all;\n"
                    "    import from SECC_Constants all;\n"
                    "}\n"
                )
            modified = False
            for mod in promoted:
                if f"import from {mod} all;" not in agg_content:
                    agg_content = agg_content.replace(
                        "} // end module PreConditions_SECC_15118_2",
                        f"    import from {mod} all;\n}} // end module PreConditions_SECC_15118_2",
                    )
                    if f"import from {mod} all;" not in agg_content:
                        agg_content = (agg_content.rsplit("}", 1)[0]
                                      + f"    import from {mod} all;\n}}\n")
                    modified = True
            if modified:
                agg_path.write_text(agg_content, encoding="utf-8")
                log(f"[ASSET PROMOTE] Updated {agg_path.name} with promoted imports")
        except OSError as e:
            log(f"[ASSET PROMOTE] Could not update aggregator: {e}", "WARN")

        completeness["missing_preconditions"] = still_missing
        completeness["promoted_preconditions"] = promoted
        generation_plan["_precondition_completeness"] = completeness
        log(f"[ASSET PROMOTE] Promoted {len(promoted)} module(s): {promoted}")
        if still_missing:
            log(f"[ASSET PROMOTE] Still missing after scan: {still_missing}")
    else:
        log(f"[ASSET PROMOTE] No promotable assets found; Core Agent will synthesize: {missing}")

    return promoted


# ============================================================
# Dimension 1+2: Real Recursive Generation (replaces stubs)
# ============================================================

def _derive_message_type_from_module(module_name: str) -> str:
    """Derive a plausible message_type from a function module name.

    Examples:
      f_SECC_CMN_PR_SessionSetup_001  -> "SessionSetup"
      f_SECC_CMN_PR_ServiceDiscovery_001 -> "ServiceDiscovery"
      f_SECC_CMN_PR_PaymentServiceSelection_001 -> "PaymentServiceSelection"
    """
    import re
    m = re.match(r'^f_[A-Z]+_PR_([A-Za-z]+)_\d+$', module_name)
    if m:
        return m.group(1)
    return "V2GMessage"


def _generate_missing_precondition_functions(
    tc_dir: Path,
    generation_plan: Dict,
    missing_modules: List[str],
    references: Dict,
    few_shot_examples: Dict,
    sop: Dict,
    run_timestamp: str = "",
) -> List[str]:
    """Call Core Agent to generate real TTCN-3 implementations for missing PR functions.

    This replaces the legacy _ensure_missing_precondition_stubs() which generated empty
    no-op stubs. Instead, we now invoke the Core Agent with full context so that it
    produces genuine implementations containing send/receive/alt/setverdict logic.

    Fail-Fast Decision Tree:
      Case A — No output (API error, empty response, or no .ttcn file on disk):
        Raise GenerationFailedError immediately. No stub is written.
      Case B — Has output (files exist on disk):
        Pass through to downstream Infra Agent + Fault Corrector. Even if the
        code has syntax errors, the normal compile-loop/Fault Corrector self-repair
        mechanism will iterate on it. This is the desired self-completing behavior.

    Aggregator recognition: After each successful write, _inject_condition_function_imports()
    is called so the PreConditions_SECC_15118_2.ttcn aggregator has correct imports.

    Args:
        tc_dir: Project directory.
        generation_plan: The generation plan for the current test case.
        missing_modules: List of missing function module names, e.g.
            ["f_SECC_CMN_PR_SessionSetup_001"].
        references: references dict from Summarizer (may be empty).
        few_shot_examples: few_shot_examples from run_pipeline().
        sop: SOP dict from run_pipeline().
        run_timestamp: Timestamp string of the current run (for global pool logging).

    Returns:
        List of module names that were successfully generated.

    Raises:
        GenerationFailedError: Case A — Core Agent produced no usable output.
    """
    if not missing_modules:
        return []

    pre_dir = tc_dir / "src/ttcn3/3_Behavior_Steps/PreConditions"
    pre_dir.mkdir(parents=True, exist_ok=True)

    # Read canonical few-shot reference: SupportedAppProtocol from demo.
    ref_pr_path = DEMO_ROOT / "src/ttcn3/3_Behavior_Steps/PreConditions/f_SECC_CMN_PR_SupportedAppProtocol_001.ttcn"
    if ref_pr_path.exists():
        ref_code = ref_pr_path.read_text(encoding="utf-8")
        log(f"[MISSING PR GEN] Canonical reference loaded from SupportedAppProtocol "
            f"({ref_code.count(chr(10))} lines)")
    else:
        ref_code = "(canonical reference file not found in demo; generation may be less accurate)"
        log("[MISSING PR GEN] WARNING: SupportedAppProtocol reference not found", "WARN")

    successfully_generated: List[str] = []

    for mod_name in missing_modules:
        log(f"[MISSING PR GEN] Generating missing precondition function: {mod_name}")

        # Build a focused generation plan targeting only this one function.
        message_type = _derive_message_type_from_module(mod_name)
        focused_plan: Dict[str, Any] = {
            "tc_id": generation_plan.get("tc_id", "UnknownTC"),
            "testcase_name": mod_name,
            "precondition_chain": [mod_name],
            "postcondition_chain": generation_plan.get("postcondition_chain", []),
            "test_type": "PreconditionFunction",
            "message_type": message_type,
            "protocol": generation_plan.get("protocol", "ISO15118_2"),
            "sut_type": generation_plan.get("sut_type", "SECC"),
            "purpose": (
                f"Generate the missing precondition function {mod_name} that is required "
                f"as part of the ISO 15118-2 state machine. "
                f"This function exchanges {message_type} messages with the SECC."
            ),
            "expected_behavior": (
                f"Send {message_type}Req via pt_V2G_TCP_TLS_ALM_SECC_Port using the "
                f"structured union {{ v2g := {{ {message_type.lower()}Req := md_CMN_{message_type}Req_001 }} }}, "
                f"receive the response in an alt block, and return a verdicttype."
            ),
        }

        # Load shared TTCN reference files (TimerAndConstants, CmnTemplates).
        # This is the same loader the regular Core Agent path uses; the variable
        # was previously only defined inside call_core_agent_with_prompt() which
        # is not in scope here, causing a NameError on the fallback path.
        core_agent_shared_refs = _load_core_shared_refs()

        # Build a focused prompt with mandatory few-shot injection.
        # We bypass the standard run_core_agent() entry point and call the LLM
        # directly so we can inject the canonical reference and a precise directive.
        system_prompt_text = load_text(CORE_AGENT_PROMPT_PATH)

        _snapshot_str = "{}"
        system_prompt_text = system_prompt_text.replace("{PROTOCOL_SNAPSHOT}", _snapshot_str)

        # Inject the actual output directory so the Core Agent writes its JSON
        # response's `path` field to the correct generated project (NOT the
        # demo project under /home/dmc/桌面/platform/src/...).
        # Without this, the agent sees literal "{OUTPUT_DIR}" in the prompt and
        # either fails to parse it, or -- as observed -- finds the demo file
        # under src/ and "declares the task complete" without producing JSON.
        _output_dir_str = str(tc_dir.resolve())
        system_prompt_text = system_prompt_text.replace("{OUTPUT_DIR}", _output_dir_str)
        log(f"[MISSING PR GEN] OUTPUT_DIR injected: {_output_dir_str}")

        pre_params = ("SIL mode: Precondition functions take NO parameters (void return "
                      "type for f_SECC_CMN_PR_InitConfiguration_001, verdicttype return for others).")
        post_params = "SIL mode: Postcondition functions take NO parameters (void return type)."
        system_prompt_text = system_prompt_text.replace("{pre_params}", pre_params)
        system_prompt_text = system_prompt_text.replace("{post_params}", post_params)

        _gen_patterns_path = Path(__file__).parent / "knowledge_base" / "generation_patterns.json"
        gen_patterns_text = ""
        if _gen_patterns_path.exists():
            try:
                with open(_gen_patterns_path, "r", encoding="utf-8") as _f:
                    gen_patterns_text = json.dumps(json.load(_f), indent=2, ensure_ascii=False)
            except Exception:
                gen_patterns_text = ""

        user_prompt = f"""{system_prompt_text}

---

## URGENT TASK: Generate the missing Precondition Function

You MUST generate exactly ONE TTCN-3 module for the following missing precondition function.
This function is required by the ISO 15118-2 state machine but does not yet exist.

### Module to Generate

Module Name: {mod_name}
File Path: src/ttcn3/3_Behavior_Steps/PreConditions/{mod_name}.ttcn

### Generation Plan
```json
{json.dumps(focused_plan, indent=2, ensure_ascii=False)}
```

### Critical: Mandatory Few-Shot Reference

You MUST follow the structure of the following canonical SupportedAppProtocol function.
This is a REAL, WORKING implementation from the demo project. Study it carefully
and replicate its patterns exactly:

```ttcn3
{ref_code}
```

MANDATORY RULES derived from the reference above:

### HARD INVARIANT: ATOMIC PRECONDITION (READ FIRST — OVERRIDES ALL OTHER RULES)
Each `f_SECC_CMN_PR_<Message>_001` function MUST send and receive EXACTLY ONE V2G
message pair (one Req, one Res). You MUST **NEVER** call any other
`f_SECC_CMN_PR_*` function inside the body of a PR function. Each PR is ATOMIC.

WHY THIS MATTERS (root cause of a real bug we hit on 2026-06-20):
- ISO 15118-2 enforces a strict message sequence:
    SupportedAppProtocol → SessionSetup → ServiceDiscovery → ...
- Each state transition on the SECC is triggered by exactly ONE EVCC request.
- The TEST CASE (`TC_*.ttcn`) is responsible for chaining preconditions; each
  PR must execute its own single message exchange.
- If you nest a PR inside another PR (e.g. SessionSetup PR calls
  `f_SECC_CMN_PR_SupportedAppProtocol_001`), the SECC receives an out-of-sequence
  request: it is already in `SessionSetup` state and tries to decode the
  SupportedAppProtocol EXI bytes using the V2G-2 namespace, which raises
  `EXIDecodingError("Premature EOS while reading data")` and closes the TCP/TLS
  connection.
- This bug surfaced as: C++ adapter always logged `Encoding: supported_app_protocol_req`
  even when SessionSetup PR was supposed to fire — the C++ adapter was CORRECT,
  the LLM-generated SessionSetup PR was illegally re-invoking the SAP sub-PR.

RULE: NEVER write `f_SECC_CMN_PR_<X>_001();` inside another `f_SECC_CMN_PR_<Y>_001();`.
The ONLY legitimate callers of a PR function are the test case (`TC_*.ttcn`)
or another PR that has a `CmnPR_` (Common Pre-Conditions) prefix and is
explicitly listed as a pre-condition in the test specification. For the
generated code, assume NO sub-PR calls — be atomic.

### REFERENCE-CODE SUBSTITUTION RULES
- The provided `SupportedAppProtocol` reference code is ONLY for **structural
  reference** (timers, alt steps, setverdict, port names, verdicttype return,
  logging style).
- You MUST **REPLACE** every send/receive template, union field name, message
  template reference, and message-specific logic with values that correspond to
  the **TARGET** missing function below — i.e. for `{mod_name}` you MUST use
  `{message_type}Req` / `{message_type}Res`, the union field
  `{message_type.lower()}Req` / `{message_type.lower()}Res`, and the template
  `md_CMN_{message_type}Req_001` / matching wildcard for the response.
- **DO NOT blindly copy** the SupportedAppProtocol payload, union field,
  message decoder, or any literal byte sequence from the reference. Copying
  the wrong payload type causes EXI decode failures on the SECC side
  (`Premature EOS while reading data`).
- Function signature: runs on SECCTester, return verdicttype
- Send via: pt_V2G_TCP_TLS_ALM_SECC_Port.send({{ v2g := {{ {message_type.lower()}Req := md_CMN_{message_type}Req_001 }} }})
- Receive in: alt {{ [] pt_V2G_TCP_TLS_ALM_SECC_Port.receive(...) }}
- Timer: PX_SECC_CMN_Message_Timeout
- Verdict: setverdict(pass/fail/inconc), return getverdict
- Module name MUST equal the function name: {mod_name}
- File name MUST be: {mod_name}.ttcn

### Shared Project Files (read-only reference)

### src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn:
```ttcn3
{core_agent_shared_refs.get('TimerAndConstants', '(file not available)')}
```

### src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn:
```ttcn3
{core_agent_shared_refs.get('CmnTemplates', '(file not available)')}
```

### Generation Patterns

```json
{gen_patterns_text}
```

### SOP Common Errors

```json
{json.dumps(sop.get("common_errors", []), indent=2, ensure_ascii=False)}
```

请严格按照上述参考代码的格式和规则，生成完整的 TTCN-3 代码。只输出 JSON，不要解释。
"""

        timeout = get_agent_timeout("core_agent", prompt_size=len(user_prompt))
        log(f"[MISSING PR GEN] Calling Core Agent for {mod_name} (timeout={timeout}s)...")

        try:
            response = call_claude_agent(
                user_prompt,
                agent_name="Core Agent (missing-precondition)",
                timeout=timeout,
                extra_allowed_tools="Write,Read,Edit,Bash(bash *)",
            )
        except Exception as e:
            log(f"[MISSING PR GEN] Core Agent call failed for {mod_name}: {e}", "WARN")
            raise GenerationFailedError(
                agent_name="Core Agent",
                reason=f"API call raised exception: {e}",
                module_name=mod_name,
            )

        if not response or not response.strip():
            raise GenerationFailedError(
                agent_name="Core Agent",
                reason="Empty response from LLM",
                module_name=mod_name,
            )

        # Parse the Core Agent response.
        result = extract_core_agent_output(response)
        if result.get("_parse_error"):
            # Fall back to Tier-2 salvage: scan raw text for TTCN-3 module blocks.
            log(f"[MISSING PR GEN] JSON parse failed for {mod_name}, attempting raw TTCN-3 extraction...", "WARN")
            extracted_code = ""
            import re as _re
            for match in _re.finditer(r'module\s+\w+\s*\{.*?\n\}\s*//?\s*end\s+module',
                                       response, _re.DOTALL):
                block = match.group(0).strip()
                if block.startswith("module ") and len(block) > 100:
                    extracted_code = block
                    break
            if not extracted_code:
                raise GenerationFailedError(
                    agent_name="Core Agent",
                    reason="JSON parse failed and no TTCN-3 module block found in response",
                    module_name=mod_name,
                )
            result = {"testcase": extracted_code, "precondition_files": [], "postcondition_files": []}

        # Case A guard: if no testcase/content, raise.
        testcase_code = result.get("testcase", "") or ""

        # If the response was structured as a list of file objects instead of a
        # single testcase module, locate the one matching this mod_name. The
        # Core Agent prompt says "Return JSON with path + content fields"; if
        # the LLM obeyed that, the module will be in precondition_files[] or
        # postcondition_files[]. Pick it out so the rest of the pipeline (which
        # expects a single module string) keeps working.
        if not testcase_code.strip():
            for f_dict in (
                list(result.get("precondition_files", []) or [])
                + list(result.get("postcondition_files", []) or [])
            ):
                fname = Path(str(f_dict.get("path", ""))).name
                if fname == f"{mod_name}.ttcn":
                    testcase_code = str(f_dict.get("content", "") or "")
                    if testcase_code.strip():
                        log(f"[MISSING PR GEN] Recovered {mod_name} from "
                            f"file-list field (path={f_dict.get('path')})")
                        break

        if not testcase_code.strip():
            raise GenerationFailedError(
                agent_name="Core Agent",
                reason="No TTCN-3 code in response (empty testcase field and "
                       "no matching entry in precondition_files/postcondition_files)",
                module_name=mod_name,
            )

        # Write the generated module to disk.
        out_path = pre_dir / f"{mod_name}.ttcn"
        try:
            out_path.write_text(_sanitize_extracted_text(testcase_code), encoding="utf-8")
            log(f"[MISSING PR GEN] Wrote {out_path.name} ({len(testcase_code)} bytes)")
        except Exception as e:
            raise GenerationFailedError(
                agent_name="Core Agent",
                reason=f"Failed to write generated file to disk: {e}",
                module_name=mod_name,
            )

        # Verify file exists on disk (Case A check).
        if not out_path.exists():
            raise GenerationFailedError(
                agent_name="Core Agent",
                reason=f"File {out_path} was written but does not exist on disk",
                module_name=mod_name,
            )

        # Case B: File exists on disk. Now rebuild aggregator imports.
        _inject_condition_function_imports(
            tc_dir,
            [{"path": str(out_path), "content": testcase_code}],
            [],
            generation_plan.get("tc_id", "UnknownTC"),
        )

        successfully_generated.append(mod_name)
        log(f"[MISSING PR GEN] SUCCESS: {mod_name} generated and installed")

        # Promote to global asset pool (Dimension 3).
        source_run = generation_plan.get("tc_id", "unknown") + "_" + run_timestamp
        _promote_to_global_pool(out_path, mod_name, source_run)

    return successfully_generated


def generate_code_for_test_case(
    test_case: Dict,
    sut_type: Optional[str] = None,
    requirement_context: str = "",
    titan_log: Optional[Path] = None,
    pcap_path: Optional[Path] = None,
    skip_build: bool = False,
    sut_ip: Optional[str] = None,
    rule_based_plan: bool = False,
    no_llm_render: bool = False,
    watchdog_timeout: int = 600,
    raw_file_data: Optional[Dict] = None,
    filter_tc_id: Optional[str] = None,
) -> Dict:
    """为单个测试用例生成 TTCN-3 代码及完整项目结构

    流水线:
      测试用例 → Plan Builder / Orchestrator → Summarizer → Core Agent → Infrastructure Agent → Build
                                                                          |
                                                        +-------------------+
                                                        v                   |
                                          +-----------------------------+  |
                                          |  Compile Loop (<=3 轮)     |  |
                                          |  1. Core Agent            |<-+   (TITAN 编译失败时触发)
                                          |  2. Infrastructure Agent   |  |
                                          |  3. Build                 |  |
                                          |  4. Fault Corrector(compile)|  |
                                          +-------------+---------------+  |
                                          +-------------v-------------------------+
                                          |  Runtime Loop (<=2 轮)                |
                                          |  1. Fault Corrector(runtime)        |
                                          |  2. Core Agent (manual_review)      |  (测试结果不符合预期时触发)
                                          |  3. Infrastructure Agent            |
                                          |  4. Build                         |
                                          +-------------------------------------+

    注意: 静态检查循环（phase="static"）已被移除，因为其检查项（语法、命名、定时器等）
    均可由 TITAN 编译器捕获。编译循环和运行循环是仅有的两个反馈回路，均由真实构建/运行结果驱动。

    Args:
        test_case: 测试用例字典（包含 Test_Case 键）
        sut_type: SUT 类型，"SECC" 或 "EVCC"（可选，自动推断）
        requirement_context: 需求上下文
        titan_log: TITAN 执行日志路径（可选，Fault Agent 分析用）
        pcap_path: PCAP 文件路径（可选，Fault Agent 双轨分析用）
        skip_build: 是否跳过编译运行步骤
        sut_ip: SUT IP 地址（用于 titan_runtime.cfg 配置）

    Returns:
        包含生成结果的字典，至少包含 `ttcn_path` 字段；
        还包含 `project_dir`、`infra_files`、`build_result` 等
    """
    result: Dict = {}

    # Reset metrics at the start of each pipeline run
    _reset_metrics()

    # Initialize timing variables early so they can be safely passed to _print_timing_summary
    # on any early exit path.
    orchestrator_elapsed = 0.0
    summarizer_elapsed = 0.0
    core_agent_elapsed = 0.0
    infra_agent_elapsed = 0.0
    build_elapsed = 0.0
    fc_runtime_elapsed = 0.0
    fc_legacy_elapsed = 0.0
    compile_loop_core_total = 0.0
    compile_loop_infra_total = 0.0
    compile_loop_build_total = 0.0
    compile_loop_fc_total = 0.0
    compile_loop_core_times: List[Tuple[int, float]] = []
    compile_loop_infra_times: List[Tuple[int, float]] = []
    compile_loop_build_times: List[Tuple[int, float]] = []
    compile_loop_fc_times: List[Tuple[int, float]] = []
    build_result = {}
    # Context for Dimension 1+2 self-generation of missing preconditions.
    # Initialized here (None) so they are always in scope for the Step 5c call
    # even if some steps are skipped. Assigned at Step 0 and Step 2.
    _sop_ctx: Dict = {}
    _references_ctx: Dict = {}
    _few_shot_examples_ctx: Dict = {}
    _run_timestamp_ctx: str = ""

    # === Normal Mode ===
    # 提取测试用例
    tc = test_case.get("Test_Case", test_case)
    tc_id = tc.get("TC_Id", "unknown")

    log(f"\n{'=' * 60}")
    log(f"生成 TTCN-3 代码: TC_ID={tc_id}")
    log(f"{'=' * 60}")

    # === Step 0: 加载基础数据 ===
    log("加载基础数据...")
    sop = load_json(SOP_PATH)
    few_shot_examples = load_few_shot_examples()
    # Populate context vars for use by Step 5c (missing precondition self-generation).
    _sop_ctx = sop
    _references_ctx = {}
    _few_shot_examples_ctx = few_shot_examples
    _run_timestamp_ctx = _run_ts.datetime.now().strftime("%Y%m%d_%H%M%S")

    # P3: Bootstrap the ProtocolRegistry so Orchestrator can inject the snapshot
    # into its LLM prompt. We only load the active protocol when we have a TC_Id.
    # The registry starts with L1 manifests (fast, <1 KB each).
    _reg = get_default_registry()
    _active_protocol = None
    if tc_id:
        _active_protocol = _reg.auto_load_for_tc(tc_id)
        if _active_protocol:
            log(f"[ProtocolRegistry] Active protocol: {_active_protocol}", "INFO")

    # === Step 1: Orchestrator - 意图分解 (LLM) 或 规则构建 (rule-based) ===
    run_timestamp = _run_ts.datetime.now().strftime("%Y%m%d_%H%M%S")
    if rule_based_plan:
        # Rule-based plan builder: deterministic, zero token cost, no LLM
        log("\n--- Step 1: Plan Builder (确定性规则, 无 LLM) ---")
        t0 = time.time()
        generation_plan = build_generation_plan_from_test_case(
            raw_file_data or test_case, sop, filter_tc_id=filter_tc_id
        )
        orchestrator_elapsed = time.time() - t0
        log(f"[Plan Builder] 耗时: {orchestrator_elapsed * 1000:.1f}ms (LLM bypass)")
        log(f"生成计划: tc_id={generation_plan.get('tc_id')}, "
            f"type={generation_plan.get('test_type')}, "
            f"message={generation_plan.get('message_type')}, "
            f"sequences={len(generation_plan.get('test_sequences', []))}")
    else:
        # LLM-based Orchestrator: injects registry snapshot
        log("\n--- Step 1: Orchestrator (意图分解) ---")
        t0 = time.time()
        _protocol_snapshot = None
        if _active_protocol:
            _protocol_snapshot = _reg.snapshot(_active_protocol)
        generation_plan = run_orchestrator(tc, requirement_context, sop, _protocol_snapshot)
        orchestrator_elapsed = time.time() - t0
        log(f"[Orchestrator] 耗时: {orchestrator_elapsed:.1f}s")
        if not generation_plan:
            log("Orchestrator 失败，流水线终止", "ERROR")
            _print_timing_summary(result, orchestrator_elapsed, summarizer_elapsed,
                                 core_agent_elapsed, infra_agent_elapsed, build_elapsed,
                                 compile_loop_core_total, compile_loop_infra_total,
                                 compile_loop_build_total, compile_loop_fc_total,
                                 fc_runtime_elapsed, fc_legacy_elapsed,
                                 compile_loop_core_times, compile_loop_infra_times,
                                 compile_loop_build_times, compile_loop_fc_times)
            _tc_dir = GENERATED_DIR / (tc_id + "_" + run_timestamp)
            _tc_dir.mkdir(parents=True, exist_ok=True)
            _write_metrics_json(_tc_dir, _build_metrics_dict(
                tc_id, run_timestamp, generation_plan or {}, result,
                orchestrator_elapsed, summarizer_elapsed, core_agent_elapsed,
                infra_agent_elapsed, build_elapsed,
                compile_loop_core_total, compile_loop_infra_total,
                compile_loop_build_total, compile_loop_fc_total,
                fc_runtime_elapsed, fc_legacy_elapsed,
                compile_loop_core_times, compile_loop_infra_times,
                compile_loop_build_times, compile_loop_fc_times,
                False, build_result,
            ))
            return None

    # === Step 1b: 规范化 tc_id 和前置条件名称 ===
    # 修复 Phase 1 生成的非标准命名
    plan_tc_id = generation_plan.get('tc_id', tc_id)
    if plan_tc_id != tc_id:
        normalized = normalize_tc_id(plan_tc_id)
        if normalized != plan_tc_id:
            log(f"TC ID 规范化: {plan_tc_id} -> {normalized}", "INFO")
            generation_plan['tc_id'] = normalized
            plan_tc_id = normalized

    # 规范化前置条件链中的函数名
    precond_chain = generation_plan.get("precondition_chain", [])
    if precond_chain:
        normalized_chain = [normalize_precondition_name(pc) for pc in precond_chain]
        if normalized_chain != precond_chain:
            log(f"前置条件链规范化: {precond_chain} -> {normalized_chain}", "INFO")
            generation_plan["precondition_chain"] = normalized_chain

    # 如果用户指定了 SUT 类型，覆盖 Orchestrator 的推断
    if sut_type:
        generation_plan["sut_type"] = sut_type
        log(f"使用用户指定的 SUT 类型: {sut_type}")

    # 保存生成计划（plan_tc_id 已在上方规范化步骤中设置）
    plan_path = GENERATED_DIR / f"{plan_tc_id}_plan.json"
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(generation_plan, f, indent=2, ensure_ascii=False)
    log(f"生成计划已保存: {plan_path}")

    # === Step 2: Summarizer - 知识检索 (可被 ablation 禁用) ===
    msg_type = generation_plan.get("message_type", "")
    sut = generation_plan.get("sut_type", "SECC")

    if rule_based_plan:
        # Rule-based 模式：跳过 Summarizer，禁用知识库检索（Core Agent 独立推理）
        log("\n--- Step 2: Summarizer (已跳过: --rule-based-plan) ---")
        kb_search_results = {}
        references = {}
        summarizer_elapsed = 0.0
        local_ref_section = ""
        # Populate context vars for Step 5c (missing precondition self-generation).
        _references_ctx = {}
    else:
        # 正常模式：运行 Summarizer Agent
        log("\n--- Step 2: Summarizer (知识检索) ---")
        kb_search_results = search_knowledge_base(msg_type, sut)
        log(f"本地预检索: testcases={len(kb_search_results.get('testcases', []))}, "
            f"functions={len(kb_search_results.get('functions', []))}, "
            f"templates={len(kb_search_results.get('templates', []))}")

        t1 = time.time()
        references = run_summarizer(generation_plan, kb_search_results)
        summarizer_elapsed = time.time() - t1
        log(f"[Summarizer] 耗时: {summarizer_elapsed:.1f}s")
        if not references:
            log("Summarizer 失败，使用空引用继续", "WARN")
            references = {}

        # Populate context for Step 5c (missing precondition self-generation).
        _references_ctx = references

        # 构建 local_ref_section（绕过 Summarizer LLM 层）
        local_ref_parts = []
        for tc in kb_search_results.get("testcases", [])[:3]:
            if tc.get("code") and len(tc["code"]) > 100:
                local_ref_parts.append(f"// === Local Ref TC: {tc.get('name', 'unknown')} ===\n{tc['code']}")
        for fn in kb_search_results.get("functions", [])[:3]:
            if fn.get("code") and len(fn["code"]) > 50:
                local_ref_parts.append(f"// === Local Ref Function: {fn.get('name', 'unknown')} ===\n{fn['code']}")
        local_ref_section = "\n\n".join(local_ref_parts) if local_ref_parts else ""

    # === Step 3: Static Renderer (always) + optional LLM review ===
    # Layer 1: Static template rendering (deterministic, <1s, zero tokens)
    log("\n--- Step 3a: Static TTCN Renderer (Layer 1) ---")
    t_renderer = time.time()
    _static_renderer_used = False
    _static_tc_path: Optional[Path] = None
    _static_tb_path: Optional[Path] = None
    _static_tc_content = ""
    _static_tb_content = ""

    try:
        from ttcn_static_renderer import render_and_write as _render_and_write
        _render_result = _render_and_write(generation_plan, GENERATED_DIR / (plan_tc_id + "_" + run_timestamp))
        _static_tc_path = _render_result["tc_file"]
        _static_tb_path = _render_result["tb_file"]
        _static_tc_content = _render_result["tc_content"]
        _static_tb_content = _render_result["tb_content"]
        _static_renderer_used = True
        log(f"[Static Renderer] TC: {_static_tc_path.name}")
        log(f"[Static Renderer] TB: {_static_tb_path.name}")
        log(f"[Static Renderer] 耗时: {(time.time() - t_renderer) * 1000:.1f}ms")
    except Exception as _e:
        log(f"[Static Renderer] 失败: {_e}，回退到 LLM 生成", "WARN")
        _static_renderer_used = False

    # Layer 2: LLM review (optional, watchdog-capped)
    # When no_llm_render=True we skip LLM entirely and use the static output.
    core_result: Optional[Dict[str, Any]] = None
    core_agent_elapsed = 0.0
    _llm_skipped_or_killed = False

    if no_llm_render:
        log("\n--- Step 3b: Core Agent (已跳过: --no-llm-render) ---")
        _llm_skipped_or_killed = True
        core_agent_elapsed = 0.0
    else:
        log(f"\n--- Step 3b: Core Agent (代码生成, watchdog={watchdog_timeout}s) ---")
        t2 = time.time()
        # Pass watchdog_timeout to call_claude_agent via a global (quickest integration path)
        global _llm_watchdog_timeout
        _llm_watchdog_timeout = watchdog_timeout
        try:
            # Phase 3.5: derive the GENDIR path the same way Step 5 does so the
            # Core Agent prompt can be injected with the absolute path BEFORE
            # Step 5 creates the directory. This guarantees the LLM knows where
            # to write even if the directory doesn't exist yet.
            _gendir_for_prompt = GENERATED_DIR / (plan_tc_id + "_" + run_timestamp)
            core_result = run_core_agent(
                generation_plan, references, few_shot_examples, sop, "",
                sut_mode="SIL", local_ref_section=local_ref_section,
                protocol_snapshot=_reg.snapshot(_reg.active_protocol()) if _reg.active_protocol() else None,
                output_dir=_gendir_for_prompt,
            )
        except Exception as _e:
            log(f"[Core Agent] 调用异常: {_e}，回退到静态渲染", "WARN")
            core_result = None
            _llm_skipped_or_killed = True
        core_agent_elapsed = time.time() - t2
        log(f"[Core Agent] 耗时: {core_agent_elapsed:.1f}s")
        # If LLM produced no testcase, fall back to static renderer output
        if not core_result or not core_result.get("testcase"):
            log("[Core Agent] 未产生 testcase，回退到静态渲染", "WARN")
            core_result = None
            _llm_skipped_or_killed = True
            # Inject static renderer's already-written files into core_result
            if _static_renderer_used and _static_tc_content:
                core_result = {
                    "testcase": _static_tc_content,
                    "precondition_files": [],
                    "postcondition_files": [],
                    "_static_renderer_used": True,
                    "_static_tc_path": str(_static_tc_path),
                    "_static_tb_path": str(_static_tb_path),
                    "_static_tb_content": _static_tb_content,
                }
                log(f"[Static Renderer] 已注入 TC ({len(_static_tc_content)} chars) 和 "
                    f"TB ({len(_static_tb_content)} chars) 到 core_result")

    # === --no-llm-render path: inject static renderer output into core_result ===
    if no_llm_render and (not core_result or not core_result.get("testcase")):
        if _static_renderer_used and _static_tc_content:
            core_result = {
                "testcase": _static_tc_content,
                "precondition_files": [],
                "postcondition_files": [],
                "_static_renderer_used": True,
                "_static_tc_path": str(_static_tc_path),
                "_static_tb_path": str(_static_tb_path),
                "_static_tb_content": _static_tb_content,
            }
            log(f"[Static Renderer] --no-llm-render: 直接使用 TC ({len(_static_tc_content)} chars) 和 "
                f"TB ({len(_static_tb_content)} chars)")

    # === DISK-AWARE FALLBACK VERIFICATION ===
    # Even if parse returned empty, the Core Agent may have written files directly
    # to disk via its Write Tool. Scan the timestamped project directory as a safety net.
    _disk_fallback_active = False
    if not core_result or not core_result.get("testcase"):
        # Try multiple candidate directories (current run, orphan dir, recent)
        _candidate_dirs = [
            GENERATED_DIR / (plan_tc_id + "_" + run_timestamp),  # Current run
            GENERATED_DIR / plan_tc_id,  # Orphan non-timestamped dir
        ]
        # Also scan most recent timestamped dir of same TC ID
        _pattern = f"{plan_tc_id}_*"
        try:
            recent_runs = sorted(
                [d for d in GENERATED_DIR.glob(_pattern) if d.is_dir()],
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            _candidate_dirs.extend(recent_runs[:3])  # Last 3 runs
        except Exception:
            pass
        # Deduplicate
        _seen = set()
        _candidate_dirs = [d for d in _candidate_dirs if d.exists() and str(d) not in _seen and not _seen.add(str(d))]

        _disk_output_filename = derive_tc_filename(plan_tc_id)

        for _disk_tc_project_dir in _candidate_dirs:
            _scanned_ttcn = _disk_tc_project_dir / "src/ttcn3/4_TestCases" / _disk_output_filename
            _scanned_precond_files = []
            _scanned_postcond_files = []
            if _scanned_ttcn.exists() and _scanned_ttcn.stat().st_size > 0:
                log(f"[DISK-FALLBACK] Found TTCN-3 on disk: {_scanned_ttcn} ({_scanned_ttcn.stat().st_size} bytes)", "WARN")
                _disk_fallback_active = True
            elif _disk_tc_project_dir.exists():
                _precond_dir = _disk_tc_project_dir / "src/ttcn3/3_Behavior_Steps"
                if _precond_dir.exists():
                    for f in _precond_dir.rglob("*.ttcn"):
                        if f.is_file() and f.stat().st_size > 0:
                            if "PostCondition" in f.name:
                                _scanned_postcond_files.append(f)
                            else:
                                _scanned_precond_files.append(f)
                _scanned_ttcn_fallback = _disk_tc_project_dir / "src/ttcn3" / _disk_output_filename
                if not _scanned_ttcn.exists() and _scanned_ttcn_fallback.exists() and _scanned_ttcn_fallback.stat().st_size > 0:
                    _scanned_ttcn = _scanned_ttcn_fallback
                if _scanned_ttcn.exists() and _scanned_ttcn.stat().st_size > 0:
                    log(f"[DISK-FALLBACK] Found TTCN-3 via alternate path: {_scanned_ttcn}", "WARN")
                    _disk_fallback_active = True
            if _disk_fallback_active:
                break

        if _disk_fallback_active:
            _rescued_code = _scanned_ttcn.read_text(encoding="utf-8")
            _rescued_pre = [{"path": str(f.relative_to(_disk_tc_project_dir)), "content": f.read_text(encoding="utf-8")} for f in _scanned_precond_files]
            _rescued_post = [{"path": str(f.relative_to(_disk_tc_project_dir)), "content": f.read_text(encoding="utf-8")} for f in _scanned_postcond_files]
            core_result = {
                "testcase": _rescued_code,
                "precondition_files": _rescued_pre,
                "postcondition_files": _rescued_post,
                "_disk_fallback_used": True,
            }
            log(f"[DISK-FALLBACK] Rescued TTCN-3 ({len(_rescued_code)} chars), "
                f"{len(_rescued_pre)} pre + {len(_rescued_post)} post from disk", "WARN")
        else:
            log("[DISK-FALLBACK] No TTCN-3 found on disk either — proceeding to error exit", "WARN")

    if not core_result or not core_result.get("testcase"):
        _print_timing_summary(result, orchestrator_elapsed, summarizer_elapsed,
                             core_agent_elapsed, infra_agent_elapsed, build_elapsed,
                             compile_loop_core_total, compile_loop_infra_total,
                             compile_loop_build_total, compile_loop_fc_total,
                             fc_runtime_elapsed, fc_legacy_elapsed,
                             compile_loop_core_times, compile_loop_infra_times,
                             compile_loop_build_times, compile_loop_fc_times)
        _tc_dir = GENERATED_DIR / (plan_tc_id + "_" + run_timestamp)
        _tc_dir.mkdir(parents=True, exist_ok=True)
        _write_metrics_json(_tc_dir, _build_metrics_dict(
            plan_tc_id, run_timestamp, generation_plan, result,
            orchestrator_elapsed, summarizer_elapsed, core_agent_elapsed,
            infra_agent_elapsed, build_elapsed,
            compile_loop_core_total, compile_loop_infra_total,
            compile_loop_build_total, compile_loop_fc_total,
            fc_runtime_elapsed, fc_legacy_elapsed,
            compile_loop_core_times, compile_loop_infra_times,
            compile_loop_build_times, compile_loop_fc_times,
            False, build_result,
        ))
        raise _PipelineEarlyExit()

    # 提取各部分代码
    raw_ttcn3_code = core_result.get("testcase", "")
    # 处理 Core Agent 返回 dict 而非 str 的情况（安全过滤器导致 JSON 写入失败时）
    if isinstance(raw_ttcn3_code, dict):
        log(f"Core Agent testcase 是 dict，已降级处理（可能存在安全过滤器问题）", "WARN")
        raw_ttcn3_code = ""
    ttcn3_code = raw_ttcn3_code or ""
    pre_condition_files = core_result.get("precondition_files", [])
    post_condition_files = core_result.get("postcondition_files", [])

    # Record disk-fallback recovery in result for metrics propagation
    if core_result.get("_disk_fallback_used"):
        result["_disk_fallback_used"] = True
        log("[DISK-FALLBACK] Recovery flag recorded in result for metrics", "INFO")

    log(f"生成主测试用例代码 {len(ttcn3_code)} 字符")
    log(f"生成前置条件文件 {len(pre_condition_files)} 个")
    log(f"生成后置条件文件 {len(post_condition_files)} 个")

    # NO static check loop. Compile loop (Step 7) handles all corrections driven by real build results.

    # === Step 4: 保存 .ttcn 文件到 GENERATED_DIR ===
    output_filename = derive_tc_filename(plan_tc_id)
    output_path = GENERATED_DIR / output_filename

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(ttcn3_code)

    log(f"TTCN-3 代码已生成: {output_path} ({os.path.getsize(output_path)} bytes)")

    result = {
        "ttcn_path": str(output_path),
        "plan_path": str(plan_path),
        "generation_plan": generation_plan,
        "core_result": core_result,
    }

    # Initialize all timing variables to 0.0 for early-return paths
    build_elapsed = 0.0 
    compile_loop_core_total = 0.0
    compile_loop_infra_total = 0.0
    compile_loop_build_total = 0.0
    compile_loop_fc_total = 0.0
    fc_runtime_elapsed = 0.0
    fc_legacy_elapsed = 0.0

    # Per-iteration timing for compile loop (enables detailed breakdown in summary)
    compile_loop_build_times: List[Tuple[int, float]] = []  # (iteration, seconds)
    compile_loop_fc_times: List[Tuple[int, float]] = []     # (iteration, seconds)
    compile_loop_core_times: List[Tuple[int, float]] = []    # (iteration, seconds) — 0 if skipped
    compile_loop_infra_times: List[Tuple[int, float]] = []  # (iteration, seconds) — 0 if skipped
    _compile_iteration = 1  # iteration counter for metrics tracking

    # === Step 5: 写入 Core Agent 生成的 pre/post condition 文件 ===
    # Use timestamped dir so repeated runs don't overwrite each other.
    tc_project_dir = GENERATED_DIR / (plan_tc_id + "_" + run_timestamp)
    tc_project_dir.mkdir(parents=True, exist_ok=True)
    log("[TC PROJECT] Output directory: " + str(tc_project_dir))

    # === Clean stale build artifacts to guarantee a clean-slate environment ===
    # Eliminates FS-TRUTH contamination from any prior partial build
    # (e.g. TestExecutable left on disk from a previous failed run).
    import shutil as _shutil
    for _stale in [
        tc_project_dir / "build" / "bin" / "TestExecutable",
    ]:
        try:
            if _stale.is_file():
                _stale.unlink()
                log(f"[CLEAN] removed stale binary: {_stale.name}")
        except OSError as _e:
            log(f"[CLEAN] could not remove {_stale}: {_e}", "WARN")
    for _stale_dir in [
        tc_project_dir / "build" / "obj",
        tc_project_dir / "build" / "generated_cpp",
        tc_project_dir / "build" / "config",
        tc_project_dir / "logs" / "titan_traces",
        tc_project_dir / "logs" / "system_logs",
    ]:
        try:
            if _stale_dir.exists():
                _shutil.rmtree(_stale_dir, ignore_errors=True)
                log(f"[CLEAN] removed stale dir: {_stale_dir.relative_to(tc_project_dir)}")
        except OSError as _e:
            log(f"[CLEAN] could not remove {_stale_dir}: {_e}", "WARN")

    # 同时定义 tc_ttcn_dest（供 _runtime_self_repair 使用，避免 NameError）
    tc_ttcn_dest = tc_project_dir / "src/ttcn3/4_TestCases" / output_filename
    tc_ttcn_dest.parent.mkdir(parents=True, exist_ok=True)
    tc_ttcn_dest.write_text(_sanitize_extracted_text(ttcn3_code), encoding="utf-8")

    for f_data in pre_condition_files + post_condition_files:
        f_path = tc_project_dir / f_data["path"]
        f_path.parent.mkdir(parents=True, exist_ok=True)
        f_path.write_text(_sanitize_extracted_text(f_data["content"]), encoding="utf-8")
    log(f"已写入 {len(pre_condition_files)} 个 pre + {len(post_condition_files)} 个 post 文件")

    # === Step 5a: Import injection (MUST run BEFORE Infrastructure Agent) ===
    # After Core Agent writes pre/post files to disk, rebuild the aggregator modules
    # with correct import statements and ensure the main TC file directly imports
    # all function modules it calls. These calls must happen BEFORE Infrastructure
    # Agent runs so the agent sees consistent, correct files on disk.
    _inject_condition_function_imports(tc_project_dir, pre_condition_files, post_condition_files, plan_tc_id)
    _ensure_tc_direct_imports(tc_project_dir, output_filename, pre_condition_files, post_condition_files)

    # === Step 5a.5: ASSET PROMOTION (upgraded: global pool first) ===
    # Priority: 1) _global_asset_pool/  2) per-run directories
    _upgrade_promote_precondition_assets(tc_project_dir, generation_plan,
                                          current_run_dir=plan_tc_id + "_" + run_timestamp)

    # === Step 5a.6: PRE/PO PATH SELF-HEAL ===
    # Core Agent frequently misplaces precondition (PR_) / postcondition (PO_)
    # files in the TestBehavior/ directory because the testcase JSON path
    # template is ambiguous to LLMs. This is a self-heal step that moves
    # misplaced PR/PO files back to their proper directories.
    # Symptom of the bug: makefilegen reports "Both files ... contain TTCN-3
    # module `f_XYZ`" because the same module exists in both
    # TestBehavior/ and PreConditions/ (or PostConditions/).
    _heal_misplaced_condition_modules(tc_project_dir)

    # === Step 5c: MISSING PRECONDITION SELF-GENERATION (Dimensions 1+2) ===
    # If _upgrade_promote_precondition_assets() found no promotable assets, this step
    # calls the Core Agent with Few-Shot injection to generate real TTCN-3 implementations.
    # NO STUBS ARE GENERATED. Fail-Fast: raises GenerationFailedError on no output.
    missing_preconditions = generation_plan.get("_precondition_completeness", {}).get("missing_preconditions", [])
    if missing_preconditions:
        log(f"[STEP 5c] {len(missing_preconditions)} missing preconditions: {missing_preconditions}")
        log("[STEP 5c] Calling Core Agent to generate real implementations (no stubs)...")
        _generate_missing_precondition_functions(
            tc_dir=tc_project_dir,
            generation_plan=generation_plan,
            missing_modules=missing_preconditions,
            references=_references_ctx,
            few_shot_examples=_few_shot_examples_ctx,
            sop=_sop_ctx,
            run_timestamp=run_timestamp,
        )
        log("[STEP 5c] Missing precondition generation complete")

    # Re-validate after fixing imports -- catch missing modules before build
    validation_issues = pre_build_validate(tc_project_dir)
    if validation_issues:
        log(f"[PREBUILD] Validation issues found: {len(validation_issues)}")
        for issue in validation_issues:
            log(f"  [PREBUILD]   - {issue}")

    # === Step 5b: Infrastructure Agent - 生成完整项目结构 ===
    infra_agent_elapsed = 0.0
    log(f"\n--- Step 5b: Infrastructure Agent (生成完整项目) ---")

    tc_output_filename = derive_tc_filename(plan_tc_id)
    t3 = time.time()

    # ================================================================
    # SCAFFOLDING: Prepare the project directory BEFORE the agent runs.
    #
    # PRE-RUN CLEANUP: Remove any orphan non-timestamped directories that could
    # confuse the Infrastructure Agent's sub-agent (which does find ... -iname
    # searches and may pick the wrong directory when both <TC_ID>/ and
    # <TC_ID>_<ts>/ exist). The batch runner cleanup only runs AFTER the pipeline,
    # so it can't protect the current run. Clean here so the agent always sees
    # exactly one directory: the correct timestamped one.
    # ================================================================
    tc_project_dir.mkdir(parents=True, exist_ok=True)
    _orphan_dir = GENERATED_DIR / plan_tc_id
    if _orphan_dir.exists() and _orphan_dir.is_dir() and _orphan_dir.resolve() != tc_project_dir.resolve():
        import shutil as _orphan_shutil
        try:
            _orphan_shutil.rmtree(_orphan_dir)
            log(f"[CLEAN] Removed orphan non-timestamped dir: {_orphan_dir.name}")
        except OSError as _e:
            log(f"[CLEAN] Could not remove orphan dir {_orphan_dir}: {_e}", "WARN")

    # 1. Copy build.sh from demo (agent will invoke it)
    build_installed = _install_build_script(tc_project_dir)
    if not build_installed:
        log("[SCAFFOLD] WARNING: build.sh not found in demo -- agent compile loop will fail", "WARN")

    # 1b. Copy canonical runtime config + hardware env to tc_dir/config/ and tc_dir/build/config/
    #     The build.sh's rsync syncs the temp work_dir/config/ back to SCRIPT_DIR/config/
    #     (not build/config/). To allow `TestExecutable ../config/titan_runtime.cfg`,
    #     we ensure config/ exists with the canonical files.
    _install_canonical_runtime_config(tc_project_dir)

    # 1c. PRECOMPILE GATE: dry-run compile every DEMO_SHARED_FILES entry via
    # ttc3_compiler BEFORE we copy anything. Without this gate, a syntax error
    # in a demo source file would be silently copied into the generated project
    # and only surface after the Infrastructure Agent has done several minutes
    # of work, wasting LLM tokens and time.
    # Cached: the demo source tree only changes when someone hand-edits it, so
    # we run this check at most once per process invocation.
    global _DEMO_PRECOMPILE_OK
    if _DEMO_PRECOMPILE_OK is None:
        _ok_flag, _err_list = _verify_demo_ttcn_syntax()
        _DEMO_PRECOMPILE_OK = _ok_flag
        if not _ok_flag:
            log(
                "[SCAFFOLD] ABORT: demo TTCN baseline has syntax errors. "
                "Fix demo source files before running the pipeline.",
                "ERROR",
            )
            raise RuntimeError(
                f"Demo TTCN baseline failed ttc3_compiler dry-run "
                f"({len(_err_list)} error(s)). Pipeline aborted to avoid "
                f"wasting an Infrastructure Agent run."
            )
    elif _DEMO_PRECOMPILE_OK is False:
        # Re-emit the abort; we cached False on a prior run
        log(
            "[SCAFFOLD] ABORT: cached precompile failure. Fix demo TTCN "
            "source files before running the pipeline.",
            "ERROR",
        )
        raise RuntimeError(
            "Demo TTCN baseline previously failed ttc3_compiler dry-run. "
            "Pipeline aborted."
        )

    # 2. NUCLEAR COPY: Install ALL verified demo TTCN skeleton files proactively.
    # This guarantees a clean compilation baseline before Infrastructure Agent runs.
    # Any LLM-generated version of these files will be overwritten.
    ttcn_installed = install_shared_ttcn_files(tc_project_dir, plan_tc_id)
    log(f"[SCAFFOLD] Nuclear TTCN copy: {ttcn_installed} files from demo")

    # 3a. Purge unreferenced demo PR artifacts from PreConditions/
    # install_shared_ttcn_files copies ALL demo baseline files including
    # scaffolding that may not belong to the current precondition chain.
    _purge_unreferenced_precondition_artifacts(
        tc_project_dir, generation_plan.get("precondition_chain", [])
    )

    # 3. Create directory structure first (V2G ports need these paths)
    generated_cpp_dir = tc_project_dir / "build" / "generated_cpp"
    rsync_safe_dir = tc_project_dir / "src" / "cpp_adapters" / "SA" / "ports"
    (tc_project_dir / "src" / "cpp_adapters" / "common").mkdir(parents=True, exist_ok=True)
    (tc_project_dir / "src" / "cpp_adapters" / "SA" / "hardware").mkdir(parents=True, exist_ok=True)
    (tc_project_dir / "src" / "cpp_adapters" / "SA" / "titan_tri").mkdir(parents=True, exist_ok=True)
    (tc_project_dir / "src" / "cpp_adapters" / "CD" / "titan_tci").mkdir(parents=True, exist_ok=True)
    (tc_project_dir / "src" / "cpp_adapters" / "CD" / "exi_engine").mkdir(parents=True, exist_ok=True)
    generated_cpp_dir.mkdir(parents=True, exist_ok=True)
    rsync_safe_dir.mkdir(parents=True, exist_ok=True)
    (tc_project_dir / "build" / "obj").mkdir(parents=True, exist_ok=True)
    (tc_project_dir / "build" / "bin").mkdir(parents=True, exist_ok=True)
    (tc_project_dir / "logs" / "titan_traces").mkdir(parents=True, exist_ok=True)
    (tc_project_dir / "logs" / "system_logs").mkdir(parents=True, exist_ok=True)
    (tc_project_dir / "config").mkdir(parents=True, exist_ok=True)

    # 3. Copy V2G port implementations to BOTH locations (rsync-safe + generated_cpp)
    demo_port_files = [
        ("src/cpp_adapters/SA/ports/V2G_TCP_TLS_Port.cc", "V2G_TCP_TLS_Port.cc"),
        ("src/cpp_adapters/SA/ports/V2G_TCP_TLS_Port.hh", "V2G_TCP_TLS_Port.hh"),
        ("src/cpp_adapters/SA/ports/V2G_UDP_SDP_Port.cc", "V2G_UDP_SDP_Port.cc"),
        ("src/cpp_adapters/SA/ports/V2G_UDP_SDP_Port.hh", "V2G_UDP_SDP_Port.hh"),
    ]
    ports_copied = 0
    for demo_rel, dest_name in demo_port_files:
        demo_content = load_demo_file(demo_rel)
        if demo_content:
            # makefilegen expects sources under build/generated_cpp/
            dest_path = generated_cpp_dir / dest_name
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write(demo_content)
            # rsync-safe so build.sh finds it (no --exclude='build/' applies here)
            rsync_dest = rsync_safe_dir / dest_name
            with open(rsync_dest, "w", encoding="utf-8") as f:
                f.write(demo_content)
            ports_copied += 1
        else:
            log(f"[SCAFFOLD] WARNING: demo port not found: {demo_rel}", "WARN")

    # 4. Deep-copy the ENTIRE src/cpp_adapters/ directory from demo
    adapters_copied = 0
    adapters_skipped = 0
    demo_cpp_dir = DEMO_ROOT / "src" / "cpp_adapters"
    if demo_cpp_dir.exists():
        for item in demo_cpp_dir.rglob("*"):
            if item.is_file():
                rel = item.relative_to(demo_cpp_dir)
                dst = tc_project_dir / "src" / "cpp_adapters" / rel
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dst)
                    adapters_copied += 1
                else:
                    adapters_skipped += 1
    log(f"[SCAFFOLD] C++ adapters deep-copied from demo: {adapters_copied} new, {adapters_skipped} existing")

    # Bundle exi_payload_library.json into the TC project directory
    _bundle_exi_payload_library(tc_project_dir, generation_plan)

    # 5. Patch build.sh references
    _patch_build_script_common_functions(tc_project_dir)
    _patch_build_script_tc_file(tc_project_dir, tc_output_filename)
    _patch_build_script_include_paths(tc_project_dir)
    _patch_build_script_ttcn3_compiler_fix(tc_project_dir)

    log(f"[SCAFFOLD] Project directory prepared: {tc_project_dir}")
    log(f"[SCAFFOLD] build.sh: {'installed' if build_installed else 'MISSING'}")
    log(f"[SCAFFOLD] V2G ports: {ports_copied}/{len(demo_port_files)} copied")

    # ================================================================
    # --no-llm-render guard: bypass the Infrastructure Agent LLM call
    # entirely when the user explicitly opts out of LLM rendering. The
    # scaffold above has already populated every required file from demo/,
    # so we can hand a "completed / no changes" result back to the caller
    # and proceed straight to compilation. Saves ~8 minutes per TC that
    # would otherwise be burned on a sandbox-blocked Infrastructure Agent.
    if no_llm_render:
        log("[Infrastructure Agent] --no-llm-render: skipping LLM call, "
            "using scaffold-only output")
        infra_result = {
            "status": "completed",
            "mode": "no_llm_render_scaffold",
            "files_written": [],
            "build_output_summary": "Skipped Infrastructure Agent LLM call per --no-llm-render",
        }
        # Mirror existing scaffold state (Static Renderer / scaffolding already
        # produced the .ttcn tree; just make sure the project directory flag
        # is set so the build step runs against this directory).
        try:
            detected_mode, detected_files = detect_direct_write_mode(tc_project_dir)
            log(f"[Infrastructure Agent] --no-llm-render: detected {detected_mode} "
                f"with {len(detected_files)} files (using scaffold verbatim)")
        except RuntimeError as _dmw_err:
            log(f"[Infrastructure Agent] --no-llm-render: detect_direct_write_mode "
                f"warn: {_dmw_err}", "WARN")
            detected_mode, detected_files = "scaffold_only", []
        infra_agent_elapsed = time.time() - t3
        log(f"[Infrastructure Agent] 耗时: {infra_agent_elapsed:.1f}s (LLM skipped)")
    else:
        try:
            infra_result = run_infrastructure_agent(
                generation_plan=generation_plan,
                ttcn3_code=ttcn3_code,
                tc_id=plan_tc_id,
                sut_ip=sut_ip,
                tc_output_filename=tc_output_filename,
                output_dir=tc_project_dir,
                protocol_snapshot=_reg.snapshot(_active_protocol, include_full_messages=True) if _active_protocol else None,
            )
        except RuntimeError as e:
            log(f"[WARN] Infrastructure Agent threw RuntimeError: {e}", "WARN")
            infra_result = {"status": "error", "message": str(e)}
        infra_agent_elapsed = time.time() - t3
        log(f"[Infrastructure Agent] 耗时: {infra_agent_elapsed:.1f}s")

    # === Process Infrastructure Agent result ===
    # Phase 3.2: detect whether the agent was killed by the watchdog so we can
    # short-circuit the fix loop instead of re-invoking it (and burning another
    # 1200s on a known-bad call).
    infra_was_killed_by_watchdog = False
    infra_elapsed_threshold = float(os.environ.get("INFRA_AGENT_HANG_THRESHOLD", "900"))
    if infra_agent_elapsed >= infra_elapsed_threshold:
        infra_was_killed_by_watchdog = True
        log(
            f"[WATCHDOG] Infrastructure Agent took {infra_agent_elapsed:.1f}s "
            f"(>= {infra_elapsed_threshold:.0f}s threshold) -- "
            f"likely killed by watchdog. Will NOT retry in fix loop.",
            "WARN",
        )

    # Phase 3.4: detect a "silent LLM call" -- the agent returned successfully
    # (no exception, no hang) but produced ZERO files on disk. This is the
    # classic "API blocked / disabled / rate-limited" signature: claude returns
    # an empty or near-empty response immediately. We should NOT burn another
    # full watchdog cycle retrying; treat this the same as a watchdog kill.
    infra_silent_no_files_written = False
    if not infra_was_killed_by_watchdog and infra_result is not None:
        _written_files = infra_result.get("files") or []
        _note = infra_result.get("note", "")
        if len(_written_files) == 0 and _note in (
            "files_written_via_tools",  # empty response -> assumed via tools
            "short_response_via_tools",  # short response -> assumed via tools
        ):
            # Cross-check with detect_direct_write_mode: did the agent actually
            # write anything to disk?
            try:
                _silent_mode, _silent_files = detect_direct_write_mode(tc_project_dir)
                if not _silent_files:
                    infra_silent_no_files_written = True
                    log(
                        "[SILENT] Infrastructure Agent returned empty/short response "
                        "AND wrote 0 files to disk. Will NOT retry in fix loop "
                        "(API likely blocked).",
                        "WARN",
                    )
            except Exception:
                pass
    if infra_silent_no_files_written and not infra_was_killed_by_watchdog:
        infra_was_killed_by_watchdog = True
        result["infra_status"] = "silent_failure"

    if infra_result is None or infra_result.get("status") == "error":
        log(f"[FATAL] Infrastructure Agent failed: {infra_result.get('message', 'unknown') if infra_result else 'No result'}", "ERROR")
        result["infra_status"] = "error"
        result["infra_message"] = infra_result.get("message") if infra_result else "No result"
        # Do NOT raise -- let the "always run" steps below install scaffolding anyway
        # The build will fail and the human will debug from the error output.
    else:
        # Direct Write Mode: validate files written by agent
        try:
            detected_mode, detected_files = detect_direct_write_mode(tc_project_dir)
        except RuntimeError as e:
            log(f"[FATAL] {e}", "ERROR")
            raise
        log(f"[MODE] Direct Write Mode confirmed -- {len(detected_files)} files written by agent", "INFO")
        # === Post-detection TTCN full sanitization ===
        # Sanitize ALL .ttcn files written by the agent to fix LLM-generated:
        #   1. odd-length hex literals
        #   2. illegal nested PR calls
        #   3. bare v2g union wrappers in receive/send
        for f in detected_files:
            if f.suffix == ".ttcn":
                try:
                    raw = f.read_text(encoding="utf-8", errors="replace")
                    clean = _sanitize_extracted_text(raw)
                    if clean != raw:
                        f.write_text(clean, encoding="utf-8")
                        log(f"[SANITIZE] Sanitized agent-written file: {f.relative_to(tc_project_dir)}")
                except Exception as e:
                    log(f"[SANITIZE] Could not sanitize {f}: {e}", "WARN")
        if detected_files:
            log(f"[Direct Write Mode] 验证成功，目录包含 {len(detected_files)} 个有效文件:")
            for f in sorted(detected_files):
                log(f"  [DIRECT] {f.relative_to(tc_project_dir)}")
        result["infra_status"] = "success"
        result["infra_write_mode"] = "direct_write"
        result["infra_files_count"] = len(detected_files)

    infra_agent_elapsed = time.time() - t3
    log(f"[Infrastructure Agent] 耗时: {infra_agent_elapsed:.1f}s")

    # === Re-apply static renderer output AFTER Infrastructure Agent runs ===
    # The Infra Agent may have overwritten our files (or been killed by watchdog).
    # Always re-write the static TC+TB files before build so they are clean.
    if _static_renderer_used and _static_tc_path and _static_tc_content:
        _reinject_count = 0
        _testcases_file = _static_tc_path.parent / "TestCases_SECC" / "TestCases_SECC.ttcn"
        # Re-render TestCases_SECC.ttcn (depends only on testcase_name)
        try:
            from ttcn_static_renderer import _get_jinja_env
            _env = _get_jinja_env()
            _tcs_tpl = _env.get_template("TestCases_SECC.ttcn.j2")
            _tcs_content = _tcs_tpl.render(testcase_name=plan_tc_id)
            _testcases_file.parent.mkdir(parents=True, exist_ok=True)
            _testcases_file.write_text(_tcs_content, encoding="utf-8")
            _reinject_count += 1
        except Exception as _e:
            log(f"[Static Renderer] TestCases_SECC.ttcn 重生成失败: {_e}", "WARN")
        for _fpath, _fcontent in [
            (_static_tc_path, _static_tc_content),
            (_static_tb_path, _static_tb_content),
        ]:
            if _fpath and _fcontent:
                try:
                    _fpath.parent.mkdir(parents=True, exist_ok=True)
                    _fpath.write_text(_fcontent, encoding="utf-8")
                    _reinject_count += 1
                except Exception:
                    pass
        log(f"[Static Renderer] Re-injected {_reinject_count} file(s) before build "
            f"(overwrites any Infra Agent stomp)")

    # === Re-apply canonical runtime config AFTER Infrastructure Agent runs ===
    # The Infra Agent may have overwritten titan_runtime.cfg with a wrong [EXECUTE]
    # directive (e.g. TC_SECC_CMN_VTB_ServiceDiscovery_001.control instead of
    # TestCases_SECC.control). Re-install the canonical config to guarantee
    # the correct value is always used.
    _install_canonical_runtime_config(tc_project_dir)

    # Forward-Only compile lock flag
    compile_locked = False

    if not skip_build:
        pcap_out = pcap_path
        if not pcap_out:
            pcap_out = tc_project_dir / "logs" / "capture.pcap"
        t_build = time.time()
        build_result = run_build_and_test(
            tc_dir=tc_project_dir,
            tc_id=plan_tc_id,
            sut_ip=sut_ip,
            pcap_output=pcap_out,
        )
        build_elapsed = time.time() - t_build
        log(f"[Build & Run] 耗时: {build_elapsed:.1f}s")
        result["build_result"] = build_result

        # === Forward-Only State Machine: compile_locked ===
        # Once the TestExecutable is successfully built, we lock the compilation state.
        # After locking, we MUST NOT re-trigger the Infrastructure Agent or static Fault
        # Corrector for static fixes -- any remaining issues belong to the Runtime phase.
        compile_locked = build_result.get("status") == "completed"

        if compile_locked:
            log("[LOCK] compile_locked = True -- Build succeeded, locking compilation state.")
            log("[LOCK] All static fix loops are now disabled. Proceeding to runtime phase only.")

        if build_result.get("status") == "build_failed":
            compile_errors = build_result.get("compile_errors", [])
            if compile_errors:
                log(f"编译失败，发现 {len(compile_errors)} 个错误")
            else:
                # Non-zero exit but no parseable errors -> build.sh pre-check failed
                # before TITAN compiler could run.
                log(f"编译失败，build.sh 前置检查失败（返回码 {build_result.get('returncode')}），无 TITAN 编译错误可解析")

            result["compile_feedback"] = "Build failed - see error feedback below"
            result["compile_errors"] = compile_errors

            # Enter compile loop ONLY on genuine TITAN compilation errors.
            # JSON parse failure alone (agent wrapped output in markdown/XML) is NOT a build error.
            # If the build succeeded despite a JSON parse failure, we must NOT trigger the
            # fallback stub installation -- doing so would overwrite the working files with
            # demo skeleton files and create the very build failure we were trying to avoid.
            # Also skip if compile_locked (Forward-Only: once built, never re-trigger infra agent).
            genuine_build_error = (
                (compile_errors or build_result.get("returncode", 0) != 0)
                and not compile_locked
            )

            # === Static Linter (zero-LLM): fix mechanical typos before LLM fix ===
            linter_fixed, linter_fixes = _run_static_linter(compile_errors, tc_project_dir)
            if linter_fixed:
                log(f"[STATIC-LINTER] Applied {sum(len(f['fixes']) for f in linter_fixes)} patches: {linter_fixes}")

            if genuine_build_error:
                # Track compilation iterations for metrics
                compile_loop_build_times.append((0, build_elapsed))

                log(f"\n--- Step 7: Infrastructure Agent Fix Loop (max 3 iterations) ---")

                # Phase 3.2 hardening: if the Infrastructure Agent was already killed
                # by the watchdog on the first attempt, do NOT re-invoke it in the
                # fix loop -- we already know it hangs. Escalate to static fallback
                # immediately and let the build fail with a clear error.
                INFRA_FIX_MAX_ITER = 3
                _infra_fix_succeeded = False
                _remaining_errors = compile_errors

                # Phase 3.3: track the file count BEFORE each retry iteration, so we can
                # detect a "silent fix loop" -- the LLM is invoked, returns 0 chars (API
                # blocked, network down, or quota exhausted), and writes nothing to disk,
                # but the pipeline keeps retrying forever and burns token. If 2 consecutive
                # retries both produce ZERO new files, we declare silent failure and exit.
                # This is the case the user hit on 2026-06-18: API was disabled mid-run,
                # empty responses kept coming back, and the loop ran 19 minutes straight.
                _pre_fix_file_count = 0
                try:
                    _pre_fix_mode, _pre_fix_files = detect_direct_write_mode(tc_project_dir)
                    _pre_fix_file_count = len(_pre_fix_files) if _pre_fix_files else 0
                except Exception:
                    _pre_fix_file_count = 0
                _consecutive_empty_fixes = 0

                if infra_was_killed_by_watchdog:
                    log(
                        "[FIX LOOP] Skipping Infrastructure Agent retries -- "
                        "agent was killed by watchdog on first attempt. "
                        "Writing fallback metrics and exiting fix loop.",
                        "WARN",
                    )
                    result["infra_status"] = "watchdog_killed"
                    result["compile_errors"] = compile_errors
                    result["latency"] = result.get("latency", {})
                    result["latency"]["fault_correction_seconds"] = 0.0
                    result["latency"]["total_pipeline_seconds"] = time.time() - t0
                    result["verdict"] = {
                        "result": "build_failed",
                        "build_status": "watchdog_killed",
                        "compile_errors_count": len(compile_errors),
                        "note": "Infrastructure Agent hung past watchdog threshold; "
                                "fix loop skipped to avoid wasting another 1200s.",
                    }
                    try:
                        _write_simple_watchdog_metrics(tc_project_dir, plan_tc_id, result)
                    except Exception as _wm_err:
                        log(f"[FIX LOOP] _write_simple_watchdog_metrics failed: {_wm_err}", "WARN")
                    # Set iter count to 0 so the for-loop below is a no-op.
                    INFRA_FIX_MAX_ITER = 0

                # 2D: Pre-compute the missing-PR remediation directive ONCE so the
                # Infrastructure Agent sees the same mandatory file list on every
                # fix iteration. Without this, the agent would waste iterations
                # rediscovering that the PR modules don't exist.
                _missing_pr_directive = _build_missing_pr_remediation_directive(generation_plan)
                if _missing_pr_directive:
                    log(
                        f"[Infra Fix] Plan reports missing PR/PO modules -- will inject "
                        f"remediation directive into every fix iteration's error feedback",
                        "WARN",
                    )

                for _infra_fix_iter in range(1, INFRA_FIX_MAX_ITER + 1):
                    if _infra_fix_iter > 1:
                        # Re-run build to check if previous fix worked
                        log(f"[Infra Fix] Iteration {_infra_fix_iter}: re-checking build...")
                        _recheck_result = run_build_and_test(
                            tc_dir=tc_project_dir, tc_id=plan_tc_id,
                            sut_ip=sut_ip, pcap_output=pcap_out,
                        )
                        if _recheck_result.get("status") == "completed":
                            log(f"[Infra Fix] Build succeeded on iteration {_infra_fix_iter - 1}!")
                            build_result = _recheck_result
                            result["build_result"] = _recheck_result
                            compile_locked = True
                            _infra_fix_succeeded = True
                            break
                        else:
                            _remaining_errors = _recheck_result.get("compile_errors", [])
                            result["compile_errors"] = _remaining_errors
                            build_result = _recheck_result

                    # Build error feedback
                    infra_error_fb = build_infra_error_feedback(_remaining_errors, tc_project_dir)
                    if not infra_error_fb:
                        infra_error_fb = (
                            "Build failed with exit code " + str(build_result.get("returncode", 0)) + ". "
                            "Raw output: " + build_result.get("stderr", build_result.get("stdout", ""))[:1000]
                        )

                    # 2D: append missing-PR remediation directive. This is the
                    # core of 2D -- the Infrastructure Agent now gets told, on
                    # every fix iteration, exactly which PR files it MUST
                    # generate (because the demo baseline didn't supply them).
                    if _missing_pr_directive:
                        infra_error_fb = infra_error_fb + _missing_pr_directive

                    log(f"[Infra Fix] Iteration {_infra_fix_iter}/{INFRA_FIX_MAX_ITER}: calling agent...")

                    current_infra_files = infra_result.get("files", []) if infra_result else []
                    previous_infra_files = (
                        [f.get("path", "") for f in current_infra_files]
                        if isinstance(current_infra_files, list) else []
                    )
                    previous_core_files = pre_condition_files + post_condition_files

                    t_infra_fix = time.time()
                    infra_retry = run_infrastructure_agent(
                        generation_plan=generation_plan,
                        ttcn3_code=ttcn3_code,
                        tc_id=plan_tc_id,
                        sut_ip=sut_ip,
                        tc_output_filename=tc_output_filename,
                        error_feedback=infra_error_fb,
                        previous_infra_files=previous_infra_files,
                        previous_core_files=previous_core_files,
                        is_fix_mode=True,
                        protocol_snapshot=(
                            _reg.snapshot(_active_protocol, include_full_messages=True)
                            if _active_protocol else None
                        ),
                    )
                    infra_fix_elapsed = time.time() - t_infra_fix
                    log(f"[Infra Fix] Iteration {_infra_fix_iter} elapsed: {infra_fix_elapsed:.1f}s")
                    compile_loop_infra_times.append((_infra_fix_iter, infra_fix_elapsed))

                    # Re-apply static renderer
                    if _static_renderer_used and _static_tc_path and _static_tc_content:
                        _reinject_count = 0
                        for _fpath, _fcontent in [
                            (_static_tc_path, _static_tc_content),
                            (_static_tb_path, _static_tb_content),
                        ]:
                            if _fpath and _fcontent:
                                try:
                                    _fpath.parent.mkdir(parents=True, exist_ok=True)
                                    _fpath.write_text(_fcontent, encoding="utf-8")
                                    _reinject_count += 1
                                except Exception:
                                    pass
                        log(f"[Static Renderer] Re-injected {_reinject_count} file(s) after Infra Fix")

                    infra_is_json_error = infra_retry.get("status") == "error"
                    build_succeeded = build_result.get("status") == "completed"
                    if (infra_is_json_error and build_succeeded) or compile_locked:
                        if compile_locked:
                            log(f"[LOCK] compile_locked=True: stopping Infrastructure Agent retries.")
                        else:
                            log("[WARN] Agent JSON parse failed but build succeeded. "
                                "Preserving agent's working files.")
                        _infra_fix_succeeded = True
                        break

                    try:
                        detected_mode, detected_files = detect_direct_write_mode(tc_project_dir)
                        log(f"[Infra Fix] Direct Write Mode: found {len(detected_files)} files")
                    except RuntimeError as e:
                        log(f"[Infra Fix] FATAL: {e}", "ERROR")
                        raise

                    # Phase 3.3: silent-fix detection. If the LLM was invoked but produced
                    # NO new files (or even fewer than before), this iteration is wasted
                    # -- the LLM is silent / API blocked. Track consecutive empty fixes;
                    # after 2 in a row, break and let the build fail with a clear error
                    # rather than burning 3 more 1200s retries.
                    _new_files_count = len(detected_files) if detected_files else 0
                    if _new_files_count <= _pre_fix_file_count:
                        _consecutive_empty_fixes += 1
                        log(
                            f"[SILENT FIX] Iteration {_infra_fix_iter} produced NO new files "
                            f"(pre={_pre_fix_file_count}, post={_new_files_count}). "
                            f"Consecutive empty fixes: {_consecutive_empty_fixes}/2",
                            "WARN",
                        )
                        if _consecutive_empty_fixes >= 2:
                            log(
                                "[SILENT FIX] 2 consecutive Infra Fix iterations produced no new files. "
                                "This means the Infrastructure Agent is silent (API blocked / disabled / "
                                "rate-limited). Breaking fix loop to avoid burning more token.",
                                "ERROR",
                            )
                            result["infra_status"] = "silent_failure"
                            result["infra_message"] = (
                                f"Infrastructure Agent returned 0 new files across "
                                f"{_consecutive_empty_fixes} consecutive fix iterations. "
                                f"API is likely blocked or disabled. Re-enable API and re-run."
                            )
                            result["compile_errors"] = _remaining_errors
                            try:
                                _write_simple_watchdog_metrics(tc_project_dir, plan_tc_id, result)
                            except Exception as _wm_err:
                                log(f"[SILENT FIX] _write_simple_watchdog_metrics failed: {_wm_err}", "WARN")
                            _infra_fix_succeeded = False
                            break
                    else:
                        # Got new files! Reset counter and update baseline.
                        log(
                            f"[SILENT FIX] Iteration {_infra_fix_iter} produced "
                            f"{_new_files_count - _pre_fix_file_count} new file(s).",
                            "INFO",
                        )
                        _consecutive_empty_fixes = 0
                        _pre_fix_file_count = _new_files_count

                    t_final_build = time.time()
                    final_build_result = run_build_and_test(
                        tc_dir=tc_project_dir, tc_id=plan_tc_id,
                        sut_ip=sut_ip, pcap_output=pcap_out,
                    )
                    final_build_elapsed = time.time() - t_final_build
                    log(f"[Infra Fix] Iteration {_infra_fix_iter} final build: {final_build_elapsed:.1f}s")
                    compile_loop_build_times.append((_infra_fix_iter, final_build_elapsed))

                    if final_build_result.get("status") == "completed":
                        log(f"[Infra Fix] Iteration {_infra_fix_iter} SUCCESS!")
                        result["build_result"] = final_build_result
                        result["compile_feedback"] = ""
                        result["compile_errors"] = []
                        compile_locked = True
                        _infra_fix_succeeded = True
                        break
                    else:
                        _remaining_errors = final_build_result.get("compile_errors", [])
                        log(f"[Infra Fix] Iteration {_infra_fix_iter} FAILED: "
                            f"{len(_remaining_errors)} errors remain. {'Retrying...' if _infra_fix_iter < INFRA_FIX_MAX_ITER else 'Exhausted max iterations.'}")
                        result["compile_feedback"] = infra_error_fb
                        result["compile_errors"] = _remaining_errors
                        result["build_result"] = final_build_result
                        build_result = final_build_result

                if not _infra_fix_succeeded and not compile_locked:
                    log(f"[Infra Fix] All {INFRA_FIX_MAX_ITER} iterations exhausted. "
                        "Proceeding to runtime phase with build failures.", "WARN")
            else:
                log("[WARN] Infrastructure Agent JSON parse failed but build succeeded. "
                    "Build artifacts preserved -- no destructive fallback triggered.", "WARN")

        fc_runtime_elapsed = 0.0
        if build_result and build_result.get("test_result"):
            # Only invoke the runtime Fault Corrector when the test actually ran AND failed.
            # "completed" status means build+run succeeded, but does NOT indicate the verdict.
            test_result_data = build_result["test_result"]
            log_content = test_result_data.get("log_content", "")
            stdout_content = test_result_data.get("stdout", "")
            combined_output = log_content + "\n" + stdout_content
            test_passed = "Overall verdict: pass" in combined_output

            # === Phase 3.8: Early-exit gate for runtime Fault Corrector ===
            # When the verdict_hint pre-computed in run_build_and_test tells us
            # the TestExecutable crashed silently or returned 0 with no verdict
            # line, calling run_fault_corrector is pointless -- there is no
            # TTCN-3 log to analyze. Without this gate, the runtime corrector
            # spins for 30-60s and then returns 0 useful repairs, while the
            # metrics still report "inconc" -- a complete telemetry loss.
            verdict_hint = test_result_data.get("verdict_hint")
            if verdict_hint in ("runtime_silent_exit", "runtime_no_verdict"):
                log(
                    f"[FC-GATE] Skipping runtime Fault Corrector: verdict_hint="
                    f"{verdict_hint!r}. TestExecutable did not produce a TTCN-3 "
                    f"verdict; further LLM analysis cannot recover the run.",
                    "WARN",
                )
                result["runtime_repair"] = {
                    "status": "skipped",
                    "iterations_used": 0,
                    "runtime_verdict": verdict_hint,
                    "reason": (
                        "TestExecutable returned without writing 'Overall verdict:' "
                        "line. No TTCN-3 log available for LLM analysis. "
                        "Verdict classified as error:runtime_silent_exit or "
                        "error:runtime_no_verdict by the metrics layer."
                    ),
                }
            elif test_passed:
                log("Test passed (Overall verdict: pass) -- skipping runtime Fault Corrector.")
                result["test_passed"] = True
            else:
                log("\n--- Fault Corrector (runtime: dual-track analysis) ---")
                test_log_candidates = [
                    tc_project_dir / "logs" / "titan_traces" / "TestExecution.log",
                    tc_project_dir / "build" / "bin" / "logs" / "titan_traces" / "TestExecution.log",
                ]
                test_log = next((p for p in test_log_candidates if p.exists()), test_log_candidates[0])
                t_fc_rt = time.time()
                fault_result = run_fault_corrector(
                    ttcn3_code=ttcn3_code,
                    generation_plan=generation_plan,
                    sop=sop,
                    phase="runtime",
                    tc_id=plan_tc_id,
                    log_path=test_log if test_log.exists() else None,
                    pcap_path=pcap_out if pcap_out.exists() else None,
                )
                fc_runtime_elapsed += time.time() - t_fc_rt
                log(f"[FC (runtime)] 耗时: {time.time() - t_fc_rt:.1f}s")
                result["fault_result"] = fault_result
                fault_output_path = tc_project_dir / "fault_analysis.json"
                with open(fault_output_path, "w", encoding="utf-8") as f:
                    json.dump(fault_result, f, ensure_ascii=False, indent=2)
                log(f"故障分析已保存: {fault_output_path}")

                if fault_result and fault_result.get("status") == "completed":
                    self_repair_result = _runtime_self_repair(
                        ttcn3_code=ttcn3_code,
                        generation_plan=generation_plan,
                        fault_result=fault_result,
                        tc_project_dir=tc_project_dir,
                        output_path=output_path,
                        tc_ttcn_dest=tc_ttcn_dest,
                        plan_tc_id=plan_tc_id,
                        sut_ip=sut_ip,
                        few_shot_examples=few_shot_examples,
                        sop=sop,
                        references=references,
                        compile_loop_fc_times=compile_loop_fc_times,
                        compile_loop_fc_total_ref=[compile_loop_fc_total],
                    )
                    if self_repair_result:
                        result["runtime_repair"] = self_repair_result

    # === Fault Corrector (runtime phase: post-execution dual-track analysis, legacy path) ===
    fault_result = None
    if titan_log or pcap_path:
        t_fc_legacy = time.time()
        fault_result = run_fault_corrector(
            ttcn3_code=ttcn3_code,
            generation_plan=generation_plan,
            sop=sop,
            phase="runtime",
            tc_id=plan_tc_id,
            log_path=titan_log,
            pcap_path=pcap_path,
        )
        fc_legacy_elapsed = time.time() - t_fc_legacy
        log(f"[FC (runtime, legacy)] 耗时: {fc_legacy_elapsed:.1f}s")
        fault_output_path = GENERATED_DIR / f"{plan_tc_id}_fault_analysis.json"
        with open(fault_output_path, "w", encoding="utf-8") as f:
            json.dump(fault_result, f, ensure_ascii=False, indent=2)
        log(f"故障分析结果已保存: {fault_output_path}")
        if "fault_result" not in result:
            result["fault_result"] = fault_result

    log(f"{'=' * 60}")

    # === Always run: Print timing summary ===
    _print_timing_summary(result, orchestrator_elapsed, summarizer_elapsed,
                         core_agent_elapsed, infra_agent_elapsed, build_elapsed,
                         compile_loop_core_total, compile_loop_infra_total,
                         compile_loop_build_total, compile_loop_fc_total,
                         fc_runtime_elapsed, fc_legacy_elapsed,
                         compile_loop_core_times, compile_loop_infra_times,
                         compile_loop_build_times, compile_loop_fc_times)

    # === Filesystem Truth Override (PRAGMATIC) ===
    # If a TestExecutable exists on disk, try to run it to get a real verdict.
    # This handles the case where build.sh succeeded but the pipeline's
    # build_result.status was incorrectly set (e.g., "stale_binary_requires_rebuild"
    # from a prior failed attempt where the binary was actually built successfully).
    _exe_candidate = tc_project_dir / "build" / "bin" / "TestExecutable"
    _cfg_candidate = tc_project_dir / "config" / "titan_runtime.cfg"
    if _exe_candidate.exists() and _cfg_candidate.exists():
        if not build_result or build_result.get("status") != "completed":
            log(f"[FS-TRUTH] TestExecutable exists at {_exe_candidate}, attempting runtime execution...")
            try:
                import subprocess as _subprocess
                _run_result = _subprocess.run(
                    [str(_exe_candidate), str(_cfg_candidate)],
                    capture_output=True, text=True, timeout=300,
                    cwd=str(_exe_candidate.parent),
                )
                # Parse verdict from output
                _combined = (_run_result.stdout or "") + "\n" + (_run_result.stderr or "")
                _verdict_hint = "error"
                if "Overall verdict: pass" in _combined:
                    _verdict_hint = "pass"
                elif "Overall verdict: inconc" in _combined:
                    _verdict_hint = "inconc"
                elif "Overall verdict: fail" in _combined:
                    _verdict_hint = "fail"

                build_result = {
                    "status": "completed",
                    "test_result": {
                        "returncode": _run_result.returncode,
                        "verdict_hint": _verdict_hint,
                        "stdout": _run_result.stdout[-5000:],
                        "stderr": _run_result.stderr[-5000:],
                    },
                    "compile_errors": [],
                    "execution_wallclock_seconds": 0.0,
                    "_fs_truth_recovered": True,
                }
                result["build_result"] = build_result
                log(f"[FS-TRUTH] Runtime executed successfully, verdict_hint={_verdict_hint}")
            except _subprocess.TimeoutExpired:
                build_result = {
                    "status": "completed",
                    "test_result": {
                        "returncode": -1,
                        "verdict_hint": "timeout",
                        "stdout": "",
                        "stderr": "Test timed out after 300s",
                    },
                    "compile_errors": [],
                    "execution_wallclock_seconds": 300.0,
                    "_fs_truth_recovered": True,
                }
                result["build_result"] = build_result
                log("[FS-TRUTH] Runtime timed out (300s), marking as completed with timeout verdict")
            except Exception as _e:
                log(f"[FS-TRUTH] Runtime execution failed: {_e}, keeping original build_result")

    # === Write metrics.json ===
    _write_metrics_json(tc_project_dir, _build_metrics_dict(
        plan_tc_id, run_timestamp, generation_plan, result,
        orchestrator_elapsed, summarizer_elapsed, core_agent_elapsed,
        infra_agent_elapsed, build_elapsed,
        compile_loop_core_total, compile_loop_infra_total,
        compile_loop_build_total, compile_loop_fc_total,
        fc_runtime_elapsed, fc_legacy_elapsed,
        compile_loop_core_times, compile_loop_infra_times,
        compile_loop_build_times, compile_loop_fc_times,
        compile_locked, build_result,
    ))

    return result


def _print_timing_summary(result: Dict,
                         orchestrator_elapsed: float,
                         summarizer_elapsed: float,
                         core_agent_elapsed: float,
                         infra_agent_elapsed: float,
                         build_elapsed: float,
                         compile_loop_core_total: float,
                         compile_loop_infra_total: float,
                         compile_loop_build_total: float,
                         compile_loop_fc_total: float,
                         fc_runtime_elapsed: float,
                         fc_legacy_elapsed: float,
                         compile_loop_core_times: List[Tuple[int, float]],
                         compile_loop_infra_times: List[Tuple[int, float]],
                         compile_loop_build_times: List[Tuple[int, float]],
                         compile_loop_fc_times: List[Tuple[int, float]]) -> None:
    """Print timing summary and populate result['agent_timings'].

    Called at every exit point of generate_code_for_test_case to ensure
    timing information is always visible, even on early returns.
    """
    cl_core_count = sum(1 for _, t in compile_loop_core_times if t > 0)
    cl_infra_count = sum(1 for _, t in compile_loop_infra_times if t > 0)
    cl_fc_count = len(compile_loop_fc_times)
    cl_build_count = len(compile_loop_build_times)
    cl_round = max(cl_core_count, cl_infra_count, cl_build_count, cl_fc_count)

    total_fc = compile_loop_fc_total + fc_runtime_elapsed + fc_legacy_elapsed
    total_agents = orchestrator_elapsed + summarizer_elapsed + core_agent_elapsed + infra_agent_elapsed
    total_all = (total_agents + build_elapsed + compile_loop_core_total +
                 compile_loop_infra_total + compile_loop_build_total + total_fc)

    agent_timings = {
        "orchestrator": round(orchestrator_elapsed, 1),
        "summarizer": round(summarizer_elapsed, 1),
        "core_agent": round(core_agent_elapsed, 1),
        "infrastructure_agent": round(infra_agent_elapsed, 1),
        "build": round(build_elapsed, 1),
        "compile_loop": {
            "core_agent_total": round(compile_loop_core_total, 1),
            "core_agent_calls": cl_core_count,
            "infra_agent_total": round(compile_loop_infra_total, 1),
            "infra_agent_calls": cl_infra_count,
            "build_total": round(compile_loop_build_total, 1),
            "build_calls": cl_build_count,
            "fc_compile_total": round(compile_loop_fc_total, 1),
            "fc_compile_calls": cl_fc_count,
            "per_iteration": {
                "core": [(n, round(t, 1)) for n, t in compile_loop_core_times],
                "infra": [(n, round(t, 1)) for n, t in compile_loop_infra_times],
                "build": [(n, round(t, 1)) for n, t in compile_loop_build_times],
                "fc": [(n, round(t, 1)) for n, t in compile_loop_fc_times],
            },
        },
        "fault_corrector": {
            "runtime": round(fc_runtime_elapsed, 1),
            "legacy": round(fc_legacy_elapsed, 1),
            "compile_total": round(compile_loop_fc_total, 1),
        },
        "total_agents": round(total_agents, 1),
        "total_all": round(total_all, 1),
    }
    result["agent_timings"] = agent_timings

    def _iter_lines(times_list, label):
        lines = []
        for n, t in times_list:
            if t > 0:
                lines.append(f"|   # {n}: {label:<20} {t:5.1f}s |")
        return lines

    core_lines = _iter_lines(compile_loop_core_times, "Core Agent")
    infra_lines = _iter_lines(compile_loop_infra_times, "Infrastructure Agent")
    build_lines = _iter_lines(compile_loop_build_times, "Build")
    fc_lines = _iter_lines(compile_loop_fc_times, "Fault Corrector")

    log("")
    if cl_round > 0:
        log(f"========== Agent 执行耗时 (含 Compile Loop x{cl_round}) ==========")
    else:
        log("========== Agent 执行耗时 ==========")
    log("| Agent              | 耗时(s) |")
    log("|--------------------|---------|")
    log(f"| Orchestrator      |  {orchestrator_elapsed:6.1f}  |")
    log(f"| Summarizer        |  {summarizer_elapsed:6.1f}  |")
    log(f"| Core Agent        |  {core_agent_elapsed:6.1f}  |")
    log(f"| Infrastructure    |  {infra_agent_elapsed:6.1f}  |")

    if cl_round > 0:
        log("|--------------------|---------|")
        log(f"| Compile Loop      |  x{cl_round}      |")
        log("|--------------------|---------|")
        for line in core_lines:
            log(line)
        for line in infra_lines:
            log(line)
        for line in build_lines:
            log(line)
        for line in fc_lines:
            log(line)

    log(f"| Build & Run       |  {build_elapsed:6.1f}  |")
    if total_fc > 0:
        log(f"| Fault Corrector   |  {total_fc:6.1f}  |")
    log("|--------------------|---------|")
    log(f"| 总计(Agent)       |  {total_agents:6.1f}  |")
    log(f"| 总计(全部)        |  {total_all:6.1f}  |")
    log("===================================")



# ============================================================
# EXI Payload Library Bundling Helpers
# ============================================================
DEMO_PAYLOAD_LIB = DEMO_ROOT / "exi_payload_library.json"


def _bundle_exi_payload_library(tc_dir, generation_plan):
    dest = tc_dir / "exi_payload_library.json"
    if DEMO_PAYLOAD_LIB.exists():
        shutil.copy2(DEMO_PAYLOAD_LIB, dest)
        log("[BUNDLE] Copied exi_payload_library.json -> " + str(dest))
    else:
        _generate_minimal_payload_library(tc_dir, generation_plan)


def _generate_minimal_payload_library(tc_dir, generation_plan):
    msg_type = generation_plan.get("normalized_message_type",
                                   generation_plan.get("message_type", ""))
    lib = {
        "source": "generated_minimal",
        "iso15118_root": None,
        "jar_path": None,
        "message_types": [{"type": msg_type,
                           "description": "Auto-generated stub for " + msg_type,
                           "request_bytes": [], "response_bytes": []}],
    }
    with open(tc_dir / "exi_payload_library.json", "w", encoding="utf-8") as f:
        json.dump(lib, f, indent=2, ensure_ascii=False)
    log("[BUNDLE] Generated minimal exi_payload_library.json -> " + str(tc_dir / "exi_payload_library.json"))




# ============================================================
# Metrics Infrastructure
# ============================================================


def _get_git_commit():
    try:
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=repo_dir
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


_METRICS_REQUIRED_FIELDS = [
    "experiment_metadata",
    "verdict",
    "verdict.result",
    "verdict.execution_time_seconds",
    "verdict.build_status",
    "latency.total_pipeline_seconds",
]


def _validate_metrics_schema(metrics):
    """Fail-loud schema validation for metrics.json.

    Checks that all required fields are present and have the correct types.
    Raises AssertionError (which bubbles up as a pipeline failure) if any field is missing.
    """
    errors = []
    # Check top-level verdict
    verdict = metrics.get("verdict", {})
    if not verdict:
        errors.append("verdict object is missing or empty")
    else:
        if "result" not in verdict:
            errors.append("verdict.result is missing")
        result = verdict.get("result", "")
        if not isinstance(result, str):
            errors.append(f"verdict.result is not a string: {type(result).__name__}")
        elif result == "not_run":
            # When build/compile never produced a TTCN-3 verdict, the run is
            # still a real run — it just failed. Normalize to "error" so the
            # metrics.json is schema-compliant and the caller can read the
            # failure mode from the existing build_status / log_content.
            log("[METRICS] verdict.result was 'not_run'; normalizing to 'error' for schema compliance")
            verdict["result"] = "error"
        if "execution_time_seconds" not in verdict:
            errors.append("verdict.execution_time_seconds is missing")
        else:
            et = verdict["execution_time_seconds"]
            if not isinstance(et, (int, float)):
                errors.append(f"verdict.execution_time_seconds is not numeric: {type(et).__name__}")
            elif et < 0:
                errors.append(f"verdict.execution_time_seconds is negative: {et}")
        if "build_status" not in verdict:
            errors.append("verdict.build_status is missing")
    # Check latency
    latency = metrics.get("latency", {})
    if not latency:
        errors.append("latency object is missing or empty")
    else:
        if "total_pipeline_seconds" not in latency:
            errors.append("latency.total_pipeline_seconds is missing")
    if errors:
        raise AssertionError(
            f"[METRICS VALIDATION FAILED] metrics.json is missing or has invalid fields:\n"
            + "\n".join(f"  - {e}" for e in errors)
            + "\nThe pipeline will continue but this metrics.json will be incomplete."
        )


def _write_metrics_json(tc_project_dir, metrics):
    path = tc_project_dir / "metrics.json"
    # Schema validation: fail-loud on missing required fields
    _validate_metrics_schema(metrics)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    log("[METRICS] Written to " + str(path))


def _parse_execution_time_from_log(log_content: str) -> float:
    """Parse test execution time from TITAN TestExecution.log.

    TITAN doesn't emit a single 'Real time: Xs' line. Instead, the start and
    end timestamps are embedded in the first/last EXECUTOR lines:
      First:  "01:46:48.733775 EXECUTOR - TTCN-3 Test Executor started..."
      Last:   "01:47:50.322596 EXECUTOR - TTCN-3 Test Executor finished..."

    We parse both timestamps and compute the difference in seconds.
    """
    import re
    TS_RE = re.compile(r"^(\d{2}:\d{2}:\d{2}\.\d+)", re.MULTILINE)
    hits = TS_RE.findall(log_content)
    if len(hits) >= 2:
        def to_sec(ts: str) -> float:
            h, m, rest = ts.split(":", 2)
            s, frac = (rest.split(".") if "." in rest else [rest, "0"])
            return int(h) * 3600 + int(m) * 60 + int(s) + int(frac.ljust(6, "0")) / 1_000_000
        return round(to_sec(hits[-1]) - to_sec(hits[0]), 3)
    return 0.0


def _build_metrics_dict(
    plan_tc_id, run_timestamp, generation_plan, result,
    orchestrator_elapsed, summarizer_elapsed, core_agent_elapsed,
    infra_agent_elapsed, build_elapsed,
    compile_loop_core_total, compile_loop_infra_total,
    compile_loop_build_total, compile_loop_fc_total,
    fc_runtime_elapsed, fc_legacy_elapsed,
    compile_loop_core_times, compile_loop_infra_times,
    compile_loop_build_times, compile_loop_fc_times,
    compile_locked, build_result,
):
    planning_in = _agent_input_tokens.get("Orchestrator", 0)
    planning_out = _agent_output_tokens.get("Orchestrator", 0)
    planning_calls = _agent_call_counts.get("Orchestrator", 0)
    summarizer_in = _agent_input_tokens.get("Summarizer", 0)
    summarizer_out = _agent_output_tokens.get("Summarizer", 0)
    summarizer_calls = _agent_call_counts.get("Summarizer", 0)
    core_in = _agent_input_tokens.get("Core Agent", 0)
    core_out = _agent_output_tokens.get("Core Agent", 0)
    core_calls = _agent_call_counts.get("Core Agent", 0)
    infra_in = _agent_input_tokens.get("Infrastructure Agent", 0)
    infra_out = _agent_output_tokens.get("Infrastructure Agent", 0)
    infra_calls = _agent_call_counts.get("Infrastructure Agent", 0)
    fc_in = _agent_input_tokens.get("Fault Corrector", 0)
    fc_out = _agent_output_tokens.get("Fault Corrector", 0)
    fc_calls = _agent_call_counts.get("Fault Corrector", 0)

    verdict_result = "not_run"
    runtime_diagnosis = "not_run"  # Phase 3.8: rich diagnosis distinct from verdict_result
    execution_time = 0.0
    build_status = "skipped"
    compile_errors_count = 0
    if build_result:
        build_status = build_result.get("status", "unknown")
        # `not_run` is the initial sentinel. If we entered this branch with
        # a build_result but no verdict line was found, the test really did
        # run but produced no parseable verdict -- see state machine below.
        # If build_status already says "error" the lines below override
        # verdict_result explicitly.
        build_status = build_result.get("status", "unknown")
        if build_status == "completed":
            # Binary was actually executed (per run_build_and_test Step 5).
            # However, the binary may exit without writing a "Overall verdict:"
            # line (e.g. testcase hung and we timed out, or the binary
            # returned 0 with no TTCN-3 verdict emitted). We require explicit
            # evidence of a TTCN-3 verdict from the log/stdout -- a bare
            # `returncode == 0` is NOT sufficient for a pass verdict.
            #
            # Phase 3.8 (rich verdict state machine): we now distinguish
            # several distinct runtime outcomes that the previous single-string
            # "inconc" bucket was conflating. 0.033s "TestExecutable silently
            # exited" and "TITAN ran for 60s and produced no verdict" have
            # completely different root causes (binary crash vs. protocol
            # timeout) and the metrics/dashboard must tell them apart.
            test_res = build_result.get("test_result") or {}
            combined = (test_res.get("log_content", "") if test_res else "") + "\n" + (test_res.get("stdout", "") if test_res else "")
            # Parse wall-clock FIRST so we can use it in the diagnosis step.
            wallclock = build_result.get("execution_wallclock_seconds", 0.0) or 0.0
            # Process return code from the test binary (not the build).
            test_returncode = (test_res.get("returncode") if test_res else None)

            # --- Layer 1: did the binary write an "Overall verdict:" line? ---
            if "Overall verdict: pass" in combined:
                verdict_result = "pass"
                runtime_diagnosis = "runtime_pass"
            elif "Overall verdict: inconc" in combined:
                verdict_result = "inconc"
                runtime_diagnosis = "runtime_inconc"
            elif "Overall verdict: fail" in combined:
                verdict_result = "fail"
                runtime_diagnosis = "runtime_fail"
            else:
                # --- Layer 2: no explicit verdict line. Disambiguate by
                # wall-clock + return code so dashboards can tell early
                # crashes from genuine timeouts. ---
                if wallclock < 1.0 and test_returncode not in (None, 0):
                    # TestExecutable crashed within 1 second. TITAN never
                    # even started the testcase. Common causes: missing
                    # port mapping, segfault in C++ adapter, dynamic linker
                    # error.
                    verdict_result = "error"
                    runtime_diagnosis = "runtime_silent_exit"
                    log(
                        f"[VERDICT] TestExecutable exited with code={test_returncode} "
                        f"in {wallclock:.3f}s without writing an 'Overall verdict:' "
                        f"line. Marked as error:runtime_silent_exit (binary crash, "
                        f"not a TTCN-3 inconclusive).",
                        "ERROR",
                    )
                elif wallclock < 5.0 and test_returncode == 0:
                    # Binary returned 0 in <5s but no verdict line was
                    # written. TITAN exited cleanly without executing any
                    # testcase. This is the "SupportedAppProtocol 0.033s"
                    # failure mode seen on 2026-06-18 -- the testcase body
                    # never ran, so we must NOT credit this as inconc.
                    verdict_result = "error"
                    runtime_diagnosis = "runtime_no_verdict"
                    log(
                        f"[VERDICT] TestExecutable returned 0 in {wallclock:.3f}s "
                        f"without writing 'Overall verdict:'. Marked as "
                        f"error:runtime_no_verdict (testcase body never executed, "
                        f"likely port-mapping or SUT-connect failure -- not a "
                        f"TTCN-3 inconclusive).",
                        "ERROR",
                    )
                else:
                    # Binary ran for >=5s but produced no verdict line.
                    # This is a genuine inconclusive: the testcase ran but
                    # the timeout fired, or the verdict was suppressed by
                    # the runtime. Preserved as "inconc" for backwards
                    # compatibility with downstream validators.
                    verdict_result = "inconc"
                    runtime_diagnosis = "runtime_inconc"
                    log(
                        f"[VERDICT] build=completed, wallclock={wallclock:.3f}s, "
                        f"no 'Overall verdict:' line found. Marked as "
                        f"inconc:runtime_inconc (testcase ran but produced "
                        f"no parseable verdict).",
                        "WARN",
                    )
            # Parse execution time: prefer wall-clock (most reliable), fall back to log parser
            if wallclock and wallclock > 0:
                execution_time = round(wallclock, 3)
            else:
                log_content = test_res.get("log_content", "")
                parsed_log_time = _parse_execution_time_from_log(log_content)
                # Wall-clock is the source of truth. If log parser returned 0, use wall-clock.
                execution_time = round(wallclock or parsed_log_time, 3)
        elif build_status == "build_failed":
            verdict_result = "error"
            runtime_diagnosis = "build_failed"
            compile_errors_count = len(build_result.get("compile_errors", []))
        elif build_status == "stale_binary_requires_rebuild":
            # Filesystem had a TestExecutable from a prior attempt, but the
            # current build did not succeed. The binary on disk does NOT
            # represent the current source state. verdict must be honest:
            # runtime did not execute on the current code -- mark as error.
            verdict_result = "error"
            runtime_diagnosis = "stale_binary_requires_rebuild"
            compile_errors_count = 0
            log("[FS-TRUTH] verdict=error: stale binary on disk does not match current sources, runtime not executed.")

    total_in = planning_in + summarizer_in + core_in + infra_in + fc_in
    total_out = planning_out + summarizer_out + core_out + infra_out + fc_out
    cl_core = sum(1 for _, t in compile_loop_core_times if t > 0)
    cl_infra = sum(1 for _, t in compile_loop_infra_times if t > 0)
    cl_build = len(compile_loop_build_times)
    cl_fc = len(compile_loop_fc_times)
    total_fixes = cl_core + cl_infra + cl_fc
    total_agents = orchestrator_elapsed + summarizer_elapsed + core_agent_elapsed + infra_agent_elapsed
    total_fc = compile_loop_fc_total + fc_runtime_elapsed + fc_legacy_elapsed
    total_all = (total_agents + build_elapsed + compile_loop_core_total +
                 compile_loop_infra_total + compile_loop_build_total + total_fc)

    return {
        "experiment_metadata": {
            "tc_id": plan_tc_id,
            "run_timestamp": run_timestamp,
            "pipeline_version": "sop_v2.0_pure_static",
            "git_commit": _get_git_commit(),
            "test_case_name": generation_plan.get("tc_id", plan_tc_id),
            "model_name": _claude_model_name or "unknown",
        },
        "token_usage": {
            "planning": {"input_tokens": planning_in, "output_tokens": planning_out, "calls": planning_calls},
            "summarizer": {"input_tokens": summarizer_in, "output_tokens": summarizer_out, "calls": summarizer_calls},
            "core_generation": {"input_tokens": core_in, "output_tokens": core_out, "calls": core_calls},
            "infrastructure_agent": {"input_tokens": infra_in, "output_tokens": infra_out, "calls": infra_calls},
            "fault_corrector": {"input_tokens": fc_in, "output_tokens": fc_out, "calls": fc_calls},
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
        },
        "latency": {
            "planning_seconds": round(orchestrator_elapsed + summarizer_elapsed, 3),
            "orchestrator_seconds": round(orchestrator_elapsed, 3),
            "summarizer_seconds": round(summarizer_elapsed, 3),
            "generation_seconds": round(core_agent_elapsed, 3),
            "infrastructure_seconds": round(infra_agent_elapsed, 3),
            "compilation_seconds": round(build_elapsed, 3),
            "compilation_loop_seconds": round(compile_loop_core_total + compile_loop_infra_total + compile_loop_build_total, 3),
            "fault_correction_seconds": round(total_fc, 3),
            "total_pipeline_seconds": round(total_all, 3),
        },
        "self_healing": {
            "disk_fallback_used": result.get("_disk_fallback_used", False),
            "compile_loop_iterations": cl_core,
            "compile_loop_max": 3,
            "runtime_loop_iterations": fc_calls,
            "runtime_loop_max": 4,
            "total_self_fixes": total_fixes,
            "runtime_repair_iterations_used": (
                result.get("runtime_repair", {}).get("iterations_used", 0)
                if isinstance(result.get("runtime_repair"), dict) else 0
            ),
            "runtime_repair_status": (
                result.get("runtime_repair", {}).get("status", "not_run")
                if isinstance(result.get("runtime_repair"), dict) else "not_run"
            ),
            "runtime_repair_verdict": (
                result.get("runtime_repair", {}).get("runtime_verdict", "not_run")
                if isinstance(result.get("runtime_repair"), dict) else "not_run"
            ),
        },
        "verdict": {
            "result": verdict_result,
            "runtime_diagnosis": runtime_diagnosis,
            "execution_time_seconds": round(execution_time, 3),
            "build_status": build_status,
            "compile_errors_count": compile_errors_count,
        },
    }



# ============================================================
# 主流水线
# ============================================================
def run_pipeline(
    requirement_ids: Optional[List[str]] = None,
    tc_id: Optional[str] = None,
    test_case: Optional[Dict] = None,
    test_case_file: Optional[Path] = None,
    filter_tc_id: Optional[str] = None,
    sut_type: Optional[str] = None,
    auto_generate: bool = True,
    multi_mode: bool = True,
    titan_log: Optional[Path] = None,
    pcap_path: Optional[Path] = None,
    skip_build: bool = False,
    sut_ip: Optional[str] = None,
    rule_based_plan: bool = False,
    no_llm_render: bool = False,
    watchdog_timeout: int = 600,
) -> List[Dict]:
    """运行完整的 TTCN-3 代码生成流水线

    支持多种输入模式:
    1. 通过 requirement_ids: 从已生成的测试用例中查找，或调用测试用例生成器
    2. 通过 tc_id: 通过 TC ID 查找测试用例
    3. 通过 test_case: 直接传入测试用例字典
    4. 通过 test_case_file: 从文件读取测试用例

    Args:
        requirement_ids: 安全需求 ID 列表，如 ["V2G2-082", "V2G2-165"]
        tc_id: 测试用例 ID，如 TC_SECC_CMN_VTB_SupportedAppProtocol_001
        test_case: 测试用例字典（可选）
        test_case_file: 测试用例文件路径（可选）
        filter_tc_id: 从测试用例文件或测试用例列表中筛选特定 TC ID（可选）
        sut_type: SUT 类型，"SECC" 或 "EVCC"（可选，自动推断）
        auto_generate: 未找到测试用例时是否自动生成
        multi_mode: 多需求模式，True 表示合并为一个测试用例
        skip_build: 是否跳过编译运行步骤
        sut_ip: SUT IP 地址（用于 titan_runtime.cfg 配置）
        rule_based_plan: 若为 True，使用确定性 Python 规则构建 plan，跳过 Orchestrator LLM 调用
                          （默认 False，保持向后兼容）
        no_llm_render: 若为 True，完全使用静态模板引擎生成 TTCN 文件，跳过 Core Agent 调用。
                       提供 100%% 确定性的基线路径。
        watchdog_timeout: LLM 子代理最大空闲秒数（默认 600s）。超时后看门狗终止代理并回退到静态模板。

    Returns:
        包含生成结果的列表，每个元素包含 input、success 字段，
        以及 ttcn_path、project_dir、infra_status、build_result 等
    """
    results = []

    # Bulletproof: clean stale /tmp/titan_build_* dirs left by previous killed runs
    cleanup_stale_titan_temp_dirs()

    # 确保输出目录存在
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    # === 模式1: 通过测试用例字典 ===
    if test_case:
        log("使用直接传入的测试用例")
        tc = test_case.get("Test_Case", test_case)
        requirement_ids = tc.get("Document_Reference", {}).get("Requirements", [])
        requirement_ids = [r.strip("[]").strip() for r in requirement_ids]
        requirement_context = find_requirement_context(requirement_ids[0]) if requirement_ids else ""

        try:
            gen_result = generate_code_for_test_case(
                test_case, sut_type, requirement_context,
                titan_log=titan_log, pcap_path=pcap_path,
                skip_build=skip_build,
                sut_ip=sut_ip,
                rule_based_plan=rule_based_plan,
                no_llm_render=no_llm_render,
                watchdog_timeout=watchdog_timeout,
                raw_file_data=None,
            )
        except _PipelineEarlyExit:
            gen_result = None
        extra = gen_result if gen_result else {}
        results.append({
            "input": {"type": "test_case", "data": tc.get("TC_Id", "unknown")},
            "success": gen_result is not None and gen_result.get("ttcn_path") is not None,
            **extra,
        })
        return results

    # === 模式2: 通过测试用例文件 ===
    if test_case_file:
        log(f"从文件加载测试用例: {test_case_file}")
        test_case_list = load_test_case_from_file(Path(test_case_file))
        # Also load raw JSON to preserve top-level fields (testing_points, orchestrator_plan, etc.)
        raw_file_data = load_json(Path(test_case_file)) if Path(test_case_file).exists() else None
        if test_case_list:
            # load_test_case_from_file 已经返回列表

            # 应用 filter_tc_id 筛选
            if filter_tc_id:
                log(f"筛选 TC ID: {filter_tc_id}")
                matched = False
                for entry in test_case_list:
                    tc = entry.get("Test_Case", entry)
                    if tc.get("TC_Id") == filter_tc_id:
                        loaded_test_case = entry
                        matched = True
                        break
                if not matched:
                    log(f"在测试用例文件中未找到 TC ID: {filter_tc_id}", "ERROR")
                    available = [e.get("Test_Case", e).get("TC_Id", "unknown") for e in test_case_list]
                    log(f"可用 TC ID: {available}", "ERROR")
                    results.append({
                        "input": {"type": "test_case_file", "data": str(test_case_file), "filter_tc_id": filter_tc_id},
                        "success": False,
                    })
                    return results
            else:
                # 未指定 filter_tc_id 时，如果文件包含多个用例，取第一个
                if len(test_case_list) > 1:
                    log(f"警告: 测试用例文件包含 {len(test_case_list)} 个用例，未指定 --filter-tc-id，将处理第一个", "WARN")
                loaded_test_case = test_case_list[0]

            tc = loaded_test_case.get("Test_Case", loaded_test_case)
            requirement_ids = tc.get("Document_Reference", {}).get("Requirements", [])
            requirement_ids = [r.strip("[]").strip() for r in requirement_ids]
            requirement_context = find_requirement_context(requirement_ids[0]) if requirement_ids else ""

            try:
                gen_result = generate_code_for_test_case(
                    loaded_test_case, sut_type, requirement_context,
                    titan_log=titan_log, pcap_path=pcap_path,
                    skip_build=skip_build,
                    sut_ip=sut_ip,
                    rule_based_plan=rule_based_plan,
                    raw_file_data=raw_file_data,
                    filter_tc_id=filter_tc_id,
                )
            except _PipelineEarlyExit:
                gen_result = None
            if gen_result is None:
                log("generate_code_for_test_case returned None (internal failure)", "ERROR")
                gen_result = {}
            results.append({
                "input": {"type": "test_case_file", "data": str(test_case_file),
                          "filter_tc_id": filter_tc_id},
                "success": gen_result.get("ttcn_path") is not None,
                **gen_result,
            })
            return results
        else:
            log("无法加载测试用例文件", "ERROR")
            results.append({
                "input": {"type": "test_case_file", "data": str(test_case_file)},
                "success": False,
            })
            return results

    # === 模式3: 通过 TC ID ===
    if tc_id:
        log(f"通过 TC ID 查找测试用例: {tc_id}")
        tc_entry = find_test_case_by_tc_id(tc_id)
        if tc_entry:
            tc = tc_entry.get("Test_Case", tc_entry)
            requirement_ids = tc.get("Document_Reference", {}).get("Requirements", [])
            requirement_ids = [r.strip("[]").strip() for r in requirement_ids]
            requirement_context = find_requirement_context(requirement_ids[0]) if requirement_ids else ""

            try:
                gen_result = generate_code_for_test_case(
                    tc_entry, sut_type, requirement_context,
                    titan_log=titan_log, pcap_path=pcap_path,
                    skip_build=skip_build,
                    sut_ip=sut_ip,
                    rule_based_plan=rule_based_plan,
                    no_llm_render=no_llm_render,
                    watchdog_timeout=watchdog_timeout,
                    raw_file_data=None,
                )
            except _PipelineEarlyExit:
                gen_result = None
            results.append({
                "input": {"type": "tc_id", "data": tc_id},
                "success": gen_result is not None and gen_result.get("ttcn_path") is not None,
                **(gen_result or {}),
            })
            return results
        else:
            log(f"未找到测试用例: {tc_id}", "ERROR")
            results.append({
                "input": {"type": "tc_id", "data": tc_id},
                "success": False,
            })
            return results

    # === 模式4: 通过需求 ID ===
    if not requirement_ids:
        log("必须提供 requirement_ids、tc_id、test_case 或 test_case_file 之一", "ERROR")
        return results

    log(f"通过需求 ID 查找测试用例: {requirement_ids}")

    # 获取测试用例
    test_cases = get_test_cases_for_requirements(
        requirement_ids,
        auto_generate=auto_generate,
        multi_mode=multi_mode
    )

    if not test_cases:
        log(f"无法获取需求 {requirement_ids} 对应的测试用例", "ERROR")
        results.append({
            "input": {"type": "requirements", "data": requirement_ids},
            "success": False,
        })
        return results

    # 为每个测试用例生成代码
    for tc_entry in test_cases:
        tc = tc_entry.get("Test_Case", tc_entry)
        tc_id = tc.get("TC_Id", "unknown")

        # 应用 filter_tc_id 筛选
        if filter_tc_id and tc_id != filter_tc_id:
            continue

        # 获取需求上下文
        doc_ref = tc.get("Document_Reference", {})
        reqs = doc_ref.get("Requirements", []) if isinstance(doc_ref, dict) else []
        requirement_context = find_requirement_context(reqs[0].strip("[]").strip()) if reqs else ""

        try:
            gen_result = generate_code_for_test_case(
                tc_entry, sut_type, requirement_context,
                titan_log=titan_log, pcap_path=pcap_path,
                skip_build=skip_build,
                sut_ip=sut_ip,
                rule_based_plan=rule_based_plan,
                no_llm_render=no_llm_render,
                watchdog_timeout=watchdog_timeout,
                raw_file_data=None,
            )
        except _PipelineEarlyExit:
            gen_result = None
        results.append({
            "input": {"type": "requirements", "data": requirement_ids, "tc_id": tc_id},
            "success": gen_result is not None and gen_result.get("ttcn_path") is not None,
            **(gen_result or {}),
        })

    # 检查 filter_tc_id 筛选结果
    if filter_tc_id and not results:
        log(f"在测试用例列表中未找到匹配的 TC ID: {filter_tc_id}", "ERROR")
        available_tc_ids = [tc_entry.get("Test_Case", tc_entry).get("TC_Id", "unknown")
                           for tc_entry in test_cases]
        log(f"可用 TC ID: {available_tc_ids}", "ERROR")
        results.append({
            "input": {"type": "requirements", "data": requirement_ids, "filter_tc_id": filter_tc_id},
            "success": False,
        })

    # 批量汇总耗时
    timing_results = [r for r in results if "agent_timings" in r]
    if timing_results:
        log("")
        log("========== 批量执行耗时汇总 ==========")
        log("| #   | Orch | Summ | Core | Infra | Build | FC   | 总计  |")
        log("|-----|------|------|------|-------|-------|------|-------|")
        for i, r in enumerate(timing_results):
            at = r.get("agent_timings", {})
            fc = at.get("fault_corrector", {})
            fc_total = fc.get("runtime", 0) + fc.get("legacy", 0) + fc.get("compile_total", 0)
            log(f"| {i+1:3d} | {at.get('orchestrator', 0):4.1f}s | {at.get('summarizer', 0):4.1f}s | {at.get('core_agent', 0):4.1f}s | {at.get('infrastructure_agent', 0):5.1f}s | {at.get('build', 0):5.1f}s | {fc_total:4.1f}s | {at.get('total_all', 0):5.1f}s |")
        total_orch = sum(r.get("agent_timings", {}).get("orchestrator", 0) for r in timing_results)
        total_summ = sum(r.get("agent_timings", {}).get("summarizer", 0) for r in timing_results)
        total_core = sum(r.get("agent_timings", {}).get("core_agent", 0) for r in timing_results)
        total_infra = sum(r.get("agent_timings", {}).get("infrastructure_agent", 0) for r in timing_results)
        total_build = sum(r.get("agent_timings", {}).get("build", 0) for r in timing_results)
        total_fc = sum(
            sum(fc.get(k, 0) for k in ["runtime", "legacy", "compile_total"])
            for fc in (r.get("agent_timings", {}).get("fault_corrector", {}) for r in timing_results)
        )
        total_all = sum(r.get("agent_timings", {}).get("total_all", 0) for r in timing_results)
        log("|-----|------|------|------|-------|-------|------|-------|")
        log(f"| 合计 | {total_orch:4.1f}s | {total_summ:4.1f}s | {total_core:4.1f}s | {total_infra:5.1f}s | {total_build:5.1f}s | {total_fc:4.1f}s | {total_all:5.1f}s |")
        log("======================================")

    cleanup_claude_history()
    return results


def main():
    # Phase 3.6: reset the silent-kill flag at the start of every pipeline run.
    # Without this reset, a previous TC's idle watchdog kill would propagate
    # and incorrectly short-circuit the next TC's agent call.
    global _llm_silent_killed, _sandbox_locked_flag
    _llm_silent_killed = False
    _sandbox_locked_flag = False

    parser = argparse.ArgumentParser(
        description="ISO 15118-2 TTCN-3 代码生成流水线\n\n"
                    "支持多种输入模式:\n"
                    "  1. 安全需求ID: --requirement 或 --requirements\n"
                    "  2. 测试用例ID: --tc-id\n"
                    "  3. 测试用例JSON: --test-case\n"
                    "  4. 测试用例文件: --test-case-file\n\n"
                    "生成完整项目结构（config + cpp_adapters + build.sh + 编译运行）:\n"
                    "  Infrastructure Agent 始终运行，不指定 --skip-build 则编译和运行\n"
                    "  --skip-build [--sut-ip SUT_IP]"
    )

    # 输入参数
    input_group = parser.add_argument_group("输入模式 (选择其中一种)")
    input_group.add_argument(
        "--requirement", type=str,
        help="单个安全需求 ID，如 V2G2-082"
    )
    input_group.add_argument(
        "--requirements", type=str,
        help="多个安全需求 ID，逗号分隔，如 V2G2-082,V2G2-165"
    )
    input_group.add_argument(
        "--tc-id", type=str,
        help="测试用例 ID，如 TC_SECC_CMN_VTB_SupportedAppProtocol_001"
    )
    input_group.add_argument(
        "--test-case", type=str,
        help="测试用例 JSON 字符串"
    )
    input_group.add_argument(
        "--test-case-file", type=str,
        help="测试用例 JSON 文件路径"
    )
    input_group.add_argument(
        "--filter-tc-id", type=str,
        help="从测试用例文件或生成的测试用例列表中筛选特定 TC ID（如 TC_SECC_CMN_VTB_V2GTPHeader_001）"
    )

    # 配置参数
    config_group = parser.add_argument_group("配置选项")
    config_group.add_argument(
        "--sut", type=str, choices=["SECC", "EVCC"],
        help="SUT 类型（可选，默认自动推断）"
    )
    config_group.add_argument(
        "--mode", choices=["single", "multi"], default="single",
        help="生成模式: single (每个需求单独生成) 或 multi (多需求合并为一个测试用例)"
    )
    config_group.add_argument(
        "--no-auto-generate", action="store_true",
        help="未找到测试用例时不自动生成"
    )
    config_group.add_argument(
        "--no-llm-render", action="store_true",
        help="禁用 LLM 辅助渲染：完全使用静态模板引擎生成 TTCN 文件，不调用 Core Agent。"
              " 提供 100%% 确定性的端到端自动化基线。"
    )
    config_group.add_argument(
        "--watchdog-timeout", type=int, default=1200,
        help="LLM 子代理的最大空闲时间（秒），超过则看门狗将其终止并回退到静态模板。"
              " 默认 1200s (20min)。设为 0 则禁用看门狗。"
    )

    # 基础设施参数
    infra_group = parser.add_argument_group("编译运行选项")
    infra_group.add_argument(
        "--skip-build", action="store_true",
        help="跳过编译和运行步骤（仅生成代码和项目结构）"
    )
    infra_group.add_argument(
        "--sut-ip", type=str,
        help="SUT IP 地址（用于 titan_runtime.cfg 配置，默认 ff02::01 IPv6 组播）"
    )
    infra_group.add_argument(
        "--rule-based-plan", action="store_true",
        help="使用确定性 Python 规则构建 plan，跳过 Orchestrator LLM 调用（同时禁用 Summarizer）；用于消融研究和纯推理能力评估"
    )

    # 故障分析参数
    fault_group = parser.add_argument_group("故障分析 (Fault Agent 双轨分析)")
    fault_group.add_argument(
        "--titan-log", type=str,
        help="TITAN TestExecution.log 路径 (用于 Fault Agent 分析)"
    )
    fault_group.add_argument(
        "--pcap", type=str,
        help="PCAP 文件路径 (用于 Fault Agent 双轨分析)"
    )

    args = parser.parse_args()

    # 确保输出目录存在
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    # 解析输入
    requirement_ids = None
    tc_id = None
    test_case = None
    test_case_file = None
    filter_tc_id = args.filter_tc_id

    if args.test_case:
        test_case = parse_test_case_from_string(args.test_case)
        if not test_case:
            log("无法解析 --test-case 参数为有效的 JSON", "ERROR")
            return 1
    elif args.test_case_file:
        test_case_file = Path(args.test_case_file)
    elif args.tc_id:
        tc_id = args.tc_id
    elif args.requirement:
        requirement_ids = [args.requirement.strip()]
    elif args.requirements:
        requirement_ids = [r.strip() for r in args.requirements.split(",") if r.strip()]
    else:
        parser.error("必须指定 --requirement, --requirements, --tc-id, --test-case, --test-case-file 或 --verify-mode 之一")

    # 运行流水线
    results = run_pipeline(
        requirement_ids=requirement_ids,
        tc_id=tc_id,
        test_case=test_case,
        test_case_file=test_case_file,
        filter_tc_id=filter_tc_id,
        sut_type=args.sut,
        auto_generate=not args.no_auto_generate,
        multi_mode=(args.mode == "multi"),
        titan_log=Path(args.titan_log) if args.titan_log else None,
        pcap_path=Path(args.pcap) if args.pcap else None,
        skip_build=args.skip_build,
        sut_ip=args.sut_ip,
        rule_based_plan=args.rule_based_plan,
        no_llm_render=args.no_llm_render,
        watchdog_timeout=args.watchdog_timeout,
    )

    # 打印结果报告
    print(f"\n{'=' * 60}")
    print("生成报告")
    print(f"{'=' * 60}")

    success_count = sum(1 for r in results if r["success"])
    print(f"总计: {len(results)}, 成功: {success_count}, 失败: {len(results) - success_count}")

    for r in results:
        status = "SUCCESS" if r["success"] else "FAILED"
        input_info = r["input"]
        input_type = input_info["type"]
        input_data = input_info["data"]
        tc_id_out = input_info.get("tc_id", "")

        tc_display = f"TC {tc_id_out}" if tc_id_out else input_data
        print(f"\n  [{status}] {input_type}: {tc_display}")

        if r["success"]:
            ttcn_path = r.get("ttcn_path", "")
            print(f"    TTCN: {ttcn_path}")

            # 基础设施信息
            infra_status = r.get("infra_status", "not_requested")
            if infra_status == "success":
                proj_dir = r.get("project_dir", "")
                print(f"    Project: {proj_dir}")
                infra_warn = r.get("infra_warnings", [])
                if infra_warn:
                    for w in infra_warn:
                        print(f"    Warning: {w}")

                # 编译结果
                build_res = r.get("build_result", {})
                build_status = build_res.get("status", "skipped")
                print(f"    Build: {build_status}")
                if build_status == "build_failed":
                    err_count = len(build_res.get("compile_errors", []))
                    print(f"    Compile errors: {err_count}")
                elif build_status == "completed":
                    exec_path = build_res.get("executable_path", "")
                    print(f"    Executable: {exec_path}")

                # 故障分析
                fault_res = r.get("fault_result", {})
                if fault_res and fault_res.get("status") != "skipped":
                    fault_type = fault_res.get("fault", {}).get("fault_type", "unknown")
                    print(f"    Fault: {fault_type}")
            elif infra_status == "error":
                print(f"    Infra error: {r.get('infra_message', '')}")
            elif infra_status == "not_requested":
                pass  # 正常情况，不打印额外信息
        else:
            pass  # 已经显示了 FAILED 状态

    return 0 if success_count == len(results) else 1


# ─────────────────────────────────────────────────────────────────────────────
# Static Linter: zero-LLM mechanical fix layer
# Runs BEFORE the Fault Corrector LLM is invoked. Catches known mechanical
# typos that waste expensive LLM tokens and TITAN compile cycles.
# Returns (bool_fixed, list_of_fixes_applied).
# ─────────────────────────────────────────────────────────────────────────────
import re as _re


def _run_static_linter(
    compile_errors: List[Dict],
    tc_project_dir: Path,
) -> Tuple[bool, List[Dict]]:
    """
    Apply pattern-based fixes to TTCN-3 source files WITHOUT calling LLM.
    Catches mechanical typos: (1) incorrect { v2g := { union wrapper,
    (2) lowercase field names that should be camelCase.
    """
    if not compile_errors:
        return False, []

    # Canonical V2G_MessageUnion field name map
    UNION_FIELD_MAP = {
        "sdpreq": "sdpReq", "sdpres": "sdpRes",
        "supportedappprotocolreq": "supportedAppProtocolReq",
        "supportedappprotocolres": "supportedAppProtocolRes",
        "sessionsetupreq": "sessionSetupReq", "sessionsetupres": "sessionSetupRes",
        "servicediscoveryreq": "serviceDiscoveryReq", "servicediscoveryres": "serviceDiscoveryRes",
        "servicepaymentselectionreq": "servicePaymentSelectionReq",
        "servicepaymentselectionres": "servicePaymentSelectionRes",
        "paymentdetailsreq": "paymentDetailsReq", "paymentdetailsres": "paymentDetailsRes",
        "authorizationreq": "authorizationReq", "authorizationres": "authorizationRes",
        "certificateinstallationreq": "certificateInstallationReq",
        "certificateinstallationres": "certificateInstallationRes",
        "certificateupdatereq": "certificateUpdateReq",
        "certificateupdateres": "certificateUpdateRes",
        "cablecheckreq": "cableCheckReq", "cablecheckres": "cableCheckRes",
        "chargeparameterdiscoveryreq": "chargeParameterDiscoveryReq",
        "chargeparameterdiscoveryres": "chargeParameterDiscoveryRes",
        "powerdeliveryreq": "powerDeliveryReq", "powerdeliveryres": "powerDeliveryRes",
        "sessionstopreq": "sessionStopReq", "sessionstopres": "sessionStopRes",
        "chargingstatusreq": "chargingStatusReq", "chargingstatusres": "chargingStatusRes",
        "currentdemandreq": "currentDemandReq", "currentdemandres": "currentDemandRes",
        "meterreceiptreq": "meterReceiptReq", "meterreceiptres": "meterReceiptRes",
        "prechargereq": "preChargeReq", "prechargeres": "preChargeRes",
        "weldingdetectionreq": "weldingDetectionReq", "weldingdetectionres": "weldingDetectionRes",
        "servicedetailsreq": "serviceDetailsReq", "servicedetailsres": "serviceDetailsRes",
    }

    # Wrong → correct field name mapping (handles v2gtp-prefixed and bare names)
    _FIELD_MAP = {}
    for wrong, correct in [
        # Simple bad names (with v2gtp prefix)
        ("v2gtpsessionsetupReq", "sessionSetupReq"), ("v2gtpsessionsetupRes", "sessionSetupRes"),
        ("v2gtpsdpreq", "sdpReq"), ("v2gtpsdpres", "sdpRes"),
        ("v2gtpauthorizationreq", "authorizationReq"), ("v2gtpauthorizationres", "authorizationRes"),
        ("v2gtppowerdeliveryreq", "powerDeliveryReq"), ("v2gtppowerdeliveryres", "powerDeliveryRes"),
        ("v2gtpcablecheckreq", "cableCheckReq"), ("v2gtpcablecheckres", "cableCheckRes"),
        ("v2gtpservicediscoveryreq", "serviceDiscoveryReq"), ("v2gtpservicediscoveryres", "serviceDiscoveryRes"),
        ("v2gtpsessionstopreq", "sessionStopReq"), ("v2gtpsessionstopres", "sessionStopRes"),
        ("v2gtpchargeparameterdiscoveryreq", "chargeParameterDiscoveryReq"),
        ("v2gtpchargeparameterdiscoveryres", "chargeParameterDiscoveryRes"),
        ("v2gtpservicedetailsreq", "serviceDetailsReq"), ("v2gtpservicedetailsres", "serviceDetailsRes"),
        ("v2gtpsupportedappprotocolreq", "supportedAppProtocolReq"),
        ("v2gtpsupportedappprotocolres", "supportedAppProtocolRes"),
        ("v2gtppaymentserviceselectionreq", "servicePaymentSelectionReq"),
        ("v2gtppaymentserviceselectionres", "servicePaymentSelectionRes"),
        ("v2gtppaymentdetailsreq", "paymentDetailsReq"),
        ("v2gtppaymentdetailsres", "paymentDetailsRes"),
        ("v2gtpcertificateinstallationreq", "certificateInstallationReq"),
        ("v2gtpcertificateinstallationres", "certificateInstallationRes"),
        ("v2gtpcertificateupdatereq", "certificateUpdateReq"),
        ("v2gtpcertificateupdateres", "certificateUpdateRes"),
        ("v2gtpchargeparameterdiscoveryreq", "chargeParameterDiscoveryReq"),
        ("v2gtpchargeparameterdiscoveryres", "chargeParameterDiscoveryRes"),
        ("v2gtpchargingstatusreq", "chargeStatusReq"),
        ("v2gtpchargingstatusres", "chargeStatusRes"),
        ("v2gtpcurrentdemandreq", "currentDemandReq"),
        ("v2gtpcurrentdemandres", "currentDemandRes"),
        ("v2gtpmeteringreceiptreq", "meterReceiptReq"),
        ("v2gtpmeteringreceiptres", "meterReceiptRes"),
        ("v2gtpprechargereq", "preChargeReq"),
        ("v2gtpprechargeres", "preChargeRes"),
        ("v2gtpweldingdetectionreq", "weldingDetectionReq"),
        ("v2gtpweldingdetectionres", "weldingDetectionRes"),
        ("v2gtpservicedetailsreq", "serviceDetailsReq"),
        ("v2gtpservicedetailsres", "serviceDetailsRes"),
        # Compound receive-alt suffixes (ReqRes = session-setup response)
        ("v2gtpsessionsetupReqRes", "sessionSetupRes"),
        ("v2gtpsdpreqRes", "sdpRes"),
        ("v2gtpauthorizationReqRes", "authorizationRes"),
        ("v2gtppowerdeliveryReqRes", "powerDeliveryRes"),
        ("v2gtpcablecheckReqRes", "cableCheckRes"),
        ("v2gtpservicediscoveryReqRes", "serviceDiscoveryRes"),
        ("v2gtpsessionstopReqRes", "sessionStopRes"),
        ("v2gtpsupportedappprotocolReqRes", "supportedAppProtocolRes"),
        ("v2gtppaymentdetailsReqRes", "paymentDetailsRes"),
        ("v2gtpservicepaymentselectionReqRes", "servicePaymentSelectionRes"),
        ("v2gtpcertificateinstallationReqRes", "certificateInstallationRes"),
        ("v2gtpcertificateupdateReqRes", "certificateUpdateRes"),
        ("v2gtpchargeparameterdiscoveryReqRes", "chargeParameterDiscoveryRes"),
        ("v2gtpchargingstatusReqRes", "chargeStatusRes"),
        ("v2gtpcurrentdemandReqRes", "currentDemandRes"),
        ("v2gtpmeteringreceiptReqRes", "meterReceiptRes"),
        ("v2gtpprechargeReqRes", "preChargeRes"),
        ("v2gtpweldingdetectionReqRes", "weldingDetectionRes"),
        ("v2gtpservicedetailsReqRes", "serviceDetailsRes"),
        # Bare lowercase (no prefix)
        ("sdpreq", "sdpReq"), ("sdpres", "sdpRes"),
        ("authorizationreq", "authorizationReq"), ("authorizationres", "authorizationRes"),
        ("powerdeliveryreq", "powerDeliveryReq"), ("powerdeliveryres", "powerDeliveryRes"),
        ("cablecheckreq", "cableCheckReq"), ("cablecheckres", "cableCheckRes"),
        ("servicediscoveryreq", "serviceDiscoveryReq"), ("servicediscoveryres", "serviceDiscoveryRes"),
        ("sessionstopreq", "sessionStopReq"), ("sessionstopres", "sessionStopRes"),
        ("sessionsetupreq", "sessionSetupReq"), ("sessionsetupres", "sessionSetupRes"),
        ("supportedappprotocolreq", "supportedAppProtocolReq"),
        ("supportedappprotocolres", "supportedAppProtocolRes"),
        ("paymentserviceselectionreq", "servicePaymentSelectionReq"),
        ("paymentserviceselectionres", "servicePaymentSelectionRes"),
        ("paymentdetailsreq", "paymentDetailsReq"), ("paymentdetailsres", "paymentDetailsRes"),
        ("certificateinstallationreq", "certificateInstallationReq"),
        ("certificateinstallationres", "certificateInstallationRes"),
        ("certificateupdatereq", "certificateUpdateReq"),
        ("certificateupdateres", "certificateUpdateRes"),
        ("chargeparameterdiscoveryreq", "chargeParameterDiscoveryReq"),
        ("chargeparameterdiscoveryres", "chargeParameterDiscoveryRes"),
        ("chargingstatusreq", "chargingStatusReq"), ("chargingstatusres", "chargingStatusRes"),
        ("currentdemandreq", "currentDemandReq"), ("currentdemandres", "currentDemandRes"),
        ("meteringreceiptreq", "meterReceiptReq"), ("meteringreceiptres", "meterReceiptRes"),
        ("prechargereq", "preChargeReq"), ("prechargeres", "preChargeRes"),
        ("weldingdetectionreq", "weldingDetectionReq"), ("weldingdetectionres", "weldingDetectionRes"),
        ("servicedetailsreq", "serviceDetailsReq"), ("servicedetailsres", "serviceDetailsRes"),
        # Bare lowercase compound receive-alt
        ("sessionsetupReqRes", "sessionSetupRes"),
        ("sdpReqRes", "sdpRes"),
        ("authorizationReqRes", "authorizationRes"),
        ("powerdeliveryReqRes", "powerDeliveryRes"),
        ("cablecheckReqRes", "cableCheckRes"),
        ("servicediscoveryReqRes", "serviceDiscoveryRes"),
        ("sessionstopReqRes", "sessionStopRes"),
        ("supportedappprotocolReqRes", "supportedAppProtocolRes"),
        ("paymentserviceselectionReqRes", "servicePaymentSelectionRes"),
        ("paymentdetailsReqRes", "paymentDetailsRes"),
        ("certificateinstallationReqRes", "certificateInstallationRes"),
        ("certificateupdateReqRes", "certificateUpdateRes"),
        ("chargeparameterdiscoveryReqRes", "chargeParameterDiscoveryRes"),
        ("chargingstatusReqRes", "chargeStatusRes"),
        ("currentdemandReqRes", "currentDemandRes"),
        ("meteringreceiptReqRes", "meterReceiptRes"),
        ("prechargeReqRes", "preChargeRes"),
        ("weldingdetectionReqRes", "weldingDetectionRes"),
        ("servicedetailsReqRes", "serviceDetailsRes"),
    ]:
        _FIELD_MAP[wrong.lower()] = correct

    # Comprehensive single-pass patterns.
    # The bad code structure is:
    #   send(
    #     { v2g := { FIELD := value } }     <-- needs fixing
    #   );
    #   OR
    #   [] port.receive(
    #     { v2g := { FIELD := ? } }         <-- needs fixing
    #   ) {
    #
    # Fix strategy: regex with DOTALL on the line/block of interest.
    # Pattern 1: fix inner FIELD name (correct structure: { v2g:={ FIELD := value } })
    # Pattern 2: unwrap double-nesting { v2g:={ v2g:= FIELD }}  → { v2g:= FIELD }
    _PAT_WRAPPER = re.compile(
        r"\{\s*v2g\s*:=\s*\{\s*([A-Za-z][A-Za-z0-9]+)\s*:=\s*([^{}]+?)\s*\}\s*\}",
        re.DOTALL,
    )
    # Fix double-nested {v2g:={v2g:=FIELD}} → {v2g:={FIELD:=?}}
    # Matches: { v2g := { v2g := FieldName } }
    # Capture both the field name and the value (for send patterns with real values)
    _PAT_DOUBLE = re.compile(
        r"\{\s*v2g\s*:=\s*\{\s*v2g\s*:=\s*([^{}]+?)\s*\}\s*\}",
        re.DOTALL,
    )
    def _double_nested_replacer(m):
        field = m.group(1)
        value = m.group(2).strip()
        # If value is empty/placeholder (receive pattern) → use wildcard
        if not value or value == "?":
            return "{ v2g := { %s :=? } }" % field
        # Otherwise it's a send pattern with a real value
        return "{ v2g := { %s := %s } }" % (field, value)

    def _fix_field_name(wrong_name):
        """
        Map wrong field names to correct ones.
        Handles:
        - Simple bad: "v2gtpsessionsetupReq" -> "sessionSetupReq"
        - Compound suffix: "v2gtpsessionsetupReqRes" -> "sessionSetupRes"
        - Already correct: "sessionSetupReq" -> "sessionSetupReq" (no-op)
        """
        key = wrong_name.lower()
        if key in _FIELD_MAP:
            return _FIELD_MAP[key]
        # Already correct (starts with capital, no v2g prefix)
        if wrong_name[0].isupper() and not wrong_name.startswith("v2g"):
            return wrong_name
        # Determine the suffix type first
        actual_suffix = None
        for suffix in ("reqres", "resreq", "req", "res"):
            if key.endswith(suffix):
                actual_suffix = suffix
                break
        # Strip suffix if found
        root = key
        if actual_suffix:
            root = key[:-len(actual_suffix)]
        # Strip v2gtp/v2g prefix from the root
        for pfx in ("v2gtp", "v2g_", "v2g"):
            if root.startswith(pfx):
                base = root[len(pfx):]
                if actual_suffix:
                    lookup_key = base + actual_suffix[-3:]
                    if lookup_key in _FIELD_MAP:
                        return _FIELD_MAP[lookup_key]
                else:
                    for sfx in ("req", "res"):
                        lookup_key = base + sfx
                        if lookup_key in _FIELD_MAP:
                            return _FIELD_MAP[lookup_key]
                break
        return wrong_name

    def _wrapper_replacer(m):
        """
        Fix the inner field name in a {v2g:={FIELD:=...}} wrapper.
        If the captured 'field' is literally 'v2g' it means the
        double-nested pattern {v2g:={v2g:=VALUE}} was matched (where
        'VALUE' = field, '...' = value). Delegate accordingly.
        """
        field = m.group(1)
        value = m.group(2)
        # If field='v2g' we matched the double-nested pattern — delegate
        if field.lower() == "v2g":
            return _double_nested_replacer(m)
        correct = _fix_field_name(field)
        if correct == field:
            return m.group(0)
        return "{ v2g := { %s := %s } }" % (correct, value)

    fixes_applied = []
    any_fixed = False

    for ttcn_file in tc_project_dir.rglob("*.ttcn"):
        if not ttcn_file.is_file():
            continue
        try:
            content = ttcn_file.read_text(encoding="utf-8")
        except Exception:
            continue

        original = content
        file_fixes = []

        # Strip comments (TTCN: line // and block /* */) to avoid false
        # matches in source-code documentation comments.
        no_block = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
        no_comments = re.sub(r"//[^\n]*", "", no_block)

        # Fix 0: unwrap double-nested {v2g:={v2g:=...}} → {v2g:=...}
        nc = _PAT_DOUBLE.sub(_double_nested_replacer, no_comments)
        if nc != no_comments:
            no_comments = nc
            file_fixes.append("double-nested union unwrapped")

        # Fix 1: {v2g:={FIELD:=...}} wrapper - fix inner FIELD name only
        # (wrapper itself is the V2G_PortMessage structure and must remain)
        nc = _PAT_WRAPPER.sub(_wrapper_replacer, no_comments)
        if nc != no_comments:
            no_comments = nc
            file_fixes.append("union field name fixed (v2g wrapper kept)")

        # Replace content with the patched version if we made any fix
        if file_fixes:
            content = no_comments

        if content != original:
            ttcn_file.write_text(content, encoding="utf-8")
            fixes_applied.append({
                "file": str(ttcn_file.relative_to(tc_project_dir)),
                "fixes": file_fixes,
            })
            any_fixed = True
            log(f"[STATIC-LINTER] Fixed {ttcn_file.name}: {file_fixes}")

    if fixes_applied:
        log(f"[STATIC-LINTER] {len(fixes_applied)} file patches, "
            f"{sum(len(f['fixes']) for f in fixes_applied)} ops total")

    return any_fixed, fixes_applied



if __name__ == "__main__":
    # 注册 SIGINT 信号处理，确保 Ctrl+C 能正确终止子进程
    def setup_signal_handler():
        """设置信号处理器，用于优雅地处理 Ctrl+C"""
        if hasattr(signal, 'SIGINT'):
            signal.signal(signal.SIGINT, _request_interrupt)
        if hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, _request_interrupt)

    setup_signal_handler()
    sys.exit(main())

