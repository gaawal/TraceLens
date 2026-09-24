"""Detached assistant jobs with durable events; browser connections only subscribe.

Database is authoritative; Redis is a progress mirror, so Redis outages do not
lose tasks. A browser refresh never replays a command or starts another worker.
"""
from __future__ import annotations
import asyncio
import json
import threading
import uuid
from django.db import close_old_connections
from django.utils import timezone
from asgiref.sync import sync_to_async
from apps.tooling.models import AgentTask, AgentTaskEvent
from apps.logsources.services.redis_store import RedisLogStore


def persist_event(task_id, event):
    event = json.loads(json.dumps(event, ensure_ascii=False, default=str))
    task = AgentTask.objects.get(pk=task_id)
    state = task.state
    state['updated_at'] = timezone.now().isoformat()
    kind = event.get('type')
    if kind == 'task':
        state.update(stage=event.get('phase', state.get('stage')), progress=event.get('progress',state.get('progress',0)))
    if kind == 'tool_call': state['current_tool'] = event.get('tool', event.get('tool_id', ''))
    if kind == 'token_reset': state['response'] = event.get('text','')
    if kind == 'token': state['response'] = str(state.get('response','')) + str(event.get('text',event.get('delta','')))
    if kind == 'done':
        task.status = 'completed'
        state.update(response=event.get('message',''), progress=100, current_tool='', result=event)
    if kind == 'error': task.status = 'failed'; state['error'] = event.get('message','')
    if kind == 'task' and event.get('status') == 'stopped': state['stopped'] = True
    if kind == 'done' and state.get('stopped'): task.status = 'stopped'
    task.state = state
    task.save(update_fields=['state','status','updated_at'])
    row = AgentTaskEvent.objects.create(task=task, payload=event)
    RedisLogStore.set_json(f'ai:task:{task_id}', {**state, 'status':task.status}, 86400)
    return row.id


def start_task(payload, context):
    task_id = str(payload.get('run_id') or uuid.uuid4().hex)[:96]
    session_id = str(payload.get('session_id') or context.get('session_id') or '')[:96]
    if not session_id: raise ValueError('缺少 session_id')
    # Model / reasoning effort chosen in the composer. Validated by the caller; persisted
    # so a resumed stream reports the brain that actually ran.
    model_choice = str(payload.get('model') or '')[:120]
    effort_choice = str(payload.get('effort') or '')[:32]
    state = {'session_id':session_id, 'task_id':task_id, 'context_snapshot':context,
             'messages':payload.get('history') or [], 'stage':'prepare', 'progress':0,
             'model':model_choice, 'effort':effort_choice,
             'current_tool':'', 'updated_at':timezone.now().isoformat(), 'response':''}
    task, created = AgentTask.objects.get_or_create(task_id=task_id, defaults={'session_id':session_id,'state':state})
    if task.session_id != session_id: raise ValueError('任务不属于此会话')
    if not created: return task
    context = {**context, 'session_id':session_id, 'task_id':task_id}
    def worker():
        close_old_connections()
        try:
            # The worker runs in its own thread, so the contextvars must be set here
            # rather than in the request that spawned it.
            from apps.tooling.llm.client import set_llm_overrides, set_llm_session_id
            set_llm_overrides(model_choice, effort_choice)
            # The sync /assistant-chat/ view sets this, but the detached stream path never
            # did, so streamed runs reached the gateway without X-Session-Id.
            set_llm_session_id(session_id)
            from apps.tooling.assistant_runtime.state import RUN_STATE
            RUN_STATE.make_durable(task_id)
            from apps.tooling.assistant import chat_stream
            for event in chat_stream(message=str(payload.get('message') or ''), history=payload.get('history') or [],
                                     context=context, skill_id=str(payload.get('skill_id') or 'auto'),
                                     memory=str(payload.get('memory') or ''), run_id=task_id):
                persist_event(task_id,event)
        except Exception as exc:
            persist_event(task_id,{'type':'error','message':str(exc)})
        finally:
            close_old_connections()
    threading.Thread(target=worker,name=f'ai-task-{task_id}',daemon=True).start()
    return task


def read_events(task_id, session_id, after=0):
    task = AgentTask.objects.get(task_id=task_id,session_id=session_id)
    events = list(task.events.filter(id__gt=after).order_by('id').values('id','payload')[:200])
    from apps.tooling.assistant_runtime.state import RUN_STATE
    for item in events:
        event = item['payload']
        if event.get('type') == 'ui_action' and not RUN_STATE.is_ui_pending(task_id,event.get('action_id','')):
            item['payload'] = {'type':'trace', 'trace':{'stage':'ui', 'id':event.get('action_id'), 'title':'历史页面操作', 'status':'info', 'detail':'回放记录，不重复操作页面；执行结果见操作回执'}}
    return task.status, events


async def event_stream(task_id, session_id, after=0):
    yield b': tracepilot-stream-open\n\n'
    quiet = 0
    while True:
        status, events = await sync_to_async(read_events, thread_sensitive=False)(task_id,session_id,after)
        for item in events:
            after = item['id']
            event = {**item['payload'], 'event_id':after}
            yield f'id: {after}\nevent: {event.get("type", "message")}\ndata: {json.dumps(event,ensure_ascii=False)}\n\n'.encode()
        if not events and status not in {'running','cancelling'}: break
        if not events:
            quiet += 1
            if quiet % 40 == 0: yield b': heartbeat\n\n'
            await asyncio.sleep(.25)
