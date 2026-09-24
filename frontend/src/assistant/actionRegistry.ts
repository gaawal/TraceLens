export type TracePilotUiAction = Record<string, unknown> & { type?: string };
export type TracePilotActionHandler = (detail: TracePilotUiAction) => unknown | Promise<unknown>;

export interface TracePilotActionContext {
  confirmed?: boolean;
  source?: string;
  page?: string;
  signal?: AbortSignal;
}

export interface TracePilotActionRegistry {
  has(type: string): boolean;
  dispatch(detail: TracePilotUiAction, context?: TracePilotActionContext): boolean;
  execute(detail: TracePilotUiAction, context?: TracePilotActionContext): Promise<unknown>;
  types(): string[];
}

/**
 * Semantic UI Action Registry used by TracePilot.
 *
 * Handlers operate on business actions (open log locator, run extraction, ...),
 * never DOM selectors, coordinates, CSS paths or synthetic mouse clicks.
 */
export function createTracePilotActionRegistry(
  handlers: Record<string, TracePilotActionHandler>,
): TracePilotActionRegistry {
  const normalized = new Map<string, TracePilotActionHandler>();
  Object.entries(handlers).forEach(([type, handler]) => {
    const key = String(type || '').trim();
    if (key && typeof handler === 'function') normalized.set(key, handler);
  });

  return {
    has(type: string) {
      return normalized.has(String(type || '').trim());
    },
    dispatch(detail: TracePilotUiAction, context?: TracePilotActionContext) {
      const type = String(detail?.type || '').trim();
      if (context) detail = { ...detail, __trace_context: context };
      const handler = normalized.get(type);
      if (!handler) return false;
      // Legacy dispatch remains fire-and-forget; the workstation uses execute.
      void Promise.resolve().then(() => handler(detail)).catch(() => undefined);
      return true;
    },
    async execute(detail, context) {
      context?.signal?.throwIfAborted();
      const handler = normalized.get(String(detail.type || '').trim());
      if (!handler) throw new Error(`当前页面不支持操作：${String(detail.type || '')}`);
      const result = await handler({ ...detail, __trace_context: context });
      context?.signal?.throwIfAborted();
      return result;
    },
    types() {
      return Array.from(normalized.keys());
    },
  };
}
