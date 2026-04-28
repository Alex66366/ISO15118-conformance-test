#!/usr/bin/env python3
"""
代码生成模块
封装 ttcn3_generation_pipeline.py，提供统一的代码生成接口。

功能：
1. 调用已有的 ttcn3_generation_pipeline.py 生成 TTCN-3 代码
2. 支持缓存，避免重复生成
3. 支持从安全需求直接生成（自动查找或生成测试用例）
4. 支持从测试用例 ID、测试用例字典、测试用例文件生成
5. 支持多需求输入对应一个测试用例

输入模式:
  1. 安全需求ID: 从 generated_test_cases.json 查找，或调用测试用例生成器
  2. 测试用例ID: 从 generated_test_cases.json 查找
  3. 测试用例JSON: 直接传入测试用例字典
  4. 测试用例文件: 从JSON文件读取测试用例
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# === 路径常量 ===
AGENT_DIR = Path(__file__).resolve().parent.parent  # agent/
TEST_CODE_DIR = AGENT_DIR / "test_code"
GENERATED_DIR = TEST_CODE_DIR / "generated"
PIPELINE_SCRIPT = TEST_CODE_DIR / "ttcn3_generation_pipeline.py"
TEST_CASE_GENERATOR_SCRIPT = AGENT_DIR / "test_case" / "test_case_generator.py"
GENERATED_TEST_CASES_PATH = AGENT_DIR / "test_case" / "generated_test_cases.json"


def log(msg: str, level: str = "INFO") -> None:
    """统一日志输出"""
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] [CodeGenerator] {msg}")


def load_json(path: Path) -> Any:
    """加载 JSON 文件"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_tc_id(tc_id: str) -> Dict[str, str]:
    """解析 TC ID，提取组件信息

    Args:
        tc_id: 测试用例 ID，格式 TC_{SUT}_{Cat}_VTB_{Msg}_{Seq}

    Returns:
        包含解析结果的字典
    """
    import re
    pattern = r"TC_(SECC|EVCC)_(CMN|AC|DC)_VTB_(.+?)_(\d{3})"
    m = re.match(pattern, tc_id)
    if not m:
        return {
            "tc_id": tc_id,
            "sut_type": "SECC",
            "category": "CMN",
            "message_type": tc_id,
            "sequence_number": "001",
        }
    return {
        "tc_id": tc_id,
        "sut_type": m.group(1),
        "category": m.group(2),
        "message_type": m.group(3),
        "sequence_number": m.group(4),
    }


def find_test_case_in_generated_file(requirement_ids: List[str]) -> Tuple[Optional[Dict], List[str]]:
    """从 generated_test_cases.json 中查找匹配的测试用例

    支持一个测试用例对应多个安全需求。

    Args:
        requirement_ids: 安全需求 ID 列表

    Returns:
        (匹配的测试用例, 未匹配的需求ID列表)
    """
    if not GENERATED_TEST_CASES_PATH.exists():
        log(f"测试用例文件不存在: {GENERATED_TEST_CASES_PATH}", "WARN")
        return None, requirement_ids

    try:
        data = load_json(GENERATED_TEST_CASES_PATH)
        test_cases = data.get("test_cases", [])

        for tc_entry in test_cases:
            tc = tc_entry.get("Test_Case", {})
            doc_ref = tc.get("Document_Reference", {})
            requirements = doc_ref.get("Requirements", []) if isinstance(doc_ref, dict) else []

            # 提取需求ID（去除方括号）
            tc_req_ids = []
            for req in requirements:
                req_clean = req.strip("[]").strip()
                tc_req_ids.append(req_clean)

            # 检查是否所有输入的需求都在这个测试用例中
            if all(req_id in tc_req_ids for req_id in requirement_ids):
                return tc_entry, []

        # 没有找到完全匹配的，返回第一个部分匹配的
        for tc_entry in test_cases:
            tc = tc_entry.get("Test_Case", {})
            doc_ref = tc.get("Document_Reference", {})
            requirements = doc_ref.get("Requirements", []) if isinstance(doc_ref, dict) else []

            tc_req_ids = [req.strip("[]").strip() for req in requirements]
            if any(req_id in tc_req_ids for req_id in requirement_ids):
                matched = [r for r in requirement_ids if r in tc_req_ids]
                unmatched = [r for r in requirement_ids if r not in tc_req_ids]
                return tc_entry, unmatched

        return None, requirement_ids

    except Exception as e:
        log(f"读取测试用例文件失败: {e}", "ERROR")
        return None, requirement_ids


def find_test_case_by_tc_id(tc_id: str) -> Optional[Dict]:
    """从 generated_test_cases.json 中通过 TC ID 查找测试用例

    Args:
        tc_id: 测试用例 ID

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
        mode: 生成模式，"single" 或 "multi"

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
            timeout=600,
            cwd=str(AGENT_DIR),
        )

        if result.returncode != 0:
            log(f"测试用例生成器执行失败: {result.stderr[:500]}", "ERROR")
            return None

        # 查找最近生成的测试用例文件
        output_dir = AGENT_DIR / "test_case" / "output"
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
    matched_case, unmatched = find_test_case_in_generated_file(requirement_ids)

    if matched_case:
        log(f"找到匹配的测试用例: TC_ID={matched_case.get('Test_Case', {}).get('TC_Id', 'unknown')}")
        return [matched_case]

    # 未找到，尝试自动生成
    if auto_generate and unmatched:
        log(f"未找到匹配的测试用例，尝试生成: {unmatched}")
        mode = "multi" if multi_mode and len(unmatched) > 1 else "single"
        generated = call_test_case_generator(unmatched, mode=mode)

        if generated:
            test_cases = generated.get("test_cases", [])
            if test_cases:
                log(f"成功生成 {len(test_cases)} 个测试用例")
                return test_cases

    log(f"无法获取需求 {requirement_ids} 对应的测试用例", "WARN")
    return []


def generate_code_from_requirements(
    requirement_ids: List[str],
    sut_type: Optional[str] = None,
    use_cache: bool = True,
    overwrite_existing: bool = False,
    auto_generate: bool = True,
    multi_mode: bool = True,
) -> Tuple[bool, Optional[Path], str]:
    """通过安全需求 ID 直接生成 TTCN-3 代码

    工作流程:
    1. 从 generated_test_cases.json 查找匹配的测试用例
    2. 如果未找到，调用测试用例生成器生成
    3. 调用 ttcn3_generation_pipeline.py 生成代码

    Args:
        requirement_ids: 安全需求 ID 列表
        sut_type: SUT 类型
        use_cache: 是否使用缓存
        overwrite_existing: 是否覆盖已有文件
        auto_generate: 未找到时是否自动生成测试用例
        multi_mode: 多需求模式，True 表示合并为一个测试用例

    Returns:
        (success, output_path, message)
    """
    # 获取测试用例
    test_cases = get_test_cases_for_requirements(
        requirement_ids,
        auto_generate=auto_generate,
        multi_mode=multi_mode
    )

    if not test_cases:
        return False, None, f"无法获取需求 {requirement_ids} 对应的测试用例"

    # 取第一个测试用例生成代码
    test_case = test_cases[0]
    tc = test_case.get("Test_Case", test_case)
    tc_id = tc.get("TC_Id", "unknown")

    return generate_code_from_test_case(
        test_case,
        sut_type=sut_type,
        use_cache=use_cache,
        overwrite_existing=overwrite_existing,
    )


def generate_code_from_test_case(
    test_case: Dict,
    sut_type: Optional[str] = None,
    use_cache: bool = True,
    overwrite_existing: bool = False,
) -> Tuple[bool, Optional[Path], str]:
    """通过测试用例字典生成 TTCN-3 代码

    Args:
        test_case: 测试用例字典
        sut_type: SUT 类型
        use_cache: 是否使用缓存
        overwrite_existing: 是否覆盖已有文件

    Returns:
        (success, output_path, message)
    """
    tc = test_case.get("Test_Case", test_case)
    tc_id = tc.get("TC_Id", "unknown")

    # 确定输出文件名
    output_filename = f"{tc_id}.ttcn"
    output_path = GENERATED_DIR / output_filename

    # 检查缓存
    if use_cache and output_path.exists() and not overwrite_existing:
        log(f"使用缓存文件: {output_path}")
        return True, output_path, "使用缓存"

    # 确保输出目录存在
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    log(f"开始生成代码: TC_ID={tc_id}, SUT={sut_type or 'auto'}")
    start_time = time.time()

    try:
        # 调用 ttcn3_generation_pipeline.py
        cmd = [
            sys.executable,
            str(PIPELINE_SCRIPT),
            "--test-case",
            json.dumps(test_case, ensure_ascii=False),
        ]
        if sut_type:
            cmd.extend(["--sut", sut_type])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(AGENT_DIR),
        )

        elapsed = time.time() - start_time
        log(f"流水线执行完成，耗时 {elapsed:.1f}s")

        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown error"
            log(f"流水线执行失败: {error_msg[:500]}", "ERROR")
            return False, None, f"流水线执行失败: {error_msg[:200]}"

        # 检查生成的文件
        if output_path.exists():
            file_size = output_path.stat().st_size
            log(f"代码生成成功: {output_path} ({file_size} bytes)")
            return True, output_path, f"生成成功 ({elapsed:.1f}s)"

        # 可能文件名不同，检查 generated 目录
        generated_files = list(GENERATED_DIR.glob(f"{tc_id}*.ttcn"))
        if generated_files:
            output_path = generated_files[0]
            file_size = output_path.stat().st_size
            log(f"代码生成成功（文件名略有不同）: {output_path}")
            return True, output_path, f"生成成功 ({elapsed:.1f}s)"

        log(f"未找到生成的文件，搜索最近修改的文件...", "WARN")
        recent_files = list(GENERATED_DIR.glob("*.ttcn"))
        if recent_files:
            recent = max(recent_files, key=lambda p: p.stat().st_mtime)
            log(f"使用最近生成的文件: {recent}")
            return True, recent, f"生成成功，使用最近文件 ({elapsed:.1f}s)"

        log(f"流水线执行但未找到输出文件", "ERROR")
        return False, None, "流水线执行但未找到输出文件"

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start_time
        log(f"流水线执行超时（{elapsed:.1f}s）", "ERROR")
        return False, None, "流水线执行超时"
    except Exception as e:
        elapsed = time.time() - start_time
        log(f"生成过程异常: {e}", "ERROR")
        return False, None, f"异常: {str(e)}"


def generate_code_from_tc_id(
    tc_id: str,
    sut_type: Optional[str] = None,
    use_cache: bool = True,
    overwrite_existing: bool = False,
) -> Tuple[bool, Optional[Path], str]:
    """通过 TC ID 直接生成 TTCN-3 代码

    工作流程:
    1. 从 generated_test_cases.json 查找测试用例
    2. 调用 ttcn3_generation_pipeline.py 生成代码

    Args:
        tc_id: 测试用例 ID
        sut_type: SUT 类型
        use_cache: 是否使用缓存
        overwrite_existing: 是否覆盖已有文件

    Returns:
        (success, output_path, message)
    """
    tc_info = parse_tc_id(tc_id)
    if not sut_type:
        sut_type = tc_info["sut_type"]

    # 确定输出文件名
    output_filename = f"{tc_id}.ttcn"
    output_path = GENERATED_DIR / output_filename

    # 检查缓存
    if use_cache and output_path.exists() and not overwrite_existing:
        log(f"使用缓存文件: {output_path}")
        return True, output_path, "使用缓存"

    # 确保输出目录存在
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    # 从 generated_test_cases.json 获取测试用例
    test_case = find_test_case_by_tc_id(tc_id)
    if not test_case:
        log(f"未找到测试用例: {tc_id}", "ERROR")
        return False, None, f"未找到测试用例: {tc_id}"

    tc = test_case.get("Test_Case", test_case)
    log(f"找到测试用例: TC_ID={tc_id}")

    return generate_code_from_test_case(
        test_case,
        sut_type=sut_type,
        use_cache=use_cache,
        overwrite_existing=overwrite_existing,
    )


def generate_code_from_test_case_file(
    file_path: Union[str, Path],
    sut_type: Optional[str] = None,
    use_cache: bool = True,
    overwrite_existing: bool = False,
) -> Tuple[bool, Optional[Path], str]:
    """从测试用例文件生成 TTCN-3 代码

    Args:
        file_path: 测试用例文件路径
        sut_type: SUT 类型
        use_cache: 是否使用缓存
        overwrite_existing: 是否覆盖已有文件

    Returns:
        (success, output_path, message)
    """
    file_path = Path(file_path)
    if not file_path.exists():
        log(f"测试用例文件不存在: {file_path}", "ERROR")
        return False, None, f"文件不存在: {file_path}"

    try:
        test_case = load_json(file_path)
    except Exception as e:
        log(f"读取测试用例文件失败: {e}", "ERROR")
        return False, None, f"读取失败: {e}"

    tc = test_case.get("Test_Case", test_case)
    tc_id = tc.get("TC_Id", file_path.stem)

    return generate_code_from_test_case(
        test_case,
        sut_type=sut_type,
        use_cache=use_cache,
        overwrite_existing=overwrite_existing,
    )


def generate_batch(
    requirement_ids_list: List[List[str]] = None,
    tc_ids: List[str] = None,
    sut_type: Optional[str] = None,
    use_cache: bool = True,
    overwrite_existing: bool = False,
    auto_generate: bool = True,
    multi_mode: bool = True,
) -> Dict[str, Any]:
    """批量生成多个测试用例的代码

    Args:
        requirement_ids_list: 需求 ID 列表的列表，每个内层列表对应一个测试用例
        tc_ids: 测试用例 ID 列表
        sut_type: SUT 类型
        use_cache: 是否使用缓存
        overwrite_existing: 是否覆盖已有文件
        auto_generate: 未找到时是否自动生成测试用例
        multi_mode: 多需求模式

    Returns:
        包含生成结果的字典
    """
    results = {
        "total": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "details": [],
    }

    # 处理需求 ID 列表
    if requirement_ids_list:
        for req_ids in requirement_ids_list:
            log(f"\n{'=' * 50}")
            log(f"处理需求: {req_ids}")

            success, output_path, message = generate_code_from_requirements(
                req_ids,
                sut_type=sut_type,
                use_cache=use_cache,
                overwrite_existing=overwrite_existing,
                auto_generate=auto_generate,
                multi_mode=multi_mode,
            )

            detail = {
                "type": "requirements",
                "input": req_ids,
                "success": success,
                "output_path": str(output_path) if output_path else None,
                "message": message,
            }
            results["details"].append(detail)
            results["total"] += 1

            if success:
                results["success"] += 1
            else:
                results["failed"] += 1

    # 处理测试用例 ID 列表
    if tc_ids:
        for tc_id in tc_ids:
            log(f"\n{'=' * 50}")
            log(f"处理测试用例: {tc_id}")

            success, output_path, message = generate_code_from_tc_id(
                tc_id,
                sut_type=sut_type,
                use_cache=use_cache,
                overwrite_existing=overwrite_existing,
            )

            detail = {
                "type": "tc_id",
                "input": tc_id,
                "success": success,
                "output_path": str(output_path) if output_path else None,
                "message": message,
            }
            results["details"].append(detail)
            results["total"] += 1

            if success:
                results["success"] += 1
            else:
                results["failed"] += 1

    log(f"\n{'=' * 50}")
    log(f"批量生成完成: 成功={results['success']}, 失败={results['failed']}, 总计={results['total']}")

    return results


def get_generated_code(tc_id: str) -> Optional[str]:
    """获取已生成的代码内容

    Args:
        tc_id: 测试用例 ID

    Returns:
        代码内容，失败返回 None
    """
    output_path = GENERATED_DIR / f"{tc_id}.ttcn"
    if not output_path.exists():
        # 尝试查找任何匹配的文件
        matches = list(GENERATED_DIR.glob(f"*{tc_id}*.ttcn"))
        if matches:
            output_path = matches[0]
        else:
            return None

    try:
        return output_path.read_text(encoding="utf-8")
    except Exception:
        return None


# ============================================================
# 单元测试（直接运行时执行）
# ============================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="TTCN-3 代码生成模块\n\n"
                    "支持多种输入模式:\n"
                    "  1. 安全需求: --requirement 或 --requirements\n"
                    "  2. 测试用例ID: --tc-id\n"
                    "  3. 测试用例文件: --test-case-file"
    )

    # 输入模式
    input_group = parser.add_argument_group("输入模式 (选择其中一种)")
    input_group.add_argument("--requirement", type=str, help="单个安全需求 ID")
    input_group.add_argument("--requirements", type=str, help="多个安全需求 ID，逗号分隔")
    input_group.add_argument("--tc-id", type=str, help="测试用例 ID")
    input_group.add_argument("--test-case-file", type=str, help="测试用例文件路径")

    # 配置选项
    config_group = parser.add_argument_group("配置选项")
    config_group.add_argument("--sut", type=str, choices=["SECC", "EVCC"], help="SUT 类型")
    config_group.add_argument("--no-cache", action="store_true", help="禁用缓存")
    config_group.add_argument("--overwrite", action="store_true", help="覆盖已有文件")
    config_group.add_argument("--no-auto-generate", action="store_true", help="未找到时不自动生成")
    config_group.add_argument("--single-mode", action="store_true", help="多需求模式单独生成")

    args = parser.parse_args()

    use_cache = not args.no_cache
    auto_generate = not args.no_auto_generate
    multi_mode = not args.single_mode

    # 处理输入
    if args.requirement:
        req_ids = [args.requirement.strip()]
        success, path, msg = generate_code_from_requirements(
            req_ids,
            sut_type=args.sut,
            use_cache=use_cache,
            overwrite_existing=args.overwrite,
            auto_generate=auto_generate,
            multi_mode=multi_mode,
        )
        print(f"\n结果: {'成功' if success else '失败'}")
        print(f"路径: {path}")
        print(f"信息: {msg}")

    elif args.requirements:
        req_ids = [r.strip() for r in args.requirements.split(",") if r.strip()]
        success, path, msg = generate_code_from_requirements(
            req_ids,
            sut_type=args.sut,
            use_cache=use_cache,
            overwrite_existing=args.overwrite,
            auto_generate=auto_generate,
            multi_mode=multi_mode,
        )
        print(f"\n结果: {'成功' if success else '失败'}")
        print(f"路径: {path}")
        print(f"信息: {msg}")

    elif args.tc_id:
        success, path, msg = generate_code_from_tc_id(
            args.tc_id,
            sut_type=args.sut,
            use_cache=use_cache,
            overwrite_existing=args.overwrite,
        )
        print(f"\n结果: {'成功' if success else '失败'}")
        print(f"路径: {path}")
        print(f"信息: {msg}")

    elif args.test_case_file:
        success, path, msg = generate_code_from_test_case_file(
            args.test_case_file,
            sut_type=args.sut,
            use_cache=use_cache,
            overwrite_existing=args.overwrite,
        )
        print(f"\n结果: {'成功' if success else '失败'}")
        print(f"路径: {path}")
        print(f"信息: {msg}")

    else:
        parser.print_help()
        print("\n\n示例用法:")
        print("  # 通过安全需求生成")
        print("  python code_generator.py --requirement V2G2-082")
        print("  python code_generator.py --requirements V2G2-082,V2G2-165")
        print()
        print("  # 通过测试用例ID生成")
        print("  python code_generator.py --tc-id TC_SECC_CMN_VTB_SupportedAppProtocol_001")
        print()
        print("  # 通过测试用例文件生成")
        print("  python code_generator.py --test-case-file ./my_test_case.json")
