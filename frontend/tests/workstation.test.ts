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

// --- AI 日志证据：先量后压 + 组件名解析 -------------------------------------------
{
  const {
    buildLogEvidence,
    normalizeEvidenceMaxChars,
    formatEvidenceMaxChars,
    saveEvidenceMaxChars,
    loadEvidenceMaxChars,
    DEFAULT_EVIDENCE_MAX_CHARS,
    EVIDENCE_MAX_CHARS_MIN,
    EVIDENCE_MAX_CHARS_MAX,
  } = await import('../src/assistant/logEvidence');
  const { resolveComponentTargets } = await import('../src/components/RemoteLogQueryPanel');

  const entry = (index: number, message: string) => ({
    id: `e${index}`,
    lineNumber: index + 1,
    timestamp: `2026-09-25 10:00:${String(index % 60).padStart(2, '0')}`,
    level: index === 5 ? 'ERROR' : 'INFO',
    severity: index === 5 ? 'error' : 'info',
    component: 'cpfr',
    logModule: 'cpfr',
    message,
    raw: `[2026-09-25 10:00:00] [INFO] [CPFR] [25312] [30312] [cpfr] [normal] [cpfr:F:1] ${message}`,
    source: { fileName: 'cpfr.log', lineNumber: index + 1 },
    sourceFile: 'cpfr.log',
  });

  // 没超上限 → 原文直送：正文里必须是逐行真实日志，而不是模板占位符。
  const small = [0, 1, 2, 3, 4, 5, 6, 7].map((index) => entry(index, `step ${index} reached nominal state`));
  const raw = buildLogEvidence(small as never, 100, DEFAULT_EVIDENCE_MAX_CHARS);
  assert.equal(raw?.mode, 'raw');
  assert.equal(raw?.max_chars, DEFAULT_EVIDENCE_MAX_CHARS);
  assert.equal(raw?.text_chars, raw?.text.length);
  assert.ok(raw!.text.includes('step 2 reached nominal state'), 'raw 模式必须包含原始日志行');
  assert.ok(!raw!.text.includes('×'), 'raw 模式不该出现模板归并记号');
  assert.equal(raw?.stats.mode, 'raw');
  assert.equal(raw?.stats.char_budget, DEFAULT_EVIDENCE_MAX_CHARS + 512);
  assert.equal(raw?.text.length <= DEFAULT_EVIDENCE_MAX_CHARS, true);

  // 超上限 → 走压缩：同一模板的连续行归并成一条并标 ×N，总长明显变小。
  const noisy = Array.from({ length: 400 }, (_, index) => entry(index, `wafer=W${index % 9} thermal budget recalculated with a fairly long trailing description to inflate the payload`));
  const compressed = buildLogEvidence(noisy as never, 100, 4000);
  assert.equal(compressed?.mode, 'compressed');
  assert.ok(compressed!.raw_text_chars > 4000, '压缩前必须确实超过上限');
  assert.ok(compressed!.text_chars < compressed!.raw_text_chars);
  assert.ok(compressed!.groups < 400, `模板归并应显著减少条目，实际 ${compressed!.groups}`);
  assert.equal(compressed?.stats.max_chars, 4000);
  assert.equal(compressed?.stats.token_budget, undefined, '已经不用 token 预算了');

  // 上限本身：脏数据回退默认值，越界被夹住。
  assert.equal(normalizeEvidenceMaxChars(0), DEFAULT_EVIDENCE_MAX_CHARS);
  assert.equal(normalizeEvidenceMaxChars('not-a-number'), DEFAULT_EVIDENCE_MAX_CHARS);
  assert.equal(normalizeEvidenceMaxChars(10), EVIDENCE_MAX_CHARS_MIN);
  assert.equal(normalizeEvidenceMaxChars(9_999_999), EVIDENCE_MAX_CHARS_MAX);
  assert.equal(formatEvidenceMaxChars(40000), '4 万字符');
  // window 上的 storage 由本文件顶部注入；这里显式挂一次，验证真正写读回来的是同一个值。
  Object.assign(window, { localStorage: storage });
  assert.equal(saveEvidenceMaxChars(80000), 80000);
  assert.equal(loadEvidenceMaxChars(), 80000);
  assert.equal(saveEvidenceMaxChars(DEFAULT_EVIDENCE_MAX_CHARS), DEFAULT_EVIDENCE_MAX_CHARS);
  assert.equal(loadEvidenceMaxChars(), DEFAULT_EVIDENCE_MAX_CHARS);

  // 组件名 → 目录里的真实 (subsystem, fm)：大小写/显示名都要能对上。
  const catalog = [
    { id: 1, name: 'cpfr', display_name: '配方流量', effective_name: 'cpfr', enabled: true, sort_order: 1, description: '', fms: [{ id: 1, name: 'cpfr', display_name: '', effective_name: 'cpfr', kind: 'normal' }] },
    { id: 3, name: 'mecore', display_name: '运动核心', effective_name: 'mecore', enabled: true, sort_order: 2, description: '', fms: [
      { id: 5, name: 'cpcore', display_name: '', effective_name: 'cpcore', kind: 'normal' },
      { id: 6, name: 'mecore', display_name: '', effective_name: 'mecore', kind: 'normal' },
    ] },
  ] as never;
  assert.deepEqual(resolveComponentTargets('MECORE', catalog), ['mecore\u0000mecore\u0000normal'], '大小写不敏感地命中同名模块');
  assert.deepEqual(resolveComponentTargets('配方流量', catalog), ['cpfr\u0000cpfr\u0000normal'], '显示名也要能命中');
  assert.deepEqual(resolveComponentTargets('运动核心', catalog).length, 2, '子系统显示名命中时选中该子系统全部模块');
  assert.deepEqual(resolveComponentTargets('nope', catalog), []);
  console.log('AI 日志证据与组件名解析检查通过');
}

// --- 多行日志（续行）兼容 -------------------------------------------------------
// 解析是按行的：一条记录打好几行才出现下一个时间戳时，过去的续行会被当成
// 「不符合当前日志格式」丢掉，于是正文被截断。这里锁定「补全正文 + 不破坏原有解析」。
{
  const {
    parseLogText,
    looksLikeLogContinuation,
    appendLogContinuation,
    logFormatIsKnown,
    LOG_CONTINUATION_MAX_LINES,
  } = await import('../src/parser/logParser');

  const head = (message: string) => `[2026-09-25 10:00:00.100] [ERROR] [CPFR] [25312] [30312] [cpfr] [normal] [cpfr:F:1] [F] ${message}`;

  // ① 堆栈型多行记录：续行并进上一条，正文完整，且不再产生格式告警。
  const traceback = [
    head('coolant loop failed:'),
    'Traceback (most recent call last):',
    '  File "cpfr/coolant.py", line 430, in CoolantLoop',
    '    raise CoolantError("flow below limit")',
    'CoolantError: flow 3.8L/min below threshold 5.0L/min',
    head('next record'),
  ].join('\n');
  const parsed = parseLogText(traceback);
  assert.equal(parsed.entries.length, 2);
  assert.equal(parsed.issues.length, 0, '续行不该再报「不符合当前日志格式」');
  assert.equal(parsed.entries[0].continuationLines, 4);
  assert.ok(parsed.entries[0].message.includes('coolant loop failed:'));
  assert.ok(parsed.entries[0].message.includes('raise CoolantError'), '正文必须包含续行');
  assert.ok(parsed.entries[0].raw.includes('Traceback (most recent call last):'), '原文必须完整');
  assert.ok(!parsed.entries[0].message.includes('next record'), '下一条正常日志不能被吸进来');
  assert.equal(parsed.entries[1].continuationLines, undefined);

  // ② JSON dump 型（以 { 开头的续行）。
  const dump = [
    head('payload={'),
    '  "wafer": "W09",',
    '  "steps": [1, 2, 3]',
    '}',
  ].join('\n');
  const dumpParsed = parseLogText(dump);
  assert.equal(dumpParsed.entries.length, 1);
  assert.equal(dumpParsed.issues.length, 0);
  assert.equal(dumpParsed.entries[0].continuationLines, 3);
  assert.ok(dumpParsed.entries[0].raw.includes('"steps": [1, 2, 3]'));

  // ③ 未知格式（全都不解析不出来）维持原样：逐行告警，绝不合并成一条。
  const unknown = parseLogText(['custom one', 'custom two', 'custom three'].join('\n'));
  assert.equal(unknown.entries.length, 0);
  assert.equal(unknown.issues.length, 3);

  // ④ 行内带时间戳但格式不支持 → 是新记录，保持告警，不能当续行吞掉。
  const foreign = parseLogText([
    head('header ok'),
    '2026-09-25T10:00:01.500Z other-service 42 - foreign format',
    head('second'),
  ].join('\n'));
  assert.equal(foreign.entries.length, 2);
  assert.equal(foreign.issues.length, 1);
  assert.ok(!foreign.entries[0].message.includes('foreign format'));

  // ⑤ 判定函数本身：无提示且格式未知时不乱认；正文末尾有开口提示时认。
  assert.equal(looksLikeLogContinuation('  indented line'), true);
  assert.equal(looksLikeLogContinuation('plain sentence with no cue'), false);
  assert.equal(looksLikeLogContinuation('plain sentence with no cue', { message: 'x', raw: 'x' }, { knownFormat: true }), true);
  assert.equal(looksLikeLogContinuation('2026-09-25 10:00:01 x', { message: 'a:', raw: 'a:' }), false);
  assert.equal(looksLikeLogContinuation('continuation', { message: 'tail ends with:', raw: 'tail ends with:' }), true);
  assert.equal(logFormatIsKnown(10, 2), true);
  assert.equal(logFormatIsKnown(0, 5), false);
  assert.equal(logFormatIsKnown(3, 9), false);
  const capped = { message: 'm', raw: 'r', continuationLines: LOG_CONTINUATION_MAX_LINES };
  assert.equal(looksLikeLogContinuation('  more', capped), false, '续行数到上限就退回原来的告警行为');
  assert.equal(appendLogContinuation({ message: 'a', raw: 'a' }, '  b').message, 'a\n  b');
  console.log('多行日志续行兼容检查通过');
}
