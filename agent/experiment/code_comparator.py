#!/usr/bin/env python3
"""
TTCN-3 代码对比分析器
用于对比 Code Agent 生成的代码与 ISO 15118-4 标准参考代码之间的差异。

功能特性：
- 多维度代码对比（结构、语法、语义、命名规范等）
- 差异高亮显示
- 相似度评分计算
- 生成详细对比报告

用法:
    python code_comparator.py --generated ./test_code/generated/TC_XXX.ttcn \
                              --reference ./experiment/standard_references/standard_reference_TC_XXX.ttcn
"""

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# 数据结构定义
# ============================================================
class DifferenceType(Enum):
    """差异类型"""
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    SIMILAR = "similar"
    IDENTICAL = "identical"
    MISSING = "missing"


class Severity(Enum):
    """严重程度"""
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"
    OK = "ok"


@dataclass
class ComponentComparison:
    """组件对比结果"""
    component_name: str
    component_type: str
    exists_in_generated: bool
    exists_in_reference: bool
    similarity_score: float  # 0.0 - 1.0
    difference_type: str
    differences: List[Dict[str, Any]] = field(default_factory=list)
    generated_code: str = ""
    reference_code: str = ""
    line_diff: List[Dict] = field(default_factory=list)


@dataclass
class CodeStructure:
    """代码结构"""
    module_name: str = ""
    imports: List[str] = field(default_factory=list)
    templates: List[str] = field(default_factory=list)
    functions: List[str] = field(default_factory=list)
    testcases: List[str] = field(default_factory=list)
    timers: List[str] = field(default_factory=list)
    altsteps: List[str] = field(default_factory=list)
    port_mappings: List[str] = field(default_factory=list)


@dataclass
class ComparisonResult:
    """完整对比结果"""
    tc_id: str
    generated_path: str
    reference_path: str
    overall_similarity: float
    status: str  # pass, warning, fail
    
    # 结构对比
    generated_structure: CodeStructure = field(default_factory=CodeStructure)
    reference_structure: CodeStructure = field(default_factory=CodeStructure)
    structure_match_score: float = 0.0
    
    # 组件对比
    component_comparisons: List[ComponentComparison] = field(default_factory=list)
    
    # 指标得分
    metrics: Dict[str, float] = field(default_factory=dict)
    
    # 问题列表
    issues: List[Dict[str, Any]] = field(default_factory=list)
    
    # 统计信息
    stats: Dict[str, int] = field(default_factory=dict)
    
    # 详细差异
    differences: Dict[str, Any] = field(default_factory=dict)


# ============================================================
# TTCN-3 语法分析器
# ============================================================
class TTCN3Parser:
    """TTCN-3 代码解析器"""
    
    # 关键语法模式
    PATTERNS = {
        "module": re.compile(r"module\s+(\w+)", re.IGNORECASE),
        "import": re.compile(r"import\s+from\s+([\w_]+)", re.IGNORECASE),
        "template": re.compile(r"template\s+(?:\w+\s+)?(\w+)\s*:?=" , re.IGNORECASE),
        "function": re.compile(r"function\s+(\w+)", re.IGNORECASE),
        "testcase": re.compile(r"testcase\s+(\w+)\s*\(", re.IGNORECASE),
        "timer": re.compile(r"timer\s+(\w+)", re.IGNORECASE),
        "altstep": re.compile(r"altstep\s+(\w+)", re.IGNORECASE),
        "port_mapping": re.compile(r"(?:map|unmap)\s*\(", re.IGNORECASE),
        "port_def": re.compile(r"port\s+(\w+)", re.IGNORECASE),
        "runs_on": re.compile(r"runs\s+on\s+(\w+)", re.IGNORECASE),
        "system": re.compile(r"system\s+(\w+)", re.IGNORECASE),
        "var": re.compile(r"var\s+(\w+)", re.IGNORECASE),
        "const": re.compile(r"const\s+(\w+)", re.IGNORECASE),
    }
    
    @classmethod
    def parse(cls, code: str) -> CodeStructure:
        """解析 TTCN-3 代码结构"""
        structure = CodeStructure()
        
        if not code:
            return structure
        
        lines = code.split('\n')
        
        # 提取 module 名称
        module_match = cls.PATTERNS["module"].search(code)
        if module_match:
            structure.module_name = module_match.group(1)
        
        # 提取 imports
        for match in cls.PATTERNS["import"].finditer(code):
            structure.imports.append(match.group(1))
        
        # 提取 templates
        for match in cls.PATTERNS["template"].finditer(code):
            name = match.group(1)
            if name and not name.startswith("_"):
                structure.templates.append(name)
        
        # 提取 functions
        for match in cls.PATTERNS["function"].finditer(code):
            name = match.group(1)
            if name and not name.startswith("_"):
                structure.functions.append(name)
        
        # 提取 testcases
        for match in cls.PATTERNS["testcase"].finditer(code):
            name = match.group(1)
            if name:
                structure.testcases.append(name)
        
        # 提取 timers
        for match in cls.PATTERNS["timer"].finditer(code):
            name = match.group(1)
            if name:
                structure.timers.append(name)
        
        # 提取 altsteps
        for match in cls.PATTERNS["altstep"].finditer(code):
            name = match.group(1)
            if name:
                structure.altsteps.append(name)
        
        return structure
    
    @classmethod
    def extract_components(cls, code: str) -> Dict[str, str]:
        """提取各个组件的代码"""
        components = {}
        
        if not code:
            return components
        
        # 提取每个组件的代码块
        lines = code.split('\n')
        current_component = None
        current_type = None
        brace_count = 0
        code_lines = []
        
        for line in lines:
            # 检查是否是组件声明
            for comp_type in ["testcase", "function", "template", "altstep"]:
                pattern = cls.PATTERNS[comp_type]
                match = pattern.search(line)
                if match:
                    # 保存上一个组件
                    if current_component and code_lines:
                        components[current_component] = '\n'.join(code_lines)
                    
                    current_component = match.group(1) if match.groups() else f"anon_{comp_type}"
                    current_type = comp_type
                    code_lines = [line]
                    brace_count = line.count('{') - line.count('}')
                    break
            else:
                if current_component:
                    code_lines.append(line)
                    brace_count += line.count('{') - line.count('}')
                    
                    # 如果花括号闭合，保存组件
                    if brace_count == 0 and current_component in components:
                        pass  # 已经存在
                    elif brace_count <= 0:
                        components[current_component] = '\n'.join(code_lines)
                        current_component = None
                        code_lines = []
        
        # 保存最后一个组件
        if current_component and code_lines:
            components[current_component] = '\n'.join(code_lines)
        
        return components
    
    @classmethod
    def check_syntax(cls, code: str) -> List[Dict[str, Any]]:
        """检查 TTCN-3 语法错误"""
        issues = []
        
        if not code:
            return [{"severity": "critical", "issue": "Empty code"}]
        
        # 检查花括号匹配
        open_braces = code.count('{')
        close_braces = code.count('}')
        if open_braces != close_braces:
            issues.append({
                "severity": "critical",
                "type": "unbalanced_braces",
                "issue": f"Unbalanced braces: {{ {open_braces} vs }} {close_braces}",
            })
        
        # 检查圆括号匹配
        open_parens = code.count('(')
        close_parens = code.count(')')
        if open_parens != close_parens:
            issues.append({
                "severity": "critical",
                "type": "unbalanced_parens",
                "issue": f"Unbalanced parentheses: ( {open_parens} vs ) {close_parens}",
            })
        
        # 检查方括号匹配
        open_brackets = code.count('[')
        close_brackets = code.count(']')
        if open_brackets != close_brackets:
            issues.append({
                "severity": "warning",
                "type": "unbalanced_brackets",
                "issue": f"Unbalanced brackets: [ {open_brackets} vs ] {close_brackets}",
            })
        
        # 检查是否有 semicolon 缺失（简单检查）
        lines = code.split('\n')
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # 跳过注释和空行
            if not stripped or stripped.startswith('//') or stripped.startswith('/*') or stripped.startswith('*'):
                continue
            
            # 检查语句是否以分号或开括号结尾
            if stripped.endswith(',') or stripped.endswith('{') or stripped.endswith('}') or stripped.endswith(':'):
                continue
            
            # 简单语句应该以分号结尾
            simple_stmts = ['var ', 'const ', 'if(', 'if (', 'log(', 'setverdict(', 'return ']
            if any(stripped.startswith(s) for s in simple_stmts) and not stripped.endswith(';'):
                # 这可能是一个问题，但需要更复杂的分析
                pass
        
        return issues


# ============================================================
# 命名规范检查器
# ============================================================
class NamingConventionChecker:
    """TTCN-3 命名规范检查器"""
    
    # ISO 15118-4 标准命名规范
    NAMING_RULES = {
        "testcase": {
            "pattern": r"^TC_(SECC|EVCC)_(CMN|AC|DC)_VTB_\w+_\d{3}$",
            "description": "测试用例名称应为 TC_{SUT}_{Category}_VTB_{MessageType}_{SeqNo}",
        },
        "function": {
            "prefix_test_behavior": r"^f_(SECC|EVCC)_(CMN|AC|DC)_TB_VTB_\w+_\d{3}$",
            "prefix_precondition": r"^f_(SECC|EVCC)_(CMN|AC|DC)_PR_\w+$",
            "prefix_postcondition": r"^f_(SECC|EVCC)_(CMN|AC|DC)_PO_\w+$",
            "description": "函数名称应遵循标准前缀规则",
        },
        "template": {
            "pattern": r"^(md|mw|m_|mw_)",
            "send_pattern": r"^md_",
            "receive_pattern": r"^mw_",
            "description": "模板名称应以 md_ (send) 或 mw_ (receive) 开头",
        },
        "timer": {
            "pattern": r"^tc?_",
            "description": "计时器应以 tc_ 或 tc 开头",
        },
    }
    
    @classmethod
    def check(cls, code: str) -> List[Dict[str, Any]]:
        """检查命名规范"""
        issues = []
        
        if not code:
            return [{"severity": "info", "issue": "Empty code"}]
        
        # 检查 testcase 命名
        for match in TTCN3Parser.PATTERNS["testcase"].finditer(code):
            name = match.group(1)
            pattern = cls.NAMING_RULES["testcase"]["pattern"]
            if not re.match(pattern, name):
                issues.append({
                    "severity": "warning",
                    "type": "naming_convention",
                    "component": "testcase",
                    "name": name,
                    "issue": f"测试用例名称 '{name}' 不符合规范",
                    "expected": cls.NAMING_RULES["testcase"]["description"],
                })
        
        # 检查 function 命名
        for match in TTCN3Parser.PATTERNS["function"].finditer(code):
            name = match.group(1)
            is_valid = any(
                re.match(pattern, name)
                for pattern in [
                    cls.NAMING_RULES["function"]["prefix_test_behavior"],
                    cls.NAMING_RULES["function"]["prefix_precondition"],
                    cls.NAMING_RULES["function"]["prefix_postcondition"],
                ]
            )
            if not is_valid and not name.startswith("f_") == False:
                issues.append({
                    "severity": "info",
                    "type": "naming_convention",
                    "component": "function",
                    "name": name,
                    "issue": f"函数名称 '{name}' 可能不符合标准前缀规则",
                })
        
        # 检查 template 命名
        for match in TTCN3Parser.PATTERNS["template"].finditer(code):
            name = match.group(1)
            pattern = cls.NAMING_RULES["template"]["pattern"]
            if not re.match(pattern, name):
                issues.append({
                    "severity": "warning",
                    "type": "naming_convention",
                    "component": "template",
                    "name": name,
                    "issue": f"模板名称 '{name}' 应以 md_ 或 mw_ 开头",
                })
        
        return issues


# ============================================================
# 语义分析器
# ============================================================
class SemanticAnalyzer:
    """语义分析器"""
    
    # 必需组件检查
    REQUIRED_COMPONENTS = {
        "testcase": True,
        "runs_on": True,
        "system": True,
    }
    
    # 推荐组件
    RECOMMENDED_COMPONENTS = {
        "import": True,
        "var_verdicttype": True,
        "precondition": True,
        "postcondition": True,
        "timeout_handling": True,
        "setverdict": True,
    }
    
    @classmethod
    def analyze(cls, code: str) -> Dict[str, Any]:
        """分析代码语义"""
        analysis = {
            "has_testcase": False,
            "has_runs_on": False,
            "has_system": False,
            "has_imports": False,
            "has_var_verdicttype": False,
            "has_precondition": False,
            "has_postcondition": False,
            "has_timeout": False,
            "has_setverdict": False,
            "has_alt": False,
            "missing_components": [],
            "recommendations": [],
        }
        
        if not code:
            analysis["missing_components"].append("Empty code")
            return analysis
        
        # 检查必需组件
        analysis["has_testcase"] = bool(TTCN3Parser.PATTERNS["testcase"].search(code))
        analysis["has_runs_on"] = bool(TTCN3Parser.PATTERNS["runs_on"].search(code))
        analysis["has_system"] = bool(TTCN3Parser.PATTERNS["system"].search(code))
        analysis["has_imports"] = bool(TTCN3Parser.PATTERNS["import"].search(code))
        analysis["has_var_verdicttype"] = "var verdicttype" in code.lower()
        analysis["has_precondition"] = "precondition" in code.lower() or "PR_" in code
        analysis["has_postcondition"] = "postcondition" in code.lower() or "PO_" in code
        analysis["has_timeout"] = bool(re.search(r"\.start\(|\.stop\(|timer\s+", code, re.IGNORECASE))
        analysis["has_setverdict"] = bool(re.search(r"setverdict\s*\(", code, re.IGNORECASE))
        analysis["has_alt"] = bool(re.search(r"\balt\s*\{", code, re.IGNORECASE))
        
        # 检查缺失组件
        if not analysis["has_testcase"]:
            analysis["missing_components"].append("testcase")
        if not analysis["has_runs_on"]:
            analysis["missing_components"].append("runs on clause")
        if not analysis["has_system"]:
            analysis["missing_components"].append("system clause")
        
        # 推荐组件
        if not analysis["has_imports"]:
            analysis["recommendations"].append("建议添加必要的 import 语句")
        if not analysis["has_var_verdicttype"]:
            analysis["recommendations"].append("建议添加 var verdicttype 变量")
        if not analysis["has_setverdict"]:
            analysis["missing_components"].append("setverdict")
        
        return analysis


# ============================================================
# 代码对比器
# ============================================================
class CodeComparator:
    """代码对比器"""
    
    def __init__(self, ignore_whitespace: bool = True, ignore_comments: bool = True):
        self.ignore_whitespace = ignore_whitespace
        self.ignore_comments = ignore_comments
    
    def compare(self, generated: str, reference: str) -> Dict[str, Any]:
        """对比两段代码"""
        result = {
            "similarity_score": 0.0,
            "line_diff": [],
            "summary": {},
        }
        
        if not generated and not reference:
            result["similarity_score"] = 1.0
            return result
        
        if not generated:
            result["similarity_score"] = 0.0
            result["summary"]["status"] = "missing_generated"
            return result
        
        if not reference:
            result["similarity_score"] = 0.0
            result["summary"]["status"] = "missing_reference"
            return result
        
        # 预处理
        gen_lines = self._preprocess(generated)
        ref_lines = self._preprocess(reference)
        
        # 计算相似度
        result["similarity_score"] = difflib.SequenceMatcher(None, gen_lines, ref_lines).ratio()
        
        # 生成行级差异
        matcher = difflib.SequenceMatcher(None, gen_lines, ref_lines)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != 'equal':
                result["line_diff"].append({
                    "type": tag,
                    "generated_range": (i1, i2),
                    "reference_range": (j1, j2),
                    "generated_lines": gen_lines[i1:i2] if i2 > i1 else [],
                    "reference_lines": ref_lines[j1:j2] if j2 > j1 else [],
                })
        
        # 总结
        result["summary"] = {
            "status": "compared",
            "generated_lines": len(gen_lines),
            "reference_lines": len(ref_lines),
            "added_lines": sum(1 for d in result["line_diff"] if d["type"] == "insert"),
            "removed_lines": sum(1 for d in result["line_diff"] if d["type"] == "delete"),
            "modified_lines": sum(1 for d in result["line_diff"] if d["type"] == "replace"),
        }
        
        return result
    
    def compare_structures(self, gen_struct: CodeStructure, ref_struct: CodeStructure) -> Dict[str, Any]:
        """对比代码结构"""
        result = {
            "module_match": gen_struct.module_name == ref_struct.module_name,
            "imports_match": self._list_similarity(gen_struct.imports, ref_struct.imports),
            "templates_match": self._list_similarity(gen_struct.templates, ref_struct.templates),
            "functions_match": self._list_similarity(gen_struct.functions, ref_struct.functions),
            "testcases_match": self._list_similarity(gen_struct.testcases, ref_struct.testcases),
            "overall_score": 0.0,
            "missing_in_generated": {
                "templates": list(set(ref_struct.templates) - set(gen_struct.templates)),
                "functions": list(set(ref_struct.functions) - set(gen_struct.functions)),
                "testcases": list(set(ref_struct.testcases) - set(gen_struct.testcases)),
            },
            "extra_in_generated": {
                "templates": list(set(gen_struct.templates) - set(ref_struct.templates)),
                "functions": list(set(gen_struct.functions) - set(ref_struct.functions)),
                "testcases": list(set(gen_struct.testcases) - set(ref_struct.testcases)),
            },
        }
        
        # 计算总体结构匹配分数
        scores = [
            1.0 if result["module_match"] else 0.0,
            result["imports_match"],
            result["templates_match"],
            result["functions_match"],
            result["testcases_match"],
        ]
        result["overall_score"] = sum(scores) / len(scores)
        
        return result
    
    def _preprocess(self, code: str) -> List[str]:
        """预处理代码"""
        lines = code.split('\n')
        processed = []
        
        for line in lines:
            # 移除注释
            if self.ignore_comments:
                line = re.sub(r"//.*$", "", line)
                line = re.sub(r"/\*.*?\*/", "", line)
            
            # 移除多余空白
            if self.ignore_whitespace:
                line = line.strip()
            
            if line:
                processed.append(line)
        
        return processed
    
    def _list_similarity(self, list1: List[str], list2: List[str]) -> float:
        """计算列表相似度"""
        if not list1 and not list2:
            return 1.0
        
        if not list1 or not list2:
            return 0.0
        
        set1 = set(list1)
        set2 = set(list2)
        
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        
        return intersection / union if union > 0 else 0.0
    
    def generate_diff_html(self, generated: str, reference: str) -> str:
        """生成 HTML 格式的差异报告"""
        gen_lines = generated.split('\n')
        ref_lines = reference.split('\n')
        
        html_lines = [
            '<!DOCTYPE html>',
            '<html>',
            '<head>',
            '<style>',
            'body { font-family: monospace; }',
            '.added { background-color: #90EE90; }',
            '.removed { background-color: #FFB6C1; }',
            '.modified { background-color: #FFFF00; }',
            '.line-num { color: gray; }',
            '</style>',
            '</head>',
            '<body>',
            '<h1>Code Comparison Report</h1>',
        ]
        
        matcher = difflib.HtmlDiff()
        html_diff = matcher.make_table(gen_lines, ref_lines, fromdesc='Generated', todesc='Reference', context=True)
        html_lines.append(html_diff)
        
        html_lines.extend(['</body>', '</html>'])
        
        return '\n'.join(html_lines)


# ============================================================
# 主对比分析器
# ============================================================
class ComparisonAnalyzer:
    """完整对比分析器"""
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.ignore_whitespace = self.config.get("ignore_whitespace", True)
        self.ignore_comments = self.config.get("ignore_comments", True)
        self.normalized_template_fields = self.config.get("normalize_template_fields", True)
        
        self.comparator = CodeComparator(
            ignore_whitespace=self.ignore_whitespace,
            ignore_comments=self.ignore_comments
        )
    
    def analyze(self, tc_id: str, generated_path: Path, reference_path: Path) -> ComparisonResult:
        """完整分析"""
        # 读取代码
        with open(generated_path, "r", encoding="utf-8") as f:
            generated_code = f.read()
        
        with open(reference_path, "r", encoding="utf-8") as f:
            reference_code = f.read()
        
        # 初始化结果
        result = ComparisonResult(
            tc_id=tc_id,
            generated_path=str(generated_path),
            reference_path=str(reference_path),
            overall_similarity=0.0,
            status="pending",
        )
        
        # 1. 解析代码结构
        result.generated_structure = TTCN3Parser.parse(generated_code)
        result.reference_structure = TTCN3Parser.parse(reference_code)
        
        # 2. 结构对比
        struct_result = self.comparator.compare_structures(
            result.generated_structure,
            result.reference_structure
        )
        result.structure_match_score = struct_result["overall_score"]
        result.differences["structure"] = struct_result
        
        # 3. 语法检查
        gen_issues = TTCN3Parser.check_syntax(generated_code)
        ref_issues = TTCN3Parser.check_syntax(reference_code)
        result.differences["syntax"] = {
            "generated_issues": gen_issues,
            "reference_issues": ref_issues,
        }
        
        # 4. 命名规范检查
        gen_naming_issues = NamingConventionChecker.check(generated_code)
        ref_naming_issues = NamingConventionChecker.check(reference_code)
        result.differences["naming"] = {
            "generated_issues": gen_naming_issues,
            "reference_issues": ref_naming_issues,
        }
        
        # 5. 语义分析
        gen_semantic = SemanticAnalyzer.analyze(generated_code)
        ref_semantic = SemanticAnalyzer.analyze(reference_code)
        result.differences["semantic"] = {
            "generated": gen_semantic,
            "reference": ref_semantic,
        }
        
        # 6. 代码对比
        code_comparison = self.comparator.compare(generated_code, reference_code)
        result.overall_similarity = code_comparison["similarity_score"]
        result.differences["code"] = code_comparison
        
        # 7. 计算各项指标得分
        result.metrics = self._calculate_metrics(result)
        
        # 8. 收集问题
        result.issues = self._collect_issues(result)
        
        # 9. 统计信息
        result.stats = {
            "generated_lines": len(generated_code.split('\n')),
            "reference_lines": len(reference_code.split('\n')),
            "generated_components": len(result.generated_structure.templates) + 
                                   len(result.generated_structure.functions) + 
                                   len(result.generated_structure.testcases),
            "reference_components": len(result.reference_structure.templates) + 
                                    len(result.reference_structure.functions) + 
                                    len(result.reference_structure.testcases),
            "syntax_errors": len(gen_issues),
            "naming_issues": len(gen_naming_issues),
        }
        
        # 10. 确定状态
        result.status = self._determine_status(result)
        
        return result
    
    def _calculate_metrics(self, result: ComparisonResult) -> Dict[str, float]:
        """计算各项指标得分"""
        metrics = {}
        
        # 结构相似度 (权重 0.3)
        metrics["structural_similarity"] = result.structure_match_score
        
        # 语法正确度 (权重 0.25)
        syntax_issues = result.differences.get("syntax", {}).get("generated_issues", [])
        critical_syntax = sum(1 for i in syntax_issues if i.get("severity") == "critical")
        metrics["syntax_correctness"] = max(0.0, 1.0 - (critical_syntax * 0.25))
        
        # 语义覆盖度 (权重 0.25)
        gen_semantic = result.differences.get("semantic", {}).get("generated", {})
        ref_semantic = result.differences.get("semantic", {}).get("reference", {})
        
        # 计算语义覆盖率
        required_in_gen = 0
        required_total = 0
        for key in ["has_testcase", "has_runs_on", "has_system", "has_setverdict"]:
            if key in gen_semantic:
                required_total += 1
                if gen_semantic[key]:
                    required_in_gen += 1
        
        metrics["semantic_coverage"] = required_in_gen / required_total if required_total > 0 else 0.0
        
        # 命名规范度 (权重 0.1)
        naming_issues = result.differences.get("naming", {}).get("generated_issues", [])
        warning_naming = sum(1 for i in naming_issues if i.get("severity") == "warning")
        metrics["naming_convention"] = max(0.0, 1.0 - (warning_naming * 0.1))
        
        # 代码完整性 (权重 0.1)
        missing_components = gen_semantic.get("missing_components", [])
        metrics["completeness"] = max(0.0, 1.0 - (len(missing_components) * 0.2))
        
        # 综合得分
        weights = {
            "structural_similarity": 0.3,
            "syntax_correctness": 0.25,
            "semantic_coverage": 0.25,
            "naming_convention": 0.1,
            "completeness": 0.1,
        }
        
        overall = sum(metrics[k] * weights[k] for k in weights)
        metrics["overall"] = overall
        
        return metrics
    
    def _collect_issues(self, result: ComparisonResult) -> List[Dict[str, Any]]:
        """收集所有问题"""
        issues = []
        
        # 语法问题
        for issue in result.differences.get("syntax", {}).get("generated_issues", []):
            issues.append({
                "type": "syntax",
                "severity": issue.get("severity", "warning"),
                "description": issue.get("issue", ""),
            })
        
        # 命名问题
        for issue in result.differences.get("naming", {}).get("generated_issues", []):
            issues.append({
                "type": "naming",
                "severity": issue.get("severity", "warning"),
                "description": issue.get("issue", ""),
                "component": issue.get("name", ""),
            })
        
        # 缺失组件
        for component in result.differences.get("semantic", {}).get("generated", {}).get("missing_components", []):
            issues.append({
                "type": "missing",
                "severity": "critical",
                "description": f"缺失必需组件: {component}",
            })
        
        # 结构差异
        missing = result.differences.get("structure", {}).get("missing_in_generated", {})
        for comp_type, items in missing.items():
            if items:
                issues.append({
                    "type": "missing_component",
                    "severity": "warning",
                    "description": f"缺失 {comp_type}: {', '.join(items[:3])}" + ("..." if len(items) > 3 else ""),
                })
        
        return issues
    
    def _determine_status(self, result: ComparisonResult) -> str:
        """确定对比状态"""
        # 检查严重问题
        critical_issues = [i for i in result.issues if i.get("severity") == "critical"]
        
        if critical_issues:
            return "fail"
        
        # 检查警告
        warning_issues = [i for i in result.issues if i.get("severity") == "warning"]
        if warning_issues:
            return "warning"
        
        # 检查综合得分
        if result.metrics.get("overall", 0) >= 0.8:
            return "pass"
        
        return "warning"


# ============================================================
# 主函数
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="TTCN-3 代码对比分析器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 单个对比
  python code_comparator.py --generated ./test_code/generated/TC_XXX.ttcn \\
                            --reference ./experiment/standard_references/standard_reference_TC_XXX.ttcn
  
  # 输出 JSON 格式
  python code_comparator.py --generated ./test_code/generated/TC_XXX.ttcn \\
                            --reference ./experiment/standard_references/standard_reference_TC_XXX.ttcn \\
                            --output-format json --output ./comparison_result.json
        """
    )
    
    parser.add_argument("--generated", type=str, required=True, help="生成的代码文件路径")
    parser.add_argument("--reference", type=str, required=True, help="标准参考代码文件路径")
    parser.add_argument("--tc-id", type=str, help="测试用例 ID（用于报告）")
    parser.add_argument("--output", type=str, help="输出文件路径")
    parser.add_argument("--output-format", type=str, choices=["json", "text", "html"], default="text",
                        help="输出格式")
    parser.add_argument("--ignore-whitespace", action="store_true", default=True,
                        help="忽略空白差异")
    parser.add_argument("--ignore-comments", action="store_true", default=True,
                        help="忽略注释差异")
    
    args = parser.parse_args()
    
    # 解析路径
    generated_path = Path(args.generated)
    reference_path = Path(args.reference)
    
    if not generated_path.exists():
        print(f"错误: 生成的代码文件不存在: {generated_path}")
        sys.exit(1)
    
    if not reference_path.exists():
        print(f"错误: 标准参考代码文件不存在: {reference_path}")
        sys.exit(1)
    
    # 推断 TC ID
    tc_id = args.tc_id
    if not tc_id:
        tc_id = generated_path.stem.replace("standard_reference_", "")
    
    # 配置
    config = {
        "ignore_whitespace": args.ignore_whitespace,
        "ignore_comments": args.ignore_comments,
    }
    
    # 分析
    print(f"对比分析: {tc_id}")
    analyzer = ComparisonAnalyzer(config)
    result = analyzer.analyze(tc_id, generated_path, reference_path)
    
    # 输出
    if args.output_format == "json":
        output = json.dumps(asdict(result), indent=2, ensure_ascii=False)
    elif args.output_format == "html":
        # 读取代码生成 HTML
        with open(generated_path, "r", encoding="utf-8") as f:
            generated_code = f.read()
        with open(reference_path, "r", encoding="utf-8") as f:
            reference_code = f.read()
        output = analyzer.comparator.generate_diff_html(generated_code, reference_code)
    else:
        output = format_text_report(result)
    
    # 保存或打印
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"报告已保存: {output_path}")
    else:
        print(output)


def asdict(obj):
    """将 dataclass 转换为 dict（递归处理）"""
    if hasattr(obj, '__dataclass_fields__'):
        return {k: asdict(v) for k, v in obj.__dict__.items()}
    elif isinstance(obj, list):
        return [asdict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: asdict(v) for k, v in obj.items()}
    else:
        return obj


def format_text_report(result: ComparisonResult) -> str:
    """格式化文本报告"""
    lines = []
    
    lines.append("=" * 70)
    lines.append(f"TTCN-3 代码对比分析报告: {result.tc_id}")
    lines.append("=" * 70)
    
    lines.append(f"\n状态: {result.status.upper()}")
    lines.append(f"综合相似度: {result.overall_similarity:.2%}")
    
    lines.append(f"\n--- 指标得分 ---")
    for metric, score in result.metrics.items():
        bar = "█" * int(score * 20) + "░" * (20 - int(score * 20))
        lines.append(f"  {metric:25s}: [{bar}] {score:.1%}")
    
    lines.append(f"\n--- 统计信息 ---")
    lines.append(f"  生成的代码行数: {result.stats['generated_lines']}")
    lines.append(f"  参考代码行数: {result.stats['reference_lines']}")
    lines.append(f"  生成的组件数: {result.stats['generated_components']}")
    lines.append(f"  参考组件数: {result.stats['reference_components']}")
    
    if result.issues:
        lines.append(f"\n--- 问题列表 ({len(result.issues)}) ---")
        for i, issue in enumerate(result.issues, 1):
            severity = issue.get("severity", "info")
            severity_marker = {"critical": "✗", "warning": "⚠", "info": "ℹ"}.get(severity, "•")
            lines.append(f"  {i}. [{severity.upper()}] {severity_marker} {issue.get('description', '')}")
    
    lines.append("\n" + "=" * 70)
    
    return "\n".join(lines)


if __name__ == "__main__":
    main()
