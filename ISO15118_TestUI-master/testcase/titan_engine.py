import os
import uuid
import json
from datetime import datetime
from django.conf import settings
from .ssh_client import get_ssh_client


class TitanEngine:
    """TTCN-3 Titan引擎控制器"""

    def __init__(self):
        self.ssh_client = get_ssh_client()
        self.workspace = settings.TITAN_WORKSPACE
        self.compiler = settings.TITAN_COMPILER
        self.executor = settings.TITAN_EXECUTOR

    def _generate_project_name(self):
        """生成唯一的项目名称"""
        return f"ttcn_project_{uuid.uuid4().hex[:8]}"

    def create_project(self, project_name=None):
        """
        在远程服务器上创建TTCN-3项目目录

        Args:
            project_name: 项目名称（可选）

        Returns:
            str: 项目路径
        """
        if project_name is None:
            project_name = self._generate_project_name()

        remote_path = os.path.join(self.workspace, project_name)

        command = f"mkdir -p {remote_path} && echo {remote_path}"
        result = self.ssh_client.execute_command(command)

        if result['exit_code'] == 0:
            return remote_path.strip()
        return None

    def compile_code(self, code, project_name=None, main_module=None):
        """
        编译TTCN-3代码

        Args:
            code: TTCN-3源代码
            project_name: 项目名称
            main_module: 主模块名称（不含.ttcn扩展名）

        Returns:
            dict: {
                'success': bool,
                'output': str,
                'errors': str,
                'project_path': str
            }
        """
        if project_name is None:
            project_name = self._generate_project_name()

        if main_module is None:
            main_module = "TestSuite"

        remote_path = self.create_project(project_name)
        if not remote_path:
            return {
                'success': False,
                'output': '',
                'errors': 'Failed to create project directory',
                'project_path': None
            }

        # 保存TTCN-3源文件
        remote_file = os.path.join(remote_path, f"{main_module}.ttcn")
        write_command = f"cat > {remote_file} << 'EOF'\n{code}\nEOF"

        result = self.ssh_client.execute_command(write_command)
        if result['exit_code'] != 0:
            return {
                'success': False,
                'output': '',
                'errors': f'Failed to write source file: {result["stderr"]}',
                'project_path': remote_path
            }

        # 执行编译
        compile_command = f"cd {remote_path} && {self.compiler} {main_module}.ttcn 2>&1"
        compile_result = self.ssh_client.execute_command(compile_command, timeout=600)

        success = compile_result['exit_code'] == 0

        return {
            'success': success,
            'output': compile_result['stdout'],
            'errors': compile_result['stderr'],
            'project_path': remote_path,
            'main_module': main_module
        }

    def execute_test(self, project_path, main_module, config_file=None, secc_ip="ff02::1"):
        """
        执行TTCN-3测试用例

        Args:
            project_path: 项目路径
            main_module: 主模块名称
            config_file: 配置文件路径（可选）
            secc_ip: SECC IP地址

        Returns:
            dict: {
                'success': bool,
                'output': str,
                'errors': str,
                'exit_code': int,
                'result': str (pass/fail/error/inconclusive)
            }
        """
        # 默认配置文件
        if config_file is None:
            config_file = f"{project_path}/.cfg"

        # 构建执行命令
        execute_command = f"cd {project_path} && {self.executor} {main_module} {config_file} {secc_ip} 2>&1"

        # 执行测试
        exec_result = self.ssh_client.execute_command(execute_command, timeout=600)

        # 解析结果
        result_map = {
            0: 'none',
            1: 'pass',
            2: 'inconclusive',
            3: 'fail',
            4: 'error'
        }

        exit_code = exec_result['exit_code']
        test_result = result_map.get(exit_code, 'unknown')

        return {
            'success': exit_code in [0, 1],  # 0=none, 1=pass considered success
            'output': exec_result['stdout'],
            'errors': exec_result['stderr'],
            'exit_code': exit_code,
            'result': test_result
        }

    def get_test_result(self, project_path):
        """
        获取测试执行结果

        Args:
            project_path: 项目路径

        Returns:
            dict: 测试结果信息
        """
        # 查找日志文件
        log_command = f"ls -la {project_path}/"
        result = self.ssh_client.execute_command(log_command)

        return {
            'files': result['stdout'],
            'project_path': project_path
        }

    def cleanup_project(self, project_path):
        """
        清理项目目录

        Args:
            project_path: 项目路径

        Returns:
            bool: 是否成功
        """
        command = f"rm -rf {project_path}"
        result = self.ssh_client.execute_command(command)
        return result['exit_code'] == 0

    def check_connection(self):
        """
        检查SSH连接和Titan环境

        Returns:
            dict: {
                'connected': bool,
                'titan_available': bool,
                'message': str
            }
        }
        """
        # 检查SSH连接
        if not self.ssh_client.connect():
            return {
                'connected': False,
                'titan_available': False,
                'message': 'Failed to connect to remote server'
            }

        # 检查Titan编译器
        compiler_check = self.ssh_client.execute_command(f"which {self.compiler}")
        if compiler_check['exit_code'] != 0:
            return {
                'connected': True,
                'titan_available': False,
                'message': f'Titan compiler ({self.compiler}) not found'
            }

        # 检查Titan执行器
        executor_check = self.ssh_client.execute_command(f"which {self.executor}")
        if executor_check['exit_code'] != 0:
            return {
                'connected': True,
                'titan_available': False,
                'message': f'Titan executor ({self.executor}) not found'
            }

        return {
            'connected': True,
            'titan_available': True,
            'message': 'Titan environment is ready'
        }


def get_titan_engine():
    """获取Titan引擎实例"""
    return TitanEngine()
