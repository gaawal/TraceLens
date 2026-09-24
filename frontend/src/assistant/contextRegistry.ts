/** Semantic context providers; snapshots are immutable and belong to a chat. */
type PageContext = Record<string, unknown>;
const providers = new Map<symbol, {page: string; priority?: number; getContext: (base?: PageContext) => PageContext}>();
let runtimeContextPatch: PageContext = {};
export function updateAssistantRuntimeContext(patch: PageContext) {
  runtimeContextPatch = { ...runtimeContextPatch, ...patch };
}

const SECRET = /password|passwd|secret|authorization|access.?token|api.?key|private.?key/i;
export function cloneContext(value: PageContext): PageContext {
  const scrub = (item: unknown, depth = 0): unknown => {
    if (depth > 12) return null;
    if (typeof item === 'string') return item.slice(0, 16000).replace(/(Bearer\s+)[\w.\-]+/gi, '$1[redacted]');
    if (Array.isArray(item)) return item.slice(0, 100).map(v => scrub(v, depth+1));
    if (item && typeof item === 'object') return Object.fromEntries(Object.entries(item).filter(([key]) => !SECRET.test(key)).map(([key,v]) => [key,scrub(v,depth+1)]));
    return item;
  };
  return scrub(value) as PageContext;
}
export function registerAIContext(provider: {page: string; priority?: number; getContext: (base?: PageContext) => PageContext}) {
  const id = Symbol(provider.page); providers.set(id,provider);
  return () => {providers.delete(id);};
}
export function collectPageContext(): PageContext {
  if (typeof window === 'undefined') return {};
  const url = new URL(window.location.href);
  const environmentId = Number(sessionStorage.getItem('tracelens-active-resource-v1'));
  const context: PageContext = {page: url.searchParams.get('page') || 'resources', url: url.pathname+url.search,
    environment_id: environmentId > 0 ? environmentId : null,
    client_now_iso: new Date().toISOString(), client_local_time: new Date().toLocaleString('sv-SE', {hour12:false}),
    client_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone};
  // Compatibility for existing pages. Registered providers override their legacy adapter.
  window.dispatchEvent(new CustomEvent('tracelens:assistant-context-request', {detail:{context}}));
  Object.assign(context, runtimeContextPatch);
  for (const provider of [...providers.values()].sort((a,b) => (a.priority || 0)-(b.priority || 0))) {
    if (provider.page === '*' || provider.page === context.page || provider.page === url.searchParams.get('page')) Object.assign(context, provider.getContext(context));
  }
  return cloneContext(context);
}
function generateUUID(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }

  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export function captureContextSnapshot(context = collectPageContext()): PageContext {
  return {...cloneContext(context), snapshot_id: generateUUID(), captured_at: new Date().toISOString()};
}

/** Migration adapter: pages keep their semantic reader while sharing one registry. */
export function registerPageContextReader(reader: (event: Event) => void, priority = 10) {
  return registerAIContext({page:'*', priority, getContext: (base = {}) => {
    const detail = {context:{...base}};
    reader(new CustomEvent('tracelens:assistant-context-request', {detail}));
    return detail.context;
  }});
}
