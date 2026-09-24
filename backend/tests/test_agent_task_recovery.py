from datetime import timedelta
from types import SimpleNamespace
import pytest
from django.utils import timezone
from rest_framework.test import APIClient
from apps.tooling.models import AgentTask, AgentUiReceipt
from apps.tooling.task_runtime import start_task, persist_event, read_events
from apps.tooling.assistant_runtime.state import RunStateStore

@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    monkeypatch.setattr('apps.tooling.task_runtime.RedisLogStore.set_json',lambda *a,**k: True)

@pytest.mark.django_db
def test_start_idempotent_and_session_isolation(monkeypatch):
    starts=[]
    monkeypatch.setattr('apps.tooling.task_runtime.threading.Thread',lambda **kw:SimpleNamespace(start=lambda: starts.append(kw)))
    first=start_task({'run_id':'r1','session_id':'s1','message':'query'},{'page':'logs'})
    assert start_task({'run_id':'r1','session_id':'s1'},{'page':'other'}).task_id==first.task_id
    assert len(starts)==1
    assert AgentTask.objects.get(pk='r1').state['context_snapshot']=={'page':'logs'}
    with pytest.raises(ValueError): start_task({'run_id':'r1','session_id':'s2'}, {})

@pytest.mark.django_db
def test_progress_replay_and_no_repeated_page_mutation():
    AgentTask.objects.create(task_id='r',session_id='s',state={})
    persist_event('r',{'type':'token','delta':'hello'})
    persist_event('r',{'type':'ui_action','action_id':'a','action':{'type':'open_workspace_page'}})
    persist_event('r',{'type':'done','message':'hello world'})
    status,events=read_events('r','s')
    assert status=='completed' and len(events)==3
    assert events[1]['payload']['type']=='trace'
    assert events[1]['payload']['trace']['stage']=='ui'
    assert read_events('r','s',events[-1]['id'])[1]==[]
    with pytest.raises(AgentTask.DoesNotExist): read_events('r','another')

@pytest.mark.django_db
def test_receipt_cross_worker_and_duplicate_rejected():
    AgentTask.objects.create(task_id='r',session_id='s',state={})
    AgentUiReceipt.objects.create(task_id='r',action_id='unique',expires_at=timezone.now()+timedelta(seconds=30))
    client=APIClient();body={'run_id':'r','action_id':'unique','receipt':{'status':'success'}}
    assert client.post('/api/tools/assistant-ui-receipt/',body,format='json').status_code==200
    assert client.post('/api/tools/assistant-ui-receipt/',body,format='json').status_code==409
    store=RunStateStore();store.make_durable('r')
    assert store.wait_ui('r','unique',timeout=.1)['status']=='success'

@pytest.mark.django_db
def test_running_endpoint_does_not_expose_other_sessions():
    AgentTask.objects.create(task_id='r1',session_id='s1',state={'progress':60})
    AgentTask.objects.create(task_id='r2',session_id='s2',state={'progress':70})
    client=APIClient()
    assert client.get('/api/ai/tasks/running').json()['tasks']==[]
    tasks=client.get('/api/ai/tasks/running?session_ids=s1').json()['tasks']
    assert len(tasks)==1 and tasks[0]['task_id']=='r1'

def test_explicit_llm_identity_works_without_contextvar():
    from apps.tooling.llm.client import LLMClient, set_llm_session_id
    client=object.__new__(LLMClient);client.session_id='isolated'
    set_llm_session_id('unrelated')
    assert client._request_kwargs('model',[])['extra_headers']['X-Session-Id']=='isolated'
    set_llm_session_id('')
