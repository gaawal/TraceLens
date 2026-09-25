import assert from 'node:assert/strict';
import {createTracePilotActionRegistry} from '../src/assistant/actionRegistry';
import {executeUiAction} from '../src/assistant/workstation';
import {captureContextSnapshot, registerAIContext, collectPageContext} from '../src/assistant/contextRegistry';
import {appendDeploymentLog} from '../src/services/deploymentRealtime';
import {selectNextQueuedTask} from '../src/assistant/taskQueue';
const memory = new Map<string,string>();
const storage = {getItem: (k:string)=>memory.get(k)||null, setItem:(k:string,v:string)=>memory.set(k,v)};
Object.assign(globalThis,{sessionStorage:storage,localStorage:storage, window:Object.assign(new EventTarget(),{location:{href:'http://localhost/?page=logs',origin:'http://localhost'}})});
let executed=false;
const registry=createTracePilotActionRegistry({go:async()=>{await Promise.resolve();executed=true;return 'painted';}});
assert.equal(await registry.execute({type:'go'}),'painted');assert.ok(executed);
await assert.rejects(registry.execute({type:'unknown'}));
const aborted=new AbortController();aborted.abort();await assert.rejects(registry.execute({type:'go'},{signal:aborted.signal}));
assert.equal((await executeUiAction({type:'missing'})).status,'failed');
const receive=(raw:Event)=>{const detail=(raw as CustomEvent).detail;detail.__claimed=true;detail.__complete({status:'success',detail:'painted'});};
window.addEventListener('tracelens:assistant-ui',receive);
assert.equal((await executeUiAction({type:'go'})).status,'success');window.removeEventListener('tracelens:assistant-ui',receive);
let environment='prod';const unregister=registerAIContext({page:'logs',getContext:()=>({environment, password:'hidden',nested:{api_key:'hidden'}})});
const first=captureContextSnapshot();environment='staging';const second=captureContextSnapshot();
assert.equal(first.environment,'prod');assert.equal(second.environment,'staging');assert.notEqual(first.snapshot_id,second.snapshot_id);assert.equal(first.password,undefined);assert.equal((first.nested as object).hasOwnProperty('api_key'),false);
unregister();assert.equal(collectPageContext().environment,undefined);
const chunk={type:'deployment.log' as const,deployment_id:1,step_key:'start',stdout:'🙂ok',stderr:'',chunks:[{stream:'stdout',text:'🙂ok'}],process_log_length:5};
assert.deepEqual(appendDeploymentLog('hi',chunk),{text:'hi🙂ok',gap:false});
assert.deepEqual(appendDeploymentLog('hi🙂ok',chunk),{text:'hi🙂ok',gap:false});
assert.equal(appendDeploymentLog('',chunk).gap,true);
console.log('Workstation checks passed: async actions, fail-closed dispatch, abort, receipts, isolated/redacted context snapshots, deployment ordering/dedup/gap.');

// --- queued-message selection -------------------------------------------------
// Regression: a backlog in one conversation used to block the visible one, and a task
// whose conversation was deleted was dequeued and then silently dropped.
{
  const q = [
    {id:'a1',conversationId:'A'}, {id:'a2',conversationId:'A'}, {id:'b1',conversationId:'B'},
  ];
  const known = new Set(['A','B']);
  assert.equal(selectNextQueuedTask(q, known, 'B')?.id, 'b1', 'the active conversation must go first');
  assert.equal(selectNextQueuedTask(q, known, 'A')?.id, 'a1', 'otherwise oldest first');
  assert.equal(selectNextQueuedTask(q, known, 'missing')?.id, 'a1', 'unknown active id falls back to FIFO');
  assert.equal(selectNextQueuedTask(q, known, undefined)?.id, 'a1');
  // A conversation that no longer exists must never be handed back...
  assert.equal(selectNextQueuedTask([{id:'x1',conversationId:'gone'}], known, 'A'), null);
  // ...and must not mask a runnable task behind it.
  assert.equal(selectNextQueuedTask([{id:'x1',conversationId:'gone'},{id:'b1',conversationId:'B'}], known, 'A')?.id, 'b1');
  assert.equal(selectNextQueuedTask([], known, 'A'), null);
  // The selector must not mutate its input.
  assert.deepEqual(q.map(i=>i.id), ['a1','a2','b1']);
  console.log('queued-message selection checks passed');
}

// --- action claiming ----------------------------------------------------------
// Regression: App.tsx used to claim every `tracelens:assistant-ui` event, so the
// "page did not accept this action" fallback was unreachable and no page could own
// an action type of its own.
{
  const seen: Array<{claimed:boolean;status:string;detail:string}> = [];
  const owner = (raw: Event) => {
    const d = (raw as CustomEvent).detail;
    if (d.type !== 'page-owned') return;      // mirrors App.tsx's `actionRegistry.has` guard
    d.__claimed = true;
    d.__complete({status:'success', detail:'page handled it'});
  };
  window.addEventListener('tracelens:assistant-ui', owner);
  const handled = await executeUiAction({type:'page-owned'});
  assert.equal(handled.status, 'success');
  assert.equal(handled.detail, 'page handled it');
  window.removeEventListener('tracelens:assistant-ui', owner);

  const unowned = await executeUiAction({type:'nobody-owns-this'});
  assert.equal(unowned.status, 'failed');
  assert.match(unowned.detail, /未接收操作/, 'unclaimed actions must report the protocol fallback, not a registry error');
  seen.push({claimed:false,status:unowned.status,detail:unowned.detail});
  console.log('action claiming checks passed');
}

// --- 实时监听的折叠动画：差值检测 ----------------------------------------------
// 只有「刚把新日志折进上方入口卡片」的那张卡片（及其祖先链）才该播动画；
// 第一次扫描只当基线，不能让打开实时监听前就存在的日志全部动起来。
{
  const { scanFoldLogs, diffFoldLogs, mergeFoldInflux, pruneFoldInflux } = await import('../src/parser/foldInflux');
  const log = (id: string, timestamp = `2026-09-25 10:00:0${id.slice(-1)}`) => ({
    kind: 'log' as const,
    id: `leaf-${id}`,
    entry: { id, timestamp, message: `line ${id}`, level: 'INFO', severity: 'info', component: 'sil' },
  } as unknown as import('../src/types').TimelineItem);
  const fn = (id: string, children: unknown[]) => ({
    kind: 'function' as const,
    origin: 'consecutive' as const,
    id,
    name: `Func${id}`,
    component: 'sil',
    processId: 'p',
    threadId: 't',
    rpc: { traceId: 'tr', spanId: 'sp', parentSpanId: '0' },
    source: { fileName: 'a.py', lineNumber: 1, raw: 'a.py:1' },
    startEntry: (children[0] as { entry: unknown }).entry,
    endEntry: (children[children.length - 1] as { entry: unknown }).entry,
    children,
    incomplete: false,
  } as unknown as import('../src/types').TimelineItem);
  type Tree = import('../src/types').TimelineItem[];

  const tree1 = [fn('FnA', [log('a1')])] as Tree;
  const base = scanFoldLogs(tree1);
  assert.equal(diffFoldLogs(new Map(), base).length, 0, '首次扫描只建立基线，不产生动画');
  assert.equal(diffFoldLogs(base, base).length, 0, '没有新日志时不动画');

  const tree2 = [fn('FnA', [log('a1'), log('a2'), log('a3')])] as Tree;
  const grown = diffFoldLogs(base, scanFoldLogs(tree2));
  assert.equal(grown.length, 1, '只有真正变多的卡片才算「折进来」');
  assert.deepEqual(grown[0].entries.map((entry) => entry.id), ['a2', 'a3']);
  assert.deepEqual(grown[0].ids, ['FnA'], '顶层卡片没有祖先，只挂自己');

  // 内层节点新增：动画要同时挂到祖先链上，否则父级折叠时根本渲染不出来。
  const tree3 = [fn('FnOuter', [fn('FnInner', [log('b1')])])] as Tree;
  const tree4 = [fn('FnOuter', [fn('FnInner', [log('b1'), log('b2')])])] as Tree;
  const nested = diffFoldLogs(scanFoldLogs(tree3), scanFoldLogs(tree4));
  assert.deepEqual(nested[0].ids, ['FnInner', 'FnOuter']);

  // 状态合并：每张卡片最多留 3 行动画，节点数也有上限。
  let influx = mergeFoldInflux({}, grown, 1000);
  assert.deepEqual(Object.keys(influx), ['FnA']);
  assert.equal(influx.FnA.length, 2);
  influx = mergeFoldInflux(influx, [{ ids: ['FnA'], entries: [log('a4').entry, log('a5').entry, log('a6').entry] }], 1010);
  assert.equal(influx.FnA.length, 3, '同时播放的行数有上限');
  assert.equal(influx.FnA[2].id.includes('a6'), true, '保留的是最新的几行');
  assert.equal(influx.FnA[0].id.includes('a4'), true);

  // 到期清理：动画播完的行必须消失，否则实时高频追加会把 DOM 越堆越多。
  const pruned = pruneFoldInflux(influx, 1010 + 2000);
  assert.deepEqual(pruned, {});
  assert.equal(pruneFoldInflux(influx, 1010 + 100), influx, '没过期时返回原对象，避免无意义重渲染');
  console.log('实时折叠动画差值检查通过');
}
