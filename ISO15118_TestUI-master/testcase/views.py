from .execute_tc import ExecTestcase, TestExecManagement, TaskQueueManager
import os
import tempfile
import collections
from typing import Any
import django
from django.db.models.query import QuerySet
from django.shortcuts import redirect, render, get_object_or_404
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.db.models import Count, Q, F
from django.conf import settings
from django.views.generic import detail
from drf_yasg import views
import rest_framework
from rest_framework.decorators import action
from rest_framework import renderers, viewsets, status, generics, mixins
import django_filters
from rest_framework.response import Response
from rest_framework.request import HttpRequest
from rest_framework.parsers import JSONParser, MultiPartParser
from .models import TestcaseType, MessageType, TestExecutionTask, ResourcePool
from .serializers import MessageSerializer, TestcaseSerializer, TestExecutionTaskSerializer, ResourcePoolSerializer
import re
import json
import multiprocessing
from .consumers import LoggingConsumer


# Create your views here.

class TestcaseViewSet(viewsets.ModelViewSet):
    queryset = TestcaseType.objects.all()
    serializer_class = TestcaseSerializer
    pagination_class = None
    filter_backends = [django_filters.rest_framework.DjangoFilterBackend]

    # override queryset
    def get_queryset(self):
        # if query param has msg_type
        msg_type = self.request.query_params.get('msg_type')
        msg_name = self.request.query_params.get('name')
        if msg_type is not None:
            queryset = self.queryset.filter(msg_type__name__contains=msg_type)
            return queryset
        elif msg_name is not None:
            queryset = self.queryset.filter(name=msg_name)
            return queryset
        return self.queryset.all()

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        if (queryset.count() > 1):
            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = self.get_serializer(page, many=True)
                return self.get_paginated_response(serializer.data)
            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data, status=status.HTTP_200_OK)
        elif (queryset.count() == 0):
            return Response({
                        "status": "NG",
                        "message": "No testcase matched query params"
                    }, status=status.HTTP_404_NOT_FOUND)
        else:
            queryset = queryset.first()
            serializer = self.get_serializer(queryset, many=False)
            return Response(serializer.data, status=status.HTTP_200_OK)

    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

from .execute_tc import TestExecManagement, TaskQueueManager


class TestcaseExecute(mixins.RetrieveModelMixin, mixins.UpdateModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = TestcaseType.objects.all()
    serializer_class = TestcaseSerializer
    exec_class = TestExecManagement

    def list(self, request, *args, **kwargs):
        """处理不需要pk的操作：获取队列状态等"""
        try:
            request_operation = request.query_params.get('action')

            if request_operation == "get_info":
                queue_info = TaskQueueManager.get_all_tasks()
                current_exec = TaskQueueManager.get_current_exec()
                return Response({
                    "current": current_exec,
                    "queue": queue_info,
                    "status": "OK"
                }, status=status.HTTP_200_OK)

            elif request_operation == "queue_status":
                queue_info = TaskQueueManager.get_all_tasks()
                return Response({
                    "status": "OK",
                    "queue": queue_info
                }, status=status.HTTP_200_OK)

            elif request_operation == "task_status":
                task_id = request.query_params.get('task_id')
                if not task_id:
                    return Response({
                        "status": "NG",
                        "message": "task_id is required"
                    }, status=status.HTTP_400_BAD_REQUEST)
                task_status = TaskQueueManager.get_task_status(task_id)
                if task_status:
                    return Response({
                        "status": "OK",
                        "task": task_status
                    }, status=status.HTTP_200_OK)
                else:
                    return Response({
                        "status": "NG",
                        "message": "Task not found"
                    }, status=status.HTTP_404_NOT_FOUND)

            elif request_operation == "cancel":
                task_id = request.query_params.get('task_id')
                if not task_id:
                    return Response({
                        "status": "NG",
                        "message": "task_id is required"
                    }, status=status.HTTP_400_BAD_REQUEST)
                try:
                    sts, sts_msg = self.exec_class.cancel(task_id)
                    if sts:
                        return Response({
                            "status": "OK",
                            "message": sts_msg
                        }, status=status.HTTP_200_OK)
                    else:
                        return Response({
                            "status": "NG",
                            "message": sts_msg
                        }, status=status.HTTP_404_NOT_FOUND)
                except Exception as e:
                    return Response({
                        "status": "NG",
                        "message": str(e)
                    }, status=status.HTTP_404_NOT_FOUND)

            else:
                return Response({
                    "status": "Invalid action",
                    "message": "Requested action is not available. Available actions: get_info, queue_status, cancel, task_status"
                }, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            return Response({
                "status": "Error",
                "message": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def retrieve(self, request, pk, *args, **kwargs):
        """处理查询操作：获取执行状态、取消任务等"""
        try:
            request_operation = request.query_params.get('action')

            if request_operation == "get_info":
                # Get queue status
                queue_info = TaskQueueManager.get_all_tasks()
                current_exec = TaskQueueManager.get_current_exec()
                return Response({
                    "current": current_exec,
                    "queue": queue_info,
                    "status": "OK"
                }, status=status.HTTP_200_OK)

            elif request_operation == "queue_status":
                # Get all queue information
                queue_info = TaskQueueManager.get_all_tasks()
                return Response({
                    "status": "OK",
                    "queue": queue_info
                }, status=status.HTTP_200_OK)

            elif request_operation == "cancel":
                # Cancel by task_id (passed as pk or query param)
                task_id = request.query_params.get('task_id', pk)
                try:
                    sts, sts_msg = self.exec_class.cancel(task_id)
                    if sts:
                        return Response({
                            "status": "OK",
                            "message": sts_msg
                        }, status.HTTP_200_OK)
                    else:
                        return Response({
                            "status": "NG",
                            "message": sts_msg
                        }, status.HTTP_404_NOT_FOUND)
                except Exception as e:
                    return Response({
                        "status": "NG",
                        "message": str(e)
                    }, status.HTTP_404_NOT_FOUND)

            elif request_operation == "task_status":
                # Get status of specific task
                task_id = request.query_params.get('task_id', pk)
                task_status = TaskQueueManager.get_task_status(task_id)
                if task_status:
                    return Response({
                        "status": "OK",
                        "task": task_status
                    }, status.HTTP_200_OK)
                else:
                    return Response({
                        "status": "NG",
                        "message": "Task not found"
                    }, status.HTTP_404_NOT_FOUND)

            else:
                return Response({
                    "status": "Invalid action",
                    "message": "Requested action is not available. Available actions: get_info, queue_status, cancel, task_status"
                }, status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            return Response({
                "status": "Error",
                "message": str(e)
            }, status.HTTP_404_NOT_FOUND)

    def update(self, request, pk):
        """提交测试任务到队列（支持并发）"""
        try:
            tc = self.queryset.get(pk=pk)

            # Get priority from request (default 5)
            priority = int(request.data.get('priority', 5))
            secc_ip = request.data.get('secc_ip', 'ff02::1')

            # Parse config files
            v2g_path, slac_path = self.parse_config(request.data)

            # Submit to task queue instead of direct execution
            result = TaskQueueManager.submit_task(
                tc, v2g_path, slac_path, secc_ip, priority
            )

            # Also try to save to database for persistence
            try:
                db_task = TestExecutionTask.objects.create(
                    task_id=result['task_id'],
                    testcase=tc,
                    status='pending',
                    priority=priority,
                    v2g_config_path=v2g_path,
                    slac_config_path=slac_path,
                    secc_ip=secc_ip
                )
            except Exception:
                pass  # Non-critical if DB save fails

            # Check if task is running immediately or queued
            if result['status'] == 'running':
                return Response({
                    "status": "OK",
                    "message": f"Executing {tc.name} (Task ID: {result['task_id']})",
                    "task": result
                }, status.HTTP_200_OK)
            else:
                return Response({
                    "status": "QUEUED",
                    "message": f"Test {tc.name} queued (Task ID: {result['task_id']})",
                    "task": result
                }, status.HTTP_202_ACCEPTED)

        except TestcaseType.DoesNotExist:
            return Response({
                "status": "Error",
                "message": "Testcase not found"
            }, status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({
                "status": "Error",
                "message": str(e)
            }, status.HTTP_417_EXPECTATION_FAILED)

    def parse_config(self, test_config):
        """
        Generate runtime config files for the C++ TestSuite.

        - V2G config: mostly trust frontend, but normalize obvious type issues.
        - SLAC config: base on reference docs/slac_config.json, then apply overrides.
        """
        # -------- V2G CONFIG (v2g_config.json) --------
        timer_cfg = test_config.get("timer", {})
        pics_cfg = test_config.get("pics", {})
        pixit_cfg = test_config.get("pixit", {})

        # Best-effort normalization for pixit measurement objects where unit
        # might accidentally be a string like "v" / "a" instead of an int.
        unit_map = {
            "v": 4, "V": 4,
            "a": 3, "A": 3,
            "w": 6, "W": 6,
            "wh": 7, "Wh": 7, "WH": 7,
        }

        if isinstance(pixit_cfg, dict):
            normalized_pixit = {}
            for key, value in pixit_cfg.items():
                if isinstance(value, dict) and "unit" in value:
                    unit_val = value.get("unit")
                    if isinstance(unit_val, str) and unit_val in unit_map:
                        value = dict(value)
                        value["unit"] = unit_map[unit_val]
                normalized_pixit[key] = value
            pixit_cfg = normalized_pixit

        v2g_config = {
            "timer": timer_cfg,
            "pics": pics_cfg,
            "pixit": pixit_cfg,
        }

        v2g_path = os.path.join(tempfile.gettempdir(), "v2g_config.json")
        with open(v2g_path, "w", encoding="utf-8") as fp:
            json.dump(v2g_config, fp, indent=4)

        # -------- SLAC CONFIG (slac_config.json) --------
        # Start from reference docs/slac_config.json to match the working CLI setup.
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        docs_root = os.path.join(project_root, "ISO15118_4_TestEnv-master", "docs")
        slac_ref_path = os.path.join(docs_root, "slac_config.json")

        with open(slac_ref_path, "r", encoding="utf-8") as fp:
            slac_base = json.load(fp)

        # If frontend provided SLAC overrides, apply them to the appropriate section
        slac_frontend = test_config.get("slac", {})
        if isinstance(slac_frontend, dict):
            for k, v in slac_frontend.items():
                if isinstance(k, str) and k.startswith("C_"):
                    slac_base.setdefault("threshold", {})[k] = v
                else:
                    slac_base.setdefault("timer", {})[k] = v

        slac_path = os.path.join(tempfile.gettempdir(), "slac_config.json")
        with open(slac_path, "w", encoding="utf-8") as fp:
            json.dump(slac_base, fp, indent=4)

        return v2g_path, slac_path

    @action(detail=False, methods=['post'])
    def export_config(self, request):
        """
        Export processed configuration (v2g_config.json + slac_config.json).
        This endpoint processes the frontend configuration through the same logic
        as parse_config, then returns both configs as a combined JSON for download.
        """
        try:
            test_config = request.data
            # Use parse_config logic to generate the processed configs
            # -------- V2G CONFIG --------
            timer_cfg = test_config.get("timer", {})
            pics_cfg = test_config.get("pics", {})
            pixit_cfg = test_config.get("pixit", {})

            unit_map = {
                "v": 4, "V": 4,
                "a": 3, "A": 3,
                "w": 6, "W": 6,
                "wh": 7, "Wh": 7, "WH": 7,
            }

            if isinstance(pixit_cfg, dict):
                normalized_pixit = {}
                for key, value in pixit_cfg.items():
                    if isinstance(value, dict) and "unit" in value:
                        unit_val = value.get("unit")
                        if isinstance(unit_val, str) and unit_val in unit_map:
                            value = dict(value)
                            value["unit"] = unit_map[unit_val]
                    normalized_pixit[key] = value
                pixit_cfg = normalized_pixit

            v2g_config = {
                "timer": timer_cfg,
                "pics": pics_cfg,
                "pixit": pixit_cfg,
            }

            # -------- SLAC CONFIG --------
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            docs_root = os.path.join(project_root, "ISO15118_4_TestEnv-master", "docs")
            slac_ref_path = os.path.join(docs_root, "slac_config.json")

            with open(slac_ref_path, "r", encoding="utf-8") as fp:
                slac_base = json.load(fp)

            slac_frontend = test_config.get("slac", {})
            if isinstance(slac_frontend, dict):
                for k, v in slac_frontend.items():
                    if isinstance(k, str) and k.startswith("C_"):
                        slac_base.setdefault("threshold", {})[k] = v
                    else:
                        slac_base.setdefault("timer", {})[k] = v

            # Return both configs in a single response
            return Response({
                "v2g_config": v2g_config,
                "slac_config": slac_base
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({
                "status": "Error",
                "message": str(e)
            }, status=status.HTTP_417_EXPECTATION_FAILED)


class SummaryViewSet(viewsets.GenericViewSet):
    queryset = MessageType.objects.all()
    serializer_class = MessageSerializer

    def list(self, request):
        serializer = self.get_serializer(self.queryset, many=True)
        return Response(serializer.data, status.HTTP_200_OK)


class ClearDataView(viewsets.GenericViewSet):
    queryset = MessageType.objects.all()
    serializer_class = MessageSerializer

    @action(detail=False, methods=['post'])
    def clear(self, request):
        try:
            # Reset all testcase status to 'n' (none)
            TestcaseType.objects.all().update(status='n')
            return Response({
                "status": "success",
                "message": "All testcase status has been reset"
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                "status": "error",
                "message": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class LLMGenerateView(viewsets.GenericViewSet):
    """LLM测试脚本生成视图"""
    queryset = TestcaseType.objects.all()
    serializer_class = TestcaseSerializer
    permission_classes = []

    def create(self, request):
        """
        生成TTCN-3测试脚本

        POST /api/v1/llm/generate/
        Body: { "test_intent": "测试V2GTP头部解析功能", "test_type": "CMN", "api_key": "xxx", "model": "gpt-4o" }
        """
        from .llm_client import LLMClient

        test_intent = request.data.get('test_intent', '')
        test_type = request.data.get('test_type', 'CMN')

        # Get API key from request body, header, or settings
        api_key = request.data.get('api_key') or request.headers.get('X-LLM-Api-Key') or getattr(settings, 'OPENAI_API_KEY', None)

        # Get model from request body or use default
        model = request.data.get('model', 'gpt-4o')

        if not test_intent:
            return Response({
                "status": "error",
                "message": "test_intent is required"
            }, status=status.HTTP_400_BAD_REQUEST)

        if not api_key:
            return Response({
                "status": "error",
                "message": "API key is required. Please provide api_key in request body or set OPENAI_API_KEY environment variable."
            }, status=status.HTTP_400_BAD_REQUEST)

        # Initialize LLM client
        llm_client = LLMClient(api_key=api_key, model=model)

        if not llm_client.is_available():
            return Response({
                "status": "error",
                "message": "Failed to initialize LLM client. Please check your API key."
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        # Generate script
        result = llm_client.generate_ttcn3_script(test_intent, test_type)

        if result.get("status") == "success":
            return Response({
                "status": "success",
                "message": result.get("message", "Script generated successfully"),
                "code": result.get("code", ""),
                "model": result.get("model", model)
            }, status=status.HTTP_200_OK)
        else:
            return Response({
                "status": "error",
                "message": result.get("message", "Failed to generate script")
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['post'])
    def verify(self, request):
        """验证API Key是否有效"""
        from .llm_client import LLMClient

        api_key = request.data.get('api_key', '')
        model = request.data.get('model', 'gpt-4o')

        if not api_key:
            return Response({
                "status": "error",
                "message": "API key is required"
            }, status=status.HTTP_400_BAD_REQUEST)

        # Initialize LLM client
        llm_client = LLMClient(api_key=api_key, model=model)

        if not llm_client.is_available():
            return Response({
                "status": "error",
                "message": "Failed to initialize LLM client"
            }, status=status.HTTP_400_BAD_REQUEST)

        # Try to list models to verify the API key
        try:
            llm_client.client.models.list()
            return Response({
                "status": "success",
                "message": "API key is valid"
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                "status": "error",
                "message": f"API key validation failed: {str(e)}"
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'])
    def config(self, request):
        """获取LLM配置信息"""
        from .llm_client import LLMConfig
        config = LLMConfig.get_default_config()
        # Don't expose the actual API key
        config["api_key"] = "***" if config.get("api_key") else ""
        config["available"] = bool(config.get("api_key") or os.getenv("OPENAI_API_KEY"))
        return Response(config, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def list_generated(self, request):
        """
        获取AI生成的测试用例列表

        GET /api/v1/llm/generate/list_generated/
        """
        # 获取所有 is_generated=True 的测试用例
        generated_testcases = TestcaseType.objects.filter(is_generated=True).order_by('-created_at')

        testcase_list = []
        for tc in generated_testcases:
            testcase_list.append({
                'id': tc.id,
                'name': tc.name,
                'type': tc.type,
                'status': tc.status,
                'build_status': tc.build_status,
                'compile_status': tc.compile_status,
                'project_path': tc.project_path,
                'created_at': tc.created_at.isoformat() if tc.created_at else None,
                'updated_at': tc.updated_at.isoformat() if tc.updated_at else None,
            })

        return Response({
            'status': 'success',
            'testcases': testcase_list,
            'count': len(testcase_list)
        }, status=status.HTTP_200_OK)


class TTCN3CompileView(viewsets.GenericViewSet):
    """TTCN-3编译视图"""
    queryset = TestcaseType.objects.all()
    serializer_class = TestcaseSerializer
    permission_classes = []

    @action(detail=False, methods=['post'])
    def compile(self, request):
        """
        编译TTCN-3代码

        POST /api/v1/ttcn/compile/
        Body: { "code": "TTCN-3 source code", "project_name": "optional_project_name" }
        """
        from .titan_engine import get_titan_engine

        code = request.data.get('code', '')
        project_name = request.data.get('project_name', None)
        main_module = request.data.get('main_module', 'TestSuite')

        if not code:
            return Response({
                "status": "error",
                "message": "TTCN-3 code is required"
            }, status=status.HTTP_400_BAD_REQUEST)

        # Get Titan engine
        titan = get_titan_engine()

        # Check connection first
        conn_check = titan.check_connection()
        if not conn_check['connected']:
            return Response({
                "status": "error",
                "message": f"Cannot connect to Titan server: {conn_check['message']}"
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if not conn_check['titan_available']:
            return Response({
                "status": "error",
                "message": f"Titan not available: {conn_check['message']}"
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        # Compile the code
        result = titan.compile_code(code, project_name, main_module)

        return Response({
            "status": "success" if result['success'] else "error",
            "message": "Compilation successful" if result['success'] else "Compilation failed",
            "output": result['output'],
            "errors": result['errors'],
            "project_path": result['project_path'],
            "main_module": result.get('main_module', main_module)
        }, status=status.HTTP_200_OK if result['success'] else status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'])
    def check_env(self, request):
        """检查Titan环境连接状态"""
        from .titan_engine import get_titan_engine

        titan = get_titan_engine()
        result = titan.check_connection()

        return Response({
            "connected": result['connected'],
            "titan_available": result['titan_available'],
            "message": result['message']
        }, status=status.HTTP_200_OK if result['connected'] else status.HTTP_503_SERVICE_UNAVAILABLE)


class TTCN3ExecuteView(viewsets.GenericViewSet):
    """TTCN-3测试执行视图"""
    queryset = TestcaseType.objects.all()
    serializer_class = TestcaseSerializer
    permission_classes = []

    @action(detail=False, methods=['post'])
    def execute(self, request):
        """
        执行TTCN-3测试用例

        POST /api/v1/ttcn/execute/
        Body: { "project_path": "/path/to/project", "main_module": "TestSuite", "secc_ip": "192.168.1.1" }
        """
        from .titan_engine import get_titan_engine

        project_path = request.data.get('project_path', '')
        main_module = request.data.get('main_module', 'TestSuite')
        secc_ip = request.data.get('secc_ip', 'ff02::1')
        config_file = request.data.get('config_file', None)

        if not project_path:
            return Response({
                "status": "error",
                "message": "project_path is required"
            }, status=status.HTTP_400_BAD_REQUEST)

        # Get Titan engine
        titan = get_titan_engine()

        # Check connection
        conn_check = titan.check_connection()
        if not conn_check['connected']:
            return Response({
                "status": "error",
                "message": f"Cannot connect to Titan server: {conn_check['message']}"
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if not conn_check['titan_available']:
            return Response({
                "status": "error",
                "message": f"Titan not available: {conn_check['message']}"
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        # Execute test
        result = titan.execute_test(project_path, main_module, config_file, secc_ip)

        return Response({
            "status": "success" if result['success'] else "error",
            "result": result['result'],
            "exit_code": result['exit_code'],
            "output": result['output'],
            "errors": result['errors']
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'])
    def compile_and_execute(self, request):
        """
        编译并执行TTCN-3测试用例

        POST /api/v1/ttcn/compile_execute/
        Body: { "code": "TTCN-3 code", "project_name": "optional", "main_module": "TestSuite", "secc_ip": "192.168.1.1" }
        """
        from .titan_engine import get_titan_engine

        code = request.data.get('code', '')
        project_name = request.data.get('project_name', None)
        main_module = request.data.get('main_module', 'TestSuite')
        secc_ip = request.data.get('secc_ip', 'ff02::1')
        save_to_db = request.data.get('save_to_db', False)
        testcase_name = request.data.get('testcase_name', 'LLM_Generated_Test')

        if not code:
            return Response({
                "status": "error",
                "message": "TTCN-3 code is required"
            }, status=status.HTTP_400_BAD_REQUEST)

        # Get Titan engine
        titan = get_titan_engine()

        # Check connection
        conn_check = titan.check_connection()
        if not conn_check['connected']:
            return Response({
                "status": "error",
                "message": f"Cannot connect to Titan server: {conn_check['message']}"
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if not conn_check['titan_available']:
            return Response({
                "status": "error",
                "message": f"Titan not available: {conn_check['message']}"
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        # Step 1: Compile
        compile_result = titan.compile_code(code, project_name, main_module)

        if not compile_result['success']:
            return Response({
                "status": "error",
                "phase": "compile",
                "message": "Compilation failed",
                "output": compile_result['output'],
                "errors": compile_result['errors']
            }, status=status.HTTP_400_BAD_REQUEST)

        # Step 2: Execute
        exec_result = titan.execute_test(
            compile_result['project_path'],
            compile_result['main_module'],
            None,
            secc_ip
        )

        # Step 3: Optionally save to database
        testcase_id = None
        if save_to_db:
            try:
                tc = TestcaseType.objects.create(
                    name=testcase_name,
                    type='CMN',
                    status='p' if exec_result['result'] == 'pass' else 'f',
                    build_status='OK',
                    code=code,
                    is_generated=True,
                    compile_status='success' if compile_result['success'] else 'failed',
                    compile_output=compile_result['output'] + '\n' + compile_result['errors'],
                    project_path=compile_result['project_path']
                )
                testcase_id = tc.id
            except Exception as e:
                pass

        return Response({
            "status": "success" if exec_result['success'] else "error",
            "phase": "execute",
            "result": exec_result['result'],
            "exit_code": exec_result['exit_code'],
            "output": exec_result['output'],
            "errors": exec_result['errors'],
            "project_path": compile_result['project_path'],
            "testcase_id": testcase_id
        }, status=status.HTTP_200_OK)


class TaskQueueViewSet(viewsets.GenericViewSet):
    """任务队列管理视图"""
    queryset = TestExecutionTask.objects.all()
    serializer_class = TestExecutionTaskSerializer

    def list(self, request):
        """获取所有任务状态"""
        queue_info = TaskQueueManager.get_all_tasks()
        return Response(queue_info, status=status.HTTP_200_OK)

    def retrieve(self, request, pk):
        """获取特定任务状态"""
        task_status = TaskQueueManager.get_task_status(pk)
        if task_status:
            return Response(task_status, status=status.HTTP_200_OK)
        return Response({"error": "Task not found"}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['post'])
    def cancel(self, request):
        """取消指定任务"""
        task_id = request.data.get('task_id')
        if not task_id:
            return Response({"error": "task_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        success, message = TaskQueueManager.cancel_task(task_id)
        if success:
            return Response({"status": "OK", "message": message}, status=status.HTTP_200_OK)
        return Response({"status": "NG", "message": message}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'])
    def status(self, request):
        """获取队列状态摘要"""
        queue_info = TaskQueueManager.get_all_tasks()
        return Response({
            "max_concurrent": queue_info['max_concurrent'],
            "current_running": queue_info['current_running'],
            "pending_count": len(queue_info['pending']),
            "running": queue_info['running'],
            "pending": queue_info['pending'][:10]  # Limit to 10 for display
        }, status=status.HTTP_200_OK)


class ResourcePoolViewSet(viewsets.ModelViewSet):
    """资源池管理视图"""
    queryset = ResourcePool.objects.all()
    serializer_class = ResourcePoolSerializer

    @action(detail=False, methods=['post'])
    def allocate(self, request):
        """分配资源给任务"""
        resource_id = request.data.get('resource_id')
        task_id = request.data.get('task_id')

        try:
            resource = ResourcePool.objects.get(id=resource_id)
            task = TestExecutionTask.objects.get(task_id=task_id)

            if resource.allocate(task):
                return Response({
                    "status": "OK",
                    "message": f"Resource {resource.name} allocated to task {task_id}"
                }, status=status.HTTP_200_OK)
            else:
                return Response({
                    "status": "NG",
                    "message": "Resource not available or at capacity"
                }, status=status.HTTP_400_BAD_REQUEST)
        except ResourcePool.DoesNotExist:
            return Response({"error": "Resource not found"}, status=status.HTTP_404_NOT_FOUND)
        except TestExecutionTask.DoesNotExist:
            return Response({"error": "Task not found"}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['post'])
    def release(self, request):
        """释放资源"""
        resource_id = request.data.get('resource_id')

        try:
            resource = ResourcePool.objects.get(id=resource_id)
            resource.release()
            return Response({
                "status": "OK",
                "message": f"Resource {resource.name} released"
            }, status=status.HTTP_200_OK)
        except ResourcePool.DoesNotExist:
            return Response({"error": "Resource not found"}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['get'])
    def available(self, request):
        """获取可用资源列表"""
        available_resources = ResourcePool.objects.filter(
            status='available'
        ).filter(
            Q(current_load__lt=F('max_concurrent')) | Q(max_concurrent__gt=1)
        )
        serializer = self.get_serializer(available_resources, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
