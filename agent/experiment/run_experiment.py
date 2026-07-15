#!/usr/bin/env python3
"""
TTCN-3 代码生成实验 Pipeline 主入口

功能：
1. 支持单个测试用例的完整实验流程
2. 支持批量测试用例的批量实验
3. 协调代码生成、标准提取、代码对比、报告生成四个模块
4. 提供完整的日志输出和错误处理

用法：
    # 单个测试用例
    python run_experiment.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001
    python run_experiment.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001 --skip-generation

    # 批量测试
    python run_experiment.py --batch
    python run_experiment.py --batch --config ./test_cases_config.json

    # 仅运行对比
    python run_experiment.py --compare-only \
        --generated /path/to/generated.ttcn \
        --standard /path/to/standard.ttcn \
        --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 添加当前目录到路径，以便导入本地模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from code_generator import (
    generate_code_from_tc_id,
    generate_code_from_requirements,
    generate_batch,
    get_generated_code,
    GENERATED_DIR as CODE_GENERATOR_OUTPUT_DIR,
)
from standard_extractor import (
    extract_standard_reference,
    save_standard_reference,
    get_standard_code,
    EXPERIMENT_DIR as STD_EXTRACTOR_DIR,
)
from code_comparator import ComparisonAnalyzer
from report_generator import TestCaseReport, TextReportGenerator, ReportFormat, MetricScore, BatchReport, MarkdownReportGenerator

# 辅助函数：将旧版 ComparisonResult 转换为新格式
def convert_comparison_result(old_result, tc_id: str):
    """将旧版 ComparisonResult 转换为标准格式"""
    from dataclasses import dataclass, field
    from typing import List, Dict, Any

    @dataclass
    class DiffItem:
        level: str
        category: str
        description: str
        generated_snippet: str = None
        standard_snippet: str = None
        location: str = None
        suggestion: str = None

    diffs = []
    for issue in old_result.issues:
        diffs.append(DiffItem(
            level=issue.get("severity", "info"),
            category=issue.get("type", "unknown"),
            description=issue.get("description", ""),
        ))

    return {
        "tc_id": tc_id,
        "success": old_result.status == "pass",
        "score": old_result.metrics.get("overall", 0) * 100,
        "structure_score": old_result.metrics.get("structural_similarity", 0) * 100,
        "naming_score": old_result.metrics.get("naming_convention", 0) * 100,
        "syntax_score": old_result.metrics.get("syntax_correctness", 0) * 100,
        "completeness_score": old_result.metrics.get("completeness", 0) * 100,
        "diffs": [
            {
                "level": d.level,
                "category": d.category,
                "description": d.description,
                "generated_snippet": d.generated_snippet,
                "standard_snippet": d.standard_snippet,
                "location": d.location,
                "suggestion": d.suggestion,
            }
            for d in diffs
        ],
        "generated_lines": old_result.stats.get("generated_lines", 0),
        "standard_lines": old_result.stats.get("reference_lines", 0),
        "generated_elements": {},
        "standard_elements": {},
        "missing_elements": [],
        "extra_elements": [],
        "comparison_levels": ["structure", "naming", "syntax", "completeness"],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# === 路径常量 ===
EXPERIMENT_DIR = Path(__file__).resolve().parent
AGENT_DIR = EXPERIMENT_DIR.parent
RESULTS_DIR = EXPERIMENT_DIR / "results"
CONFIG_PATH = EXPERIMENT_DIR / "test_cases_config.json"


def log(msg: str, level: str = "INFO") -> None:
    """统一日志输出"""
    ts = time.strftime("%H:%M:%S")
    prefix = {
        "INFO": "[Pipeline]",
        "STEP": "[Step]",
        "WARN": "[WARNING]",
        "ERROR": "[ERROR]",
        "SUCCESS": "[OK]",
    }.get(level, "[Pipeline]")
    print(f"[{ts}] {prefix} {msg}")


def load_json(path: Path) -> Any:
    """加载 JSON 文件"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data: Any, path: Path) -> None:
    """保存 JSON 文件"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """加载配置文件"""
    path = config_path or CONFIG_PATH
    if not path.exists():
        log(f"配置文件不存在: {path}，使用默认配置", "WARN")
        return get_default_config()

    try:
        config = load_json(path)
        log(f"已加载配置: {path}")
        return config
    except Exception as e:
        log(f"加载配置失败: {e}，使用默认配置", "ERROR")
        return get_default_config()


def get_default_config() -> Dict[str, Any]:
    """获取默认配置"""
    return {
        "pipeline_config": {
            "steps": {
                "code_generation": {"enabled": True},
                "standard_extraction": {"enabled": True},
                "code_comparison": {"enabled": True},
                "report_generation": {"enabled": True},
            },
            "results_dir": str(RESULTS_DIR),
            "log_level": "INFO",
        },
        "single_test": {
            "enabled": True,
            "test_case": {
                "tc_id": "TC_SECC_CMN_VTB_SupportedAppProtocol_001",
            },
        },
        "batch_test": {
            "enabled": False,
            "test_cases": [],
        },
    }


def get_test_case_config(config: Dict[str, Any], tc_id: str) -> Optional[Dict[str, Any]]:
    """从配置中获取测试用例信息

    Args:
        config: 配置字典
        tc_id: 测试用例 ID

    Returns:
        测试用例配置字典，包含 requirement_ids 等信息
    """
    # 1. 检查 single_test
    single_test = config.get("single_test", {})
    if single_test.get("test_case", {}).get("tc_id") == tc_id:
        return single_test["test_case"]

    # 2. 检查 batch_test
    batch_test = config.get("batch_test", {})
    for tc in batch_test.get("test_cases", []):
        if tc.get("tc_id") == tc_id:
            return tc

    # 3. 检查 single_test_cases（用于批量提取的格式）
    for tc in config.get("single_test_cases", []):
        if tc.get("tc_id") == tc_id:
            return tc

    return None


def run_single_experiment(
    tc_id: str,
    config: Dict[str, Any],
    skip_generation: bool = False,
    skip_extraction: bool = False,
    overwrite_existing: bool = False,
) -> Dict[str, Any]:
    """运行单个测试用例的完整实验流程

    流程:
    1. 代码生成（可选）
    2. 标准代码提取
    3. 代码对比分析
    4. 报告生成

    Args:
        tc_id: 测试用例 ID
        config: 配置字典
        skip_generation: 跳过代码生成步骤
        skip_extraction: 跳过标准提取步骤
        overwrite_existing: 覆盖已有文件

    Returns:
        实验结果字典
    """
    log(f"{'=' * 60}")
    log(f"开始实验: {tc_id}")
    log(f"{'=' * 60}")

    start_time = time.time()
    result = {
        "tc_id": tc_id,
        "success": False,
        "steps": {},
        "output_dir": None,
        "error": None,
    }

    # 确定输出目录
    results_dir = Path(config.get("pipeline_config", {}).get("results_dir", str(RESULTS_DIR)))
    tc_result_dir = results_dir / tc_id
    tc_result_dir.mkdir(parents=True, exist_ok=True)
    result["output_dir"] = str(tc_result_dir)

    # 从配置获取测试用例信息（包括需求 ID）
    tc_config = get_test_case_config(config, tc_id)
    requirement_ids = None
    if tc_config and tc_config.get("requirement_ids"):
        requirement_ids = tc_config["requirement_ids"]
        log(f"从配置获取需求 ID: {requirement_ids}", "INFO")
    elif tc_config and tc_config.get("requirement_id"):
        requirement_ids = [tc_config["requirement_id"]]

    # ========== Step 1: 代码生成 ==========
    if not skip_generation:
        log("\n--- Step 1: 代码生成 ---", "STEP")

        # 如果有需求 ID，使用需求 ID 模式
        gen_success, gen_path, gen_msg = None, None, None
        if requirement_ids:
            gen_success, gen_path, gen_msg = generate_code_from_requirements(
                requirement_ids=requirement_ids,
                sut_type=None,
                use_cache=True,
                overwrite_existing=overwrite_existing,
            )

        # 如果需求 ID 模式失败或没有需求 ID，回退到 TC ID 模式
        if not gen_success:
            gen_success, gen_path, gen_msg = generate_code_from_tc_id(
                tc_id=tc_id,
                sut_type=None,
                use_cache=True,
                overwrite_existing=overwrite_existing,
            )

        result["steps"]["code_generation"] = {
            "success": gen_success,
            "output_path": str(gen_path) if gen_path else None,
            "message": gen_msg,
        }

        if not gen_success:
            log(f"代码生成失败: {gen_msg}", "ERROR")
            result["error"] = f"代码生成失败: {gen_msg}"
            return result

        # 读取生成的代码
        generated_code = get_generated_code(tc_id)
        if not generated_code:
            # 尝试从生成路径读取
            if gen_path and gen_path.exists():
                generated_code = gen_path.read_text(encoding="utf-8")

        if not generated_code:
            log("无法读取生成的代码", "ERROR")
            result["error"] = "无法读取生成的代码"
            return result

        # 保存生成的代码到结果目录
        (tc_result_dir / "generated.ttcn").write_text(generated_code, encoding="utf-8")
        log(f"代码生成成功", "SUCCESS")
    else:
        log("\n--- Step 1: 代码生成（跳过） ---", "STEP")
        # 尝试从结果目录或生成的目录读取已有代码
        cached_path = tc_result_dir / "generated.ttcn"
        log(f"检查缓存路径: {cached_path} (exists={cached_path.exists()})", "INFO")
        if cached_path.exists():
            generated_code = cached_path.read_text(encoding="utf-8")
            log(f"使用缓存的生成代码: {cached_path}")
            result["steps"]["code_generation"] = {
                "success": True,
                "output_path": str(cached_path),
                "message": "使用缓存",
            }
        else:
            log(f"缓存路径不存在，检查生成目录...", "INFO")
            generated_code = get_generated_code(tc_id)
            log(f"get_generated_code 结果: {generated_code is not None}", "INFO")
            if generated_code:
                (tc_result_dir / "generated.ttcn").write_text(generated_code, encoding="utf-8")
                result["steps"]["code_generation"] = {
                    "success": True,
                    "output_path": str(CODE_GENERATOR_OUTPUT_DIR / f"{tc_id}.ttcn"),
                    "message": "使用已有生成文件",
                }
            else:
                log("未找到已生成的代码，请先运行代码生成步骤", "ERROR")
                result["error"] = "未找到生成的代码"
                return result

    # ========== Step 2: 标准代码提取 ==========
    if not skip_extraction:
        log("\n--- Step 2: 标准代码提取 ---", "STEP")
        std_success, std_data, std_msg = extract_standard_reference(
            tc_id=tc_id,
            use_cache=True,
            overwrite_existing=overwrite_existing,
        )
        result["steps"]["standard_extraction"] = {
            "success": std_success,
            "message": std_msg,
        }

        if not std_success:
            log(f"标准代码提取失败: {std_msg}", "ERROR")
            result["error"] = f"标准代码提取失败: {std_msg}"
            return result

        # 保存标准代码
        standard_code = std_data.get("formatted_ttcn", "") if std_data else ""
        if standard_code:
            (tc_result_dir / "standard.ttcn").write_text(standard_code, encoding="utf-8")
        else:
            log("标准代码为空", "WARN")

        # 保存标准代码 JSON 数据
        if std_data:
            save_json(std_data, tc_result_dir / "standard_data.json")

        log(f"标准代码提取成功", "SUCCESS")
    else:
        log("\n--- Step 2: 标准代码提取（跳过） ---", "STEP")
        # 尝试从结果目录读取已有标准代码
        std_path = tc_result_dir / "standard.ttcn"
        if std_path.exists():
            standard_code = std_path.read_text(encoding="utf-8")
            log(f"使用缓存的标准代码: {std_path}")
            result["steps"]["standard_extraction"] = {
                "success": True,
                "message": "使用缓存",
            }
        else:
            std_success, std_data, std_msg = extract_standard_reference(tc_id=tc_id)
            if std_success and std_data:
                standard_code = std_data.get("formatted_ttcn", "")
                (tc_result_dir / "standard.ttcn").write_text(standard_code, encoding="utf-8")
                save_json(std_data, tc_result_dir / "standard_data.json")
                result["steps"]["standard_extraction"] = {
                    "success": True,
                    "message": std_msg,
                }
            else:
                log("未找到标准代码", "ERROR")
                result["error"] = "未找到标准代码"
                return result

    # ========== Step 3: 代码对比 ==========
    log("\n--- Step 3: 代码对比分析 ---", "STEP")

    comparison_levels = config.get("pipeline_config", {}).get("comparison_levels", ["structure", "naming", "syntax", "completeness"])
    strict_naming = config.get("pipeline_config", {}).get("strict_naming", False)

    try:
        # 使用旧版 ComparisonAnalyzer API
        analyzer_config = {
            "ignore_whitespace": True,
            "ignore_comments": True,
        }
        analyzer = ComparisonAnalyzer(analyzer_config)

        # 创建临时文件用于分析
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ttcn', delete=False, encoding='utf-8') as gen_file:
            gen_file.write(generated_code)
            gen_path = gen_file.name

        with tempfile.NamedTemporaryFile(mode='w', suffix='.ttcn', delete=False, encoding='utf-8') as std_file:
            std_file.write(standard_code)
            std_path = std_file.name

        try:
            old_result = analyzer.analyze(tc_id, Path(gen_path), Path(std_path))
            comparison_result_dict = convert_comparison_result(old_result, tc_id)
            score = comparison_result_dict["score"]
        finally:
            # 清理临时文件
            Path(gen_path).unlink(missing_ok=True)
            Path(std_path).unlink(missing_ok=True)

        result["steps"]["code_comparison"] = {
            "success": True,
            "score": score,
            "diffs_count": len(comparison_result_dict.get("diffs", [])),
        }
        log(f"代码对比完成: score={score:.1f}", "SUCCESS")
    except Exception as e:
        log(f"代码对比失败: {e}", "ERROR")
        result["steps"]["code_comparison"] = {
            "success": False,
            "error": str(e),
        }
        result["error"] = f"代码对比失败: {e}"
        return result

    # ========== Step 4: 报告生成 ==========
    log("\n--- Step 4: 报告生成 ---", "STEP")

    try:
        # 使用旧版 ReportGenerator API
        # 构建 TestCaseReport
        report = TestCaseReport(
            tc_id=tc_id,
            overall_similarity=comparison_result_dict["score"] / 100.0,
            status="pass" if comparison_result_dict["success"] else "warning",
            metrics={
                "structure": MetricScore(name="structure", score=comparison_result_dict["structure_score"] / 100.0, weight=0.3, status="pass"),
                "naming": MetricScore(name="naming", score=comparison_result_dict["naming_score"] / 100.0, weight=0.2, status="pass"),
                "syntax": MetricScore(name="syntax", score=comparison_result_dict["syntax_score"] / 100.0, weight=0.3, status="pass"),
                "completeness": MetricScore(name="completeness", score=comparison_result_dict["completeness_score"] / 100.0, weight=0.2, status="pass"),
            },
            stats={
                "generated_lines": comparison_result_dict.get("generated_lines", 0),
                "reference_lines": comparison_result_dict.get("standard_lines", 0),
            },
            issues=comparison_result_dict.get("diffs", []),
            generated_path=str(tc_result_dir / "generated.ttcn"),
            reference_path=str(tc_result_dir / "standard.ttcn"),
        )

        # 生成文本报告
        text_generator = TextReportGenerator()
        text_output_path = tc_result_dir / "comparison_report.txt"
        text_generator.generate(report, text_output_path, ReportFormat.TEXT)

        # 生成 JSON 摘要
        json_output_path = tc_result_dir / "summary.json"
        with open(json_output_path, "w", encoding="utf-8") as f:
            json.dump(comparison_result_dict, f, indent=2, ensure_ascii=False)

        outputs = {
            "text_report": text_output_path,
            "json_summary": json_output_path,
        }

        result["steps"]["report_generation"] = {
            "success": True,
            "outputs": {k: str(v) for k, v in outputs.items()},
        }
        log(f"报告已生成: {tc_result_dir}", "SUCCESS")
    except Exception as e:
        log(f"报告生成失败: {e}", "ERROR")
        result["steps"]["report_generation"] = {
            "success": False,
            "error": str(e),
        }

    # 保存实验结果元数据
    result["success"] = True
    result["elapsed_time"] = time.time() - start_time
    save_json(result, tc_result_dir / "experiment_result.json")

    log(f"\n{'=' * 60}")
    log(f"实验完成: {tc_id}")
    log(f"结果目录: {tc_result_dir}")
    log(f"总分: {comparison_result_dict['score']:.1f}")
    log(f"耗时: {result['elapsed_time']:.1f}s")
    log(f"{'=' * 60}")

    return result


def run_batch_experiments(
    tc_ids: List[str],
    config: Dict[str, Any],
    overwrite_existing: bool = False,
) -> Dict[str, Any]:
    """运行批量测试用例实验

    Args:
        tc_ids: 测试用例 ID 列表
        config: 配置字典
        overwrite_existing: 覆盖已有文件

    Returns:
        批量实验结果
    """
    log(f"{'=' * 60}")
    log(f"开始批量实验: {len(tc_ids)} 个测试用例")
    log(f"{'=' * 60}")

    start_time = time.time()
    results = []
    comparison_results = []

    for i, tc_id in enumerate(tc_ids, 1):
        log(f"\n[{i}/{len(tc_ids)}] 处理测试用例: {tc_id}")

        try:
            result = run_single_experiment(
                tc_id=tc_id,
                config=config,
                overwrite_existing=overwrite_existing,
            )
            results.append(result)

            # 收集对比结果用于汇总
            tc_result_dir_str = result.get("output_dir")
            if tc_result_dir_str:
                tc_result_dir = Path(tc_result_dir_str)
                comp_path = tc_result_dir / "comparison_result.json"
                if comp_path.exists():
                    comparison_results.append(load_json(comp_path))

        except Exception as e:
            log(f"处理测试用例 {tc_id} 时出错: {e}", "ERROR")
            results.append({
                "tc_id": tc_id,
                "success": False,
                "error": str(e),
            })

    # 生成汇总报告
    elapsed = time.time() - start_time
    results_dir = Path(config.get("pipeline_config", {}).get("results_dir", str(RESULTS_DIR)))

    if comparison_results:
        # 使用 MarkdownReportGenerator 生成批量汇总
        batch_report = BatchReport(
            suite_name="TTCN-3 代码生成批量测试",
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            total_cases=len(comparison_results),
            passed=sum(1 for r in comparison_results if r.get("success", False)),
            warnings=sum(1 for r in comparison_results if not r.get("success", False) and not any(d.get("level") == "critical" for d in r.get("diffs", []))),
            failed=sum(1 for r in comparison_results if any(d.get("level") == "critical" for d in r.get("diffs", []))),
            overall_score=sum(r.get("score", 0) for r in comparison_results) / len(comparison_results) / 100.0 if comparison_results else 0,
            case_reports=[],
            aggregate_metrics={},
        )
        md_generator = MarkdownReportGenerator()
        summary_path = results_dir / "batch_summary.md"
        md_generator.generate(batch_report, summary_path, ReportFormat.MARKDOWN)
        log(f"批量汇总报告已保存: {summary_path}")

    # 汇总统计
    success_count = sum(1 for r in results if r.get("success", False))
    avg_score = sum(
        r.get("steps", {}).get("code_comparison", {}).get("score", 0)
        for r in results if r.get("success", False)
    ) / success_count if success_count > 0 else 0

    summary = {
        "total": len(tc_ids),
        "success": success_count,
        "failed": len(tc_ids) - success_count,
        "avg_score": avg_score,
        "elapsed_time": elapsed,
        "results": results,
    }

    # 保存批量实验结果
    save_json(summary, results_dir / "batch_results.json")

    log(f"\n{'=' * 60}")
    log(f"批量实验完成")
    log(f"总计: {len(tc_ids)}, 成功: {success_count}, 失败: {len(tc_ids) - success_count}")
    log(f"平均得分: {avg_score:.1f}")
    log(f"总耗时: {elapsed:.1f}s")
    log(f"结果目录: {results_dir}")
    log(f"{'=' * 60}")

    return summary


def compare_only(
    tc_id: str,
    generated_path: Path,
    standard_path: Path,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """仅运行代码对比

    Args:
        tc_id: 测试用例 ID
        generated_path: 生成的代码文件路径
        standard_path: 标准代码文件路径
        config: 配置字典

    Returns:
        对比结果
    """
    log(f"{'=' * 60}")
    log(f"仅运行代码对比: {tc_id}")
    log(f"{'=' * 60}")

    # 读取代码
    generated_code = generated_path.read_text(encoding="utf-8")
    standard_code = standard_path.read_text(encoding="utf-8")

    # 运行对比
    comparison_levels = config.get("pipeline_config", {}).get("comparison_levels", ["structure", "naming", "syntax", "completeness"])
    comparison_result = compare_codes(
        generated_code=generated_code,
        standard_code=standard_code,
        tc_id=tc_id,
        comparison_levels=comparison_levels,
    )

    # 保存结果
    results_dir = Path(config.get("pipeline_config", {}).get("results_dir", str(RESULTS_DIR)))
    tc_result_dir = results_dir / tc_id
    tc_result_dir.mkdir(parents=True, exist_ok=True)

    save_json(comparison_result.to_dict(), tc_result_dir / "comparison_result.json")

    log(f"\n{'=' * 60}")
    log(f"对比完成: {tc_id}")
    log(f"总分: {comparison_result.score:.1f}")
    log(f"结果目录: {tc_result_dir}")
    log(f"{'=' * 60}")

    return {
        "tc_id": tc_id,
        "success": True,
        "score": comparison_result.score,
        "result_dir": tc_result_dir,
    }


# ============================================================
# 主函数
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="TTCN-3 代码生成实验 Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 单个测试用例完整流程
  python run_experiment.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001

  # 跳过代码生成，使用已有代码
  python run_experiment.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001 --skip-generation

  # 批量测试
  python run_experiment.py --batch

  # 仅运行对比
  python run_experiment.py --compare-only \\
      --generated ./generated.ttcn \\
      --standard ./standard.ttcn \\
      --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001
        """,
    )

    # 模式选择
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--tc-id", type=str, help="单个测试用例 ID")
    mode_group.add_argument("--batch", action="store_true", help="运行批量测试")
    mode_group.add_argument("--compare-only", action="store_true", help="仅运行代码对比")

    # 通用参数
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    parser.add_argument("--results-dir", type=str, default=None, help="结果输出目录")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有文件")
    parser.add_argument("--skip-generation", action="store_true", help="跳过代码生成步骤")
    parser.add_argument("--skip-extraction", action="store_true", help="跳过标准提取步骤")

    # 对比专用参数
    parser.add_argument("--generated", type=str, help="生成的代码文件路径（用于 --compare-only）")
    parser.add_argument("--standard", type=str, help="标准代码文件路径（用于 --compare-only）")

    args = parser.parse_args()

    # 加载配置
    config = load_config(Path(args.config) if args.config else None)

    # 覆盖配置中的结果目录
    if args.results_dir:
        config.setdefault("pipeline_config", {})["results_dir"] = args.results_dir

    # 执行
    if args.compare_only:
        if not args.generated or not args.standard:
            parser.error("--compare-only 模式需要 --generated 和 --standard 参数")
        result = compare_only(
            tc_id=args.tc_id or "unknown",
            generated_path=Path(args.generated),
            standard_path=Path(args.standard),
            config=config,
        )
        return 0 if result.get("success") else 1

    elif args.batch:
        # 从配置或扫描结果目录获取测试用例列表
        tc_ids = []

        # 首先尝试从配置读取
        batch_config = config.get("batch_test", {})
        if batch_config.get("test_cases"):
            tc_ids = [tc.get("tc_id") for tc in batch_config["test_cases"] if tc.get("tc_id")]
        elif batch_config.get("enabled"):
            # 扫描结果目录获取已有测试用例
            results_dir = Path(config.get("pipeline_config", {}).get("results_dir", str(RESULTS_DIR)))
            if results_dir.exists():
                tc_ids = [d.name for d in results_dir.iterdir() if d.is_dir() and d.name.startswith("TC_")]

        if not tc_ids:
            log("未找到测试用例，请先在配置文件中添加 test_cases 或运行单个测试", "WARN")
            return 1

        summary = run_batch_experiments(
            tc_ids=tc_ids,
            config=config,
            overwrite_existing=args.overwrite,
        )
        return 0 if summary.get("success", 0) > 0 else 1

    else:
        # 单个测试用例
        result = run_single_experiment(
            tc_id=args.tc_id,
            config=config,
            skip_generation=args.skip_generation,
            skip_extraction=args.skip_extraction,
            overwrite_existing=args.overwrite,
        )
        return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
