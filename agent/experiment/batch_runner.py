#!/usr/bin/env python3
"""
批量测试运行脚本
提供便捷的命令行界面来运行和管理批量测试

功能：
- 支持配置文件定义测试用例
- 支持并行/串行执行
- 支持进度显示
- 支持结果缓存和增量运行
- 支持结果导出

用法:
    # 运行所有测试
    python batch_runner.py --run-all
    
    # 运行指定测试套件
    python batch_runner.py --suite suite_communication_setup
    
    # 运行指定测试用例
    python batch_runner.py --tc-ids TC_SECC_CMN_VTB_SupportedAppProtocol_001,TC_SECC_CMN_VTB_V2GTPHeader_001
    
    # 增量运行（跳过已成功的测试）
    python batch_runner.py --run-all --incremental
    
    # 并行执行
    python batch_runner.py --run-all --parallel --workers 4
    
    # 查看状态
    python batch_runner.py --status
    
    # 导出结果
    python batch_runner.py --export ./results.csv
"""

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# 路径配置
# ============================================================
AGENT_DIR = Path(__file__).resolve().parent.parent
EXPERIMENT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = EXPERIMENT_DIR / "test_cases_config.json"
STATE_FILE = EXPERIMENT_DIR / ".batch_runner_state.json"


# ============================================================
# 状态管理
# ============================================================
@dataclass
class TestState:
    """测试状态"""
    tc_id: str
    status: str  # pending, running, success, failed, skipped
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    similarity: float = 0.0
    error: str = ""
    last_run: str = ""


@dataclass
class BatchRunnerState:
    """批量运行器状态"""
    suite_name: str
    last_run: str
    tests: Dict[str, TestState] = field(default_factory=dict)
    
    def save(self):
        """保存状态"""
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "suite_name": self.suite_name,
                "last_run": self.last_run,
                "tests": {
                    tc_id: {
                        "tc_id": state.tc_id,
                        "status": state.status,
                        "start_time": state.start_time,
                        "end_time": state.end_time,
                        "similarity": state.similarity,
                        "error": state.error,
                        "last_run": state.last_run,
                    }
                    for tc_id, state in self.tests.items()
                }
            }, f, indent=2, ensure_ascii=False)
    
    @classmethod
    def load(cls) -> Optional["BatchRunnerState"]:
        """加载状态"""
        if not STATE_FILE.exists():
            return None
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            tests = {
                tc_id: TestState(**test_data)
                for tc_id, test_data in data.get("tests", {}).items()
            }
            return cls(
                suite_name=data.get("suite_name", ""),
                last_run=data.get("last_run", ""),
                tests=tests,
            )
        except Exception:
            return None


# ============================================================
# 配置加载
# ============================================================
def load_config() -> Dict:
    """加载配置"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_test_cases(config: Dict, suite_id: Optional[str] = None, 
                  tc_ids: Optional[List[str]] = None) -> List[Dict]:
    """获取测试用例列表"""
    if tc_ids:
        return [tc for tc in config.get("single_test_cases", []) 
                if tc["tc_id"] in tc_ids]
    elif suite_id:
        for suite in config.get("batch_test_suites", []):
            if suite.get("suite_id") == suite_id:
                tc_id_list = suite.get("tc_ids", [])
                return [tc for tc in config.get("single_test_cases", [])
                        if tc["tc_id"] in tc_id_list]
    else:
        return config.get("single_test_cases", [])
    
    return []


# ============================================================
# 测试执行
# ============================================================
class TestRunner:
    """测试运行器"""
    
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
    
    def run_single(self, tc_id: str, skip_generation: bool = False) -> Dict[str, Any]:
        """运行单个测试"""
        # 调用 evaluation_pipeline
        import subprocess
        
        cmd = [
            sys.executable,
            str(EXPERIMENT_DIR / "evaluation_pipeline.py"),
            "--tc-id", tc_id,
            "--skip-generation" if skip_generation else "",
        ]
        cmd = [c for c in cmd if c]  # 移除空字符串
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 分钟超时
            )
            
            # 查找对比结果
            comparison_file = EXPERIMENT_DIR / "comparison_results" / f"{tc_id}_comparison.json"
            
            if comparison_file.exists():
                with open(comparison_file, "r", encoding="utf-8") as f:
                    comparison = json.load(f)
                return {
                    "tc_id": tc_id,
                    "success": True,
                    "similarity": comparison.get("overall_similarity", 0),
                    "status": comparison.get("status", "unknown"),
                    "metrics": comparison.get("metrics", {}),
                    "issues": comparison.get("issues", []),
                }
            else:
                return {
                    "tc_id": tc_id,
                    "success": False,
                    "error": "对比结果文件不存在",
                }
                
        except subprocess.TimeoutExpired:
            return {
                "tc_id": tc_id,
                "success": False,
                "error": "执行超时",
            }
        except Exception as e:
            return {
                "tc_id": tc_id,
                "success": False,
                "error": str(e),
            }
    
    def run_batch(self, tc_ids: List[str], parallel: bool = False, 
                  workers: int = 4, incremental: bool = False,
                  suite_name: str = "Batch Test") -> List[Dict]:
        """批量运行测试"""
        
        # 加载状态
        state = BatchRunnerState.load()
        if state and state.suite_name != suite_name:
            state = BatchRunnerState(suite_name=suite_name, last_run="")
        
        # 过滤待运行的测试
        tests_to_run = []
        for tc_id in tc_ids:
            if incremental and state and tc_id in state.tests:
                test_state = state.tests[tc_id]
                if test_state.status == "success":
                    print(f"跳过已成功的测试: {tc_id}")
                    tests_to_run.append({
                        "tc_id": tc_id,
                        "status": "skipped",
                        "similarity": test_state.similarity,
                    })
                    continue
            tests_to_run.append(tc_id)
        
        # 显示进度
        total = len(tests_to_run)
        results = []
        completed = 0
        
        print(f"\n开始批量测试: {total} 个测试用例")
        print("=" * 60)
        
        if parallel and workers > 1:
            # 并行执行
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(self.run_single, tc_id): tc_id 
                    for tc_id in tests_to_run if isinstance(tc_id, str)
                }
                
                for future in as_completed(futures):
                    tc_id = futures[future]
                    completed += 1
                    
                    try:
                        result = future.result()
                        results.append(result)
                        
                        status_icon = "✓" if result.get("success") else "✗"
                        similarity = result.get("similarity", 0)
                        
                        print(f"[{completed}/{total}] {status_icon} {tc_id}: {similarity:.1%}")
                        
                        if self.verbose and result.get("error"):
                            print(f"    错误: {result['error']}")
                            
                    except Exception as e:
                        results.append({"tc_id": tc_id, "success": False, "error": str(e)})
                        print(f"[{completed}/{total}] ✗ {tc_id}: {e}")
        else:
            # 串行执行
            for item in tests_to_run:
                if isinstance(item, dict):  # 跳过的测试
                    results.append(item)
                    continue
                
                tc_id = item
                completed += 1
                
                print(f"[{completed}/{total}] 运行 {tc_id}...", end=" ")
                sys.stdout.flush()
                
                result = self.run_single(tc_id)
                results.append(result)
                
                status_icon = "✓" if result.get("success") else "✗"
                similarity = result.get("similarity", 0)
                
                print(f"{status_icon} {similarity:.1%}")
                
                if self.verbose and result.get("error"):
                    print(f"    错误: {result['error']}")
        
        print("=" * 60)
        
        # 更新并保存状态
        state = BatchRunnerState(
            suite_name=suite_name,
            last_run=datetime.now().isoformat(),
        )
        for result in results:
            tc_id = result["tc_id"]
            state.tests[tc_id] = TestState(
                tc_id=tc_id,
                status="success" if result.get("success") else "failed",
                similarity=result.get("similarity", 0),
                error=result.get("error", ""),
                last_run=datetime.now().isoformat(),
            )
        state.save()
        
        return results


# ============================================================
# 状态显示
# ============================================================
def show_status():
    """显示状态"""
    state = BatchRunnerState.load()
    
    if not state:
        print("没有运行记录")
        return
    
    print(f"\n批量测试状态: {state.suite_name}")
    print(f"最后运行: {state.last_run}")
    print(f"\n测试用例数量: {len(state.tests)}")
    
    # 统计
    success = sum(1 for t in state.tests.values() if t.status == "success")
    failed = sum(1 for t in state.tests.values() if t.status == "failed")
    running = sum(1 for t in state.tests.values() if t.status == "running")
    
    print(f"  成功: {success}")
    print(f"  失败: {failed}")
    print(f"  运行中: {running}")
    
    # 列出测试
    print(f"\n测试用例详情:")
    print(f"{'TC ID':<50} {'状态':<12} {'相似度':<10} {'最后运行'}")
    print("-" * 90)
    
    for tc_id, test_state in sorted(state.tests.items()):
        status_icon = {"success": "✓", "failed": "✗", "running": "⟳"}.get(test_state.status, "?")
        print(f"{tc_id:<50} {status_icon} {test_state.status:<10} {test_state.similarity:.1%}    {test_state.last_run}")


# ============================================================
# 结果导出
# ============================================================
def export_results(output_path: str, format: str = "json"):
    """导出结果"""
    state = BatchRunnerState.load()
    
    if not state:
        print("没有可导出的结果")
        return
    
    results = [
        {
            "tc_id": tc_id,
            "status": test_state.status,
            "similarity": test_state.similarity,
            "error": test_state.error,
            "last_run": test_state.last_run,
        }
        for tc_id, test_state in state.tests.items()
    ]
    
    if format == "json":
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump({
                "suite_name": state.suite_name,
                "last_run": state.last_run,
                "results": results,
            }, f, indent=2, ensure_ascii=False)
    elif format == "csv":
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["tc_id", "status", "similarity", "error", "last_run"])
            writer.writeheader()
            writer.writerows(results)
    
    print(f"结果已导出: {output_path}")


# ============================================================
# 主函数
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="批量测试运行脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    # 运行模式
    mode_group = parser.add_argument_group("运行模式")
    mode_group.add_argument("--run-all", action="store_true", help="运行所有配置的测试用例")
    mode_group.add_argument("--suite", type=str, help="运行指定测试套件")
    mode_group.add_argument("--tc-ids", type=str, help="指定测试用例 ID（逗号分隔）")
    mode_group.add_argument("--incremental", action="store_true", help="增量运行（跳过已成功的测试）")
    
    # 执行选项
    exec_group = parser.add_argument_group("执行选项")
    exec_group.add_argument("--parallel", action="store_true", help="并行执行")
    exec_group.add_argument("--workers", type=int, default=4, help="并行工作线程数")
    exec_group.add_argument("--verbose", action="store_true", help="详细输出")
    
    # 其他选项
    other_group = parser.add_argument_group("其他选项")
    other_group.add_argument("--status", action="store_true", help="显示运行状态")
    other_group.add_argument("--export", type=str, help="导出结果到文件")
    other_group.add_argument("--export-format", type=str, choices=["json", "csv"], default="json",
                           help="导出格式")
    
    args = parser.parse_args()
    
    if args.status:
        show_status()
        return
    
    if args.export:
        export_results(args.export, args.export_format)
        return
    
    if not any([args.run_all, args.suite, args.tc_ids]):
        parser.print_help()
        print("\n请指定 --run-all, --suite, 或 --tc-ids")
        return
    
    # 加载配置
    config = load_config()
    
    # 获取测试用例列表
    if args.run_all:
        test_cases = get_test_cases(config)
        suite_name = "All Tests"
    elif args.suite:
        test_cases = get_test_cases(config, suite_id=args.suite)
        suite_name = args.suite
    elif args.tc_ids:
        tc_id_list = [t.strip() for t in args.tc_ids.split(",")]
        test_cases = get_test_cases(config, tc_ids=tc_id_list)
        suite_name = "Custom List"
    else:
        return
    
    tc_ids = [tc["tc_id"] for tc in test_cases]
    
    if not tc_ids:
        print("没有找到测试用例")
        return
    
    # 运行测试
    runner = TestRunner(verbose=args.verbose)
    results = runner.run_batch(
        tc_ids=tc_ids,
        parallel=args.parallel,
        workers=args.workers,
        incremental=args.incremental,
        suite_name=suite_name,
    )
    
    # 打印摘要
    success_count = sum(1 for r in results if r.get("success"))
    avg_similarity = 0.0
    if results:
        similarities = [r.get("similarity", 0) for r in results if r.get("success")]
        if similarities:
            avg_similarity = sum(similarities) / len(similarities)
    
    print(f"\n批量测试摘要:")
    print(f"  总计: {len(results)}")
    print(f"  成功: {success_count}")
    print(f"  失败: {len(results) - success_count}")
    print(f"  平均相似度: {avg_similarity:.2%}")


if __name__ == "__main__":
    main()
