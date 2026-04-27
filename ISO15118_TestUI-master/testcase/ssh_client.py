import paramiko
import os
import time
from pathlib import Path
from django.conf import settings


class SSHClient:
    """SSH连接管理类，用于远程执行Titan命令"""

    _instance = None
    _client = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, 'initialized'):
            self.host = getattr(settings, 'TITAN_HOST', '192.168.1.100')
            self.port = getattr(settings, 'TITAN_SSH_PORT', 22)
            self.username = getattr(settings, 'TITAN_SSH_USER', 'ttcn3')
            self.password = getattr(settings, 'TITAN_SSH_PASSWORD', None)
            self.key_file = getattr(settings, 'TITAN_SSH_KEY', None)
            self.remote_workspace = getattr(settings, 'TITAN_WORKSPACE', '/home/ttcn3/workspace')
            self.initialized = True

    def connect(self):
        """建立SSH连接"""
        if self._client and self._client.get_transport() and self._client.get_transport().is_active():
            return True

        try:
            self._client = paramiko.SSHClient()
            self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs = {
                'hostname': self.host,
                'port': self.port,
                'username': self.username,
                'timeout': 30
            }

            if self.key_file and os.path.exists(self.key_file):
                connect_kwargs['key_filename'] = self.key_file
            elif self.password:
                connect_kwargs['password'] = self.password
            else:
                raise ValueError("No authentication method provided")

            self._client.connect(**connect_kwargs)
            return True
        except Exception as e:
            print(f"SSH connection failed: {e}")
            self._client = None
            return False

    def disconnect(self):
        """断开SSH连接"""
        if self._client:
            self._client.close()
            self._client = None

    def execute_command(self, command, timeout=300):
        """
        执行远程命令

        Args:
            command: 要执行的命令
            timeout: 超时时间（秒）

        Returns:
            dict: {
                'stdout': 标准输出,
                'stderr': 标准错误,
                'exit_code': 退出码
            }
        """
        if not self.connect():
            return {
                'stdout': '',
                'stderr': 'Failed to connect to remote server',
                'exit_code': -1
            }

        try:
            stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()

            return {
                'stdout': stdout.read().decode('utf-8'),
                'stderr': stderr.read().decode('utf-8'),
                'exit_code': exit_code
            }
        except Exception as e:
            return {
                'stdout': '',
                'stderr': str(e),
                'exit_code': -1
            }

    def upload_file(self, local_path, remote_path):
        """
        上传文件到远程服务器

        Args:
            local_path: 本地文件路径
            remote_path: 远程目标路径

        Returns:
            bool: 是否成功
        """
        if not self.connect():
            return False

        try:
            sftp = self._client.open_sftp()
            sftp.put(local_path, remote_path)
            sftp.close()
            return True
        except Exception as e:
            print(f"File upload failed: {e}")
            return False

    def download_file(self, remote_path, local_path):
        """
        从远程服务器下载文件

        Args:
            remote_path: 远程文件路径
            local_path: 本地目标路径

        Returns:
            bool: 是否成功
        """
        if not self.connect():
            return False

        try:
            sftp = self._client.open_sftp()
            sftp.get(remote_path, local_path)
            sftp.close()
            return True
        except Exception as e:
            print(f"File download failed: {e}")
            return False

    def file_exists(self, remote_path):
        """检查远程文件是否存在"""
        if not self.connect():
            return False

        try:
            sftp = self._client.open_sftp()
            sftp.stat(remote_path)
            sftp.close()
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False


def get_ssh_client():
    """获取SSH客户端单例"""
    return SSHClient()
