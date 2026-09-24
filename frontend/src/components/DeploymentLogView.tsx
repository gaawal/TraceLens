import { registerPageContextReader } from '../assistant/contextRegistry';
import { useEffect, useRef, useState } from 'react';
export function DeploymentLogView({text, taskId, stepKey}: {text: string; taskId: number; stepKey: string}) {
  const view = useRef<HTMLDivElement>(null);
  const [selected, setSelected] = useState<number>();
  const lines = text.replace(/\u001b\[[0-9;]*[A-Za-z]/g, '').split('\n').filter(line => line.trim().length > 0);
  const start = Math.max(0,lines.length-2000);
  useEffect(() => {
    const el = view.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    if (nearBottom || el.scrollHeight === el.clientHeight) {
      el.scrollTop = el.scrollHeight;
    }
  }, [text]);
  useEffect(() => {
    const handler = (event: Event) => {
      const context = (event as CustomEvent).detail?.context;
      if (context && selected !== undefined) context.selected_deployment_log = {task_id:taskId,step:stepKey,line:selected+1,text:lines[selected],context:lines.slice(Math.max(0,selected-10),selected+11)};
    };
    return registerPageContextReader(handler, 40);
  }, [taskId,stepKey,selected,text]);
  return <><div className="deployment-log-follow"><span>部署过程日志{start > 0 ? ' · 最近 2000 行' : ''}</span></div><div className="deployment-process-log" ref={view} >{text ? lines.slice(start).map((line,i) => {
    const tone = /\b(error|failed?|fatal|exception)\b|失败|错误|异常/i.test(line) ? 'error' : /\b(warn(?:ing)?)\b|警告/i.test(line) ? 'warning' : /\b(success(?:ful(?:ly)?)?|passed?|completed)\b|成功|完成/i.test(line) ? 'success' : '';
    return <div key={start+i} onClick={() => setSelected(start+i)} className={`deployment-process-log-line ${tone} ${selected===start+i ? 'selected' : ''}`}><span>{start+i+1}</span><code>{line || ' '}</code></div>;
  }) : <div className="deployment-no-live-log-note">等待远程终端输出。</div>}</div></>;
}
