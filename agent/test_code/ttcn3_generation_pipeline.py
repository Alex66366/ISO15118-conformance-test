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

# === 全局子进程管理和中断标志 ===
_subprocess_ref = None  # 保存当前活动的子进程引用
_interrupt_requested = False  # 中断请求标志


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
MAX_RUNTIME_REPAIR_ITERATIONS = 2  # maximum self-repair iterations after test execution

# ============================================================
# Agent 超时配置（按类型区分）
# ============================================================
AGENT_TIMEOUTS = {
    "orchestrator": 900,        # 意图分解，reasoning model ~7min/turn
    "summarizer": 600,          # 知识检索，reasoning model ~7min/turn
    "core_agent": 1800,         # 代码生成，reasoning model ~7min/turn
    "infrastructure_agent": 3600,  # 项目结构生成，3-iteration compile loop
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
                allowed_tools = (f"Write,Read,Edit,Bash(bash *),Bash(makefilegen *),Bash(ttcn3_compiler *){extra_tools}")
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
            stdout_val, _ = proc.communicate()
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
                    "Write,Read,Edit,Bash(bash *),Bash(makefilegen *),Bash(ttcn3_compiler *)"
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

            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            raw_stdout, _ = proc.communicate()  # any remaining trailing bytes
            returncode = proc.returncode
            _subprocess_ref = None

            # Build combined stdout from thread output + any trailing communicate bytes
            all_stdout_bytes = b"".join(line.encode("utf-8", errors="replace")
                                         for line in stdout_lines)
            if raw_stdout:
                all_stdout_bytes += raw_stdout
            stdout_val = all_stdout_bytes.decode("utf-8", errors="replace")

        # Collect streamed stderr lines for post-processing
        stderr_val = "".join(stderr_lines)
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
                text = output.get("result", raw_output)
            elif isinstance(output, list):
                text = next(
                    (item.get("result") for item in output
                     if isinstance(item, dict) and item.get("type") == "result"),
                    raw_output
                )
            else:
                text = raw_output
        except json.JSONDecodeError:
            text = raw_output

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
        return chains[message_type]
    # 模糊匹配
    for key, chain in chains.items():
        if message_type.lower() in key.lower():
            return chain

    # 尝试 _missing_precondition_chains（补充标准链之外的场景）
    missing_chains = sop.get("_missing_precondition_chains", {})
    if message_type in missing_chains:
        return missing_chains[message_type].get("chain", [])
    for key, info in missing_chains.items():
        if message_type.lower() in key.lower():
            return info.get("chain", [])

    # 别名规范化：将空格/下划线变体映射到标准名称
    normalized = message_type.replace(" ", "").replace("_", "")
    alias_map = {
        "V2GTPHeader": "V2GTP",
        "V2GTPBR": "V2GTP",
    }
    for alias, canonical in alias_map.items():
        if normalized == alias:
            if canonical in missing_chains:
                return missing_chains[canonical].get("chain", [])
            if canonical in chains:
                return chains[canonical]
            return [f"f_{{SUT}}_CMN_PR_InitConfiguration_001"]

    # V2GTP/SDP 等特殊场景的默认处理
    if message_type in ("V2GTP", "SDP"):
        return [f"f_SECC_CMN_PR_InitConfiguration_001"]
    return []


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


def _extract_ttcn3_code_from_text(text: str) -> str:
    """从文本中提取 TTCN-3 代码，支持多种格式"""
    import re

    # 尝试从 markdown TTCN 代码块中提取
    ttcn_block = re.search(r'```(?:ttcn3?|TTCN-?3?)?\s*\n(.*?)\n```', text, re.DOTALL)
    if ttcn_block:
        return ttcn_block.group(1).strip()

    # 如果响应以 module 或 /** 开头，直接返回
    stripped = text.strip()
    if stripped.startswith("module ") or stripped.startswith("/**"):
        return stripped

    # 查找 module ... } 块（使用花括号平衡追踪避免 regex 贪婪问题）
    module_match = re.search(r'(/\*\*.*?)?module\s+\w+', stripped)
    if module_match:
        start_pos = module_match.start()
        # 从 module 关键字位置开始，使用花括号平衡追踪找结束位置
        candidate = stripped[start_pos:]
        depth = 0
        in_string = False
        escape_next = False
        end_pos = -1
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
                    end_pos = i
                    break
        if end_pos >= 0:
            return candidate[:end_pos + 1].strip()

    return stripped


def _is_valid_ttcn3_code(code: str) -> bool:
    """验证提取到的代码是否是有效的 TTCN-3 模块"""
    if not code or len(code) < 20:
        return False
    # 有效 TTCN-3 模块应包含 module 关键字
    return code.startswith("module ") or code.startswith("/**")


def extract_core_agent_output(text: str) -> Dict[str, Any]:
    """从 Core Agent 响应中提取 JSON 格式的完整输出（包括 testcase + pre/post condition 文件）。

    Core Agent 现在输出 JSON 格式：
    {
      "testcase": "... (TTCN-3 module code) ...",
      "precondition_files": [{"path": "...", "content": "..."}, ...],
      "postcondition_files": [{"path": "...", "content": "..."}, ...]
    }

    如果 JSON 解析失败，回退到旧格式（纯 TTCN 代码），只包含 testcase。

    解析策略（3 层 + 截断恢复）：
      1. 直接解析 JSON
      2. 从 markdown JSON 块解析（+ _recover_truncated_json 截断恢复）
      3. 提取纯 TTCN-3 代码作为 fallback
    """
    import re
    import json as _json

    # 尝试直接解析 JSON
    try:
        result = _json.loads(text.strip())
        if isinstance(result, dict) and "testcase" in result:
            return result
    except (_json.JSONDecodeError, ValueError):
        pass

    # 尝试从 markdown 代码块中提取 JSON
    json_block = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if json_block:
        block_content = json_block.group(1).strip()
        try:
            result = _json.loads(block_content)
            if isinstance(result, dict) and "testcase" in result:
                return result
        except (_json.JSONDecodeError, ValueError):
            pass
        # 截断恢复：当 JSON 块解析失败时，尝试截断恢复
        recovered = _recover_truncated_json(block_content)
        if recovered and isinstance(recovered, dict) and "testcase" in recovered:
            log("extract_core_agent_output: JSON 截断恢复成功", "WARN")
            return recovered

    # 回退到纯 TTCN-3 代码提取（提取逻辑与 extract_ttcn3_from_response 对齐）
    ttcn_code = _extract_ttcn3_code_from_text(text)

    # 验证提取到的代码是否是有效的 TTCN-3
    if _is_valid_ttcn3_code(ttcn_code):
        return {"testcase": ttcn_code, "precondition_files": [], "postcondition_files": []}

    # 所有提取策略均失败
    log(f"extract_core_agent_output: 所有提取策略均失败，响应长度={len(text)}，前100字符: {text[:100]}", "WARN")
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
    response = call_claude_agent(prompt, agent_name, timeout=timeout,
                                 extra_allowed_tools=extra_allowed_tools,
                                 read_only=read_only)
    if not response and retry_on_timeout:
        log(f"{agent_name} Agent 超时或无响应，触发重试 (timeout={timeout}s)", "WARN")
        response = call_claude_agent(prompt, f"{agent_name} (retry)", timeout=timeout * 2,
                                     extra_allowed_tools=extra_allowed_tools,
                                     read_only=read_only)
    return response


# ============================================================
# Agent 1: Orchestrator - 意图分解
# ============================================================
def run_orchestrator(test_case: Dict, requirement_context: str, sop: Dict) -> Optional[Dict]:
    """运行 Orchestrator Agent，分解测试用例为生成计划"""
    system_prompt = load_text(ORCHESTRATOR_PROMPT_PATH)

    user_prompt = f"""{system_prompt}

---

## 输入数据

### 测试用例 JSON:
```json
{json.dumps(test_case, indent=2, ensure_ascii=False)}
```

### 需求上下文 (ISO 15118-2):
{requirement_context if requirement_context else "(无额外上下文)"}

### SOP 前置条件链参考:
```json
{json.dumps(sop.get("precondition_chains", {}), indent=2)}
```

### SOP 命名规则:
```json
{json.dumps(sop.get("naming_rules", {}), indent=2)}
```

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
        plan = {
            "tc_id": tc_id,
            "sut_type": sut_type,
            "category": "CMN",
            "message_type": message_type,
            "test_type": "GoodCase",
            "protocol_layer": "V2G_Application",
            "precondition_chain": _get_default_precondition_chain(message_type, sop),
            "port_name": f"pt_V2G_TCP_TLS_ALM_{sut_type}_Port",
            "send_template_name": f"md_CMN_{message_type}Req_001",
            "receive_template_name": f"mdw_{sut_type}_{message_type}Res_001",
            "requirements_traced": [],
            "test_behavior_func_name": f"f_{sut_type}_CMN_TB_VTB_{message_type}_001",
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
                   is_fix_mode: bool = False) -> Dict[str, Any]:
    """运行 Core Agent，生成 TTCN-3 代码。

    返回一个字典，包含:
    - "testcase": 主测试用例 module 代码 (str)
    - "precondition_files": 前置条件文件列表 [{"path": ..., "content": ...}, ...]
    - "postcondition_files": 后置条件文件列表 [{"path": ..., "content": ...}, ...]

    Args:
        generation_plan: Orchestrator 生成的计划 JSON
        references: Summarizer 提取的参考代码
        few_shot_examples: Few-shot 样例字典
        sop: SOP JSON
        error_feedback: 上一次编译错误的反馈（用于迭代修复）
        sut_mode: 测试运行模式，"SIL" 或 "HIL"
        local_ref_section: 本地知识库检索结果（绕过 Summarizer LLM 层直接注入）
        previous_core_files: 上次生成的 core 文件列表（用于修复模式）
        previous_infra_files: 上次生成的 infrastructure 文件列表（用于参考）
        is_fix_mode: 是否为修复模式（精简 prompt，不包含 demo 参考）
    """
    system_prompt = load_text(CORE_AGENT_PROMPT_PATH)

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
    CORE_AGENT_SHARED_REFS = {}
    _shared_files_for_core = {
        "TimerAndConstants": "src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn",
        "CmnTemplates":      "src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn",
    }
    for _name, _rel_path in _shared_files_for_core.items():
        _full_path = DEMO_ROOT / _rel_path
        if _full_path.exists():
            content = _full_path.read_text(encoding="utf-8")
            CORE_AGENT_SHARED_REFS[_name] = truncate_text(content, 4000)
        else:
            CORE_AGENT_SHARED_REFS[_name] = ""

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

    response = call_agent_with_timeout(user_prompt, "Core Agent", read_only=True)
    if not response:
        return {"testcase": "", "precondition_files": [], "postcondition_files": [], "_parse_error": True}

    result = extract_core_agent_output(response)
    # 所有解析策略均失败时触发重试
    if result.get("_parse_error"):
        log("Core Agent 响应解析失败，触发一次重试...", "WARN")
        retry_response = call_claude_agent(user_prompt, "Core Agent (retry)", timeout=900,
                                          read_only=True)
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

    if not result:
        log("Fault Corrector output could not be parsed as JSON", "WARN")
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
    """
    import time
    import shutil

    system_prompt = load_text(INFRASTRUCTURE_AGENT_PROMPT_PATH)

    doc_urls = load_doc_urls()
    doc_urls_text = build_doc_urls_for_infrastructure(doc_urls)
    system_prompt = system_prompt.replace("{DOC_URLS_INFRASTRUCTURE}", doc_urls_text)

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

        # === Pre-install aggregator modules (same as normal mode) ===
        _infra_tc_dir = output_dir if output_dir else (GENERATED_DIR / tc_id)
        for agg_rel in ("src/ttcn3/3_Behavior_Steps/PreConditions/PreConditions_SECC_15118_2.ttcn",
                        "src/ttcn3/3_Behavior_Steps/PostConditions/PostConditions_SECC_15118_2.ttcn"):
            agg_src = DEMO_ROOT / agg_rel
            agg_dst = _infra_tc_dir / agg_rel
            if agg_src.exists():
                agg_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(agg_src, agg_dst)
                log(f"[INFRA PREINSTALL] Aggregator stub: {agg_rel}", "DEBUG")

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

        # TTCN 骨架参考（Agent 需要这些来生成正确的文件结构）
        ttcn_ref_files = [
            "src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn",
            "src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn",
            "src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn",
            "src/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn",
            "src/ttcn3/3_Behavior_Steps/Services/CommonFunctions.ttcn",
            "src/ttcn3/1_Data_Definitions/PIXIT/PIXIT_SECC_15118_2.ttcn",
            "src/ttcn3/3_Behavior_Steps/PreConditions/PreConditions_SECC_15118_2.ttcn",
            "src/ttcn3/3_Behavior_Steps/PostConditions/PostConditions_SECC_15118_2.ttcn",
            "src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn",
        ]
        demo_parts.append("### Reference: TTCN Skeleton Files")
        for ref_file in ttcn_ref_files:
            content = _load_demo(ref_file)
            if content:
                # 截断过长的 TTCN 骨架文件
                content = truncate_text(content, INFRA_CONFIG["demo_ref_truncate_size"] // len(ttcn_ref_files))
                demo_parts.append(f"[File: {ref_file}]\n{content}")

        # NOTE: PreConditions and PostConditions function definitions are generated
        # by the Core Agent and written by the pipeline. The Infrastructure Agent
        # generates the aggregator modules (PreConditions_SECC_15118_2.ttcn, PostConditions_SECC_15118_2.ttcn).
        # Pipeline copies 12 C++ adapter files and port impls from demo/.
        demo_parts.append("\n### NOTE: The following files are handled by the pipeline automatically")
        demo_parts.append("- 12 C++ adapter files (common/TRI/TCI/EXI/HAL): copied from demo/")
        demo_parts.append("- Port adapter .cc implementations (V2G_TCP_TLS_Port, V2G_UDP_SDP_Port): copied from demo/")
        demo_parts.append("- build.sh: copied from demo/ (infrastructure-critical, never generated by Agent)")
        demo_parts.append("- PreConditions/PostConditions aggregator modules: copied from demo/ by pipeline (PreConditions_SECC_15118_2.ttcn, PostConditions_SECC_15118_2.ttcn)")
        demo_parts.append("- PreConditions/PostConditions function definitions: generated by Core Agent, placed by pipeline")
        demo_parts.append("DO NOT include these files in your output JSON.")

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

        # === Pre-install aggregator modules so makefilegen finds them immediately ===
        # The agent cannot generate PreConditions_SECC_15118_2.ttcn / PostConditions_SECC_15118_2.ttcn
        # in its first turn (they depend on the Core Agent's function names, which are not
        # yet known to the agent). Pre-installing demo versions guarantees that:
        #   1. makefilegen never fails with "Cannot find source file" for the aggregators
        #   2. The agent's own generated files are never accidentally overwritten by demo stubs
        _infra_tc_dir = output_dir if output_dir else (GENERATED_DIR / tc_id)
        for agg_rel in ("src/ttcn3/3_Behavior_Steps/PreConditions/PreConditions_SECC_15118_2.ttcn",
                        "src/ttcn3/3_Behavior_Steps/PostConditions/PostConditions_SECC_15118_2.ttcn"):
            agg_src = DEMO_ROOT / agg_rel
            agg_dst = _infra_tc_dir / agg_rel
            if agg_src.exists():
                agg_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(agg_src, agg_dst)
                log(f"[INFRA PREINSTALL] Aggregator stub: {agg_rel}", "DEBUG")
            else:
                log(f"[INFRA PREINSTALL] Aggregator not in demo (OK if no shared TTCN files needed): {agg_rel}", "DEBUG")

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

Now generate the project structure by writing files directly to disk.

IMPORTANT: Output directory is `{output_dir_str}/`

Use the Write tool to create each file with the full absolute path:
- `{output_dir_str}/config/titan_runtime.cfg`
- `{output_dir_str}/config/hardware_env.json`
- `{output_dir_str}/src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn`
- `{output_dir_str}/src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn`
- `{output_dir_str}/src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn`
- `{output_dir_str}/src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn`
- `{output_dir_str}/src/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn`
- `{output_dir_str}/src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn`

Do NOT output JSON. Use the Write tool for every file.

After writing all files, you MAY optionally run `bash build.sh` to self-verify the build.
If compilation errors occur, fix the affected files and retry.

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

    def _get_module_name(content: str) -> str | None:
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
    """Install a dynamic build.sh into the generated project directory.

    Instead of copying the demo's build.sh with hardcoded TTCN file paths (which causes
    "cannot find source file" errors when the generated project has fewer/more/different
    files than the demo), this function generates a build.sh that uses ``$(find ...)``
    to auto-discover all .ttcn and .cc files at build time.

    The makefilegen command passes files in the correct TITAN ordering:
      1. Architecture (ComponentsAndPorts)
      2. Data definitions (Types, Templates, PICS, PIXIT)
      3. Services (CommonFunctions)
      4. PreConditions / PostConditions (ALL .ttcn files -- TITAN does NOT compile
         transitively; each module must be passed to makefilegen explicitly)
      5. TestBehavior (step functions)
      6. TestCases (main module + TC file)
      7. C++ port adapters

    Returns:
        True if the file was successfully written, False otherwise.
    """
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
    # Option 1: already present with real content
    if [ -f "$filename" ] && [ "$(stat -c%s "$filename" 2>/dev/null || echo 0)" -gt 200 ]; then
        echo "$filename"; return 0
    fi
    # Option 2: project root
    if [ -f "../../$filename" ] && [ "$(stat -c%s "../../$filename" 2>/dev/null || echo 0)" -gt 200 ]; then
        cp "../../$filename" .; echo "[BUILD] Copied $filename from project root"; echo "$filename"; return 0
    fi
    # Option 3: src/cpp_adapters
    local found=$(find ../../src/cpp_adapters -name "$filename" 2>/dev/null | head -1)
    if [ -n "$found" ] && [ "$(stat -c%s "$found" 2>/dev/null || echo 0)" -gt 200 ]; then
        cp "$found" .; echo "[BUILD] Copied $filename from $found"; echo "$filename"; return 0
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
echo "[BUILD] Makefile fixed."

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
    """修补 Makefile：在 CXXFLAGS 中添加 -I.. 以解决 SECC_TestSystem.hh 路径问题

    makefilegen 生成的 SECC_TestSystem.hh 在项目根目录，而端口适配器的 .cc 文件
    在 build/generated_cpp/ 目录，编译时需要 -I.. 才能找到上级的 SECC_TestSystem.hh。
    """
    makefile_path = tc_dir / "build" / "generated_cpp" / "Makefile"
    if not makefile_path.exists():
        return

    content = makefile_path.read_text(encoding="utf-8")
    if "-I.." not in content:
        import re
        # 在 CXXFLAGS 定义中添加 -I..
        content = re.sub(
            r"(CXXFLAGS\s*=\s*[^\n]*)",
            r"\1 -I..",
            content
        )
        makefile_path.write_text(content, encoding="utf-8")
        log("Makefile 已注入 -I.. 到 CXXFLAGS")


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

# List of shared TTCN files to copy from demo.
# These files must exist in demo/ and contain valid, working definitions.
DEMO_SHARED_FILES = [
    "src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn",
    "src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn",
    "src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn",
    "src/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn",
    # EXCLUDED: PreConditions/PostConditions aggregator files are controlled by
    # _inject_condition_function_imports() -- never overwrite them with demo stubs:
    # "src/ttcn3/3_Behavior_Steps/PreConditions/PreConditions_SECC_15118_2.ttcn",
    # "src/ttcn3/3_Behavior_Steps/PostConditions/PostConditions_SECC_15118_2.ttcn",
    "src/ttcn3/3_Behavior_Steps/Services/CommonFunctions.ttcn",
    "src/ttcn3/1_Data_Definitions/PIXIT/PIXIT_SECC_15118_2.ttcn",
    # These two files are also generated by the Infrastructure Agent but have
    # correct demo versions that must override Agent output. Not including them
    # caused wrong module names (ComponentsAndPorts vs SECC_TestSystem) and
    # missing test case registration in the generated project.
    "src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn",
    "src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn",
]


def install_shared_ttcn_files(tc_dir: Path) -> int:
    """Copy shared TTCN-3 files from the demo project into the generated test project.

    The demo files contain valid, syntax-verified type/template/constant definitions
    that all test cases reference. Copying them ensures:
    1. All shared symbols (types, templates, constants) are defined and valid
    2. No LLM-generated syntax errors in shared files
    3. Consistent baseline across all generated projects

    Returns:
        Number of files successfully copied.
    """
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

        try:
            run_result = subprocess.run(
                [str(exec_path), str(runtime_cfg)],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(exec_path.parent),
            )
        finally:
            # === Step 6: 停止 Sniffer（仅在启动后才停止）===
            if sniffer:
                try:
                    saved_path = sniffer.stop_and_save()
                    log(f"Sniffer stopped, saved to {saved_path}")
                except Exception as e:
                    log(f"Sniffer stop failed: {e}", "WARN")

        # 读取测试日志
        log_path = tc_dir / "logs" / "titan_traces" / "TestExecution.log"
        test_log_content = ""
        if log_path.exists():
            with open(log_path, "r", encoding="utf-8") as f:
                test_log_content = f.read()

        test_result = {
            "returncode": run_result.returncode,
            "stdout": run_result.stdout[-5000:],
            "stderr": run_result.stderr[-5000:],
            "log_path": str(log_path),
            "log_content": test_log_content[-10000:],
        }

        return {
            "status": "completed",
            "executable_path": str(exec_path),
            "test_result": test_result,
            "compile_errors": compile_errors,
        }

    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "reason": "build_timeout",
            "message": "Build exceeded 5 minute timeout",
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
            installed = install_shared_ttcn_files(tc_dir)
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
        "(incremental: only fix listed files)" if targeted_only else "(infrastructure errors)"
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
                    generation_plan, references or {}, few_shot_examples, sop, runtime_feedback
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
                        f_path.write_text(f_data["content"], encoding="utf-8")
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

        retry_status = retry_result.get("status", "")
        if retry_status == "completed":
            log("运行时自修复: 编译运行成功!")
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
            }
        elif retry_status == "build_failed":
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
                    log_path=tc_project_dir / "logs" / "titan_traces" / "TestExecution.log",
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


def generate_code_for_test_case(
    test_case: Dict,
    sut_type: Optional[str] = None,
    requirement_context: str = "",
    titan_log: Optional[Path] = None,
    pcap_path: Optional[Path] = None,
    skip_build: bool = False,
    sut_ip: Optional[str] = None,
) -> Dict:
    """为单个测试用例生成 TTCN-3 代码及完整项目结构

    流水线:
      测试用例 → Orchestrator → Summarizer → Core Agent → Infrastructure Agent → Build
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

    # === Step 1: Orchestrator - 意图分解 ===
    log("\n--- Step 1: Orchestrator (意图分解) ---")
    t0 = time.time()
    generation_plan = run_orchestrator(tc, requirement_context, sop)
    orchestrator_elapsed = time.time() - t0
    log(f"[Orchestrator] 耗时: {orchestrator_elapsed:.1f}s")
    # 注意：run_orchestrator 现在即使 LLM 失败也会返回 fallback plan，不再需要此检查
    # 但保留此处以防未来变化
    if not generation_plan:
        log("Orchestrator 失败，流水线终止", "ERROR")
        _print_timing_summary(result, orchestrator_elapsed, summarizer_elapsed,
                             core_agent_elapsed, infra_agent_elapsed, build_elapsed,
                             compile_loop_core_total, compile_loop_infra_total,
                             compile_loop_build_total, compile_loop_fc_total,
                             fc_runtime_elapsed, fc_legacy_elapsed,
                             compile_loop_core_times, compile_loop_infra_times,
                             compile_loop_build_times, compile_loop_fc_times)
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

    # === Step 2: Summarizer - 知识检索 ===
    log("\n--- Step 2: Summarizer (知识检索) ---")
    msg_type = generation_plan.get("message_type", "")
    sut = generation_plan.get("sut_type", "SECC")
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

    # === Step 3: Core Agent (代码生成, 单次调用) ===
    log("\n--- Step 3: Core Agent (代码生成) ---")
    # 判断 SIL/HIL 模式：没有 SUT IP 时为 SIL 模式
    effective_sut_mode = "SIL" if not sut_ip else "HIL"
    # 构建 local_ref_section（绕过 Summarizer LLM 层）
    local_ref_parts = []
    for tc in kb_search_results.get("testcases", [])[:3]:
        if tc.get("code") and len(tc["code"]) > 100:
            local_ref_parts.append(f"// === Local Ref TC: {tc.get('name', 'unknown')} ===\n{tc['code']}")
    for fn in kb_search_results.get("functions", [])[:3]:
        if fn.get("code") and len(fn["code"]) > 50:
            local_ref_parts.append(f"// === Local Ref Function: {fn.get('name', 'unknown')} ===\n{fn['code']}")
    local_ref_section = "\n\n".join(local_ref_parts) if local_ref_parts else ""
    t2 = time.time()
    core_result = run_core_agent(
        generation_plan, references, few_shot_examples, sop, "",
        sut_mode=effective_sut_mode, local_ref_section=local_ref_section
    )
    core_agent_elapsed = time.time() - t2
    log(f"[Core Agent] 耗时: {core_agent_elapsed:.1f}s")
    if not core_result or not core_result.get("testcase"):
        log("Core Agent 未生成代码", "ERROR")
        raise _PipelineEarlyExit()

    # 提取各部分代码
    ttcn3_code = core_result["testcase"]
    pre_condition_files = core_result.get("precondition_files", [])
    post_condition_files = core_result.get("postcondition_files", [])

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
    compile_loop_infra_times: List[Tuple[int, float]] = [] # (iteration, seconds) — 0 if skipped

    # === Step 5: 写入 Core Agent 生成的 pre/post condition 文件 ===
    # Ensure tc_project_dir exists before writing files to it
    tc_project_dir = GENERATED_DIR / plan_tc_id
    tc_project_dir.mkdir(parents=True, exist_ok=True)

    # 同时定义 tc_ttcn_dest（供 _runtime_self_repair 使用，避免 NameError）
    tc_ttcn_dest = tc_project_dir / "src/ttcn3/4_TestCases" / output_filename
    tc_ttcn_dest.parent.mkdir(parents=True, exist_ok=True)
    tc_ttcn_dest.write_text(ttcn3_code, encoding="utf-8")

    for f_data in pre_condition_files + post_condition_files:
        f_path = tc_project_dir / f_data["path"]
        f_path.parent.mkdir(parents=True, exist_ok=True)
        f_path.write_text(f_data["content"], encoding="utf-8")
    log(f"已写入 {len(pre_condition_files)} 个 pre + {len(post_condition_files)} 个 post 文件")

    # === Step 5a: Import injection (MUST run BEFORE Infrastructure Agent) ===
    # After Core Agent writes pre/post files to disk, rebuild the aggregator modules
    # with correct import statements and ensure the main TC file directly imports
    # all function modules it calls. These calls must happen BEFORE Infrastructure
    # Agent runs so the agent sees consistent, correct files on disk.
    _inject_condition_function_imports(tc_project_dir, pre_condition_files, post_condition_files, plan_tc_id)
    _ensure_tc_direct_imports(tc_project_dir, output_filename, pre_condition_files, post_condition_files)

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
    # The agent will run `bash build.sh` in its compile loop. build.sh
    # and all C++ adapters must already exist on disk -- the agent
    # cannot generate them and will abort if they're missing.
    #
    # build.sh uses rsync with --exclude='build/' for non-ASCII paths.
    # This means files inside build/generated_cpp/ are NOT synced to the
    # temp build directory. We must write V2G port files to BOTH:
    #   1. build/generated_cpp/  -- makefilegen reads sources from here
    #   2. src/cpp_adapters/SA/ports/ -- rsync-safe so build.sh can find it
    # ================================================================
    tc_project_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy build.sh from demo (agent will invoke it)
    build_installed = _install_build_script(tc_project_dir)
    if not build_installed:
        log("[SCAFFOLD] WARNING: build.sh not found in demo -- agent compile loop will fail", "WARN")

    # 2. Create directory structure first (V2G ports need these paths)
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

    # 5. Patch build.sh references
    _patch_build_script_common_functions(tc_project_dir)
    _patch_build_script_tc_file(tc_project_dir, tc_output_filename)
    _patch_build_script_include_paths(tc_project_dir)
    _patch_build_script_ttcn3_compiler_fix(tc_project_dir)

    log(f"[SCAFFOLD] Project directory prepared: {tc_project_dir}")
    log(f"[SCAFFOLD] build.sh: {'installed' if build_installed else 'MISSING'}")
    log(f"[SCAFFOLD] V2G ports: {ports_copied}/{len(demo_port_files)} copied")

    # ================================================================

    infra_result = run_infrastructure_agent(
        generation_plan=generation_plan,
        ttcn3_code=ttcn3_code,
        tc_id=plan_tc_id,
        sut_ip=sut_ip,
        tc_output_filename=tc_output_filename,
        output_dir=tc_project_dir,
    )
    infra_agent_elapsed = time.time() - t3
    log(f"[Infrastructure Agent] 耗时: {infra_agent_elapsed:.1f}s")

    # === Process Infrastructure Agent result ===
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
        if detected_files:
            log(f"[Direct Write Mode] 验证成功，目录包含 {len(detected_files)} 个有效文件:")
            for f in sorted(detected_files):
                log(f"  [DIRECT] {f.relative_to(tc_project_dir)}")
        result["infra_status"] = "success"
        result["infra_write_mode"] = "direct_write"
        result["infra_files_count"] = len(detected_files)

    infra_agent_elapsed = time.time() - t3
    log(f"[Infrastructure Agent] 耗时: {infra_agent_elapsed:.1f}s")

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
            if genuine_build_error:
                log(f"\n--- Step 7: Infrastructure Agent Fix Loop (self-contained) ---")

                # Build error feedback for Infrastructure Agent
                infra_error_fb = build_infra_error_feedback(compile_errors, tc_project_dir)
                if not infra_error_fb:
                    infra_error_fb = (
                        "Build failed with exit code " + str(build_result.get("returncode", 0)) + ". "
                        "Raw output: " + build_result.get("stderr", build_result.get("stdout", ""))[:1000]
                    )

                log(f"[Infra Fix] Calling Infrastructure Agent with error feedback...")

                # Track previous files for fix mode
                current_infra_files = infra_result.get("files", []) if infra_result else []
                previous_infra_files = [f.get("path", "") for f in current_infra_files] if isinstance(current_infra_files, list) else []
                previous_core_files = pre_condition_files + post_condition_files

                # Call Infrastructure Agent in fix mode (it runs its own inner compile loop)
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
                    is_fix_mode=True
                )
                infra_fix_elapsed = time.time() - t_infra_fix
                log(f"[Infra Fix] Infrastructure Agent 耗时: {infra_fix_elapsed:.1f}s")

                # Guard against destructive JSON-parse-error + build-success scenario.
                # If run_infrastructure_agent returned an error (JSON parse failed) but the
                # build actually succeeded, the agent's files on disk are valid -- do NOT
                # overwrite them with demo skeleton stubs. Just warn and use what we have.
                # Also: if compile_locked (Forward-Only), never touch scaffolding again.
                infra_is_json_error = infra_retry.get("status") == "error"
                build_succeeded = build_result.get("status") == "completed"
                if (infra_is_json_error and build_succeeded) or compile_locked:
                    if compile_locked:
                        log("[LOCK] compile_locked=True: skipping Infrastructure Agent fallback.", "WARN")
                    else:
                        log("[WARN] Infrastructure Agent JSON parse failed but build succeeded. "
                            "Skipping destructive fallback -- preserving agent's working files.", "WARN")
                        # Verify the files are still there
                        try:
                            _, detected_files = detect_direct_write_mode(tc_project_dir)
                            log(f"[WARN] Preserved {len(detected_files)} files from agent output")
                        except RuntimeError as e:
                            log(f"[WARN] File verification failed: {e}", "WARN")
                    # Lock and proceed directly to runtime -- never re-trigger static fixes
                    compile_locked = True
                    # Fall through without the fallback overwrites
                else:
                    # Normal flow: process agent result, overwrite scaffolding, run final build
                    # Process Infrastructure Agent result
                    # Agent must have written files to disk. detect_direct_write_mode validates this.
                    try:
                        detected_mode, detected_files = detect_direct_write_mode(tc_project_dir)
                        log(f"[Infra Fix] Direct Write Mode: found {len(detected_files)} files")
                    except RuntimeError as e:
                        log(f"[Infra Fix] FATAL: {e}", "ERROR")
                        raise

                    # Run final build to verify
                    log(f"[Infra Fix] Running final build to verify...")
                    t_final_build = time.time()
                    final_build_result = run_build_and_test(
                        tc_dir=tc_project_dir,
                        tc_id=plan_tc_id,
                        sut_ip=sut_ip,
                        pcap_output=pcap_out,
                    )
                    final_build_elapsed = time.time() - t_final_build
                    log(f"[Infra Fix - Final Build] 耗时: {final_build_elapsed:.1f}s")

                    # Update result based on final build
                    if final_build_result.get("status") == "completed":
                        log("Infrastructure Agent fix succeeded! Build now passes.")
                        result["build_result"] = final_build_result
                        result["compile_feedback"] = ""
                        result["compile_errors"] = []
                        compile_locked = True
                    else:
                        remaining_errors = final_build_result.get("compile_errors", [])
                        log(f"Build still failing after Infrastructure Agent fix: {len(remaining_errors)} errors")
                        result["compile_feedback"] = infra_error_fb
                        result["compile_errors"] = remaining_errors
                        result["build_result"] = final_build_result
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

            if test_passed:
                log("Test passed (Overall verdict: pass) -- skipping runtime Fault Corrector.")
                result["test_passed"] = True
            else:
                log("\n--- Fault Corrector (runtime: dual-track analysis) ---")
                test_log = tc_project_dir / "logs" / "titan_traces" / "TestExecution.log"
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

        gen_result = generate_code_for_test_case(
            test_case, sut_type, requirement_context,
            titan_log=titan_log, pcap_path=pcap_path,
            skip_build=skip_build,
            sut_ip=sut_ip,
        )
        results.append({
            "input": {"type": "test_case", "data": tc.get("TC_Id", "unknown")},
            "success": gen_result.get("ttcn_path") is not None,
            **gen_result,
        })
        return results

    # === 模式2: 通过测试用例文件 ===
    if test_case_file:
        log(f"从文件加载测试用例: {test_case_file}")
        test_case_list = load_test_case_from_file(Path(test_case_file))
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

            gen_result = generate_code_for_test_case(
                loaded_test_case, sut_type, requirement_context,
                titan_log=titan_log, pcap_path=pcap_path,
                skip_build=skip_build,
                sut_ip=sut_ip,
            )
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

            gen_result = generate_code_for_test_case(
                tc_entry, sut_type, requirement_context,
                titan_log=titan_log, pcap_path=pcap_path,
                skip_build=skip_build,
                sut_ip=sut_ip,
            )
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

        gen_result = generate_code_for_test_case(
            tc_entry, sut_type, requirement_context,
            titan_log=titan_log, pcap_path=pcap_path,
            skip_build=skip_build,
            sut_ip=sut_ip,
        )
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
