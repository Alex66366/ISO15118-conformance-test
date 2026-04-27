#!/usr/bin/env python
"""
快速集成C++测试程序到TestUI的辅助脚本

使用方法:
    python integrate_test_program.py <测试程序路径> <测试用例名称> [选项]

示例:
    python integrate_test_program.py ./build/my_test MyTest_001
    python integrate_test_program.py ./build/my_test MyTest_001 --condition "测试条件" --expected "期望结果"
"""

import os
import sys
import argparse
import django

# 设置Django环境
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'testui.settings')
django.setup()

from testcase.models import TestcaseType, MessageType


def main():
    parser = argparse.ArgumentParser(
        description='将C++测试程序集成到TestUI'
    )
    parser.add_argument(
        'program_path',
        help='测试程序的可执行文件路径'
    )
    parser.add_argument(
        'testcase_name',
        help='测试用例名称（如：TC_SECC_CMN_VTB_Authorization_001）'
    )
    parser.add_argument(
        '--type',
        choices=['CMN', 'AC', 'DC'],
        default='CMN',
        help='测试用例类型'
    )
    parser.add_argument(
        '--condition',
        default='',
        help='测试条件描述'
    )
    parser.add_argument(
        '--expected',
        default='',
        help='期望结果描述'
    )
    parser.add_argument(
        '--pics',
        default='{}',
        help='PICS配置（JSON格式）'
    )
    parser.add_argument(
        '--pixit',
        default='{}',
        help='PIXIT配置（JSON格式）'
    )
    parser.add_argument(
        '--build-status',
        choices=['NA', 'OK'],
        default='OK',
        help='构建状态'
    )
    parser.add_argument(
        '--msg-type',
        default=None,
        help='消息类型名称'
    )
    parser.add_argument(
        '--overwrite',
        action='store_true',
        help='如果测试用例已存在，是否覆盖'
    )
    
    args = parser.parse_args()
    
    # 检查程序文件是否存在
    if not os.path.isfile(args.program_path):
        print(f"❌ 错误: 测试程序文件不存在: {args.program_path}")
        return 1
    
    # 获取绝对路径
    program_path = os.path.abspath(args.program_path)
    
    # 检查文件是否可执行（在Unix系统上）
    if os.name != 'nt':
        if not os.access(program_path, os.X_OK):
            print(f"⚠️  警告: 文件没有执行权限")
            print(f"   运行: chmod +x {program_path}")
    
    # 处理消息类型
    msg_type = None
    if args.msg_type:
        msg_type, _ = MessageType.objects.get_or_create(name=args.msg_type)
    
    # 检查测试用例是否已存在
    try:
        existing_tc = TestcaseType.objects.get(name=args.testcase_name)
        if not args.overwrite:
            print(f"❌ 错误: 测试用例已存在: {args.testcase_name}")
            print(f"   使用 --overwrite 选项来覆盖")
            return 1
        else:
            print(f"ℹ️  更新已存在的测试用例: {args.testcase_name}")
            existing_tc.path = program_path
            existing_tc.type = args.type
            existing_tc.condition = args.condition
            existing_tc.expected = args.expected
            existing_tc.pics = args.pics
            existing_tc.pixit = args.pixit
            existing_tc.build_status = args.build_status
            existing_tc.msg_type = msg_type
            existing_tc.save()
            print(f"✅ 测试用例更新成功!")
            return 0
    except TestcaseType.DoesNotExist:
        pass
    
    # 创建新的测试用例
    try:
        tc = TestcaseType.objects.create(
            name=args.testcase_name,
            type=args.type,
            status='n',
            build_status=args.build_status,
            condition=args.condition,
            expected=args.expected,
            pics=args.pics,
            pixit=args.pixit,
            path=program_path,
            msg_type=msg_type
        )
        print(f"✅ 测试用例创建成功!")
        print(f"\n详细信息:")
        print(f"  名称: {tc.name}")
        print(f"  类型: {tc.type}")
        print(f"  路径: {tc.path}")
        print(f"  状态: {tc.status}")
        print(f"  构建状态: {tc.build_status}")
        return 0
    except Exception as e:
        print(f"❌ 错误: 创建测试用例失败: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())

