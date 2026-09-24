from django.db import models

class AgentTask(models.Model):
    task_id = models.CharField(max_length=96, primary_key=True)
    session_id = models.CharField(max_length=96, db_index=True)
    status = models.CharField(max_length=24, default='running', db_index=True)
    state = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class AgentTaskEvent(models.Model):
    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name='events')
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

class AgentUiReceipt(models.Model):
    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE)
    action_id = models.CharField(max_length=96, unique=True)
    receipt = models.JSONField(null=True)
    expires_at = models.DateTimeField()
