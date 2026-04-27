from django.db.models import fields
from rest_framework import serializers
from .models import MessageType, TestcaseType, TestExecutionTask, ResourcePool

class TestcaseSerializer(serializers.ModelSerializer):
    class Meta(object):
        model = TestcaseType
        fields = ['id', 'name', 'type', 'status',\
            'build_status', 'condition', 'expected',\
                'pics', 'pixit', 'path']

class MessageSerializer(serializers.ModelSerializer):
    CMN = serializers.SerializerMethodField('get_cmn')
    AC = serializers.SerializerMethodField('get_ac')
    DC = serializers.SerializerMethodField('get_dc')

    def _get_type_stats(self, object, type_name):
        qs = object.testcasetype_set.filter(type=type_name)
        pass_count = qs.filter(status='p').count()
        fail_count = qs.filter(status='f').count() + qs.filter(status='e').count()
        executed_count = qs.exclude(status='n').count()
        total_count = qs.count()
        return {
            "pass": pass_count,
            "fail": fail_count,
            "executed": executed_count,
            "total": total_count
        }

    def get_cmn(self, object):
        return self._get_type_stats(object, 'CMN')
    def get_ac(self, object):
        return self._get_type_stats(object, 'AC')
    def get_dc(self, object):
        return self._get_type_stats(object, 'DC')

    class Meta(object):
        model = MessageType
        fields = ['id', 'name', "CMN", "AC", "DC"]


class TestExecutionTaskSerializer(serializers.ModelSerializer):
    testcase_name = serializers.CharField(source='testcase.name', read_only=True)

    class Meta:
        model = TestExecutionTask
        fields = [
            'id', 'task_id', 'testcase', 'testcase_name', 'status', 'priority',
            'v2g_config_path', 'slac_config_path', 'secc_ip',
            'result_status', 'error_message',
            'created_at', 'started_at', 'ended_at'
        ]
        read_only_fields = ['id', 'created_at', 'started_at', 'ended_at']


class ResourcePoolSerializer(serializers.ModelSerializer):
    current_task_id = serializers.CharField(source='current_task.task_id', read_only=True, allow_null=True)

    class Meta:
        model = ResourcePool
        fields = [
            'id', 'name', 'resource_type', 'status',
            'host', 'port', 'config',
            'current_task', 'current_task_id',
            'max_concurrent', 'current_load',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']