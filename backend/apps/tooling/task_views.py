from django.http import StreamingHttpResponse
from rest_framework.decorators import api_view
from rest_framework.response import Response
from apps.tooling.models import AgentTask
from apps.tooling.task_runtime import event_stream

@api_view(['GET'])
def running_tasks(request):
    # Random conversation IDs scope recovery; never enumerate every user's tasks.
    sessions = [s for s in request.query_params.get('session_ids','').split(',') if s][:50]
    tasks = AgentTask.objects.filter(session_id__in=sessions, status='running').order_by('-created_at')[:50]
    return Response({'tasks':[{'task_id':t.task_id,'session_id':t.session_id,'status':t.status,**t.state} for t in tasks]})

@api_view(['GET'])
def task_events(request, task_id):
    session = request.headers.get('X-Session-Id','') or request.query_params.get('session_id','')
    if not AgentTask.objects.filter(task_id=task_id,session_id=session).exists(): return Response({'message':'任务不存在'},status=404)
    try: after = max(0,int(request.query_params.get('after','0')))
    except ValueError: return Response({'message':'游标无效'},status=400)
    response = StreamingHttpResponse(event_stream(task_id,session,after),content_type='text/event-stream; charset=utf-8')
    response['Cache-Control']='no-cache, no-transform'
    response['X-Accel-Buffering']='no'
    return response
