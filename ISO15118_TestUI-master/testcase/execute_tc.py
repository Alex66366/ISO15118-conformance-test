"""
测试执行管理模块 - 支持多任务并发调度
"""
from .consumers import LoggingConsumer
import os, sys, json, re
import threading, subprocess
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
import inspect, time
from .models import TestcaseType, TestcaseManager, MessageType
from django.conf import settings

RETURN_CODE_MAP = {
    0:'n',
    1:'p',
    2:'i',
    3:'f',
    4:'e'
}
RETURN_CODE_MAP_LONG = {
    0:'none',
    1:'pass',
    2:'inconclude',
    3:'fail',
    4:'error'
}


class ExecTestcase(threading.Thread):
    """测试用例执行线程"""
    def __init__(self, testcase, v2g_cfg, slac_cfg, secc_ip="ff02::1", task_id=None):
        if (testcase != None):
            self.id = testcase.id
            self.tc_name = testcase.name
            self.status = testcase.status
            self.build_status = testcase.build_status
            self.path = testcase.path
        else:
            self.id = 0
            self.tc_name = ''
            self.status = 'n'
            self.build_status = 'NA'
            self.path = ''
        self.isrunning = False
        self.v2g_cfg_path = v2g_cfg
        self.slac_cfg_path = slac_cfg
        self.secc_ip = secc_ip
        self.handle = None
        self.task_id = task_id
        super().__init__()

    def __log(self, msg, status='none'):
        try:
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                'v2g_logging',
                {
                    'status': status,
                    'type': 'log.message',
                    'message': f"{msg}",
                    'task_id': self.task_id
                }
            )
        except Exception as e:
            print(f"[LOG] {msg}")

    def kill(self):
        if self.handle is None:
            return False
        if self.handle.poll() is None:
            self.isrunning = False
            self.handle.kill()
            while self.handle.poll() is None:
                pass
            self.__log(f"---- Cancel {self.tc_name} ----\n", 'update')
            return True
        else:
            return False

    def run(self) -> None:
        try:
            self.isrunning = True
            self.__log(f"---- Start execute {self.tc_name} ----\n", 'update')

            self.handle = subprocess.Popen(
                [self.path, self.tc_name, "I", self.v2g_cfg_path, self.slac_cfg_path, self.secc_ip],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True
            )

            while (self.handle.poll() == None):
                for line in iter(self.handle.stdout.readline, ''):
                    self.__log(line)

            if (self.isrunning):
                self.isrunning = False
                self.__log(self.handle.stdout.read())
                try:
                    tc = TestcaseType.objects.get(id=self.id)
                    if (self.handle.returncode in RETURN_CODE_MAP.keys()):
                        tc.status = RETURN_CODE_MAP[self.handle.returncode]
                    tc.save()
                    self.__log(
                        f"---- Execute {self.tc_name} completed - status code: {RETURN_CODE_MAP_LONG[self.handle.returncode]} ----\n",
                        'update'
                    )
                except Exception as e:
                    self.__log(f"---- Execute {self.tc_name} completed - {e.args} ----\n", 'update')

            # Notify task completion
            TaskQueueManager.notify_task_complete(self.task_id)

        except Exception as e:
            self.__log(f"Error: {e.args}", 'none')
            TaskQueueManager.notify_task_complete(self.task_id, error=str(e))

    def get_response_data(self):
        return {
            "id": self.id,
            "name": self.tc_name,
            "status": self.status,
            "build_status": self.build_status,
            "isrunning": self.isrunning,
            "path": self.path,
            "task_id": self.task_id
        }


class TaskInfo:
    """任务信息类"""
    def __init__(self, task_id, testcase, v2g_cfg_path, slac_cfg_path, secc_ip="ff02::1", priority=5):
        self.task_id = task_id
        self.testcase = testcase
        self.v2g_cfg_path = v2g_cfg_path
        self.slac_cfg_path = slac_cfg_path
        self.secc_ip = secc_ip
        self.priority = priority
        self.status = 'pending'  # pending, running, completed, failed, cancelled
        self.exec_thread = None
        self.result = None
        self.error = None
        self.start_time = None
        self.end_time = None

    def to_dict(self):
        return {
            'task_id': self.task_id,
            'testcase_id': self.testcase.id if self.testcase else None,
            'testcase_name': self.testcase.name if self.testcase else '',
            'status': self.status,
            'priority': self.priority,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'error': self.error
        }


class TaskQueueManager:
    """
    任务队列管理器 - 支持多任务并发执行
    """
    _instance = None
    _lock = threading.Lock()

    # Class-level task storage
    _tasks = {}
    _running_tasks = {}
    _max_concurrent = 3

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._max_concurrent = getattr(settings, 'MAX_CONCURRENT_TESTS', 3)
        self._scheduler_thread = None
        self._scheduler_running = False

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = TaskQueueManager()
        return cls._instance

    @classmethod
    def _generate_task_id(cls):
        """生成唯一任务ID"""
        import uuid
        return f"task_{uuid.uuid4().hex[:12]}"

    @classmethod
    def submit_task(cls, testcase, v2g_cfg_path, slac_cfg_path, secc_ip="ff02::1", priority=5):
        """
        提交新任务到队列

        Args:
            testcase: 测试用例对象
            v2g_cfg_path: V2G配置文件路径
            slac_cfg_path: SLAC配置文件路径
            secc_ip: SECC IP地址
            priority: 优先级 (1-10, 1为最高优先级)

        Returns:
            dict: 任务信息
        """
        task_id = cls._generate_task_id()

        # Check if we can run immediately
        running_count = len(cls._running_tasks)
        can_run = running_count < cls.get_instance()._max_concurrent

        task_info = TaskInfo(task_id, testcase, v2g_cfg_path, slac_cfg_path, secc_ip, priority)
        task_info.status = 'running' if can_run else 'pending'

        cls._tasks[task_id] = task_info

        if can_run:
            cls._start_task(task_id)
        else:
            # Start scheduler if not running
            cls.get_instance()._ensure_scheduler()

        return task_info.to_dict()

    @classmethod
    def _start_task(cls, task_id):
        """启动任务执行"""
        task_info = cls._tasks.get(task_id)
        if not task_info:
            return

        from datetime import datetime
        task_info.status = 'running'
        task_info.start_time = datetime.now()
        cls._running_tasks[task_id] = task_info

        # Create execution thread
        exec_thread = ExecTestcase(
            task_info.testcase,
            task_info.v2g_cfg_path,
            task_info.slac_cfg_path,
            task_info.secc_ip,
            task_id
        )
        exec_thread.setName(task_info.testcase.name)
        task_info.exec_thread = exec_thread
        exec_thread.start()

    @classmethod
    def _ensure_scheduler(cls):
        """确保调度线程正在运行"""
        instance = cls.get_instance()
        if not instance._scheduler_running or instance._scheduler_thread is None or not instance._scheduler_thread.is_alive():
            instance._scheduler_running = True
            instance._scheduler_thread = threading.Thread(target=cls._scheduler_loop, daemon=True)
            instance._scheduler_thread.start()

    @classmethod
    def _scheduler_loop(cls):
        """任务调度循环"""
        instance = cls.get_instance()
        while instance._scheduler_running:
            try:
                running_count = len(cls._running_tasks)
                if running_count < instance._max_concurrent:
                    # Find pending tasks (sorted by priority)
                    pending_tasks = [
                        (tid, t) for tid, t in cls._tasks.items()
                        if t.status == 'pending'
                    ]
                    pending_tasks.sort(key=lambda x: (x[1].priority, x[1].task_id))

                    if pending_tasks:
                        # Start next task
                        task_id, task_info = pending_tasks[0][0], pending_tasks[0][1]
                        cls._start_task(task_id)

                time.sleep(1)
            except Exception as e:
                print(f"Scheduler error: {e}")
                time.sleep(5)

    @classmethod
    def stop_scheduler(cls):
        """停止调度器"""
        instance = cls.get_instance()
        instance._scheduler_running = False

    @classmethod
    def notify_task_complete(cls, task_id, error=None):
        """任务完成通知"""
        if task_id in cls._running_tasks:
            task_info = cls._running_tasks.pop(task_id)

            from datetime import datetime
            task_info.status = 'completed' if not error else 'failed'
            task_info.end_time = datetime.now()
            task_info.error = error

            # Try to start next pending task
            cls._ensure_scheduler()

    @classmethod
    def get_task_status(cls, task_id):
        """获取任务状态"""
        if task_id in cls._tasks:
            return cls._tasks[task_id].to_dict()
        return None

    @classmethod
    def get_all_tasks(cls):
        """获取所有任务"""
        return {
            'tasks': [t.to_dict() for t in cls._tasks.values()],
            'running': [t.to_dict() for t in cls._running_tasks.values()],
            'pending': [t.to_dict() for t in cls._tasks.values() if t.status == 'pending'],
            'completed': [t.to_dict() for t in cls._tasks.values() if t.status == 'completed'],
            'failed': [t.to_dict() for t in cls._tasks.values() if t.status == 'failed'],
            'max_concurrent': cls.get_instance()._max_concurrent,
            'current_running': len(cls._running_tasks)
        }

    @classmethod
    def cancel_task(cls, task_id):
        """取消任务"""
        if task_id in cls._tasks:
            task_info = cls._tasks[task_id]
            if task_info.status == 'running':
                if task_info.exec_thread and task_info.exec_thread.is_alive():
                    task_info.exec_thread.kill()
                    task_info.exec_thread.join(timeout=5)
                task_info.status = 'cancelled'
                if task_id in cls._running_tasks:
                    cls._running_tasks.pop(task_id)
                return True, f"Task {task_id} cancelled"
            elif task_info.status == 'pending':
                task_info.status = 'cancelled'
                return True, f"Task {task_id} cancelled (was pending)"
            else:
                return False, f"Task {task_id} cannot be cancelled (status: {task_info.status})"
        return False, f"Task {task_id} not found"

    @classmethod
    def is_available(cls):
        """检查是否可以提交新任务"""
        running_count = len(cls._running_tasks)
        return running_count < cls.get_instance()._max_concurrent

    @classmethod
    def get_current_exec(cls):
        """获取当前执行的任务信息（兼容旧API）"""
        if cls._running_tasks:
            # Return the first running task
            for task_id, task_info in cls._running_tasks.items():
                return {
                    "id": task_info.testcase.id if task_info.testcase else 0,
                    "name": task_info.testcase.name if task_info.testcase else '',
                    "status": task_info.status,
                    "build_status": task_info.testcase.build_status if task_info.testcase else 'NA',
                    "isrunning": True,
                    "path": task_info.testcase.path if task_info.testcase else '',
                    "task_id": task_id
                }
        return {
            "id": 0,
            "name": '',
            "status": 'n',
            "build_status": 'NA',
            "isrunning": False,
            "path": '',
            "task_id": None
        }


# Keep backward compatibility
class TestExecManagement:
    """测试执行管理类 - 兼容旧API"""
    exec_object = ExecTestcase(None, "", "")

    @staticmethod
    def get_current_exec():
        return TaskQueueManager.get_current_exec()

    @staticmethod
    def create_exec(testcase, v2g_cfg_path, slac_cfg_path, secc_ip="ff02::1"):
        # This is handled by submit_task now
        pass

    @staticmethod
    def is_available():
        return TaskQueueManager.is_available()

    @staticmethod
    def deploy(id):
        # This is handled by submit_task now
        pass

    @staticmethod
    def submit(testcase, v2g_cfg_path, slac_cfg_path, secc_ip="ff02::1", priority=5):
        """提交任务到队列"""
        return TaskQueueManager.submit_task(testcase, v2g_cfg_path, slac_cfg_path, secc_ip, priority)

    @staticmethod
    def cancel(id):
        return TaskQueueManager.cancel_task(id)

    @staticmethod
    def get_task_status(task_id):
        return TaskQueueManager.get_task_status(task_id)

    @staticmethod
    def get_all_tasks():
        return TaskQueueManager.get_all_tasks()
