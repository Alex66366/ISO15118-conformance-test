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
import subprocess
import sys
import tempfile
import time
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

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
DEMO_ROOT = AGENT_DIR / "test_code" / "examples"

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
    "orchestrator": 300,       # 意图分解，数据量较小
    "summarizer": 300,        # 知识检索，数据量适中
    "core_agent": 900,        # 代码生成，最复杂，需要最长超时
    "infrastructure_agent": 600,  # 项目结构生成，复杂
    "fault_corrector": 300,   # 错误分析
    "default": 600,            # 未识别类型默认超时
}


def get_agent_timeout(agent_name: str, prompt_size: int = 0) -> int:
    """获取指定 Agent 的超时时间。

    Args:
        agent_name: Agent 名称（小写匹配）
        prompt_size: Prompt 大小（字节），用于自动调整超时

    Returns:
        超时秒数
    """
    base_timeout = AGENT_TIMEOUTS.get(agent_name.lower(), AGENT_TIMEOUTS["default"])

    # 当 prompt 大小超过阈值时，自动增加超时
    if prompt_size > PROMPT_SIZE_WARNING:
        extra = int((prompt_size - PROMPT_SIZE_WARNING) / 10000) * 60
        return min(base_timeout + extra, base_timeout * 2)

    return base_timeout


# ============================================================
# Infrastructure Agent Constants (Prompt Size Optimization)
# ============================================================

# Prompt size thresholds
PROMPT_SIZE_WARNING = 40000   # 40KB warning threshold
PROMPT_SIZE_HARD = 50000     # 50KB hard truncation

# Retry configuration
MAX_INFRA_RETRY = 3          # Maximum retry attempts
RETRY_BACKOFF = [0, 30, 60]  # Exponential backoff intervals (seconds)

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
EXPECTED_INFRA_FILES_MIN = 12  # Minimum required files from agent


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

@dataclass
class ClassifiedError:
    """经过分类的编译错误，包含修复建议。"""
    error_type: str      # "ttcn_syntax" | "cpp_link" | "port_undefined" | "import_missing" | "type_mismatch" | "runs_on_mismatch" | "timer_error"
    severity: str        # "critical" | "warning"
    location: str        # "file:line:col" 或 "unknown"
    raw_message: str
    ttcn_line_hint: Optional[int] = None
    fix_suggestion: str = ""


class CompileErrorClassifier:
    """对编译错误进行分类、定位到源文件、提供修复建议。

    参考 TITAN 官方文档中的常见错误模式。
    """

    # 错误模式 -> 分类的映射
    ERROR_PATTERNS = [
        ("port_undefined", re.compile(
            r"port type ['`].+?['`] has no associated port definition", re.IGNORECASE)),
        ("port_undefined", re.compile(
            r"unknown port ['`].+?['`]", re.IGNORECASE)),
        ("import_missing", re.compile(
            r"module ['`].+?['`] has not been imported", re.IGNORECASE)),
        ("import_missing", re.compile(
            r"cannot find definition of type ['`].+?['`]", re.IGNORECASE)),
        ("type_mismatch", re.compile(
            r"type ['`].+?['`] and ['`].+?['`] are not compatible", re.IGNORECASE)),
        ("type_mismatch", re.compile(
            r"template type mismatch", re.IGNORECASE)),
        ("runs_on_mismatch", re.compile(
            r"runs on clause mismatch", re.IGNORECASE)),
        ("runs_on_mismatch", re.compile(
            r"function ['`].+?['`] cannot be invoked from a component of type", re.IGNORECASE)),
        ("timer_error", re.compile(
            r"timer ['`].+?['`] has not been started", re.IGNORECASE)),
        ("ttcn_syntax", re.compile(
            r"syntax error, unexpected", re.IGNORECASE)),
        ("ttcn_syntax", re.compile(
            r"expected ['`].+?['`] before", re.IGNORECASE)),
        ("cpp_link", re.compile(
            r"undefined reference to ['`].+?['`]", re.IGNORECASE)),
        ("cpp_link", re.compile(
            r"ld returned \d+ exit status", re.IGNORECASE)),
        ("cpp_link", re.compile(
            r"multiple definition of ['`].+?['`]", re.IGNORECASE)),
    ]

    FIX_SUGGESTIONS = {
        "port_undefined": (
            "检查 ComponentsAndPorts.ttcn 中是否正确定义了该端口类型。"
            "确保在 testcase 中使用的 port 名称与 ComponentsAndPorts.ttcn 中声明的一致。"
        ),
        "import_missing": (
            "在 module 顶部添加缺失的 import 语句。"
            "例如: import from <module_name> all;"
        ),
        "type_mismatch": (
            "检查模板参数类型与函数签名是否匹配。"
            "注意 TTCN-3 的 runs on 子句必须一致。"
        ),
        "runs_on_mismatch": (
            "确保函数/altstep 的 runs on 组件类型与调用方一致。"
            "被调用函数的 runs on 必须是调用方组件类型的父类型或相同类型。"
        ),
        "timer_error": (
            "在 timeout 分支前确保调用了 tc_X.start()。"
            "timer 必须先 start 才能触发 timeout。"
        ),
        "ttcn_syntax": (
            "检查 TTCN-3 语法：括号匹配、分号、关键字拼写。"
            "参照 TITAN ReferenceGuide 第3章。"
        ),
        "cpp_link": (
            "C++ 链接错误，检查 cpp_adapters/ 中对应的 .cc 文件"
            "是否实现了所有必需的方法，或存在重复定义。"
        ),
    }

    def classify(self, errors: List[Dict], generated_ttcn_path: str = "") -> List[ClassifiedError]:
        """将原始错误分类，映射到生成的.ttcn源文件。

        Args:
            errors: parse_compile_errors() 返回的错误列表。
            generated_ttcn_path: 生成的 .ttcn 文件路径，用于行号映射。

        Returns:
            ClassifiedError 列表。
        """
        classified = []
        for err in errors:
            err_type = self._match_pattern(err["message"])
            location = f"{err['file']}:{err['line']}" if err.get("file") else "unknown"

            # 如果错误涉及生成的 .ttcn 文件，尝试给出行号提示
            ttcn_hint = None
            if generated_ttcn_path and err.get("file") and str(generated_ttcn_path) in str(err.get("file", "")):
                ttcn_hint = err.get("line")

            classified_err = ClassifiedError(
                error_type=err_type,
                severity="critical" if err_type in ("ttcn_syntax", "cpp_link", "runs_on_mismatch") else "warning",
                location=location,
                raw_message=err.get("message", ""),
                ttcn_line_hint=ttcn_hint,
                fix_suggestion=self.FIX_SUGGESTIONS.get(err_type, "请查看原始编译错误信息。"),
            )
            classified.append(classified_err)
        return classified

    def _match_pattern(self, message: str) -> str:
        """匹配错误消息，返回错误类型。"""
        for err_type, pattern in self.ERROR_PATTERNS:
            if pattern.search(message):
                return err_type
        return "unknown"

    def get_summary(self, classified_errors: List[ClassifiedError]) -> str:
        """生成错误摘要，用于反馈给 Core Agent。"""
        if not classified_errors:
            return ""
        lines = ["编译错误摘要:"]
        for err in classified_errors:
            lines.append(f"- [{err.error_type}] {err.location}: {err.raw_message}")
            if err.fix_suggestion:
                lines.append(f"  修复建议: {err.fix_suggestion}")
        return "\n".join(lines)


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


def call_claude_agent(prompt: str, agent_name: str, timeout: int = 120) -> str:
    """调用 Claude Code CLI 作为 sub-agent

    Args:
        prompt: 完整的 prompt 文本
        agent_name: Agent 名称（用于日志）
        timeout: 超时秒数

    Returns:
        Claude 的文本输出
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

        if os.name == "nt":
            # Windows: Use cmd.exe /c with proper encoding
            # Write prompt to temp file first
            tmp_prompt_file = Path(tempfile.gettempdir()) / f"claude_prompt_{os.getpid()}_{time.time():.0f}.txt"
            tmp_prompt_file.write_text(prompt, encoding="utf-8")
            
            # Build command string for cmd.exe
            # Note: cmd.exe /c is required when executable="cmd.exe" is set
            cmd_str = f'chcp 65001 >nul & "{claude_cmd}" -p - --output-format json <"{tmp_prompt_file}"'
            
            result = subprocess.run(
                cmd_str,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=True,
            )
            
            stdout_val = result.stdout if result.stdout else ""
            stderr_val = result.stderr if result.stderr else ""
            returncode = result.returncode
        else:
            # Unix: Direct command execution
            cmd_args = [claude_cmd, "-p", prompt, "--output-format", "json"]
            
            result = subprocess.run(
                cmd_args,
                capture_output=True,
                text=True,
                encoding=None,  # Let Python choose locale encoding on Unix
                errors="replace",
                timeout=timeout,
            )
            
            stdout_val = result.stdout if result.stdout else ""
            stderr_val = result.stderr if result.stderr else ""
            returncode = result.returncode

        _dbg("after_subprocess", {
            "agent_name": agent_name,
            "returncode": returncode,
            "stderr_len": len(stderr_val),
            "stderr_preview": stderr_val[:300],
            "stdout_len": len(stdout_val),
            "stdout_preview": stdout_val[:200],
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
            text = output.get("result", raw_output)
        except json.JSONDecodeError:
            text = raw_output

        log(f"{agent_name} Agent 完成，输出 {len(text)} 字符")
        return text

    except subprocess.TimeoutExpired:
        log(f"{agent_name} Agent 超时 ({timeout}s)", "ERROR")
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
    """从 Agent 响应中提取 JSON 对象，支持截断响应恢复"""
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试从 markdown 代码块中提取
    json_block = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if json_block:
        try:
            return json.loads(json_block.group(1))
        except json.JSONDecodeError:
            # 尝试截断恢复
            recovered = _recover_truncated_json(json_block.group(1))
            if recovered:
                return recovered

    # 尝试找到第一个 { 或 [ 到最后一个 } 或 ] 之间的内容
    first_brace = text.find('{')
    first_bracket = text.find('[')
    valid_starts = [x for x in [first_brace, first_bracket] if x >= 0]
    if not valid_starts:
        # 响应中完全没有 JSON 语法字符，无法解析
        return None
    try:
        start = min(valid_starts)
    except ValueError:
        # 防御：valid_starts 理论上不可能为空（已在上方 guard），但以防万一
        return None
    last_brace = text.rfind('}')
    last_bracket = text.rfind(']')
    end = max(last_brace, last_bracket)
    try:
        if end > start:
            candidate = text[start:end + 1]
            try:
                return json.loads(candidate)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # 尝试截断恢复
                recovered = _recover_truncated_json(candidate)
                if recovered:
                    return recovered
    except TypeError:
        # 防御：start/end 可能为 -1 导致比较或切片失败
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
    match = re.match(r'^TC_(SECC|EVCC)_(VTB_)?(\w+)_(\d+)$', tc_id)
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


def load_test_case_from_file(file_path: Path) -> Optional[Dict]:
    """从文件加载测试用例

    Args:
        file_path: 测试用例文件路径

    Returns:
        测试用例字典，或 None
    """
    if not file_path.exists():
        log(f"测试用例文件不存在: {file_path}", "ERROR")
        return None

    try:
        return load_json(file_path)
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
        # 精确匹配消息类型
        msg_view = by_message.get(message_type, {})
        if not msg_view:
            # 模糊匹配：查找包含 message_type 的键
            for key in by_message:
                if message_type.lower() in key.lower():
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

def call_agent_with_timeout(prompt: str, agent_name: str, retry_on_timeout: bool = True) -> str:
    """调用 Claude Agent，自动选择合适的超时时间。

    Args:
        prompt: 要发送的 prompt
        agent_name: Agent 名称（用于日志和超时选择）
        retry_on_timeout: 超时后是否重试一次

    Returns:
        Agent 的文本输出
    """
    timeout = get_agent_timeout(agent_name, len(prompt))
    response = call_claude_agent(prompt, agent_name, timeout=timeout)
    if not response and retry_on_timeout:
        log(f"{agent_name} Agent 超时或无响应，触发重试 (timeout={timeout}s)", "WARN")
        response = call_claude_agent(prompt, f"{agent_name} (retry)", timeout=timeout * 2)
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

    response = call_agent_with_timeout(user_prompt, "Orchestrator")
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
        plan = {
            "tc_id": tc_id,
            "sut_type": sut_type,
            "category": "CMN",
            "message_type": message_type,
            "test_type": "GoodCase",
            "protocol_layer": "V2G_Application",
            "precondition_chain": _get_default_precondition_chain(message_type, sop),
            "timer_param": f"par_V2G_{sut_type.upper()}_Msg_Timeout_{message_type}Req",
            "port_name": f"pt_V2G_TCP_TLS_ALM_{sut_type}_Port",
            "send_template_name": f"md_CMN_{message_type}Req_001",
            "receive_template_name": f"mdw_{sut_type}_{message_type}Res_001",
            "requirements_traced": [],
            "test_behavior_func_name": f"f_{sut_type}_CMN_TB_VTB_{message_type}_001",
            "postcondition_chain": [f"f_{sut_type}_CMN_PO_InitialState_001"],
        }

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

    response = call_agent_with_timeout(user_prompt, "Summarizer")
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
                   local_ref_section: str = "") -> Dict[str, Any]:
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
    """
    system_prompt = load_text(CORE_AGENT_PROMPT_PATH)

    # 选择合适的 few-shot 样例
    test_type = generation_plan.get("test_type", "GoodCase")
    if test_type == "GoodCase":
        tc_example = few_shot_examples.get("testcase_goodcase", "")
    else:
        tc_example = few_shot_examples.get("testcase_badcase", "")

    tb_example = few_shot_examples.get("test_behavior", "")
    tmpl_example = few_shot_examples.get("templates", "")
    pre_example = few_shot_examples.get("precondition", "")
    post_example = few_shot_examples.get("postcondition", "")
    tc_with_imports = few_shot_examples.get("testcase_with_imports", "")

    # 构建参考代码部分
    ref_code_parts = []
    if references:
        if references.get("reference_testcase"):
            ref_tc = references["reference_testcase"]
            if isinstance(ref_tc, dict) and ref_tc.get("code"):
                ref_code_parts.append(f"// 参考 testcase:\n{ref_tc['code']}")
        if references.get("reference_test_behavior"):
            ref_tb = references["reference_test_behavior"]
            if isinstance(ref_tb, dict) and ref_tb.get("code"):
                ref_code_parts.append(f"// 参考 test behavior:\n{ref_tb['code']}")
        for ref_tmpl in references.get("reference_templates", []):
            if isinstance(ref_tmpl, dict) and ref_tmpl.get("code"):
                ref_code_parts.append(f"// 参考 template:\n{ref_tmpl['code']}")

    ref_code_section = "\n\n".join(ref_code_parts) if ref_code_parts else "(无额外参考代码)"

    # 错误反馈（来自 Fault Corrector 的迭代修复）
    error_section = ""
    if error_feedback:
        error_section = f"""
## ⚠️ 上一次生成的代码存在以下问题，请修复:
{error_feedback}
"""

    # 动态构建 few-shot 样例部分
    few_shot_section = f"""## Few-shot 样例

### Testcase 样例:
```ttcn3
{tc_example}
```

### Test Behavior Function 样例:
```ttcn3
{tb_example}
```

### Template 样例:
```ttcn3
{tmpl_example}
```

### Precondition Function 样例:
```ttcn3
{pre_example}
```

### Postcondition Function 样例:
```ttcn3
{post_example}
```
"""

    if tc_with_imports:
        few_shot_section += f"""
### TestCase with Pre/Post Condition Imports 样例:
```ttcn3
{tc_with_imports}
```
"""

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

## From knowledge base local search (from Summarizer):
{local_ref_section if local_ref_section else "(无 local search 结果)"}

## From Summarizer Agent (reference code):
{ref_code_section}

{few_shot_section}
## SOP 常见错误提醒:
{json.dumps(sop.get("common_errors", []), indent=2, ensure_ascii=False)}

请严格按照生成计划和样例格式，生成完整的 TTCN-3 代码。只输出 JSON，不要解释。"""

    response = call_agent_with_timeout(user_prompt, "Core Agent")
    if not response:
        return {"testcase": "", "precondition_files": [], "postcondition_files": [], "_parse_error": True}

    result = extract_core_agent_output(response)
    # 所有解析策略均失败时触发重试
    if result.get("_parse_error"):
        log("Core Agent 响应解析失败，触发一次重试...", "WARN")
        retry_response = call_claude_agent(user_prompt, "Core Agent (retry)", timeout=900)
        if retry_response:
            result = extract_core_agent_output(retry_response)
            if not result.get("_parse_error"):
                log("重试成功", "INFO")
    return result


# ============================================================
# Agent 4: Unified Fault Corrector
# (compile: build error fixes, runtime: post-execution
#  dual-track analysis; static phase is deprecated)
# ============================================================

def run_fault_corrector(
    ttcn3_code: str,
    generation_plan: Dict,
    sop: Dict,
    phase: str = "static",
    tc_id: Optional[str] = None,
    log_path: Optional[Path] = None,
    pcap_path: Optional[Path] = None,
    compile_errors: Optional[List[Dict]] = None,
) -> Dict:
    """Unified Fault Corrector -- single entry point for all correction phases.

    NOTE: phase="static" is DEPRECATED. The static check loop has been removed
    from the pipeline because its checks (syntax, naming, timer, verdict, etc.)
    are fully covered by TITAN's compiler. Use phase="compile" (for build errors)
    or phase="runtime" (for test execution issues) instead.

    Phase   | Trigger                              | Output
    --------|--------------------------------------|------------------------------
    static  | DEPRECATED -- retained for manual/offline use only
    runtime | TITAN executed (log + optional PCAP) | fault_type + repairs
    compile | TITAN build failed (raw errors)      | repairs + corrected_code

    Args:
        ttcn3_code: The TTCN-3 source code under analysis.
        generation_plan: The Orchestrator generation plan JSON.
        sop: The SOP JSON from knowledge base.
        phase: One of "runtime", "compile". The "static" value is deprecated.
        tc_id: Test case ID (required for runtime phase).
        log_path: Path to TITAN TestExecution.log (runtime phase).
        pcap_path: Path to PCAP capture file (runtime phase, optional).
        compile_errors: List of parsed compile errors (compile phase).

    Returns:
        Unified result dict matching the JSON schema in fault_corrector_prompt.txt.
    """
    system_prompt = load_text(FAULT_CORRECTOR_PROMPT_PATH)

    # --- Build dynamic reference content ---
    doc_urls = load_doc_urls()
    compile_urls = build_doc_urls_for_fault_corrector(doc_urls)

    # [DEPRECATED] Compile-time / runtime reference for static phase
    # compile_time_checklist = _build_compile_time_reference()
    # runtime_error_ref = _build_runtime_error_reference()

    # Knowledge base entries for compile phase
    kb_text = _build_kb_reference_text()

    system_prompt = system_prompt.replace("{COMPILE_ERROR_URLS}", compile_urls)
    # [DEPRECATED] static phase references - removed to reduce prompt size
    # system_prompt = system_prompt.replace("{COMPILE_TIME_CHECKLIST}", compile_time_checklist)
    # system_prompt = system_prompt.replace("{RUNTIME_ERROR_REFERENCE}", runtime_error_ref)
    system_prompt = system_prompt.replace("{KNOWLEDGE_BASE_ENTRIES}", kb_text)

    user_prompt_parts = [system_prompt]

    # --- Phase-specific prompt construction ---
    # [DEPRECATED] static phase - removed because TITAN compiler already covers these checks
    # if phase == "static":
    #     user_prompt_parts.append(f"""\
    # ## PHASE: static
    #
    # ## Generated TTCN-3 Code:
    # ```ttcn3
    # {ttcn3_code}
    # ```
    #
    # ## Generation Plan (from Orchestrator):
    # ```json
    # {json.dumps(generation_plan, indent=2, ensure_ascii=False)}
    # ```
    #
    # ## SOP Naming Rules:
    # ```json
    # {json.dumps(sop.get("naming_rules", {}), indent=2)}
    # ```
    #
    # ## SOP Common Errors:
    # ```json
    # {json.dumps(sop.get("common_errors", []), indent=2, ensure_ascii=False)}
    # ```
    #
    # Please perform the 8 static checks and output the JSON result.""")
    #
    # [DEPRECATED] static phase block ends here

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

        # Format log events for the LLM
        log_text = _format_titan_log_events(testcase_events)
        user_prompt_parts.append(f"## TITAN Log Events:\n{log_text}\n")

        # Parse PCAP if available
        packets_text = ""
        if pcap_path and Path(pcap_path).exists() and FAULT_CORRECTOR_AVAILABLE:
            try:
                pcap_parser = PcapParser()
                packets = pcap_parser.parse_file(str(pcap_path))
                packets_text = _format_pcap_packets(packets)
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

    elif phase == "compile":
        user_prompt_parts.append(f"""\
## PHASE: compile

## TTCN-3 Source Code:
```ttcn3
{ttcn3_code}
```

## Generation Plan:
```json
{json.dumps(generation_plan, indent=2, ensure_ascii=False)}
```

## Raw Compile Errors:
```json
{json.dumps(compile_errors or [], indent=2, ensure_ascii=False)}
```

Please analyze the compile errors using the knowledge base and output the JSON result.""")

    else:
        return {
            "status": "error",
            "phase": phase,
            "message": f"Unknown phase: {phase}",
            "issues_found": [],
            "corrected_code": ttcn3_code,
            "repairs": [],
        }

    user_prompt = "\n".join(user_prompt_parts)
    response = call_agent_with_timeout(user_prompt, "Fault Corrector")

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


# ---- helpers used by run_fault_corrector ----

# [DEPRECATED] _build_compile_time_reference - static phase removed
# def _build_compile_time_reference() -> str:
#     return """\
# ### TITAN Compile-Time Checklist (6 items)
# 1. Component type mismatch: testcase runs on A but calls a function runs on B
# 2. Incomplete alt block: a port has send/receive/check operations not covered in the alt block
# 3. Timer state check: timeout referenced before start() is called
# 4. Template type inconsistency: template parameter count or type does not match declaration
# 5. Port mapping conflict: same port mapped to multiple system ports
# 6. runs on clause mismatch: function/altstep runs on component type differs from the caller
#
# ### Link-Time Errors
# - `undefined reference`: C++ adapter does not implement the corresponding method
# - `multiple definition`: symbol defined in multiple .cc files
# - `missing vtable`: virtual function not implemented
# """


# [DEPRECATED] _build_runtime_error_reference - static phase removed
# def _build_runtime_error_reference() -> str:
#     return """\
# ### Runtime Error Checks
# - `Port not mapped`: map(mtc:port_name, system:port_name) not called before test starts
# - `timer has not been started`: start() not called before timeout branch
# - `Component not created`: PTC not properly created or not set to alive
# - `Verdict not set`: verdict not set before testcase ends
# """


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


def _format_titan_log_events(events: List) -> str:
    """Format TitanLogEntry objects for the LLM prompt."""
    if not events:
        return "(no events)"
    lines = []
    for e in events:
        ts = getattr(e, "timestamp", "???")
        et = getattr(e, "event_type", "???")
        det = getattr(e, "details", "")
        lines.append(f"[{ts}] {et}: {det}")
    return "\n".join(lines)


def _format_pcap_packets(packets: List) -> str:
    """Format V2GTP_Packet objects for the LLM prompt."""
    if not packets:
        return "(no packets)"
    lines = []
    for p in packets:
        ts = getattr(p, "timestamp", "???")
        direction = getattr(p, "direction", "???")
        proto_ver = getattr(p, "protocol_version", "?")
        payload_type = getattr(p, "payload_type", "?")
        info = getattr(p, "packet_info", "")
        lines.append(
            f"[{ts:.3f}] {direction} proto={proto_ver} payloadType={payload_type}: {info}"
        )
    return "\n".join(lines)


# ============================================================
# Agent 6: Infrastructure Agent - 生成完整项目结构
# ============================================================

def load_demo_file(relative_path: str) -> str:
    """从 demo/ 目录加载参考文件内容"""
    path = DEMO_ROOT / relative_path
    if path.exists():
        return load_text(path)
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
Now generate the project structure in batches.

## Batch 1 (this response):
Generate these files ONLY:
1. config/titan_runtime.cfg
2. config/hardware_env.json
3. src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn
4. src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn
5. src/ttcn3/1_Data_Definitions/Types/TimerAndConstants.ttcn
6. src/ttcn3/1_Data_Definitions/Templates/CmnTemplates.ttcn
7. src/ttcn3/1_Data_Definitions/PICS/PICS_SECC_15118_2.ttcn

## Batch 2 (will be requested separately):
Generate these files AFTER receiving this response:
8. src/ttcn3/1_Data_Definitions/PIXIT/PIXIT_SECC_15118_2.ttcn
9. src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn
10. build.sh
11. port_implementation_stub.txt

NOTE: PreConditions and PostConditions function definition files
(f_SECC_CMN_PR_*.ttcn, f_SECC_CMN_PO_*.ttcn) are generated by the
Core Agent and placed by the pipeline. Do NOT include them here.

IMPORTANT: For this batch, generate ONLY the files listed in Batch 1.
Output ONLY the JSON object with those files."""

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

    batch2_instructions = f"""
Now generate the remaining files for the project.

## Previously Generated (Batch 1):
These files were already generated: config files, ComponentsAndPorts.ttcn, Types_*, Templates, PICS.

## Batch 2 (this response):
Generate these files ONLY:
8. src/ttcn3/1_Data_Definitions/PIXIT/PIXIT_SECC_15118_2.ttcn
9. src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn
10. build.sh
11. port_implementation_stub.txt

NOTE: PreConditions and PostConditions function definition files
(f_SECC_CMN_PR_*.ttcn, f_SECC_CMN_PO_*.ttcn) are generated by the
Core Agent and placed by the pipeline. Do NOT include them here.

IMPORTANT: Generate ONLY the files listed above for this batch.
Output ONLY the JSON object with those files."""

    batch2_prompt = f"""{system_prompt}

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

{batch2_instructions}
"""

    return [batch1_prompt, batch2_prompt]


def run_infrastructure_agent(
    generation_plan: Dict,
    ttcn3_code: str,
    tc_id: str,
    sut_ip: Optional[str] = None,
    tc_output_filename: str = "",
) -> Dict:
    """运行 Infrastructure Agent，生成完整的 TITAN 项目结构

    优化后的架构（v2）：
    1. 静态文件（所有 C++ 适配器 + port skeleton）全部从 demo 复制/pipeline 生成
    2. Agent 只负责生成 ~14 个 TTCN/config/build 文件（减少约 46%）
    3. Agent 输出大小从 26+ 文件减少到 ~14 文件，大幅降低 JSON 截断风险

    文件分工：
    - Pipeline 处理：14 个 C++ 适配器文件 + 4 个 port skeleton 文件
    - Agent 处理：~14 个配置文件 + TTCN 骨架 + build.sh

    Args:
        generation_plan: Orchestrator 生成的计划 JSON
        ttcn3_code: Core Agent 生成的完整测试用例 TTCN-3 代码
        tc_id: 测试用例 ID
        sut_ip: SUT IP 地址（可选，用于配置）

    Returns:
        包含生成结果和警告的字典
    """
    import time
    import shutil

    system_prompt = load_text(INFRASTRUCTURE_AGENT_PROMPT_PATH)

    doc_urls = load_doc_urls()
    doc_urls_text = build_doc_urls_for_infrastructure(doc_urls)
    system_prompt = system_prompt.replace("{DOC_URLS_INFRASTRUCTURE}", doc_urls_text)

    # === 所有静态文件：Pipeline 直接复制，不通过 Agent ===
    # C++ 适配器（12 个 .hh + .cc 文件）
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

    # === Demo 参考文件：只包含 Agent 需要的 TTCN 骨架参考 ===
    def _load_demo(path: str) -> str:
        return load_demo_file(path)

    # Agent 需要的参考文件（只加载 TTCN 骨架，prompt 大幅减小）
    demo_parts = []

    # 配置文件参考
    demo_parts.append("### Reference: Config Files")
    demo_parts.append(f"[File: config/titan_runtime.cfg]\n{_load_demo('config/titan_runtime.cfg')}")
    demo_parts.append(f"[File: config/hardware_env.json]\n{_load_demo('config/hardware_env.json')}")

    # build.sh 参考（截断）
    build_sh_content = _load_demo("build.sh")
    if len(build_sh_content) > 500:
        build_sh_content = build_sh_content[:500] + "\n... (truncated, see demo/build.sh for full content)"
    demo_parts.append(f"[File: build.sh]\n{build_sh_content}")

    # TTCN 骨架参考（Agent 需要这些来生成正确的文件结构）
    ttcn_ref_files = [
        "src/ttcn3/1_Data_Definitions/PIXIT/PIXIT_SECC_15118_2.ttcn",
        "src/ttcn3/4_TestCases/TestCases_SECC/TestCases_SECC.ttcn",
        "src/ttcn3/3_Behavior_Steps/PreConditions/PreConditions_SECC_15118_2.ttcn",
        "src/ttcn3/3_Behavior_Steps/PostConditions/PostConditions_SECC_15118_2.ttcn",
    ]
    demo_parts.append("### Reference: TTCN Skeleton Files")
    for ref_file in ttcn_ref_files:
        content = _load_demo(ref_file)
        if content:
            demo_parts.append(f"[File: {ref_file}]\n{content}")

    # NOTE: PreConditions and PostConditions function definitions are now generated
    # by the Core Agent and written by the pipeline. The Infrastructure Agent only
    # needs the aggregator modules (PreConditions_SECC_15118_2.ttcn, PostConditions_SECC_15118_2.ttcn)
    # for build.sh reference, which are already in ttcn_ref_files above.
    # The actual function definitions are provided by the Core Agent's output.

    # 说明：静态文件由 pipeline 自动处理
    demo_parts.append("\n### NOTE: The following files are handled by the pipeline automatically")
    demo_parts.append("- 12 C++ adapter files: copied from demo/")
    demo_parts.append("- 4 port skeleton files: generated by 'ttcn3_compiler -t' in build.sh")
    demo_parts.append("- PreConditions/PostConditions function definitions: generated by Core Agent, written by pipeline")
    demo_parts.append("DO NOT include these files in your output JSON.")

    demo_summary_text = "\n".join(demo_parts)

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
{ttcn3_code}
```

### SUT IP (for titan_runtime.cfg):
{sut_ip if sut_ip else "(not provided, will use IPv6 multicast ff02::01)"}

### Demo Reference Files:

{demo_summary_text}

Now generate the project structure. Output ONLY the JSON object.
The JSON must include:
1. config files (2 files)
2. TTCN skeleton files (~8 files)
3. build.sh
4. logs directory markers

Do NOT include C++ adapter files or port skeleton files - these are handled by the pipeline."""

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
    # When output is expected to be large (>200 files), use batch mode
    if BATCH_MODE_ENABLED and prompt_size > PROMPT_SIZE_WARNING:
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

        if not response_text:
            return {"status": "error", "files": [], "message": "Empty response", "attempt": attempt}

        if len(response_text.strip()) < 100:
            return {
                "status": "error",
                "files": [],
                "message": f"Response too short ({len(response_text)} chars)",
                "raw_output": response_text[:500],
                "attempt": attempt,
            }

        # Try JSON parsing
        result = None
        try:
            result = extract_json_from_response(response_text)
        except Exception as e:
            log(f"extract_json_from_response raised: {e}", "WARN")

        if not result:
            log("JSON parse failed, attempting partial recovery...", "WARN")
            partial_files = _extract_partial_files_from_text(response_text)
            if partial_files:
                log(f"Recovered {len(partial_files)} partial file entries")
                result = {
                    "status": "partial",
                    "files": partial_files,
                    "message": f"JSON truncated, partial recovery ({len(partial_files)} files)",
                    "warnings": [],
                    "attempt": attempt
                }
            else:
                return {
                    "status": "error",
                    "files": [],
                    "message": "JSON parse failed and no partial recovery",
                    "raw_output": response_text[:2000],
                    "attempt": attempt
                }

        # Normalize: ensure "files" key exists
        if "files" not in result:
            result["files"] = result.pop("files_generated", [])
        else:
            result["files"] = result.get("files", [])

        result["attempt"] = attempt
        return result

    # === 重试逻辑（改进版：指数退避 + 渐进式 prompt 缩减）===
    retry_history = []

    infra_result = None
    final_response = ""

    for retry_count in range(MAX_INFRA_RETRY):
        if batch_prompts and retry_count == 0:
            # Use batch mode for first attempt when prompt is large
            all_files = []
            for batch_idx, batch_prompt in enumerate(batch_prompts):
                log(f"Batch {batch_idx + 1}/{len(batch_prompts)}: size={len(batch_prompt)} chars", "INFO")
                batch_response = call_agent_with_timeout(batch_prompt, "Infrastructure Agent", retry_on_timeout=False)
                batch_result = _process_infra_response(batch_response, attempt=1)
                if batch_result.get("files"):
                    all_files.extend(batch_result.get("files", []))
                else:
                    log(f"Batch {batch_idx + 1} returned no files", "WARN")

            infra_result = {
                "status": "success" if len(all_files) >= EXPECTED_INFRA_FILES_MIN else "partial",
                "files": all_files,
                "message": f"Batch mode: {len(all_files)} files from {len(batch_prompts)} batches",
                "batch_mode": True
            }
            final_response = str(all_files)  # Placeholder for batch responses
        elif retry_count == 0:
            # 第一次调用（非批量模式）
            response = call_agent_with_timeout(user_prompt, "Infrastructure Agent")
            infra_result = _process_infra_response(response, attempt=1)
            final_response = response
        else:
            # 指数退避：等待一段时间后再重试
            backoff_time = RETRY_BACKOFF[retry_count] if retry_count < len(RETRY_BACKOFF) else RETRY_BACKOFF[-1]
            if backoff_time > 0:
                log(f"Waiting {backoff_time}s before retry...", "WARN")
                time.sleep(backoff_time)

            # 渐进式 prompt 缩减策略
            if retry_count == 1:
                # 第一次重试：移除 TTCN 代码
                reduced_prompt = user_prompt.replace(
                    f"### Generated TTCN-3 Test Code (will be placed at src/ttcn3/4_TestCases/{tc_output_filename}):\n```ttcn3\n{ttcn3_code}\n```",
                    f"### Generated TTCN-3 Test Code\n[CORE AGENT OUTPUT AVAILABLE - SKIPPED TO REDUCE PROMPT SIZE]"
                )
                log(f"Retry {retry_count + 1}: Removing TTCN code from prompt", "WARN")
            elif retry_count == 2:
                # 第二次重试：进一步截断 demo 参考文件
                reduced_prompt = _reduce_prompt_aggressively(user_prompt)
                log(f"Retry {retry_count + 1}: Aggressively reducing prompt", "WARN")
            else:
                # 最小化版本
                reduced_prompt = _reduce_prompt_minimal(user_prompt)
                log(f"Retry {retry_count + 1}: Using minimal prompt", "WARN")

            log(f"  - prompt reduced from {len(user_prompt)} to {len(reduced_prompt)} chars", "WARN")
            response = call_claude_agent(reduced_prompt, "Infrastructure Agent", timeout=600)
            infra_result = _process_infra_response(response, attempt=retry_count + 1)
            final_response = response

        # 记录重试历史
        retry_history.append({
            "attempt": retry_count + 1,
            "status": infra_result.get("status"),
            "files_count": len(infra_result.get("files", [])),
            "message": infra_result.get("message", ""),
        })

        # 检查是否成功
        if infra_result.get("status") == "success" and infra_result.get("files"):
            break
        elif infra_result.get("status") == "partial" and len(infra_result.get("files", [])) >= EXPECTED_INFRA_FILES_MIN:
            # 部分成功且文件数量足够
            log(f"Infrastructure Agent partial success with {len(infra_result.get('files', []))} files", "WARN")
            break
        elif retry_count < MAX_INFRA_RETRY - 1:
            log(f"  - retry needed, status: {infra_result.get('status')}, files: {len(infra_result.get('files', []))}", "WARN")

    # === 添加所有静态文件到结果 ===
    if infra_result and infra_result.get("status") in ("success", "partial"):
        # C++ 适配器文件（Pipeline 复制）
        static_copied = []
        for rel_path in ALL_CPP_ADAPTER_FILES:
            content = load_demo_file(rel_path)
            if content:
                infra_result["files"].append({
                    "path": rel_path,
                    "content": content,
                    "source": "static_copy",
                    "description": "C++ adapter copied from demo"
                })
                static_copied.append(rel_path)

        if static_copied:
            log(f"Added {len(static_copied)} C++ adapter files from demo", "DEBUG")
            infra_result["static_files_copied"] = static_copied

        # 添加重试历史
        infra_result["retry_history"] = retry_history

        # 添加 port skeleton 文件说明（由 build.sh 中的 ttcn3_compiler -t 生成）
        infra_result["port_skeleton_info"] = {
            "note": "Port skeleton files are generated by 'ttcn3_compiler -t' during build",
            "files": [
                "V2G__TCP__TLS__Port.hh",
                "V2G__TCP__TLS__Port.cc",
                "V2G__UDP__SDP__Port.hh",
                "V2G__UDP__SDP__Port.cc"
            ],
            "source": "pipeline: ttcn3_compiler -t in build.sh"
        }

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


def _validate_file_content(content: str, path: str) -> tuple[bool, str]:
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


def _reduce_prompt_aggressively(prompt: str) -> str:
    """Aggressively reduce prompt size by truncating demo reference content.

    Strategy:
    1. Keep generation plan
    2. Remove TTCN code (already done in first retry)
    3. Truncate demo reference files based on their type
    4. Keep essential structure references
    """
    import re

    # Truncate demo reference files
    lines = prompt.split('\n')
    result_lines = []
    skip_section = False
    current_file = None

    for line in lines:
        # Detect file section headers
        file_match = re.match(r'\[File:\s*(.+)\]', line)
        if file_match:
            current_file = file_match.group(1)
            skip_section = False
            # Check if this is a demo file that should be truncated
            truncate_ratio = _get_truncation_ratio(current_file)
            if truncate_ratio < 1.0:
                result_lines.append(line)
                skip_section = True
                continue
        elif line.startswith('### NOTE:') or line.startswith('Now generate'):
            skip_section = False
            current_file = None

        if skip_section and current_file:
            # This is content within a file that should be truncated
            # Only include first part of content
            if not line.startswith('[File:') and not line.startswith('###'):
                if '...' not in line and 'TRUNCATED' not in line:
                    continue  # Skip this line

        result_lines.append(line)

    reduced = '\n'.join(result_lines)

    # Also remove demo files section entirely if still too large
    if len(reduced) > PROMPT_SIZE_WARNING:
        # Remove the entire Demo Reference Files section
        demo_start = reduced.find('### Demo Reference Files:')
        if demo_start != -1:
            demo_end = reduced.find('Now generate', demo_start)
            if demo_end != -1:
                demo_section = reduced[demo_start:demo_end]
                # Only keep essential file references
                essential_files = ['PIXIT_SECC_15118_2.ttcn', 'ComponentsAndPorts.ttcn']
                kept_lines = []
                for line in demo_section.split('\n'):
                    if any(f in line for f in essential_files):
                        kept_lines.append(line)
                    elif line.startswith('### Reference:') or line.startswith('[File:'):
                        kept_lines.append(line)

                reduced = reduced[:demo_start] + '\n'.join(kept_lines) + '\n' + reduced[demo_end:]

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


def write_infrastructure_files(tc_dir: Path, infra_result: Dict,
                                tc_output_filename: str = "") -> Tuple[List[Path], Optional[str]]:
    """将 Infrastructure Agent 生成的文件写入磁盘

    Args:
        tc_dir: 测试用例项目根目录
        infra_result: run_infrastructure_agent() 返回的结果
        tc_output_filename: 测试用例文件名（如 TC_SECC_CMN_VTB_SupportedAppProtocol_001.ttcn）
                           用于注入到 infra_result 中供 verify 函数使用

    Returns:
        Tuple of (写入的所有文件路径列表, port_implementation_stub 内容)
    """
    written = []
    port_impl_content = None  # Infrastructure Agent 提供的端口实现代码
    files_list = infra_result.get("files", infra_result.get("files_generated", []))

    # 将 tc_output_filename 注入到 infra_result 中，供 verify_infrastructure_agent_output 使用
    if tc_output_filename:
        infra_result["tc_output_filename"] = tc_output_filename

    # 已知关键文件的最小有效行数阈值（用于检测截断）
    MIN_LINES = {
        ".cc": 10,    # C++ implementation file
        ".hh": 5,     # C++ header file
        ".ttcn": 5,   # TTCN-3 module
        ".cfg": 10,   # TITAN runtime config
        ".json": 5,   # JSON config
        ".sh": 5,     # Shell script
        ".bat": 5,    # Batch script
        ".gitkeep": 0,  # Empty directory marker
    }

    def _min_lines(path_str: str) -> int:
        for ext, n in MIN_LINES.items():
            if path_str.endswith(ext):
                return n
        return 5  # 默认最小 5 行

    # 明显截断的特征模式
    TRUNCATED_PATTERNS = [
        "#include \\",
        "{\"path\":",
        "---",
        "```",
    ]

    for file_entry in files_list:
        rel_path = file_entry.get("path", "")
        content = file_entry.get("content", file_entry.get("code", ""))

        if not rel_path or not content:
            continue

        # 安全检查：路径不能包含 .. 防止目录穿越
        if ".." in rel_path:
            log(f"Skipping unsafe path: {rel_path}", "WARN")
            continue

        # 特殊处理：port_implementation_stub.txt 稍后处理
        if rel_path == "port_implementation_stub.txt":
            port_impl_content = content
            continue

        # Content quality check: reject clearly truncated content
        if any(content.startswith(p) for p in TRUNCATED_PATTERNS):
            if ".gitkeep" not in rel_path:
                log(f"  Skipping truncated content for {rel_path} (starts with: {content[:30]!r})", "WARN")
                continue

        # 最小行数检查：行数少于阈值的文件跳过
        # 注意：对于 Agent 生成的较小文件（如简单的 stub TTCN），降低阈值
        min_req = _min_lines(rel_path)
        # Agent 生成的 stub TTCN 文件可能很短（5-10 行），降低检查严格度
        if rel_path.endswith('.ttcn') and not any(x in rel_path for x in ['PreConditions', 'PostConditions', 'TestCases']):
            min_req = min(min_req, 3)  # Stub 类型文件降低到 3 行
        line_count = content.count('\n') + 1
        if line_count < min_req:
            log(f"  Skipping too-short content for {rel_path} ({line_count} lines < {min_req})", "WARN")
            continue

        file_path = tc_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        written.append(file_path)
        log(f"  Written: {rel_path}")

    # 将 port_implementation_stub.txt 写入项目目录
    # 注意：端口骨架由 generate_port_skeletons() 通过 ttcn3_compiler -t 生成，
    # 这里只将 Agent 提供的 stub 内容作为参考文档写入项目根目录，
    # 供后续参考或手动使用
    if port_impl_content:
        stub_path = tc_dir / "port_implementation_stub.txt"
        stub_path.parent.mkdir(parents=True, exist_ok=True)
        with open(stub_path, "w", encoding="utf-8") as f:
            f.write(port_impl_content)
        written.append(stub_path)
        log(f"  Written: port_implementation_stub.txt ({len(port_impl_content)} bytes, for reference)")

    return written, port_impl_content


def _parse_port_implementation_stub(stub_content: str) -> List[Dict]:
    """解析 port_implementation_stub.txt 并生成端口骨架文件

    Args:
        stub_content: port_implementation_stub.txt 的内容

    Returns:
        端口文件列表 [{"path": "...", "content": "..."}]
    """
    import re

    port_files = []

    # 解析格式: ### filename.ext ### ...content... ### filename2.ext ###
    sections = re.split(r'###\s*([^\s#]+)\s*###', stub_content)

    if len(sections) < 3:
        # 尝试其他格式 - 整个内容作为 .cc 实现
        log(f"  Warning: port_implementation_stub.txt format unexpected, trying fallback", "WARN")
        return []

    # sections[0] 是第一个 ### 之前的内容（通常是空的）
    # sections[1], sections[3], ... 是文件名
    # sections[2], sections[4], ... 是文件内容
    i = 1
    while i < len(sections) - 1:
        filename = sections[i].strip()
        content = sections[i + 1].strip() if i + 1 < len(sections) else ""

        if filename and content:
            # 确定文件路径
            if filename.endswith("_Port.hh"):
                path = f"build/{filename}"
            elif filename.endswith("_Port.cc"):
                path = f"build/{filename}"
            else:
                path = f"build/{filename}"

            port_files.append({"path": path, "content": content})
        i += 2

    # 如果解析失败，尝试简单的字符串匹配
    if not port_files:
        # 尝试从内容中提取 V2G__TCP__TLS__Port.cc 和 V2G__UDP__SDP__Port.cc
        tcp_tls_match = re.search(
            r'(V2G__TCP__TLS__Port\.cc\s*=+\s*)(.*?)(?====|\Z)',
            stub_content, re.DOTALL
        )
        udp_sdp_match = re.search(
            r'(V2G__UDP__SDP__Port\.cc\s*=+\s*)(.*?)(?====|\Z)',
            stub_content, re.DOTALL
        )

        if tcp_tls_match:
            port_files.append({
                "path": "build/V2G__TCP__TLS__Port.cc",
                "content": tcp_tls_match.group(2).strip()
            })
        if udp_sdp_match:
            port_files.append({
                "path": "build/V2G__UDP__SDP__Port.cc",
                "content": udp_sdp_match.group(2).strip()
            })

    return port_files


# ============================================================
# Port Skeleton 生成
# ============================================================

def generate_port_skeletons(tc_dir: Path, port_impl_stub: Optional[str] = None) -> Dict:
    """使用 ttcn3_compiler -t 生成端口骨架文件

    Args:
        tc_dir: 测试用例项目根目录
        port_impl_stub: Infrastructure Agent 提供的端口实现代码，用于填充骨架

    Returns:
        包含生成结果的字典 {"status": "success"|"error", "files": [...], "message": "..."}
    """
    import shutil

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

    build_dir = tc_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    # 运行 ttcn3_compiler -t 生成骨架
    # 注意：在 build 目录下运行，从相对路径访问 ComponentsAndPorts.ttcn
    components_rel = "../src/ttcn3/2_Architecture/ComponentsAndPorts/ComponentsAndPorts.ttcn"

    try:
        log(f"Running ttcn3_compiler -t to generate port skeletons...")
        result = subprocess.run(
            [ttcn3_compiler, "-t", components_rel],
            capture_output=True,
            text=True,
            cwd=str(build_dir),
            timeout=60
        )

        if result.returncode != 0:
            log(f"ttcn3_compiler -t failed: {result.stderr}", "WARN")
            return {
                "status": "error",
                "files": [],
                "message": f"ttcn3_compiler -t failed: {result.stderr[:500]}"
            }

        # 检查生成的文件
        generated_files = []
        for f in build_dir.glob("V2G__*__Port.*"):
            generated_files.append({
                "path": str(f.relative_to(tc_dir)),
                "size": f.stat().st_size
            })
            log(f"  Generated: {f.name} ({f.stat().st_size} bytes)")

        if not generated_files:
            log("No port skeleton files generated", "WARN")
            return {
                "status": "error",
                "files": [],
                "message": "No port skeleton files generated"
            }

        # === 填充端口实现代码 ===
        # 在骨架生成后，用 Infrastructure Agent 提供的实现填充 .cc 文件
        if port_impl_stub:
            port_impl_files = _parse_port_implementation_stub(port_impl_stub)
            for port_impl in port_impl_files:
                impl_path = build_dir / Path(port_impl["path"]).name
                with open(impl_path, "w", encoding="utf-8") as f:
                    f.write(port_impl["content"])
                log(f"  Filled port implementation: {impl_path.name}")
                # 更新 generated_files 列表
                for gf in generated_files:
                    if gf["path"].endswith(Path(port_impl["path"]).name):
                        gf["size"] = impl_path.stat().st_size

        log(f"Successfully generated {len(generated_files)} port skeleton files")
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


def _copy_default_port_files(tc_dir: Path) -> None:
    """从 examples/ 复制默认的端口实现文件作为后备

    当 ttcn3_compiler -t 无法生成骨架时使用。

    Args:
        tc_dir: 测试用例项目根目录
    """
    examples_dir = DEMO_ROOT  # agent/test_code/examples

    if not examples_dir.exists():
        log(f"examples/ not found, cannot copy default port files", "WARN")
        return

    project_build_dir = tc_dir / "build"
    project_build_dir.mkdir(parents=True, exist_ok=True)

    # 需要复制的端口文件
    port_files = [
        "V2G__TCP__TLS__Port.hh",
        "V2G__TCP__TLS__Port.cc",
        "V2G__UDP__SDP__Port.hh",
        "V2G__UDP__SDP__Port.cc",
    ]

    for port_file in port_files:
        src = examples_dir / port_file
        dst = project_build_dir / port_file
        if src.exists():
            shutil.copy2(src, dst)
            log(f"  Copied default port file: {port_file}")
        else:
            log(f"  Default port file not found: {port_file}", "WARN")


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

    # === Step 1: 平台检测 + 选择构建脚本 ===
    is_windows = os.name == 'nt'
    build_script = tc_dir / ("build.bat" if is_windows else "build.sh")

    if not build_script.exists():
        return {
            "status": "skipped",
            "reason": "build_script_not_found",
            "message": f"构建脚本未找到: {build_script}",
        }

    # === Step 2: 启动 Sniffer（可选）===
    sniffer = None
    if pcap_output:
        try:
            from fault_corrector.sniffer import V2G_Sniffer
            sniffer = V2G_Sniffer(output_pcap=str(pcap_output))
            sniffer.start()
            log(f"Sniffer started, capturing to {pcap_output}")
        except ImportError:
            log("Fault Agent sniffer not available (scapy not installed), skipping capture", "WARN")
        except Exception as e:
            log(f"Sniffer start failed: {e}", "WARN")

    try:
        # === Step 3: 执行构建脚本 ===
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

        # 保存编译输出
        build_log_path = tc_dir / "logs" / "system_logs" / "build.log"
        build_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(build_log_path, "w", encoding="utf-8") as f:
            f.write(f"=== STDOUT ===\n{build_stdout}\n\n=== STDERR ===\n{build_stderr}\n")
        log(f"Build log saved: {build_log_path}")

        # 解析 + 分类编译错误
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

        # === Step 4: 运行测试 ===
        log(f"Running test: {exec_path} with config/titan_runtime.cfg")
        runtime_cfg = tc_dir / "config" / "titan_runtime.cfg"

        run_result = subprocess.run(
            [str(exec_path), str(runtime_cfg)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(exec_path.parent),
        )

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
    finally:
        # === Step 5: 停止 Sniffer ===
        if sniffer:
            try:
                saved_path = sniffer.stop_and_save()
                log(f"Sniffer stopped, saved to {saved_path}")
            except Exception as e:
                log(f"Sniffer stop failed: {e}", "WARN")


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

    # TTCN-3 语法错误: "<filename>:<line>: error: <message>"
    ttcn_pattern = re.compile(
        r'(?P<file>[^:\n]+):(?P<line>\d+):\s*error:\s*(?P<msg>.+)',
        re.IGNORECASE
    )

    # C++ 编译错误: "<filename>:<line>:<col>: error: <message>"
    cpp_pattern = re.compile(
        r'(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s*error:\s*(?P<msg>.+)',
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

    return errors


def build_error_to_feedback(compile_errors: List[Dict]) -> str:
    """将编译错误转换为 Core Agent 的 error_feedback 字符串。

    包含：错误类型 + 位置 + 原始消息 + 修复建议 + 错误上下文行号。
    与 CompileErrorClassifier 结合使用，提供结构化的错误反馈。

    Args:
        compile_errors: parse_compile_errors() 返回的错误列表

    Returns:
        格式化的错误描述字符串
    """
    if not compile_errors:
        return ""

    # 分类器用于获取修复建议
    classifier = CompileErrorClassifier()

    lines = ["## Compilation Errors - Please Fix:"]
    for i, err in enumerate(compile_errors, 1):
        err_type = err.get("type", "unknown")
        err_msg = err.get("message", "")
        err_file = err.get("file", "")
        err_line = err.get("line", 0)

        # 获取修复建议
        fix_suggestion = ""
        if err_type in classifier.FIX_SUGGESTIONS:
            fix_suggestion = classifier.FIX_SUGGESTIONS[err_type]

        lines.append(f"\n### Error {i}: [{err_type}]")
        lines.append(f"**Location:** {err_file}:{err_line}" if err_file else "**Location:** unknown")
        lines.append(f"**Message:** {err_msg}")
        if fix_suggestion:
            lines.append(f"**Fix:** {fix_suggestion}")

    lines.append("\n## Guidelines:")
    lines.append("- Keep the testcase ID and module name unchanged")
    lines.append("- Fix ALL errors listed above")
    lines.append("- Output the corrected TTCN-3 code as JSON")
    lines.append("- Do not change working code, only fix the errors")

    return "\n".join(lines)


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

    # === auto_fix 类型：字符串替换 ===
    auto_repairs = [
        r for r in repairs
        if r.get("repair_type") == "auto_fix"
        and r.get("suggested_code")
        and r.get("current_code")
    ]

    current_code = ttcn3_code
    applied_repairs = []

    if auto_repairs:
        log(f"Fault Agent: 发现 {len(auto_repairs)} 个 auto_fix 项")
        for repair in auto_repairs:
            current = repair.get("current_code", "")
            suggested = repair.get("suggested_code", "")
            if current and suggested and current in current_code:
                current_code = current_code.replace(current, suggested, 1)
                applied_repairs.append({
                    "type": repair.get("repair_type"),
                    "fault_type": repair.get("fault_type"),
                    "location": repair.get("location", ""),
                    "raw_message": repair.get("reasoning", ""),
                    "applied": True,
                })
                log(f"  Applied fix: [{repair.get('fault_type')}] at {repair.get('location', 'unknown')}")
            else:
                applied_repairs.append({
                    "type": repair.get("repair_type"),
                    "fault_type": repair.get("fault_type"),
                    "location": repair.get("location", ""),
                    "raw_message": repair.get("reasoning", ""),
                    "applied": False,
                    "reason": "current_code snippet not found in source",
                })
                log(f"  Skipped (not found): [{repair.get('fault_type')}]", "WARN")

    # === manual_review 类型：调用 Core Agent + Infrastructure Agent ===
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
                # Infrastructure Agent 重新生成项目骨架
                tc_output_filename_for_repair = derive_tc_filename(plan_tc_id)
                infra_result = run_infrastructure_agent(
                    generation_plan=generation_plan,
                    ttcn3_code=fixed_code,
                    tc_id=plan_tc_id,
                    sut_ip=sut_ip,
                    tc_output_filename=tc_output_filename_for_repair,
                )
                if infra_result.get("status") != "error":
                    write_infrastructure_files(tc_project_dir, infra_result, tc_output_filename_for_repair)[0]  # 忽略 port_impl_stub

                # 写入修复后的 TTCN-3 代码
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(fixed_code)
                with open(tc_ttcn_dest, "w", encoding="utf-8") as f:
                    f.write(fixed_code)
                current_code = fixed_code
                log(f"Core Agent 修复代码已写入: {output_path}")
            else:
                log("Core Agent 未生成修复代码（manual_review）", "WARN")
        else:
            log("manual_review 修复缺少必要参数（few_shot_examples/sop），跳过", "WARN")

    if not any(r["applied"] for r in applied_repairs) and not manual_review_repairs:
        return None

    # 回写最终代码
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(current_code)
    with open(tc_ttcn_dest, "w", encoding="utf-8") as f:
        f.write(current_code)
    log(f"自修复代码已写入: {output_path}")

    # rebuild 验证
    pcap_out = tc_project_dir / "logs" / "capture.pcap"
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
            for repair in applied_repairs:
                if repair.get("applied"):
                    learn_from_repair(repair, repair.get("raw_message", ""))
            log(f"知识库已更新: {len([r for r in applied_repairs if r.get('applied')])} 条新修复记录")
        except Exception as e:
            log(f"知识库更新失败: {e}", "WARN")
        return {
            "status": "repair_success",
            "repairs": applied_repairs,
            "build_status": retry_status,
        }
    elif retry_status == "build_failed":
        retry_errors = retry_result.get("compile_errors", [])
        log(f"运行时自修复: 修复后编译仍然失败 ({len(retry_errors)} 个错误)，回退", "WARN")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(ttcn3_code)
        with open(tc_ttcn_dest, "w", encoding="utf-8") as f:
            f.write(ttcn3_code)
        return {
            "status": "repair_compile_failed",
            "repairs": applied_repairs,
            "build_status": retry_status,
            "compile_errors": retry_errors,
        }
    else:
        log(f"运行时自修复: 重编译结果={retry_status}，保留修复代码")
        return {
            "status": f"repair_{retry_status}",
            "repairs": applied_repairs,
            "build_status": retry_status,
        }


# ============================================================
# 单个测试用例的代码生成
# ============================================================
def generate_code_for_test_case(
    test_case: Dict,
    sut_type: Optional[str] = None,
    requirement_context: str = "",
    titan_log: Optional[Path] = None,
    pcap_path: Optional[Path] = None,
    with_infrastructure: bool = False,
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
        with_infrastructure: 是否生成完整项目结构（config + cpp_adapters + build.sh）
        skip_build: 是否跳过编译运行步骤
        sut_ip: SUT IP 地址（用于 titan_runtime.cfg 配置）

    Returns:
        包含生成结果的字典，至少包含 `ttcn_path` 字段；
        如果 with_infrastructure=True，还包含 `project_dir`、`infra_files`、`build_result`
    """
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
    generation_plan = run_orchestrator(tc, requirement_context, sop)
    # 注意：run_orchestrator 现在即使 LLM 失败也会返回 fallback plan，不再需要此检查
    # 但保留此处以防未来变化
    if not generation_plan:
        log("Orchestrator 失败，流水线终止", "ERROR")
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

    references = run_summarizer(generation_plan, kb_search_results)
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
    core_result = run_core_agent(
        generation_plan, references, few_shot_examples, sop, "",
        sut_mode=effective_sut_mode, local_ref_section=local_ref_section
    )
    if not core_result or not core_result.get("testcase"):
        log("Core Agent 未生成代码", "ERROR")
        return None

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

    # === Step 5: 写入 Core Agent 生成的 pre/post condition 文件 ===
    # (稍后在 Infrastructure Agent 流程中整合到项目目录)

    # === Step 5b: Infrastructure Agent - 生成完整项目结构 ===
    if with_infrastructure:
        log(f"\n--- Step 5b: Infrastructure Agent (生成完整项目) ---")

        tc_output_filename = derive_tc_filename(plan_tc_id)
        infra_result = run_infrastructure_agent(
            generation_plan=generation_plan,
            ttcn3_code=ttcn3_code,
            tc_id=plan_tc_id,
            sut_ip=sut_ip,
            tc_output_filename=tc_output_filename,
        )

        if infra_result is None:
            log("Infrastructure Agent 失败: extract_json_from_response 异常", "ERROR")
            result["infra_status"] = "error"
            result["infra_message"] = "Infrastructure Agent 解析异常"
        elif infra_result.get("status") == "error":
            log(f"Infrastructure Agent 失败: {infra_result.get('message')}", "ERROR")
            result["infra_status"] = "error"
            result["infra_message"] = infra_result.get("message")
        elif infra_result.get("status") == "partial":
            log(f"Infrastructure Agent 部分恢复: {infra_result.get('message', '')}", "WARN")
            result["infra_status"] = "partial"
        elif not infra_result.get("files"):
            # status 为 success 但 files 为空，同样视为失败
            log("Infrastructure Agent 失败: status=success 但 files 列表为空", "ERROR")
            result["infra_status"] = "error"
            result["infra_message"] = "Agent 返回成功但未生成任何文件"
        else:
            # 创建项目目录
            tc_project_dir = GENERATED_DIR / plan_tc_id
            tc_project_dir.mkdir(parents=True, exist_ok=True)

            # 写入基础设施文件
            written_files, port_impl_stub = write_infrastructure_files(tc_project_dir, infra_result, tc_output_filename)
            log(f"Infrastructure Agent 生成 {len(written_files)} 个文件")

            # === Step 5b-1: 写入 Core Agent 生成的 pre/post condition 文件 ===
            all_core_files = pre_condition_files + post_condition_files
            for f_entry in all_core_files:
                f_rel_path = f_entry.get("path", "")
                f_content = f_entry.get("content", "")
                if not f_rel_path or not f_content:
                    continue
                f_path = tc_project_dir / f_rel_path
                f_path.parent.mkdir(parents=True, exist_ok=True)
                with open(f_path, "w", encoding="utf-8") as fp:
                    fp.write(f_content)
                written_files.append(f_path)
                log(f"  Core Agent 生成: {f_rel_path}")

            log(f"Core Agent pre/post condition 文件已写入 ({len(all_core_files)} 个)")

            # === Step 5b-2: 生成端口骨架文件 ===
            # 在写完 Infrastructure Agent 的文件后，调用 ttcn3_compiler -t 生成端口骨架
            # 然后用 Infrastructure Agent 提供的 port_implementation_stub.txt 填充骨架中的实现代码
            log("尝试生成端口骨架文件 (ttcn3_compiler -t)...")
            skeleton_result = generate_port_skeletons(tc_project_dir, port_impl_stub)
            if skeleton_result.get("status") == "success":
                log(f"端口骨架生成成功: {len(skeleton_result.get('files', []))} 个文件")
                # 将生成的骨架文件添加到 written_files
                for sf in skeleton_result.get("files", []):
                    written_files.append(tc_project_dir / sf["path"])
            else:
                log(f"端口骨架生成失败: {skeleton_result.get('message', 'unknown')}", "WARN")
                # 不阻塞流程，使用 examples/ 的默认端口实现作为后备
                _copy_default_port_files(tc_project_dir)

            # 将生成的 TTCN 文件复制到项目目录
            # plan_tc_id already includes TC_ prefix (e.g. "TC_SECC_CMN_VTB_SupportedAppProtocol_001"),
            # use derive_tc_filename() to get consistent filename
            tc_ttcn_dest = tc_project_dir / "src" / "ttcn3" / "4_TestCases" / derive_tc_filename(plan_tc_id)
            tc_ttcn_dest.parent.mkdir(parents=True, exist_ok=True)
            with open(tc_ttcn_dest, "w", encoding="utf-8") as f:
                f.write(ttcn3_code)
            written_files.append(tc_ttcn_dest)
            log(f"TTCN-3 代码已复制到项目: {tc_ttcn_dest}")

            result["project_dir"] = str(tc_project_dir)
            result["infra_files"] = [str(f) for f in written_files]
            result["infra_warnings"] = infra_result.get("warnings", [])
            result["infra_status"] = "success"

            # === Verification: Agent 职责检查（normal mode） ===
            log("\n--- 验证: Core Agent 职责 ---")
            core_ok, core_errors = verify_core_agent_output(core_result, sut_mode=sut_mode)
            result["core_verify_passed"] = core_ok
            result["core_verify_errors"] = core_errors
            if core_ok:
                log("Core Agent 职责验证通过", "INFO")
            else:
                for err in core_errors:
                    log(f"Core Agent 验证失败: {err}", "ERROR")

            log("\n--- 验证: Infrastructure Agent 职责 ---")
            infra_ok, infra_errors = verify_infrastructure_agent_output(infra_result)
            result["infra_verify_passed"] = infra_ok
            result["infra_verify_errors"] = infra_errors
            if infra_ok:
                log("Infrastructure Agent 职责验证通过", "INFO")
            else:
                for err in infra_errors:
                    log(f"Infrastructure Agent 验证失败: {err}", "ERROR")

            # === Step 6: 编译并运行（自动抓包）===
            if not skip_build:
                # 自动抓包：生成 TC 时传入的 pcap_path，或使用项目目录默认路径
                pcap_out = pcap_path
                if not pcap_out:
                    pcap_out = tc_project_dir / "logs" / "capture.pcap"
                build_result = run_build_and_test(
                    tc_dir=tc_project_dir,
                    tc_id=plan_tc_id,
                    sut_ip=sut_ip,
                    pcap_output=pcap_out,
                )
                result["build_result"] = build_result

                # 如果编译失败，提取错误反馈给 Core Agent
                if build_result.get("status") == "build_failed":
                    compile_errors = build_result.get("compile_errors", [])
                    classifier = CompileErrorClassifier()
                    classified = classifier.classify(compile_errors, str(tc_ttcn_dest))
                    build_feedback = classifier.get_summary(classified)

                    if compile_errors and build_result.get("returncode") == 0:
                        # 编译成功但有警告
                        pass
                    else:
                        log(f"编译失败，发现 {len(compile_errors)} 个错误")
                        result["compile_feedback"] = build_feedback
                        result["compile_errors"] = compile_errors

                        # === Step 7: Compile Loop (完整 Agent 链: Core + Infra + Build + FC compile) ===
                        if compile_errors and build_result.get("returncode") != 0:
                            log(f"\n--- Step 7: Compile Loop (Core + Infra + Build + FC compile) ---")
                            new_error_feedback = build_feedback
                            compile_loop_success = False

                            for retry_iter in range(1, MAX_CORRECTION_ITERATIONS + 1):
                                log(f"编译修复迭代 {retry_iter}/{MAX_CORRECTION_ITERATIONS}...")

                                # 1. Core Agent 根据编译错误修复 TTCN-3 代码
                                fixed_core_result = run_core_agent(
                                    generation_plan, references, few_shot_examples, sop, new_error_feedback,
                                    sut_mode=effective_sut_mode, local_ref_section=local_ref_section
                                )
                                if not fixed_core_result or not fixed_core_result.get("testcase"):
                                    log("Core Agent 未生成修复代码", "ERROR")
                                    break
                                fixed_code = fixed_core_result["testcase"]
                                ttcn3_code = fixed_code
                                # 更新 pre/post condition 文件列表
                                pre_condition_files = fixed_core_result.get("precondition_files", [])
                                post_condition_files = fixed_core_result.get("postcondition_files", [])

                                # 2. Infrastructure Agent 重新生成项目骨架（纳入修复后的代码）
                                infra_retry = run_infrastructure_agent(
                                    generation_plan=generation_plan,
                                    ttcn3_code=ttcn3_code,
                                    tc_id=plan_tc_id,
                                    sut_ip=sut_ip,
                                    tc_output_filename=tc_output_filename,
                                )
                                if infra_retry.get("status") != "error":
                                    write_infrastructure_files(tc_project_dir, infra_retry, tc_output_filename)[0]  # 忽略 port_impl_stub

                                # 写入修复后的 TTCN-3 代码
                                with open(output_path, "w", encoding="utf-8") as f:
                                    f.write(ttcn3_code)
                                with open(tc_ttcn_dest, "w", encoding="utf-8") as f:
                                    f.write(ttcn3_code)
                                log(f"修复代码已写入: {output_path}")

                                # 写入修复后的 pre/post condition 文件
                                all_retry_core_files = pre_condition_files + post_condition_files
                                for f_entry in all_retry_core_files:
                                    f_rel_path = f_entry.get("path", "")
                                    f_content = f_entry.get("content", "")
                                    if not f_rel_path or not f_content:
                                        continue
                                    f_path = tc_project_dir / f_rel_path
                                    f_path.parent.mkdir(parents=True, exist_ok=True)
                                    with open(f_path, "w", encoding="utf-8") as fp:
                                        fp.write(f_content)
                                    log(f"  修复 pre/post 文件: {f_rel_path}")

                                # 3. 重新编译
                                retry_result = run_build_and_test(
                                    tc_dir=tc_project_dir,
                                    tc_id=plan_tc_id,
                                    sut_ip=sut_ip,
                                    pcap_output=pcap_out,
                                )
                                retry_errors = retry_result.get("compile_errors", [])

                                if retry_result.get("status") == "completed" or (
                                    retry_result.get("status") == "build_failed" and not retry_errors
                                ):
                                    log("编译成功!")
                                    result["build_result"] = retry_result
                                    result["compile_feedback"] = ""
                                    result["compile_errors"] = []
                                    compile_loop_success = True
                                    break

                                # 4. Fault Corrector (phase="compile") 分析本次编译错误
                                fc_compile_result = run_fault_corrector(
                                    ttcn3_code, generation_plan, sop,
                                    phase="compile", compile_errors=retry_errors
                                )
                                new_error_feedback = build_error_to_feedback(retry_errors)

                                if retry_iter == MAX_CORRECTION_ITERATIONS:
                                    result["compile_feedback"] = new_error_feedback
                                    result["compile_errors"] = retry_errors
                                    break

                # === Step 7b: Fault Corrector (runtime phase: dual-track analysis) ===
                if build_result.get("test_result"):
                    log("\n--- Fault Corrector (runtime: dual-track analysis) ---")
                    test_log = tc_project_dir / "logs" / "titan_traces" / "TestExecution.log"
                    fault_result = run_fault_corrector(
                        ttcn3_code=ttcn3_code,
                        generation_plan=generation_plan,
                        sop=sop,
                        phase="runtime",
                        tc_id=plan_tc_id,
                        log_path=test_log if test_log.exists() else None,
                        pcap_path=pcap_out if pcap_out.exists() else None,
                    )
                    result["fault_result"] = fault_result
                    # 保存到项目目录
                    fault_output_path = tc_project_dir / "fault_analysis.json"
                    with open(fault_output_path, "w", encoding="utf-8") as f:
                        json.dump(fault_result, f, ensure_ascii=False, indent=2)
                    log(f"故障分析已保存: {fault_output_path}")

                    # === Step 7c: 运行时自修复循环 ===
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
        fault_result = run_fault_corrector(
            ttcn3_code=ttcn3_code,
            generation_plan=generation_plan,
            sop=sop,
            phase="runtime",
            tc_id=plan_tc_id,
            log_path=titan_log,
            pcap_path=pcap_path,
        )
        fault_output_path = GENERATED_DIR / f"{plan_tc_id}_fault_analysis.json"
        with open(fault_output_path, "w", encoding="utf-8") as f:
            json.dump(fault_result, f, ensure_ascii=False, indent=2)
        log(f"故障分析结果已保存: {fault_output_path}")
        if "fault_result" not in result:
            result["fault_result"] = fault_result

    log(f"{'=' * 60}")

    return result


# ============================================================
# 主流水线
# ============================================================
def run_pipeline(
    requirement_ids: Optional[List[str]] = None,
    tc_id: Optional[str] = None,
    test_case: Optional[Dict] = None,
    test_case_file: Optional[Path] = None,
    sut_type: Optional[str] = None,
    auto_generate: bool = True,
    multi_mode: bool = True,
    titan_log: Optional[Path] = None,
    pcap_path: Optional[Path] = None,
    with_infrastructure: bool = False,
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
        sut_type: SUT 类型，"SECC" 或 "EVCC"（可选，自动推断）
        auto_generate: 未找到测试用例时是否自动生成
        multi_mode: 多需求模式，True 表示合并为一个测试用例
        with_infrastructure: 是否生成完整项目结构（config + cpp_adapters + build.sh）
        skip_build: 是否跳过编译运行步骤
        sut_ip: SUT IP 地址（用于 titan_runtime.cfg 配置）

    Returns:
        包含生成结果的列表，每个元素包含 input、success 字段，
        以及 ttcn_path、project_dir、infra_status、build_result 等
    """
    results = []

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
            with_infrastructure=with_infrastructure,
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
        loaded_test_case = load_test_case_from_file(Path(test_case_file))
        if loaded_test_case:
            tc = loaded_test_case.get("Test_Case", loaded_test_case)
            requirement_ids = tc.get("Document_Reference", {}).get("Requirements", [])
            requirement_ids = [r.strip("[]").strip() for r in requirement_ids]
            requirement_context = find_requirement_context(requirement_ids[0]) if requirement_ids else ""

            gen_result = generate_code_for_test_case(
                loaded_test_case, sut_type, requirement_context,
                titan_log=titan_log, pcap_path=pcap_path,
                with_infrastructure=with_infrastructure,
                skip_build=skip_build,
                sut_ip=sut_ip,
            )
            results.append({
                "input": {"type": "test_case_file", "data": str(test_case_file)},
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
                with_infrastructure=with_infrastructure,
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

        # 获取需求上下文
        doc_ref = tc.get("Document_Reference", {})
        reqs = doc_ref.get("Requirements", []) if isinstance(doc_ref, dict) else []
        requirement_context = find_requirement_context(reqs[0].strip("[]").strip()) if reqs else ""

        gen_result = generate_code_for_test_case(
            tc_entry, sut_type, requirement_context,
            titan_log=titan_log, pcap_path=pcap_path,
            with_infrastructure=with_infrastructure,
            skip_build=skip_build,
            sut_ip=sut_ip,
        )
        results.append({
            "input": {"type": "requirements", "data": requirement_ids, "tc_id": tc_id},
            "success": gen_result is not None and gen_result.get("ttcn_path") is not None,
            **(gen_result or {}),
        })

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
                    "  --with-infrastructure [--sut-ip SUT_IP]"
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
    infra_group = parser.add_argument_group("完整项目结构 (Infrastructure Agent)")
    infra_group.add_argument(
        "--with-infrastructure", action="store_true",
        help="生成完整的 TITAN 项目结构（config + cpp_adapters + build.sh）并执行编译运行"
    )
    infra_group.add_argument(
        "--skip-build", action="store_true",
        help="配合 --with-infrastructure 使用：生成项目结构但跳过编译运行"
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
        sut_type=args.sut,
        auto_generate=not args.no_auto_generate,
        multi_mode=(args.mode == "multi"),
        titan_log=Path(args.titan_log) if args.titan_log else None,
        pcap_path=Path(args.pcap) if args.pcap else None,
        with_infrastructure=args.with_infrastructure,
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
    sys.exit(main())
