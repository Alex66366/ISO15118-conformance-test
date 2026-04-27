from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('testcase', '0002_auto_20260310_2241'),
    ]

    operations = [
        migrations.CreateModel(
            name='TestExecutionTask',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('task_id', models.CharField(db_index=True, max_length=64, unique=True)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed'), ('cancelled', 'Cancelled')], default='pending', max_length=20)),
                ('priority', models.IntegerField(default=5)),
                ('v2g_config_path', models.TextField(blank=True, default='')),
                ('slac_config_path', models.TextField(blank=True, default='')),
                ('secc_ip', models.CharField(default='ff02::1', max_length=50)),
                ('result_status', models.CharField(blank=True, default='', max_length=20)),
                ('error_message', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('testcase', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='execution_tasks', to='testcase.testcasetype')),
            ],
            options={
                'verbose_name': 'Test Execution Task',
                'verbose_name_plural': 'Test Execution Tasks',
                'ordering': ['-priority', '-created_at'],
            },
        ),
        migrations.CreateModel(
            name='ResourcePool',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('resource_type', models.CharField(choices=[('titan_executor', 'Titan Executor'), ('ssh_connection', 'SSH Connection'), ('test_environment', 'Test Environment')], max_length=30)),
                ('status', models.CharField(choices=[('available', 'Available'), ('in_use', 'In Use'), ('offline', 'Offline'), ('error', 'Error')], default='available', max_length=20)),
                ('host', models.CharField(blank=True, default='', max_length=100)),
                ('port', models.IntegerField(default=22)),
                ('config', models.TextField(blank=True, default='{}')),
                ('max_concurrent', models.IntegerField(default=1)),
                ('current_load', models.IntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('current_task', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='resource_allocations', to='testcase.testexecutiontask')),
            ],
            options={
                'verbose_name': 'Resource Pool',
                'verbose_name_plural': 'Resource Pools',
            },
        ),
    ]
