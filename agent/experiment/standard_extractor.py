#!/usr/bin/env python3
"""
标准参考代码提取模块
从 ISO 15118-4 知识库中提取指定测试用例的完整 TTCN-3 代码，
包括 testcase、test behavior function、templates、precondition functions。

功能特性：
- 支持单个和批量提取
- 自动清理 OCR 错误
- 提取完整的测试用例组件
- 支持多种知识库视图查询
- 输出标准化格式，便于后续对比分析

用法：
    # 单个提取
    python standard_extractor.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001

    # 批量提取（从配置文件）
    python standard_extractor.py --config ./test_cases_config.json --batch

    # 指定输出目录
    python standard_extractor.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001 --output-dir ./output
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# 路径配置
# ============================================================
EXPERIMENT_DIR = Path(__file__).resolve().parent
AGENT_DIR = EXPERIMENT_DIR.parent
DOCUMENT_DIR = AGENT_DIR / "document"
KNOWLEDGE_BASE_PATH = DOCUMENT_DIR / "iso15118_4_ttcn_knowledge_base.json"


# ============================================================
# 日志系统
# ============================================================
def log(msg: str, level: str = "INFO") -> None:
    """统一日志输出"""
    ts = time.strftime("%H:%M:%S")
    prefix = {
        "DEBUG": "[\033[36mDEBUG\033[0m]",
        "INFO": "[\033[32mINFO\033[0m]",
        "WARN": "[\033[33mWARN\033[0m]",
        "ERROR": "[\033[31mERROR\033[0m]",
        "SUCCESS": "[\033[32m✓\033[0m]",
    }.get(level, level)
    print(f"[{ts}] {prefix} [StandardExtractor] {msg}")


def load_json(path: Path) -> Any:
    """加载 JSON 文件"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data: Any, path: Path) -> None:
    """保存 JSON 文件"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_test_case_info(tc_id: str) -> Optional[Dict[str, Any]]:
    """从知识库获取测试用例的完整信息

    Args:
        tc_id: 测试用例 ID

    Returns:
        测试用例信息字典，包含:
        - tc_id: 测试用例 ID
        - test_type: GoodCase/BadCase
        - sut_type: SECC/EVCC
        - message_type: 消息类型
        - requirements: 需求 ID 列表
        - test_objective: 测试目标
        - expected_behavior: 预期行为
        - description: 描述信息
        或者返回 None（未找到）
    """
    if not KNOWLEDGE_BASE_PATH.exists():
        log(f"知识库文件不存在: {KNOWLEDGE_BASE_PATH}", "ERROR")
        return None

    try:
        kb = load_json(KNOWLEDGE_BASE_PATH)
    except Exception as e:
        log(f"加载知识库失败: {e}", "ERROR")
        return None

    # 解析 TC ID
    tc_info = TCIDParser.parse(tc_id)
    if not tc_info.get("valid"):
        log(f"无效的 TC ID: {tc_id}", "WARN")
        return None

    message_type = tc_info.get("message_type", "")
    sut_type = tc_info.get("sut_type", "SECC")

    # 1. 从 tc_code_associations 查找
    for assoc in kb.get("tc_code_associations", []):
        if assoc.get("tc_id") == tc_id:
            desc = assoc.get("description", {})
            return {
                "tc_id": tc_id,
                "test_type": assoc.get("test_type", "GoodCase"),
                "sut_type": sut_type,
                "message_type": message_type,
                "category": tc_info.get("category", "CMN"),
                "sequence_number": tc_info.get("seq_no", "001"),
                "requirements": desc.get("referenced_requirements", []),
                "test_objective": desc.get("test_objective", ""),
                "expected_behavior": desc.get("expected_behavior", ""),
                "document_reference": desc.get("document_reference", ""),
                "precondition": desc.get("precondition", ""),
                "testcase_code": assoc.get("testcase_code"),
                "test_behavior_function": assoc.get("test_behavior_function"),
                "referenced_templates": assoc.get("referenced_templates", []),
                "referenced_functions": assoc.get("referenced_functions", []),
            }

    # 2. 从 by_message_type 的 descriptions 查找
    by_msg = kb.get("by_message_type", {})
    msg_view = by_msg.get(message_type, {})

    # 尝试模糊匹配
    if not msg_view:
        for key in by_msg:
            if message_type.lower() in key.lower():
                msg_view = by_msg[key]
                break

    for desc in msg_view.get("descriptions", []):
        if desc.get("tc_id") == tc_id:
            return {
                "tc_id": tc_id,
                "test_type": "GoodCase",  # 默认值
                "sut_type": sut_type,
                "message_type": message_type,
                "category": tc_info.get("category", "CMN"),
                "sequence_number": tc_info.get("seq_no", "001"),
                "requirements": desc.get("referenced_requirements", []),
                "test_objective": desc.get("test_objective", ""),
                "expected_behavior": desc.get("expected_behavior", ""),
                "document_reference": desc.get("document_reference", ""),
                "precondition": desc.get("precondition", ""),
                "testcase_code": None,
                "test_behavior_function": None,
                "referenced_templates": [],
                "referenced_functions": [],
            }

    # 3. 从 test_case_descriptions_by_message 查找
    tc_descs = kb.get("test_case_descriptions_by_message", {})
    for msg_key, tcs in tc_descs.items():
        for tc_desc in tcs:
            if tc_desc.get("tc_id") == tc_id:
                return {
                    "tc_id": tc_id,
                    "test_type": "GoodCase",
                    "sut_type": sut_type,
                    "message_type": message_type,
                    "category": tc_info.get("category", "CMN"),
                    "sequence_number": tc_info.get("seq_no", "001"),
                    "requirements": tc_desc.get("referenced_requirements", []),
                    "test_objective": tc_desc.get("test_objective", ""),
                    "expected_behavior": tc_desc.get("expected_behavior", ""),
                    "document_reference": tc_desc.get("document_reference", ""),
                    "precondition": tc_desc.get("precondition", ""),
                    "testcase_code": None,
                    "test_behavior_function": None,
                    "referenced_templates": [],
                    "referenced_functions": [],
                }

    return None


# ============================================================
# OCR 清理工具
# ============================================================
class OCNCleaner:
    """清理 OCR 产生的常见错误。

    支持从外部 JSON 文件加载模式（ocn_cleaner_patterns.json），
    并在发现新模式时自动学习追加。
    """

    # 外部模式文件路径
    PATTERNS_FILE = Path(__file__).parent / "ocn_cleaner_patterns.json"

    # 内置回退模式（当 JSON 文件不存在时使用）
    BUILTIN_REPLACEMENTS = [
        (r"v HAL_61851 Listener", "v_HAL_61851_Listener"),
        (r"v HAL_61851_Listener", "v_HAL_61851_Listener"),
        (r"v_HAL_61851 Listener", "v_HAL_61851_Listener"),
        (r"HAL_61851 Listener", "v_HAL_61851_Listener"),
        (r"tc_V2G_EVCC Msg_Timer", "tc_V2G_EVCC_Msg_Timer"),
        (r"tc_V2G_SECC Msg_Timer", "tc_V2G_SECC_Msg_Timer"),
        (r"par_V2G_EVCC Msg_Timeout", "par_V2G_EVCC_Msg_Timeout"),
        (r"par_V2G_SECC Msg_Timeout", "par_V2G_SECC_Msg_Timeout"),
        (r"pt_V2G\.tcpTLS_ALM_SECC_Port", "pt_V2G_TCP_TLS_ALM_SECC_Port"),
        (r"pt_V2G\.tcpTLS_ALM_EVCC_Port", "pt_V2G_TCP_TLS_ALM_EVCC_Port"),
        (r"SECC Tester", "SECCTester"),
        (r"EVCC Tester", "EVCCTester"),
        (r"SECCTester ", "SECCTester "),
        (r"EVCCTester ", "EVCCTester "),
        (r"\[ \]", "[]"),
        (r"\$\\equiv\$", "=="),
        (r"\$= =\$", "=="),
        (r"＝", "="),
        (r"）", ")"),
        (r"（", "("),
        (r"f_SECC_CMN_PO_IInitialState", "f_SECC_CMN_PO_InitialState"),
        (r"f_EVCC_CMN_PO_IInitialState", "f_EVCC_CMN_PO_InitialState"),
    ]

    # 类级别的缓存
    _replacements_cache: Optional[List] = None
    _last_loaded_mtime: Optional[float] = None

    @classmethod
    def _load_patterns(cls) -> List:
        """从外部 JSON 文件加载模式（带缓存和自动重载）"""
        import os as _os

        # 检查缓存是否有效（文件未修改）
        if cls._replacements_cache is not None and cls.PATTERNS_FILE.exists():
            try:
                mtime = cls.PATTERNS_FILE.stat().st_mtime
                if mtime == cls._last_loaded_mtime:
                    return cls._replacements_cache
            except OSError:
                pass

        patterns = []

        # 尝试从 JSON 文件加载
        if cls.PATTERNS_FILE.exists():
            try:
                with open(cls.PATTERNS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for entry in data.get("patterns", []):
                    pat = entry.get("pattern", "")
                    rep = entry.get("replacement", "")
                    if pat and rep:
                        patterns.append((pat, rep))
                # 更新缓存
                cls._replacements_cache = patterns
                try:
                    cls._last_loaded_mtime = cls.PATTERNS_FILE.stat().st_mtime
                except OSError:
                    pass
                return patterns
            except Exception:
                pass

        # 回退到内置模式
        cls._replacements_cache = cls.BUILTIN_REPLACEMENTS
        cls._last_loaded_mtime = None
        return cls.BUILTIN_REPLACEMENTS

    @classmethod
    def _get_replacements(cls) -> List:
        """获取替换模式列表（公开接口）"""
        return cls._load_patterns()

    @classmethod
    def clean(cls, code: str) -> str:
        """清理 OCR 错误"""
        if not code:
            return code

        result = code
        for pattern, replacement in cls._get_replacements():
            result = re.sub(pattern, replacement, result)

        # 移除行首尾多余空白，合并连续空行
        lines = result.split('\n')
        cleaned_lines = []
        prev_empty = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if not prev_empty:
                    cleaned_lines.append("")
                    prev_empty = True
            else:
                cleaned_lines.append(stripped)
                prev_empty = False
        return '\n'.join(cleaned_lines)

    @classmethod
    def learn_new_pattern(cls, bad_code: str, good_code: str, category: str = "auto_learned") -> bool:
        """从代码对比中学习新的 OCR 模式并追加到模式文件。

        当发现代码对比中存在不在现有模式中的命名差异时，调用此方法自动学习。

        Args:
            bad_code: 错误的代码片段
            good_code: 正确的代码片段
            category: 模式类别

        Returns:
            是否成功追加
        """
        if not cls.PATTERNS_FILE.exists():
            return False
        try:
            with open(cls.PATTERNS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 检查是否已存在相同模式
            for entry in data.get("patterns", []):
                if entry.get("pattern") == bad_code:
                    return False

            # 追加新模式
            new_entry = {
                "pattern": bad_code,
                "replacement": good_code,
                "category": category,
                "description": f"Auto-learned pattern (confidence: 1.0)",
                "_learned_from": "code_comparator",
                "_learned_at": datetime.now().isoformat(),
            }
            data["patterns"].append(new_entry)
            data["_last_updated"] = datetime.now().isoformat()

            with open(cls.PATTERNS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            # 清除缓存以重新加载
            cls._replacements_cache = None
            cls._last_loaded_mtime = None
            return True
        except Exception:
            return False

    @classmethod
    def detect_issues(cls, code: str) -> List:
        """检测代码中可能的 OCR 问题"""
        issues = []

        space_patterns = [
            (r"[a-z][A-Z]", "可能的缺少空格"),
            (r"[a-z]_[A-Z]", "下划线后可能缺少空格"),
        ]

        for pattern, desc in space_patterns:
            for match in re.finditer(pattern, code):
                issues.append({
                    "type": "suspect_space",
                    "description": desc,
                    "match": match.group(),
                    "position": match.start(),
                })

        return issues

    @classmethod
    def detect_issues(cls, code: str) -> List:
        """检测代码中可能的 OCR 问题"""
        issues = []

        space_patterns = [
            (r"[a-z][A-Z]", "可能的缺少空格"),
            (r"[a-z]_[A-Z]", "下划线后可能缺少空格"),
        ]

        for pattern, desc in space_patterns:
            for match in re.finditer(pattern, code):
                issues.append({
                    "type": "suspect_space",
                    "description": desc,
                    "match": match.group(),
                    "position": match.start(),
                })

        if code.count('{') != code.count('}'):
            issues.append({
                "type": "unbalanced_braces",
                "description": f"花括号不匹配: {{ {code.count('{')} vs }} {code.count('}')}",
            })

        if code.count('(') != code.count(')'):
            issues.append({
                "type": "unbalanced_parens",
                "description": f"圆括号不匹配: ( {code.count('(')} vs ) {code.count(')')}",
            })

        return issues


# ============================================================
# TC ID 解析器
# ============================================================
class TCIDParser:
    """解析测试用例 ID，支持标准和非标准格式"""

    # 标准 TC ID 格式: TC_{SUT}_{Category}_{VTB|Type}_{MessageType}_{SeqNo}
    PATTERN = re.compile(
        r"TC_(SECC|EVCC)_"
        r"(CMN|AC|DC)_"
        r"(VTB_|VT_|TB_)?"
        r"(.+?)_"
        r"(\d{3})"
    )

    # 变体格式 1: TC_V2GTP_BR_MessageType_NNN
    VARIANT_V2GTP_BR = re.compile(r"TC_V2GTP_BR_(\w+)_(\d+)")
    # 变体格式 2: TC_SECC_MessageType_NNN (缺少 Category/VTB 前缀)
    VARIANT_SHORT = re.compile(r"TC_(SECC|EVCC)_(\w+)_(\d+)")

    @classmethod
    def parse(cls, tc_id: str) -> Dict[str, str]:
        """解析 TC ID，支持标准格式和常见变体格式"""
        # 尝试标准格式
        match = cls.PATTERN.match(tc_id)
        if match:
            return {
                "tc_id": tc_id,
                "sut_type": match.group(1),
                "category": match.group(2),
                "prefix": match.group(3) or "VTB",
                "message_type": match.group(4),
                "seq_no": match.group(5),
                "valid": True,
            }

        # 尝试变体格式 1: TC_V2GTP_BR_*
        match = cls.VARIANT_V2GTP_BR.match(tc_id)
        if match:
            msg_type = match.group(1)
            seq = match.group(2)
            sut = "EVCC" if "EVCC" in msg_type.upper() else "SECC"
            return {
                "tc_id": tc_id,
                "original": tc_id,
                "normalized": f"TC_{sut}_CMN_VTB_{msg_type}_{seq}",
                "sut_type": sut,
                "category": "CMN",
                "prefix": "VTB",
                "message_type": msg_type,
                "seq_no": seq,
                "valid": False,  # 非标准格式，但可以处理
                "note": "V2GTP_BR variant normalized",
            }

        # 尝试变体格式 2: TC_SECC_Message_NNN
        match = cls.VARIANT_SHORT.match(tc_id)
        if match:
            sut = match.group(1)
            msg_type = match.group(2)
            seq = match.group(3)
            return {
                "tc_id": tc_id,
                "original": tc_id,
                "normalized": f"TC_{sut}_CMN_VTB_{msg_type}_{seq}",
                "sut_type": sut,
                "category": "CMN",
                "prefix": "VTB",
                "message_type": msg_type,
                "seq_no": seq,
                "valid": False,
                "note": "Short variant normalized",
            }

        # 完全无法解析
        return {
            "tc_id": tc_id,
            "sut_type": "SECC",
            "category": "CMN",
            "prefix": "VTB",
            "message_type": tc_id,
            "seq_no": "001",
            "valid": False,
        }

    @classmethod
    def normalize(cls, tc_id: str) -> str:
        """将任意格式的 TC ID 规范化为标准格式"""
        info = cls.parse(tc_id)
        if info.get("normalized"):
            return info["normalized"]
        if info.get("valid"):
            return tc_id
        # 无法规范化
        return tc_id

    @classmethod
    def derive_test_behavior_name(cls, tc_info: Dict[str, str]) -> str:
        """推导测试行为函数名称"""
        sut = tc_info.get('sut_type', 'SECC')
        category = tc_info.get('category', 'CMN')
        prefix = tc_info.get('prefix', 'VTB')
        msg_type = tc_info.get('message_type', '')
        seq_no = tc_info.get('seq_no', '001')
        if not tc_info.get("valid") and not tc_info.get("normalized"):
            return f"f_{sut}_{category}_TB_{prefix}{msg_type}_{seq_no}"
        return f"f_{sut}_{category}_TB_{prefix}{msg_type}_{seq_no}"


# ============================================================
# 知识库加载器
# ============================================================
class KnowledgeBaseLoader:
    """知识库加载和查询"""

    def __init__(self, kb_path: Path = KNOWLEDGE_BASE_PATH):
        self.kb_path = kb_path
        self._cache = None

    def load(self) -> Dict[str, Any]:
        """加载知识库"""
        if self._cache is not None:
            return self._cache

        log(f"加载知识库: {self.kb_path}", "INFO")
        start_time = time.time()

        with open(self.kb_path, "r", encoding="utf-8") as f:
            self._cache = json.load(f)

        elapsed = time.time() - start_time
        log(f"知识库加载完成，耗时 {elapsed:.2f}秒", "INFO")
        return self._cache

    def find_by_message_type(self, message_type: str, sut_type: str = "SECC") -> Dict[str, Any]:
        """从 by_message_type 视图查找"""
        kb = self.load()
        by_msg = kb.get("by_message_type", {})

        msg_view = by_msg.get(message_type, {})

        if not msg_view:
            for key in by_msg:
                if message_type.lower() in key.lower():
                    msg_view = by_msg[key]
                    break

        return msg_view

    def find_by_tc_id(self, tc_id: str) -> Optional[Dict[str, Any]]:
        """根据 TC ID 查找"""
        kb = self.load()

        for assoc in kb.get("tc_code_associations", []):
            if assoc.get("tc_id") == tc_id:
                return assoc

        return None


# ============================================================
# 标准参考提取器
# ============================================================
class StandardReferenceExtractor:
    """标准参考代码提取器"""

    def __init__(self, kb_loader: KnowledgeBaseLoader):
        self.kb_loader = kb_loader
        self.cleaner = OCNCleaner()

    def extract(self, tc_id: str) -> Dict[str, Any]:
        """提取指定 TC 的标准参考代码"""
        log(f"\n{'=' * 60}")
        log(f"提取标准参考代码: {tc_id}")
        log(f"{'=' * 60}", "INFO")

        tc_info = TCIDParser.parse(tc_id)
        log(f"解析结果: SUT={tc_info['sut_type']}, "
            f"Category={tc_info['category']}, "
            f"MessageType={tc_info['message_type']}, "
            f"SeqNo={tc_info['seq_no']}", "INFO")

        kb = self.kb_loader.load()
        message_type = tc_info['message_type']
        sut_type = tc_info['sut_type']

        result = {
            "tc_id": tc_id,
            "tc_info": tc_info,
            "components": [],
            "reference_info": None,
            "metadata": {
                "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "knowledge_base": str(self.kb_loader.kb_path),
                "ocr_issues": [],
            }
        }

        # 1. 提取 testcase
        testcase = self._extract_testcase(kb, tc_id, sut_type)
        if testcase:
            result["components"].append(testcase)
            log(f"  ✓ 提取 testcase: {testcase['name']}", "SUCCESS")
        else:
            log(f"  ✗ 未找到 testcase", "WARN")

        # 2. 提取 test behavior function
        tb_func = self._extract_test_behavior_function(kb, tc_info)
        if tb_func:
            result["components"].append(tb_func)
            log(f"  ✓ 提取 test behavior function: {tb_func['name']}", "SUCCESS")
        else:
            log(f"  ✗ 未找到 test behavior function", "WARN")

        # 3. 提取 templates
        templates = self._extract_templates(kb, message_type, sut_type)
        result["components"].extend(templates)
        log(f"  ✓ 提取 {len(templates)} 个 templates", "SUCCESS")

        # 4. 提取 precondition functions
        pre_conditions = self._extract_precondition_functions(kb, tc_id, sut_type)
        result["components"].extend(pre_conditions)
        log(f"  ✓ 提取 {len(pre_conditions)} 个 precondition functions", "SUCCESS")

        # 5. 提取 postcondition functions
        post_conditions = self._extract_postcondition_functions(kb, tc_id, sut_type)
        result["components"].extend(post_conditions)
        log(f"  ✓ 提取 {len(post_conditions)} 个 postcondition functions", "SUCCESS")

        # 6. 提取 altsteps
        altsteps = self._extract_altsteps(kb, sut_type)
        result["components"].extend(altsteps)
        log(f"  ✓ 提取 {len(altsteps)} 个 altsteps", "SUCCESS")

        # 7. 提取描述信息
        ref_info = self._extract_reference_info(kb, tc_id)
        if ref_info:
            result["reference_info"] = ref_info
            log(f"  ✓ 提取参考信息", "SUCCESS")

        # 8. 检测 OCR 问题
        for comp in result["components"]:
            if comp.get("code"):
                issues = self.cleaner.detect_issues(comp["code"])
                if issues:
                    result["metadata"]["ocr_issues"].append({
                        "component": comp["name"],
                        "issues": issues
                    })

        log(f"\n总计提取 {len(result['components'])} 个组件", "INFO")
        log(f"检测到 {len(result['metadata']['ocr_issues'])} 个可能的 OCR 问题", "INFO")

        return result

    def _extract_testcase(self, kb: Dict, tc_id: str, sut_type: str) -> Optional[Dict]:
        """提取 testcase"""
        code_by_type = kb.get("ttcn_code_by_type", {})

        for tc in code_by_type.get("testcase", []):
            if tc.get("name") == tc_id:
                return {
                    "name": tc_id,
                    "code": self.cleaner.clean(tc.get("code", "")),
                    "component_type": "testcase",
                    "source": tc.get("source", "knowledge_base"),
                    "related_tc_id": tc_id,
                    "cleaned": True,
                }

        msg_view = kb.get("by_message_type", {})
        for msg_key, msg_data in msg_view.items():
            for tc in msg_data.get("testcases", []):
                if tc.get("name") == tc_id:
                    return {
                        "name": tc_id,
                        "code": self.cleaner.clean(tc.get("code", "")),
                        "component_type": "testcase",
                        "source": tc.get("source", "knowledge_base"),
                        "related_tc_id": tc_id,
                        "cleaned": True,
                    }

        return None

    def _extract_test_behavior_function(self, kb: Dict, tc_info: Dict) -> Optional[Dict]:
        """提取 test behavior function"""
        func_name = TCIDParser.derive_test_behavior_name(tc_info)
        code_by_type = kb.get("ttcn_code_by_type", {})

        for func in code_by_type.get("function", []):
            if func.get("name") == func_name:
                return {
                    "name": func_name,
                    "code": self.cleaner.clean(func.get("code", "")),
                    "component_type": "test_behavior_function",
                    "source": func.get("source", "knowledge_base"),
                    "related_tc_id": tc_info["tc_id"],
                    "cleaned": True,
                }

        msg_view = kb.get("by_message_type", {})
        for msg_key, msg_data in msg_view.items():
            for func in msg_data.get("test_behavior_functions", []):
                if func.get("name") == func_name:
                    return {
                        "name": func_name,
                        "code": self.cleaner.clean(func.get("code", "")),
                        "component_type": "test_behavior_function",
                        "source": func.get("source", "knowledge_base"),
                        "related_tc_id": tc_info["tc_id"],
                        "cleaned": True,
                    }

        return None

    def _extract_templates(self, kb: Dict, message_type: str, sut_type: str) -> List[Dict]:
        """提取 templates"""
        templates = []

        code_by_type = kb.get("ttcn_code_by_type", {})
        msg_view = kb.get("by_message_type", {})

        # 从 code_by_type 查找
        for tmpl in code_by_type.get("template", []):
            name = tmpl.get("name", "")
            if message_type.lower() in name.lower():
                templates.append({
                    "name": name,
                    "code": self.cleaner.clean(tmpl.get("code", "")),
                    "component_type": "template",
                    "source": tmpl.get("source", "knowledge_base"),
                    "cleaned": True,
                })

        # 从 by_message_type 查找
        for msg_key, msg_data in msg_view.items():
            if message_type.lower() in msg_key.lower():
                for tmpl in msg_data.get("send_templates", []):
                    name = tmpl.get("name", "")
                    if not any(t["name"] == name for t in templates):
                        templates.append({
                            "name": name,
                            "code": self.cleaner.clean(tmpl.get("code", "")),
                            "component_type": "send_template",
                            "source": "by_message_type",
                            "cleaned": True,
                        })

                for tmpl in msg_data.get("receive_templates", []):
                    name = tmpl.get("name", "")
                    if not any(t["name"] == name for t in templates):
                        templates.append({
                            "name": name,
                            "code": self.cleaner.clean(tmpl.get("code", "")),
                            "component_type": "receive_template",
                            "source": "by_message_type",
                            "cleaned": True,
                        })

        return templates

    def _extract_precondition_functions(self, kb: Dict, tc_id: str, sut_type: str) -> List[Dict]:
        """提取 precondition functions"""
        functions = []
        code_by_type = kb.get("ttcn_code_by_type", {})

        for func in code_by_type.get("function", []):
            name = func.get("name", "")
            if f"f_{sut_type}_CMN_PR_" in name:
                functions.append({
                    "name": name,
                    "code": self.cleaner.clean(func.get("code", "")),
                    "component_type": "precondition_function",
                    "source": func.get("source", "knowledge_base"),
                    "cleaned": True,
                })

        return functions[:10]

    def _extract_postcondition_functions(self, kb: Dict, tc_id: str, sut_type: str) -> List[Dict]:
        """提取 postcondition functions"""
        functions = []
        code_by_type = kb.get("ttcn_code_by_type", {})

        for func in code_by_type.get("function", []):
            name = func.get("name", "")
            if f"f_{sut_type}_CMN_PO_" in name:
                functions.append({
                    "name": name,
                    "code": self.cleaner.clean(func.get("code", "")),
                    "component_type": "postcondition_function",
                    "source": func.get("source", "knowledge_base"),
                    "cleaned": True,
                })

        return functions[:10]

    def _extract_altsteps(self, kb: Dict, sut_type: str) -> List[Dict]:
        """提取 altsteps"""
        altsteps = []
        code_by_type = kb.get("ttcn_code_by_type", {})

        for alt in code_by_type.get("altstep", []):
            name = alt.get("name", "")
            if sut_type in name:
                altsteps.append({
                    "name": name,
                    "code": self.cleaner.clean(alt.get("code", "")),
                    "component_type": "altstep",
                    "source": alt.get("source", "knowledge_base"),
                    "cleaned": True,
                })

        return altsteps[:5]

    def _extract_reference_info(self, kb: Dict, tc_id: str) -> Optional[Dict]:
        """提取测试用例参考信息"""
        for assoc in kb.get("tc_code_associations", []):
            if assoc.get("tc_id") == tc_id:
                desc = assoc.get("description", {})
                return {
                    "tc_id": tc_id,
                    "test_type": assoc.get("test_type", "GoodCase"),
                    "sut_type": assoc.get("sut_type", "SECC"),
                    "message_type": assoc.get("message_type", ""),
                    "document_reference": desc.get("document_reference", ""),
                    "requirements": desc.get("referenced_requirements", []),
                    "test_objective": desc.get("test_objective", ""),
                    "preconditions": desc.get("precondition", "").split(",") if desc.get("precondition") else [],
                    "expected_behavior": desc.get("expected_behavior", ""),
                    "testcase_code": assoc.get("testcase_code"),
                    "test_behavior_function": assoc.get("test_behavior_function"),
                    "referenced_templates": assoc.get("referenced_templates", []),
                    "referenced_functions": assoc.get("referenced_functions", []),
                }

        return None


# ============================================================
# 输出格式化器
# ============================================================
class ReferenceFormatter:
    """格式化标准参考输出"""

    @staticmethod
    def to_ttcn_format(extracted: Dict[str, Any]) -> str:
        """格式化为 TTCN-3 文件"""
        tc_id = extracted["tc_id"]
        lines = []

        lines.append("/**")
        lines.append(f" * @desc    Standard reference code for {tc_id}")
        lines.append(f" * @source  ISO 15118-4:2018 (OCR-extracted from knowledge base)")
        lines.append(f" * @extracted {extracted['metadata']['extracted_at']}")

        if extracted.get("reference_info"):
            ref = extracted["reference_info"]
            if ref.get("test_objective"):
                lines.append(f" * @objective {ref['test_objective']}")
            if ref.get("requirements"):
                lines.append(f" * @requirements {', '.join(ref['requirements'])}")

        lines.append(" */")
        lines.append("")

        # 按组件类型分组输出
        by_type = {}
        for comp in extracted["components"]:
            ctype = comp.get("component_type", "unknown")
            if ctype not in by_type:
                by_type[ctype] = []
            by_type[ctype].append(comp)

        # 输出 sections
        for section_name, section_types in [
            ("Templates", ["template", "send_template", "receive_template"]),
            ("Test Behavior Functions", ["test_behavior_function"]),
            ("Precondition Functions", ["precondition_function"]),
            ("Postcondition Functions", ["postcondition_function"]),
            ("Altsteps", ["altstep"]),
            ("Testcases", ["testcase"]),
        ]:
            if any(t in by_type for t in section_types):
                lines.append(f"// ============ {section_name} ============")
                for comp in by_type.get(section_types[0], []):
                    lines.append("")
                    lines.append(f"// [{comp.get('component_type', 'unknown')}] {comp['name']}")
                    lines.append(comp.get("code", ""))
                lines.append("")

        return "\n".join(lines)

    @staticmethod
    def to_summary(extracted: Dict[str, Any]) -> str:
        """输出摘要"""
        lines = []

        lines.append(f"\n{'=' * 60}")
        lines.append(f"提取摘要: {extracted['tc_id']}")
        lines.append(f"{'=' * 60}")

        lines.append(f"\n组件统计:")
        by_type = {}
        for comp in extracted["components"]:
            ctype = comp.get("component_type", "unknown")
            by_type[ctype] = by_type.get(ctype, 0) + 1

        for ctype, count in sorted(by_type.items()):
            lines.append(f"  - {ctype}: {count}")

        if extracted.get("reference_info"):
            ref = extracted["reference_info"]
            lines.append(f"\n参考信息:")
            lines.append(f"  - 测试类型: {ref['test_type']}")
            lines.append(f"  - SUT 类型: {ref['sut_type']}")
            lines.append(f"  - 消息类型: {ref['message_type']}")
            if ref.get("requirements"):
                lines.append(f"  - 需求: {', '.join(ref['requirements'][:5])}...")

        if extracted["metadata"].get("ocr_issues"):
            lines.append(f"\nOCR 问题警告:")
            for issue in extracted["metadata"]["ocr_issues"]:
                lines.append(f"  - {issue['component']}: {len(issue['issues'])} 个问题")

        return "\n".join(lines)


# ============================================================
# 保存器
# ============================================================
class ReferenceSaver:
    """保存提取的标准参考"""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def save(self, extracted: Dict[str, Any]) -> Dict[str, Path]:
        """保存提取结果"""
        tc_id = extracted["tc_id"]
        paths = {}

        # 1. 保存格式化的 TTCN-3 文件
        ttcn_content = ReferenceFormatter.to_ttcn_format(extracted)
        ttcn_path = self.output_dir / f"standard_reference_{tc_id}.ttcn"
        with open(ttcn_path, "w", encoding="utf-8") as f:
            f.write(ttcn_content)
        paths["ttcn"] = ttcn_path
        log(f"  保存 TTCN 文件: {ttcn_path}", "SUCCESS")

        # 2. 保存完整的 JSON 数据
        json_path = self.output_dir / f"standard_reference_{tc_id}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(extracted, f, indent=2, ensure_ascii=False)
        paths["json"] = json_path
        log(f"  保存 JSON 文件: {json_path}", "SUCCESS")

        # 3. 保存摘要
        summary_path = self.output_dir / f"standard_reference_{tc_id}_summary.txt"
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(ReferenceFormatter.to_summary(extracted))
        paths["summary"] = summary_path
        log(f"  保存摘要文件: {summary_path}", "SUCCESS")

        return paths


# ============================================================
# 批量提取器
# ============================================================
class BatchExtractor:
    """批量提取测试用例"""

    def __init__(self, kb_loader: KnowledgeBaseLoader, output_dir: Path):
        self.kb_loader = kb_loader
        self.output_dir = output_dir
        self.saver = ReferenceSaver(output_dir)

    def extract_single(self, tc_id: str) -> Dict[str, Any]:
        """提取单个测试用例"""
        extractor = StandardReferenceExtractor(self.kb_loader)
        return extractor.extract(tc_id)

    def extract_batch(self, tc_ids: List[str]) -> List[Dict[str, Any]]:
        """批量提取"""
        results = []

        log(f"\n{'#' * 60}")
        log(f"批量提取: {len(tc_ids)} 个测试用例")
        log(f"{'#' * 60}", "INFO")

        for i, tc_id in enumerate(tc_ids, 1):
            log(f"\n[{i}/{len(tc_ids)}] 处理 {tc_id}...", "INFO")
            try:
                result = self.extract_single(tc_id)
                paths = self.saver.save(result)
                results.append({
                    "tc_id": tc_id,
                    "success": True,
                    "paths": {k: str(v) for k, v in paths.items()},
                    "components_count": len(result["components"]),
                })
            except Exception as e:
                log(f"  ✗ 提取失败: {e}", "ERROR")
                results.append({
                    "tc_id": tc_id,
                    "success": False,
                    "error": str(e),
                })

        # 保存批量提取报告
        self._save_batch_report(results)

        return results

    def _save_batch_report(self, results: List[Dict]):
        """保存批量提取报告"""
        report = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(results),
            "success": sum(1 for r in results if r["success"]),
            "failed": sum(1 for r in results if not r["success"]),
            "results": results,
        }

        report_path = self.output_dir / "batch_extraction_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        log(f"\n批量提取报告已保存: {report_path}", "SUCCESS")
        log(f"成功: {report['success']}/{report['total']}", "INFO")


# ============================================================
# 兼容层：提供与旧版 API 兼容的函数
# ============================================================
def extract_standard_reference(
    tc_id: str,
    use_cache: bool = True,
    overwrite_existing: bool = False,
) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """提取标准参考代码（兼容旧版 API）

    从 ISO 15118-4 知识库中提取指定测试用例的完整标准 TTCN-3 代码，
    包括 testcase、test behavior function、templates、precondition functions。

    Args:
        tc_id: 测试用例 ID
        use_cache: 是否使用缓存
        overwrite_existing: 是否覆盖已有文件

    Returns:
        (success, extracted_data, message)
    """
    log(f"开始提取标准参考代码: {tc_id}", "INFO")

    if not KNOWLEDGE_BASE_PATH.exists():
        log(f"知识库文件不存在: {KNOWLEDGE_BASE_PATH}", "ERROR")
        return False, None, f"知识库文件不存在: {KNOWLEDGE_BASE_PATH}"

    start_time = time.time()

    try:
        kb_loader = KnowledgeBaseLoader()
        kb = kb_loader.load()
    except Exception as e:
        log(f"加载知识库失败: {e}", "ERROR")
        return False, None, f"加载知识库失败: {e}"

    # 解析 TC ID
    tc_info = TCIDParser.parse(tc_id)
    message_type = tc_info.get("message_type", "")
    sut_type = tc_info.get("sut_type", "SECC")
    seq_no = tc_info.get("seq_no", "001")

    result = {
        "tc_id": tc_id,
        "tc_info": tc_info,
        "testcase": None,
        "test_behavior_function": None,
        "templates": [],
        "precondition_functions": [],
        "postcondition_functions": [],
        "altsteps": [],
        "description": None,
        "association": None,
        "formatted_ttcn": None,
    }

    cleaner = OCNCleaner()

    # 1. 从 by_message_type 视图查找
    by_msg = kb.get("by_message_type", {})
    msg_view = by_msg.get(message_type, {})

    # 如果精确匹配失败，尝试模糊匹配
    if not msg_view:
        log(f"未找到精确匹配的消息类型 '{message_type}'，尝试模糊匹配...", "WARN")
        for key in by_msg:
            if message_type.lower() in key.lower():
                msg_view = by_msg[key]
                log(f"使用模糊匹配: {key}", "INFO")
                break

    # 2. 提取 testcase
    all_testcases = (
        msg_view.get("testcases", [])
        + kb.get("ttcn_code_by_type", {}).get("testcase", [])
    )
    for tc in all_testcases:
        if tc.get("name") == tc_id:
            result["testcase"] = {
                "name": tc["name"],
                "code": cleaner.clean(tc.get("code", "")),
                "source": tc.get("source", "knowledge_base"),
            }
            break

    # 3. 提取 test behavior function
    tb_func_name = f"f_{sut_type}_{tc_info.get('category', 'CMN')}_TB_VTB_{message_type}_{seq_no}"
    all_functions = (
        msg_view.get("test_behavior_functions", [])
        + msg_view.get("precondition_functions", [])
        + msg_view.get("postcondition_functions", [])
        + kb.get("ttcn_code_by_type", {}).get("function", [])
    )
    for func in all_functions:
        if func.get("name") == tb_func_name:
            result["test_behavior_function"] = {
                "name": func["name"],
                "code": cleaner.clean(func.get("code", "")),
                "source": func.get("source", "knowledge_base"),
            }
            break

    # 4. 提取 templates
    for t_type in ["send_templates", "receive_templates"]:
        for tmpl in msg_view.get(t_type, []):
            result["templates"].append({
                "name": tmpl.get("name", ""),
                "code": cleaner.clean(tmpl.get("code", "")),
                "type": t_type,
            })

    # 从 ttcn_code_by_type 补充模板
    for tmpl in kb.get("ttcn_code_by_type", {}).get("template", []):
        if message_type.lower() in tmpl.get("name", "").lower():
            if not any(t["name"] == tmpl["name"] for t in result["templates"]):
                result["templates"].append({
                    "name": tmpl.get("name", ""),
                    "code": cleaner.clean(tmpl.get("code", "")),
                    "type": "template",
                })

    # 5. 提取 precondition functions
    for func in msg_view.get("precondition_functions", []):
        if sut_type in func.get("name", ""):
            result["precondition_functions"].append({
                "name": func.get("name", ""),
                "code": cleaner.clean(func.get("code", "")),
            })

    # 6. 提取 postcondition functions
    for func in msg_view.get("postcondition_functions", []):
        if sut_type in func.get("name", ""):
            result["postcondition_functions"].append({
                "name": func.get("name", ""),
                "code": cleaner.clean(func.get("code", "")),
            })

    # 7. 提取 altsteps
    for alt in msg_view.get("altsteps", []):
        if sut_type in alt.get("name", ""):
            result["altsteps"].append({
                "name": alt.get("name", ""),
                "code": cleaner.clean(alt.get("code", "")),
            })

    # 8. 提取 description
    for desc in msg_view.get("descriptions", []):
        if desc.get("tc_id") == tc_id:
            result["description"] = desc
            break

    # 9. 从 tc_code_associations 补充关联信息
    for assoc in kb.get("tc_code_associations", []):
        if assoc.get("tc_id") == tc_id:
            result["association"] = assoc
            break

    # 10. 格式化完整的 TTCN-3 代码
    result["formatted_ttcn"] = _format_ttcn_code(result)

    elapsed = time.time() - start_time
    log(f"提取完成，耗时 {elapsed:.1f}s", "INFO")

    # 检查提取结果
    if not result["testcase"] and not result["test_behavior_function"] and not result["templates"]:
        log(f"未能提取到任何代码元素", "WARN")
        return False, result, f"未找到标准代码（耗时 {elapsed:.1f}s）"

    # 统计提取的元素
    elements = {
        "testcase": result["testcase"] is not None,
        "test_behavior": result["test_behavior_function"] is not None,
        "templates": len(result["templates"]),
        "preconditions": len(result["precondition_functions"]),
        "postconditions": len(result["postcondition_functions"]),
        "altsteps": len(result["altsteps"]),
    }
    log(f"提取摘要: {elements}", "INFO")

    return True, result, f"提取成功（耗时 {elapsed:.1f}s）"


def _format_ttcn_code(ref: Dict[str, Any]) -> str:
    """将提取的标准参考代码格式化为完整的 TTCN-3 文件"""
    tc_id = ref["tc_id"]
    tc_info = ref["tc_info"]
    lines = []

    lines.append(f"/**")
    lines.append(f" * @desc    Standard reference code for {tc_id}")
    lines.append(f" * @source  ISO 15118-4:2018 (OCR-extracted from knowledge base)")
    if ref.get("description"):
        desc = ref["description"]
        if desc.get("test_objective"):
            lines.append(f" * @objective {desc['test_objective']}")
        if desc.get("referenced_requirements"):
            lines.append(f" * @requirements {', '.join(desc['referenced_requirements'])}")
    lines.append(f" */")
    lines.append(f"")

    # Templates
    if ref["templates"]:
        lines.append(f"// ============ Templates ============")
        for tmpl in ref["templates"]:
            lines.append(f"")
            lines.append(f"// [{tmpl.get('type', 'template')}] {tmpl['name']}")
            lines.append(tmpl["code"])
        lines.append(f"")

    # Test behavior function
    if ref["test_behavior_function"]:
        lines.append(f"// ============ Test Behavior Function ============")
        lines.append(f"")
        lines.append(ref["test_behavior_function"]["code"])
        lines.append(f"")

    # Precondition functions
    if ref["precondition_functions"]:
        lines.append(f"// ============ Precondition Functions ============")
        for func in ref["precondition_functions"]:
            lines.append(f"")
            lines.append(f"// {func['name']}")
            lines.append(func["code"])
        lines.append(f"")

    # Postcondition functions
    if ref["postcondition_functions"]:
        lines.append(f"// ============ Postcondition Functions ============")
        for func in ref["postcondition_functions"]:
            lines.append(f"")
            lines.append(f"// {func['name']}")
            lines.append(func["code"])
        lines.append(f"")

    # Altsteps
    if ref["altsteps"]:
        lines.append(f"// ============ Altsteps ============")
        for alt in ref["altsteps"]:
            lines.append(f"")
            lines.append(f"// {alt['name']}")
            lines.append(alt["code"])
        lines.append(f"")

    # Testcase
    if ref["testcase"]:
        lines.append(f"// ============ Testcase ============")
        lines.append(f"")
        lines.append(ref["testcase"]["code"])
        lines.append(f"")

    return "\n".join(lines)


def save_standard_reference(
    ref: Dict[str, Any],
    output_dir: Path,
) -> Tuple[Path, Path]:
    """保存标准参考代码

    Args:
        ref: 提取的标准参考数据
        output_dir: 输出目录

    Returns:
        (ttcn_path, json_path)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    tc_id = ref["tc_id"]

    # 保存格式化的 TTCN-3 文件
    ttcn_path = output_dir / f"standard_{tc_id}.ttcn"
    with open(ttcn_path, "w", encoding="utf-8") as f:
        if ref.get("formatted_ttcn"):
            f.write(ref["formatted_ttcn"])
        else:
            f.write(_format_ttcn_code(ref))

    # 保存完整的 JSON 数据
    json_path = output_dir / f"standard_{tc_id}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(ref, f, indent=2, ensure_ascii=False)

    return ttcn_path, json_path


def get_standard_code(tc_id: str, output_dir: Path) -> Optional[str]:
    """获取标准参考代码

    Args:
        tc_id: 测试用例 ID
        output_dir: 结果目录

    Returns:
        标准参考代码内容
    """
    ttcn_path = output_dir / f"standard_{tc_id}.ttcn"
    if not ttcn_path.exists():
        return None

    try:
        return ttcn_path.read_text(encoding="utf-8")
    except Exception:
        return None


# ============================================================
# 主函数
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="标准参考代码提取器 - 从 ISO 15118-4 知识库提取测试用例代码",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 提取单个测试用例
  python standard_extractor.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001

  # 指定输出目录
  python standard_extractor.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001 --output-dir ./output

  # 从配置文件批量提取
  python standard_extractor.py --config ./test_cases_config.json --batch

  # 列出可用测试用例
  python standard_extractor.py --list
        """
    )

    parser.add_argument("--tc-id", type=str, help="测试用例 ID")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="输出目录（默认为 experiment/standard_references）")
    parser.add_argument("--config", type=str, default=None,
                        help="配置文件路径（用于批量提取）")
    parser.add_argument("--batch", action="store_true",
                        help="批量模式（需配合 --config 使用）")
    parser.add_argument("--list", action="store_true",
                        help="列出知识库中的所有测试用例")

    args = parser.parse_args()

    # 确定输出目录
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = EXPERIMENT_DIR / "standard_references"

    # 初始化
    kb_loader = KnowledgeBaseLoader()

    # 列出测试用例
    if args.list:
        kb = kb_loader.load()
        print("\n知识库中的测试用例:")
        for tc in kb.get("ttcn_code_by_type", {}).get("testcase", [])[:50]:
            print(f"  - {tc.get('name')}")
        print(f"\n... (共 {len(kb.get('ttcn_code_by_type', {}).get('testcase', []))} 个测试用例)")
        return

    # 批量模式
    if args.batch and args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            log(f"配置文件不存在: {config_path}", "ERROR")
            sys.exit(1)

        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        tc_ids = []
        for tc in config.get("single_test_cases", []):
            tc_ids.append(tc["tc_id"])

        batch_extractor = BatchExtractor(kb_loader, output_dir)
        batch_extractor.extract_batch(tc_ids)
        return

    # 单个提取
    if args.tc_id:
        extractor = StandardReferenceExtractor(kb_loader)
        result = extractor.extract(args.tc_id)

        saver = ReferenceSaver(output_dir)
        paths = saver.save(result)

        print(ReferenceFormatter.to_summary(result))
        print(f"\n输出文件:")
        for name, path in paths.items():
            print(f"  - {name}: {path}")
        return

    # 无参数
    parser.print_help()


if __name__ == "__main__":
    main()
