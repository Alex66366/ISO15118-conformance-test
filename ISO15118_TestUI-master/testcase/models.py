from django.db import models
from django.db.models.fields import AutoField, CharField, IntegerField, TextField
from base.models import BaseModel
from django.urls import reverse
from django.db.models.functions import Coalesce

class TestcaseManager(models.Manager):
    def create_testcase(self, name:str, type:str="CMN", status:str="n", build_status:str="NA", condition:str="", expected:str="", pics:str="", pixit:str="", path:str="", msg_type=None):
        tc = TestcaseType()
        if (name != ""):
            tc.name = name
        else:
            raise ValueError("Testcase name is not allowed to be blank")
        if (type in dict(TestcaseType.TESTCASE_TYPE)):
            tc.type = type
        else:
            raise ValueError(f"Testcase type must has value in {dict(TestcaseType.TESTCASE_TYPE)}")
        if (status in dict(TestcaseType.TESTCASE_STATUS)):
            tc.status = status
        else:
            raise ValueError(f"Testcase status must has value in {dict(TestcaseType.TESTCASE_STATUS)}")
        if (build_status in dict(TestcaseType.TESTCASE_BUILD_STATUS)):
            tc.build_status = build_status
        else:
            raise ValueError(f"Testcase build status must has value in {dict(TestcaseType.TESTCASE_BUILD_STATUS)}")
        tc.condition = condition
        tc.expected = expected
        tc.pics = pics
        tc.pixit = pixit
        tc.path = path
        if msg_type != None:
            tc.msg_type = msg_type
        return tc

    def with_counts(self):
        return self.annotate(
            num_responses=Coalesce(models.Count("response"), 0)
        )

    # return page of object
    def get_pages(self, pageno):
        pass

class MessageType(BaseModel):
    name = TextField(null=False, blank=False, unique=True)
    def __str__(self) -> str:
        return f"{self.name}"

# Create your models here.
class TestcaseType(BaseModel):
    '''
    primary_key
    If True, this field is the primary key for the model.
    If you don’t specify primary_key=True for any fields in your model, Django will automatically add an IntegerField to hold the primary key, so you don’t need to set primary_key=True on any of your fields unless you want to override the default primary-key behavior. For more, see Automatic primary key fields.
    unique
        If True, this field must be unique throughout the table.
    '''
    TESTCASE_TYPE = [('AC', 'AC Station'), ('DC', 'DC Station'), ('CMN', 'Common')]
    TESTCASE_STATUS = [('n', 'none'), ('p', 'pass'), ('i', 'inconclusion'), ('f', 'fail'), ('e', 'error')]
    TESTCASE_BUILD_STATUS = [('NA', 'Not available'), ('OK', 'Builded')]
    # index as primary key
    id = AutoField(primary_key=True)
    name = TextField(null=False, blank=False, unique=True)
    type = CharField(null=False, blank=False, choices=TESTCASE_TYPE, max_length=20, default='CMN')
    status = CharField(null=False, blank=False, choices=TESTCASE_STATUS, max_length=20, default='n')
    build_status = CharField(null=False, blank=False, choices=TESTCASE_BUILD_STATUS, max_length=20, default='NA')
    # describe test case objective input condition
    condition = TextField(null=False, blank=True)
    # describe test case objective expected output behavior
    expected = TextField(null=False, blank=True)
    # test case Protocol Implementation Conformance Statement (PICS) configuration - json string
    pics = TextField(null=False, blank=True)
    # test case Protocol Implementation extra Information for Testing (PIXIT) configuration - json string
    pixit = TextField(null=False, blank=True)
    path = TextField(null=False, blank=True)
    msg_type = models.ForeignKey('MessageType', on_delete=models.SET_NULL, null=True)
    # TTCN-3 code storage
    code = TextField(null=False, blank=True, default='')
    is_generated = models.BooleanField(default=False)
    compile_status = CharField(null=False, blank=True, choices=[('pending', 'Pending'), ('compiling', 'Compiling'), ('success', 'Success'), ('failed', 'Failed')], max_length=20, default='pending')
    compile_output = TextField(null=False, blank=True, default='')
    project_path = TextField(null=False, blank=True, default='')
    objects = TestcaseManager()

    def get_absolute_url(self):
        return reverse('testcase-detail', args=[str(self.id)])


class TestExecutionTask(models.Model):
    """
    测试执行任务跟踪模型 - 用于持久化任务状态
    """
    TASK_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    task_id = models.CharField(max_length=64, unique=True, db_index=True)
    testcase = models.ForeignKey(TestcaseType, on_delete=models.CASCADE, related_name='execution_tasks')
    status = models.CharField(max_length=20, choices=TASK_STATUS_CHOICES, default='pending')
    priority = models.IntegerField(default=5)

    v2g_config_path = models.TextField(blank=True, default='')
    slac_config_path = models.TextField(blank=True, default='')
    secc_ip = models.CharField(max_length=50, default='ff02::1')

    result_status = models.CharField(max_length=20, blank=True, default='')  # pass, fail, error, etc.

    error_message = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-priority', '-created_at']
        verbose_name = 'Test Execution Task'
        verbose_name_plural = 'Test Execution Tasks'

    def __str__(self):
        return f"{self.task_id} - {self.testcase.name} ({self.status})"


class ResourcePool(models.Model):
    """
    资源池模型 - 管理测试执行资源
    """
    RESOURCE_TYPE_CHOICES = [
        ('titan_executor', 'Titan Executor'),
        ('ssh_connection', 'SSH Connection'),
        ('test_environment', 'Test Environment'),
    ]

    RESOURCE_STATUS_CHOICES = [
        ('available', 'Available'),
        ('in_use', 'In Use'),
        ('offline', 'Offline'),
        ('error', 'Error'),
    ]

    name = models.CharField(max_length=100, unique=True)
    resource_type = models.CharField(max_length=30, choices=RESOURCE_TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=RESOURCE_STATUS_CHOICES, default='available')

    host = models.CharField(max_length=100, blank=True, default='')
    port = models.IntegerField(default=22)
    config = models.TextField(blank=True, default='{}')  # JSON configuration

    current_task = models.ForeignKey(
        TestExecutionTask,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='resource_allocations'
    )

    max_concurrent = models.IntegerField(default=1)
    current_load = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Resource Pool'
        verbose_name_plural = 'Resource Pools'

    def __str__(self):
        return f"{self.name} ({self.resource_type}) - {self.status}"

    def allocate(self, task):
        """分配资源给任务"""
        if self.current_load < self.max_concurrent and self.status == 'available':
            self.current_load += 1
            self.status = 'in_use'
            self.current_task = task
            self.save()
            return True
        return False

    def release(self):
        """释放资源"""
        self.current_load = max(0, self.current_load - 1)
        if self.current_load == 0:
            self.status = 'available'
        self.current_task = None
        self.save()
