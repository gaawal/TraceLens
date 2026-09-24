import { registerPageContextReader } from '../assistant/contextRegistry';
import { useEffect, useRef, useState } from 'react';
export function DeploymentLogView({text, taskId, stepKey}: {text: string; taskId: number; stepKey: string}) {
  const view = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const [selected, setSelected] = useState<number>();
  const lines = text.replace(/\u001b\[[0-9;]*[A-Za-z]/g, '').split('\n');
  const start = Math.max(0,lines.length-2000);
  useEffect(() => {if (follow && view.current) view.current.scrollTop = view.current.scrollHeight;}, [text,follow]);
  useEffect(() => {
    const handler = (event: Event) => {
      const context = (event as CustomEvent).detail?.context;
      if (context && selected !== undefined) context.selected_deployment_log = {task_id:taskId,step:stepKey,line:selected+1,text:lines[selected],context:lines.slice(Math.max(0,selected-10),selected+11)};
    };
    return registerPageContextReader(handler, 40);
  }, [taskId,stepKey,selected,text]);
  return <><div className="deployment-log-follow"><span>实时输出 · 点击行可加入 AI 页面上下文{start > 0 ? ' · 显示最近 2000 行' : ''}</span><button onClick={() => setFollow(v => !v)}>{follow ? '暂停跟随' : '跟随最新输出'}</button></div><div className="deployment-process-log" ref={view} onScroll={() => {const el=view.current; if(el && el.scrollHeight-el.scrollTop-el.clientHeight > 60) setFollow(false);}}>{text ? lines.slice(start).map((line,i) => {
    const tone = /\b(error|failed?|fatal|exception)\b|失败|错误|异常/i.test(line) ? 'error' : /\b(warn(?:ing)?)\b|警告/i.test(line) ? 'warning' : /\b(success(?:ful(?:ly)?)?|passed?|completed)\b|成功|完成/i.test(line) ? 'success' : '';
    return <div key={start+i} onClick={() => setSelected(start+i)} className={`deployment-process-log-line ${tone} ${selected===start+i ? 'selected' : ''}`}><span>{start+i+1}</span><code>{line || ' '}</code></div>;
  }) : <div className="deployment-no-live-log-note">等待远程终端输出。</div>}</div></>;
}
