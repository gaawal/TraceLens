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
