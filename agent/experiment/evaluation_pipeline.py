#!/usr/bin/env python3
"""
TTCN-3 代码生成与评估 Pipeline
完整的端到端流程：测试用例生成 → 代码生成 → 标准参考提取 → 对比分析 → 报告生成

流程概述:
  1. 加载配置和测试用例定义
  2. 提取标准参考代码
  3. 运行 Code Agent 生成 TTCN-3 代码
  4. 对比分析生成代码与标准参考
  5. 生成评估报告

用法:
  # 单个测试用例评估
  python evaluation_pipeline.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001
  
  # 批量测试套件评估
  python evaluation_pipeline.py --suite suite_communication_setup
  
  # 完整批量评估（所有配置的测试用例）
  python evaluation_pipeline.py --batch
  
  # 仅提取标准参考（不生成代码）
  python evaluation_pipeline.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001 --extract-only
  
  # 仅对比分析（假设代码已生成）
  python evaluation_pipeline.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001 --compare-only
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


# ============================================================
# 路径配置
# ============================================================
AGENT_DIR = Path(__file__).resolve().parent.parent
EXPERIMENT_DIR = Path(__file__).resolve().parent

# 相对于 AGENT_DIR 的路径
CONFIG_PATH = EXPERIMENT_DIR / "test_cases_config.json"
KNOWLEDGE_BASE_PATH = AGENT_DIR / "document" / "iso15118_4_ttcn_knowledge_base.json"
GENERATED_CODE_DIR = AGENT_DIR / "test_code" / "generated"
STANDARD_REFERENCE_DIR = EXPERIMENT_DIR / "standard_references"
COMPARISON_RESULTS_DIR = EXPERIMENT_DIR / "comparison_results"
REPORTS_DIR = EXPERIMENT_DIR / "reports"
LOGS_DIR = EXPERIMENT_DIR / "logs"


# ============================================================
# 日志系统
# ============================================================
class Logger:
    """简单的日志系统"""
    
    LEVELS = {
        "DEBUG": "[\033[36mDEBUG\033[0m]",
        "INFO": "[\033[32mINFO\033[0m]",
        "WARN": "[\033[33mWARN\033[0m]",
        "ERROR": "[\033[31mERROR\033[0m]",
        "SUCCESS": "[\033[32m✓\033[0m]",
    }
    
    def __init__(self, name: str, log_file: Optional[Path] = None):
        self.name = name
        self.log_file = log_file
    
    def log(self, message: str, level: str = "INFO") -> None:
        """输出日志"""
        timestamp = time.strftime("%H:%M:%S")
        prefix = self.LEVELS.get(level, level)
        line = f"[{timestamp}] {prefix} [{self.name}] {message}"
        
        print(line)
        
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")


# ============================================================
# 步骤状态
# ============================================================
class StepStatus(Enum):
    """Pipeline 步骤状态"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PipelineStep:
    """Pipeline 步骤"""
    name: str
    description: str
    status: StepStatus = StepStatus.PENDING
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    error: Optional[str] = None
    result: Any = None
    
    @property
    def duration(self) -> float:
        """运行时间（秒）"""
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0.0


@dataclass
class PipelineResult:
    """Pipeline 运行结果"""
    tc_id: str
    success: bool
    steps: List[PipelineStep] = field(default_factory=list)
    comparison_result: Optional[Dict] = None
    output_files: Dict[str, Path] = field(default_factory=dict)
    error: Optional[str] = None
    total_duration: float = 0.0


# ============================================================
# 配置加载器
# ============================================================
class ConfigLoader:
    """配置加载器"""
    
    @staticmethod
    def load(config_path: Path = CONFIG_PATH) -> Dict:
        """加载配置文件"""
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    
    @staticmethod
    def get_test_case(config: Dict, tc_id: str) -> Optional[Dict]:
        """获取测试用例配置"""
        for tc in config.get("single_test_cases", []):
            if tc.get("tc_id") == tc_id:
                return tc
        return None
    
    @staticmethod
    def get_test_suite(config: Dict, suite_id: str) -> Optional[Dict]:
        """获取测试套件配置"""
        for suite in config.get("batch_test_suites", []):
            if suite.get("suite_id") == suite_id:
                return suite
        return None
    
    @staticmethod
    def get_all_tc_ids(config: Dict) -> List[str]:
        """获取所有测试用例 ID"""
        tc_ids = []
        for tc in config.get("single_test_cases", []):
            tc_ids.append(tc["tc_id"])
        return tc_ids


# ============================================================
# 标准参考提取器包装器
# ============================================================
class ReferenceExtractor:
    """标准参考提取器包装器"""
    
    def __init__(self, logger: Logger):
        self.logger = logger
        self.output_dir = STANDARD_REFERENCE_DIR
    
    def extract(self, tc_id: str) -> Optional[Path]:
        """提取标准参考代码"""
        self.logger.log(f"提取标准参考: {tc_id}", "INFO")
        
        # 使用增强版的提取器
        extractor_script = EXPERIMENT_DIR / "standard_extractor.py"
        
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(extractor_script),
                    "--tc-id", tc_id,
                    "--output-dir", str(self.output_dir),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            
            if result.returncode == 0:
                self.logger.log(f"标准参考提取成功: {tc_id}", "SUCCESS")
                return self.output_dir / f"standard_reference_{tc_id}.ttcn"
            else:
                self.logger.log(f"标准参考提取失败: {result.stderr}", "ERROR")
                return None
                
        except subprocess.TimeoutExpired:
            self.logger.log("标准参考提取超时", "ERROR")
            return None
        except Exception as e:
            self.logger.log(f"标准参考提取异常: {e}", "ERROR")
            return None
    
    def extract_batch(self, tc_ids: List[str]) -> Dict[str, Path]:
        """批量提取"""
        results = {}
        for tc_id in tc_ids:
            path = self.extract(tc_id)
            results[tc_id] = path
        return results


# ============================================================
# Code Agent 包装器
# ============================================================
class CodeAgentWrapper:
    """Code Agent 包装器"""

    def __init__(self, logger: Logger):
        self.logger = logger
        self.generated_dir = GENERATED_CODE_DIR

        # 导入 code_generator 模块
        import sys
        sys.path.insert(0, str(EXPERIMENT_DIR))
        from code_generator import (
            generate_code_from_tc_id,
            generate_code_from_requirements,
        )
        self.generate_from_tc_id = generate_code_from_tc_id
        self.generate_from_requirements = generate_code_from_requirements

    def generate(self, tc_id: str, config: Dict) -> Optional[Path]:
        """运行 Code Agent 生成代码

        支持多种输入模式:
        1. 通过需求 ID（从配置中获取）
        2. 通过测试用例 ID

        Args:
            tc_id: 测试用例 ID
            config: 配置字典

        Returns:
            生成的文件路径
        """
        self.logger.log(f"启动 Code Agent: {tc_id}", "INFO")

        # 从配置中获取需求 ID 列表
        requirement_ids = self._get_requirement_ids(tc_id, config)

        try:
            gen_success = False
            gen_path = None
            gen_msg = ""

            # 优先尝试使用需求 ID 模式
            if requirement_ids:
                self.logger.log(f"使用需求 ID 模式: {requirement_ids}", "INFO")
                gen_success, gen_path, gen_msg = self.generate_from_requirements(
                    requirement_ids=requirement_ids,
                    sut_type=None,
                    use_cache=True,
                    overwrite_existing=False,
                    auto_generate=True,
                    multi_mode=True,
                )

            # 如果需求 ID 模式失败或没有需求 ID，回退到 TC ID 模式
            if not gen_success:
                self.logger.log(f"回退到 TC ID 模式: {tc_id}", "INFO")
                gen_success, gen_path, gen_msg = self.generate_from_tc_id(
                    tc_id=tc_id,
                    sut_type=None,
                    use_cache=True,
                    overwrite_existing=False,
                )

            if gen_success:
                self.logger.log(f"代码生成成功: {tc_id}", "SUCCESS")
                return self.generated_dir / f"{tc_id}.ttcn" if gen_path is None else gen_path
            else:
                self.logger.log(f"代码生成失败: {gen_msg}", "ERROR")
                return None

        except Exception as e:
            self.logger.log(f"代码生成异常: {e}", "ERROR")
            return None

    def _get_requirement_ids(self, tc_id: str, config: Dict) -> Optional[List[str]]:
        """从配置中获取测试用例对应的需求 ID 列表

        Args:
            tc_id: 测试用例 ID
            config: 配置字典

        Returns:
            需求 ID 列表
        """
        # 检查 single_test_cases
        for tc in config.get("single_test_cases", []):
            if tc.get("tc_id") == tc_id:
                req_ids = tc.get("requirement_ids", [])
                if req_ids:
                    return req_ids

        # 检查 batch_test.test_cases
        for tc in config.get("batch_test", {}).get("test_cases", []):
            if tc.get("tc_id") == tc_id:
                req_ids = tc.get("requirement_ids", [])
                if req_ids:
                    return req_ids

        # 检查 single_test
        single_test = config.get("single_test", {})
        if single_test.get("test_case", {}).get("tc_id") == tc_id:
            req_id = single_test["test_case"].get("requirement_id")
            if req_id:
                return [req_id]

        return None


# ============================================================
# 代码对比分析器包装器
# ============================================================
class ComparisonWrapper:
    """代码对比分析器包装器"""
    
    def __init__(self, logger: Logger):
        self.logger = logger
        self.output_dir = COMPARISON_RESULTS_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def compare(self, tc_id: str, generated_path: Path, reference_path: Path) -> Optional[Dict]:
        """对比代码"""
        self.logger.log(f"对比分析: {tc_id}", "INFO")
        
        comparator_script = EXPERIMENT_DIR / "code_comparator.py"
        
        if not comparator_script.exists():
            self.logger.log(f"对比脚本不存在: {comparator_script}", "ERROR")
            return None
        
        output_path = self.output_dir / f"{tc_id}_comparison.json"
        
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(comparator_script),
                    "--generated", str(generated_path),
                    "--reference", str(reference_path),
                    "--tc-id", tc_id,
                    "--output-format", "json",
                    "--output", str(output_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            
            if result.returncode == 0:
                self.logger.log(f"对比分析完成: {tc_id}", "SUCCESS")
                
                # 加载结果
                with open(output_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            else:
                self.logger.log(f"对比分析失败: {result.stderr[:500]}", "ERROR")
                return None
                
        except subprocess.TimeoutExpired:
            self.logger.log("对比分析超时", "ERROR")
            return None
        except Exception as e:
            self.logger.log(f"对比分析异常: {e}", "ERROR")
            return None


# ============================================================
# 报告生成器包装器
# ============================================================
class ReportWrapper:
    """报告生成器包装器"""
    
    def __init__(self, logger: Logger):
        self.logger = logger
        self.output_dir = REPORTS_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def generate(self, tc_id: str, comparison_result: Dict, output_format: str = "markdown") -> List[Path]:
        """生成报告"""
        self.logger.log(f"生成报告: {tc_id}", "INFO")
        
        report_script = EXPERIMENT_DIR / "report_generator.py"
        
        if not report_script.exists():
            self.logger.log(f"报告生成脚本不存在: {report_script}", "ERROR")
            return []
        
        output_paths = []
        
        for fmt in output_format.split(","):
            fmt = fmt.strip()
            output_path = self.output_dir / f"{tc_id}_report.{fmt}"
            
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(report_script),
                        "--results", str(COMPARISON_RESULTS_DIR / f"{tc_id}_comparison.json"),
                        "--format", fmt,
                        "--output", str(output_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                
                if result.returncode == 0:
                    output_paths.append(output_path)
                    self.logger.log(f"报告生成成功: {output_path}", "SUCCESS")
                else:
                    self.logger.log(f"报告生成失败 ({fmt}): {result.stderr[:200]}", "WARN")
                    
            except Exception as e:
                self.logger.log(f"报告生成异常 ({fmt}): {e}", "WARN")
        
        return output_paths
    
    def generate_batch_summary(self, results: List[PipelineResult], suite_name: str) -> List[Path]:
        """生成批量测试摘要报告"""
        self.logger.log(f"生成批量摘要报告: {suite_name}", "INFO")
        
        report_script = EXPERIMENT_DIR / "report_generator.py"
        output_paths = []
        
        # 写入临时结果文件
        temp_results = []
        for result in results:
            if result.comparison_result:
                temp_results.append(result.comparison_result)
        
        if not temp_results:
            self.logger.log("没有可用的对比结果", "WARN")
            return []
        
        temp_file = self.output_dir / f"_temp_results_{int(time.time())}.json"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(temp_results, f, ensure_ascii=False)
        
        try:
            for fmt in ["markdown", "html"]:
                output_path = self.output_dir / f"{suite_name}_summary.{fmt}"
                
                result = subprocess.run(
                    [
                        sys.executable,
                        str(report_script),
                        "--batch",
                        "--results-dir", str(self.output_dir),
                        "--suite-name", suite_name,
                        "--format", fmt,
                        "--output", str(output_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                
                if result.returncode == 0:
                    output_paths.append(output_path)
        
        finally:
            # 清理临时文件
            if temp_file.exists():
                temp_file.unlink()
        
        return output_paths


# ============================================================
# Pipeline 编排器
# ============================================================
class EvaluationPipeline:
    """评估 Pipeline 编排器"""
    
    def __init__(self, config: Optional[Dict] = None):
        # 加载配置
        self.config = config or ConfigLoader.load()
        
        # 初始化目录
        for directory in [STANDARD_REFERENCE_DIR, COMPARISON_RESULTS_DIR, REPORTS_DIR, LOGS_DIR]:
            directory.mkdir(parents=True, exist_ok=True)
        
        # 初始化日志
        self.logger = Logger("EvaluationPipeline", LOGS_DIR / f"pipeline_{int(time.time())}.log")
        
        # 初始化组件
        self.reference_extractor = ReferenceExtractor(self.logger)
        self.code_agent = CodeAgentWrapper(self.logger)
        self.comparator = ComparisonWrapper(self.logger)
        self.report_generator = ReportWrapper(self.logger)
    
    def run_single(self, tc_id: str, 
                   extract_only: bool = False,
                   compare_only: bool = False,
                   generate_only: bool = False,
                   generate_code: bool = True) -> PipelineResult:
        """运行单个测试用例的完整 Pipeline"""
        
        result = PipelineResult(
            tc_id=tc_id,
            success=False,
            steps=[],
        )
        
        start_time = time.time()
        
        # 获取测试用例配置
        tc_config = ConfigLoader.get_test_case(self.config, tc_id)
        if not tc_config:
            self.logger.log(f"未找到测试用例配置: {tc_id}", "ERROR")
            result.error = "测试用例配置不存在"
            return result
        
        self.logger.log("=" * 60, "INFO")
        self.logger.log(f"开始评估 Pipeline: {tc_id}", "INFO")
        self.logger.log("=" * 60, "INFO")
        
        # === 步骤 1: 提取标准参考代码 ===
        step1 = PipelineStep(
            name="Extract Reference",
            description="从 ISO 15118-4 知识库提取标准参考代码",
        )
        result.steps.append(step1)
        
        if not compare_only:
            step1.status = StepStatus.RUNNING
            step1.start_time = time.time()
            
            reference_path = self.reference_extractor.extract(tc_id)
            
            step1.end_time = time.time()
            if reference_path and reference_path.exists():
                step1.status = StepStatus.SUCCESS
                step1.result = str(reference_path)
                result.output_files["reference"] = reference_path
                self.logger.log(f"标准参考提取完成 ({step1.duration:.2f}s)", "SUCCESS")
            else:
                step1.status = StepStatus.FAILED
                step1.error = "标准参考提取失败"
                self.logger.log(f"标准参考提取失败", "ERROR")
                result.error = "标准参考提取失败"
                result.total_duration = time.time() - start_time
                return result
        else:
            # compare_only 模式，查找已存在的标准参考
            reference_path = STANDARD_REFERENCE_DIR / f"standard_reference_{tc_id}.ttcn"
            if reference_path.exists():
                step1.status = StepStatus.SKIPPED
                step1.result = str(reference_path)
                result.output_files["reference"] = reference_path
                self.logger.log(f"使用已有标准参考: {reference_path}", "INFO")
            else:
                step1.status = StepStatus.FAILED
                step1.error = "标准参考文件不存在"
                result.error = "标准参考文件不存在"
                result.total_duration = time.time() - start_time
                return result
        
        if extract_only:
            result.success = True
            result.total_duration = time.time() - start_time
            return result
        
        # === 步骤 2: 生成代码 ===
        step2 = PipelineStep(
            name="Generate Code",
            description="运行 Code Agent 生成 TTCN-3 代码",
        )
        result.steps.append(step2)
        
        generated_path = None
        
        if not compare_only and generate_code:
            step2.status = StepStatus.RUNNING
            step2.start_time = time.time()
            
            generated_path = self.code_agent.generate(tc_id, self.config)
            
            step2.end_time = time.time()
            if generated_path and generated_path.exists():
                step2.status = StepStatus.SUCCESS
                step2.result = str(generated_path)
                result.output_files["generated"] = generated_path
                self.logger.log(f"代码生成完成 ({step2.duration:.2f}s)", "SUCCESS")
            else:
                step2.status = StepStatus.FAILED
                step2.error = "代码生成失败"
                self.logger.log(f"代码生成失败", "ERROR")
                result.error = "代码生成失败"
                result.total_duration = time.time() - start_time
                return result
        else:
            # 查找已生成的代码
            generated_path = GENERATED_CODE_DIR / f"{tc_id}.ttcn"
            if generated_path.exists():
                step2.status = StepStatus.SKIPPED
                step2.result = str(generated_path)
                result.output_files["generated"] = generated_path
                self.logger.log(f"使用已有生成代码: {generated_path}", "INFO")
            else:
                step2.status = StepStatus.FAILED
                step2.error = "生成代码文件不存在"
                result.error = "生成代码文件不存在"
                result.total_duration = time.time() - start_time
                return result
        
        if generate_only:
            result.success = True
            result.total_duration = time.time() - start_time
            return result
        
        # === 步骤 3: 对比分析 ===
        step3 = PipelineStep(
            name="Compare & Analyze",
            description="对比生成代码与标准参考",
        )
        result.steps.append(step3)
        
        step3.status = StepStatus.RUNNING
        step3.start_time = time.time()
        
        comparison_result = self.comparator.compare(tc_id, generated_path, reference_path)
        
        step3.end_time = time.time()
        if comparison_result:
            step3.status = StepStatus.SUCCESS
            step3.result = comparison_result
            result.comparison_result = comparison_result
            self.logger.log(f"对比分析完成 ({step3.duration:.2f}s)", "SUCCESS")
            self.logger.log(f"  相似度: {comparison_result.get('overall_similarity', 0):.2%}", "INFO")
            self.logger.log(f"  状态: {comparison_result.get('status', 'unknown')}", "INFO")
        else:
            step3.status = StepStatus.FAILED
            step3.error = "对比分析失败"
            self.logger.log(f"对比分析失败", "ERROR")
            result.error = "对比分析失败"
            result.total_duration = time.time() - start_time
            return result
        
        # === 步骤 4: 生成报告 ===
        step4 = PipelineStep(
            name="Generate Report",
            description="生成评估报告",
        )
        result.steps.append(step4)
        
        step4.status = StepStatus.RUNNING
        step4.start_time = time.time()
        
        report_paths = self.report_generator.generate(tc_id, comparison_result, "markdown,html")
        
        step4.end_time = time.time()
        if report_paths:
            step4.status = StepStatus.SUCCESS
            step4.result = [str(p) for p in report_paths]
            result.output_files["reports"] = report_paths
            self.logger.log(f"报告生成完成 ({step4.duration:.2f}s)", "SUCCESS")
        else:
            step4.status = StepStatus.WARNING
            step4.error = "报告生成失败"
            self.logger.log(f"报告生成失败", "WARN")
        
        # === 完成 ===
        result.success = True
        result.total_duration = time.time() - start_time
        
        self.logger.log("=" * 60, "INFO")
        self.logger.log(f"Pipeline 完成: {tc_id}", "SUCCESS")
        self.logger.log(f"总耗时: {result.total_duration:.2f}s", "INFO")
        self.logger.log("=" * 60, "INFO")
        
        return result
    
    def run_batch(self, tc_ids: List[str], suite_name: str = "Batch Test") -> List[PipelineResult]:
        """批量运行 Pipeline"""
        
        self.logger.log("=" * 60, "INFO")
        self.logger.log(f"开始批量评估: {suite_name} ({len(tc_ids)} 个测试用例)", "INFO")
        self.logger.log("=" * 60, "INFO")
        
        results = []
        
        for i, tc_id in enumerate(tc_ids, 1):
            self.logger.log(f"\n[{i}/{len(tc_ids)}] 处理 {tc_id}...", "INFO")
            
            result = self.run_single(tc_id)
            results.append(result)
            
            # 打印简要结果
            if result.success:
                similarity = result.comparison_result.get("overall_similarity", 0) if result.comparison_result else 0
                self.logger.log(f"  ✓ {tc_id}: 相似度 {similarity:.2%}", "SUCCESS")
            else:
                self.logger.log(f"  ✗ {tc_id}: {result.error or '未知错误'}", "ERROR")
        
        # 生成批量摘要报告
        self.logger.log("\n生成批量摘要报告...", "INFO")
        summary_paths = self.report_generator.generate_batch_summary(results, suite_name)
        
        # 打印汇总
        success_count = sum(1 for r in results if r.success)
        avg_similarity = 0.0
        if results:
            similarities = [r.comparison_result.get("overall_similarity", 0) 
                          for r in results if r.comparison_result]
            if similarities:
                avg_similarity = sum(similarities) / len(similarities)
        
        self.logger.log("\n" + "=" * 60, "INFO")
        self.logger.log(f"批量评估完成", "INFO")
        self.logger.log(f"  总计: {len(results)}", "INFO")
        self.logger.log(f"  成功: {success_count}", "INFO")
        self.logger.log(f"  失败: {len(results) - success_count}", "INFO")
        self.logger.log(f"  平均相似度: {avg_similarity:.2%}", "INFO")
        self.logger.log(f"  摘要报告: {', '.join(str(p) for p in summary_paths)}", "INFO")
        self.logger.log("=" * 60, "INFO")
        
        return results
    
    def run_suite(self, suite_id: str) -> List[PipelineResult]:
        """运行指定测试套件"""
        suite_config = ConfigLoader.get_test_suite(self.config, suite_id)
        
        if not suite_config:
            self.logger.log(f"未找到测试套件: {suite_id}", "ERROR")
            return []
        
        tc_ids = suite_config.get("tc_ids", [])
        suite_name = suite_config.get("suite_name", suite_id)
        
        return self.run_batch(tc_ids, suite_name)
    
    def run_all(self) -> List[PipelineResult]:
        """运行所有配置的测试用例"""
        tc_ids = ConfigLoader.get_all_tc_ids(self.config)
        return self.run_batch(tc_ids, "All Test Cases")


# ============================================================
# 主函数
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="TTCN-3 代码生成与评估 Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 单个测试用例评估
  python evaluation_pipeline.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001
  
  # 使用测试套件评估
  python evaluation_pipeline.py --suite suite_communication_setup
  
  # 完整批量评估
  python evaluation_pipeline.py --batch
  
  # 仅提取标准参考
  python evaluation_pipeline.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001 --extract-only
  
  # 仅对比分析（假设代码已生成）
  python evaluation_pipeline.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001 --compare-only
  
  # 自定义输出目录
  python evaluation_pipeline.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001 \\
                              --output-dir ./my_experiment
        """
    )
    
    # 输入选项
    input_group = parser.add_argument_group("输入选项（选择其一）")
    input_group.add_argument("--tc-id", type=str, help="单个测试用例 ID")
    input_group.add_argument("--suite", type=str, help="测试套件 ID")
    input_group.add_argument("--batch", action="store_true", help="批量评估所有配置的测试用例")
    input_group.add_argument("--tc-list", type=str, help="测试用例 ID 列表（逗号分隔）")
    
    # 模式选项
    mode_group = parser.add_argument_group("模式选项")
    mode_group.add_argument("--extract-only", action="store_true", 
                          help="仅提取标准参考代码，不生成代码和对比")
    mode_group.add_argument("--generate-only", action="store_true",
                          help="仅生成代码，不进行对比分析")
    mode_group.add_argument("--compare-only", action="store_true",
                          help="仅进行对比分析（假设代码和参考已存在）")
    mode_group.add_argument("--skip-generation", action="store_true",
                          help="跳过代码生成步骤（用于测试 Pipeline）")
    
    # 输出选项
    output_group = parser.add_argument_group("输出选项")
    output_group.add_argument("--output-dir", type=str, help="输出目录")
    output_group.add_argument("--report-format", type=str, default="markdown,html",
                             help="报告格式（逗号分隔），默认 markdown,html")
    
    # 其他选项
    parser.add_argument("--config", type=str, help="配置文件路径")
    parser.add_argument("--verbose", action="store_true", help="详细输出")
    
    args = parser.parse_args()
    
    # 加载配置
    config_path = Path(args.config) if args.config else CONFIG_PATH
    try:
        config = ConfigLoader.load(config_path)
    except FileNotFoundError:
        print(f"错误: 配置文件不存在: {config_path}")
        sys.exit(1)
    
    # 初始化 Pipeline
    pipeline = EvaluationPipeline(config)
    
    # 确定运行模式
    if args.tc_id:
        # 单个测试用例
        result = pipeline.run_single(
            tc_id=args.tc_id,
            extract_only=args.extract_only,
            compare_only=args.compare_only,
            generate_only=args.generate_only,
            generate_code=not args.skip_generation and not args.extract_only,
        )
        
        # 打印结果摘要
        if result.success:
            print(f"\n✓ Pipeline 执行成功")
            print(f"  测试用例: {result.tc_id}")
            print(f"  总耗时: {result.total_duration:.2f}s")
            if result.comparison_result:
                print(f"  相似度: {result.comparison_result.get('overall_similarity', 0):.2%}")
                print(f"  状态: {result.comparison_result.get('status', 'unknown')}")
        else:
            print(f"\n✗ Pipeline 执行失败")
            print(f"  测试用例: {result.tc_id}")
            print(f"  错误: {result.error}")
            sys.exit(1)
    
    elif args.suite:
        # 测试套件
        results = pipeline.run_suite(args.suite)
        success_count = sum(1 for r in results if r.success)
        print(f"\n✓ 批量评估完成: {success_count}/{len(results)} 成功")
    
    elif args.batch:
        # 全部测试用例
        results = pipeline.run_all()
        success_count = sum(1 for r in results if r.success)
        print(f"\n✓ 全部评估完成: {success_count}/{len(results)} 成功")
    
    elif args.tc_list:
        # 指定测试用例列表
        tc_ids = [t.strip() for t in args.tc_list.split(",")]
        results = pipeline.run_batch(tc_ids, "Custom List")
        success_count = sum(1 for r in results if r.success)
        print(f"\n✓ 批量评估完成: {success_count}/{len(results)} 成功")
    
    else:
        parser.print_help()
        print("\n请指定 --tc-id, --suite, --batch 或 --tc-list 之一")


if __name__ == "__main__":
    main()
