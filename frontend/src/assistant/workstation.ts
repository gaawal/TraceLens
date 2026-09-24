export interface UiReceipt {
  status: 'success' | 'failed';
  detail: string;
  result?: unknown;
}

/** Render acknowledgement, bounded even when the tab is in the background. */
export function afterPaint(): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, 120);
    requestAnimationFrame(() => requestAnimationFrame(() => { clearTimeout(timer); resolve(); }));
  });
}

export async function executeUiAction(action: Record<string, unknown>, signal?: AbortSignal): Promise<UiReceipt> {
  signal?.throwIfAborted();
  return new Promise((resolve, reject) => {
    const controller = new AbortController();
    let settled = false;
    const finish = (receipt: UiReceipt) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      resolve(receipt);
    };
    const abort = () => {
      controller.abort();
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(new DOMException('用户已接管页面', 'AbortError'));
    };
    const timer = setTimeout(() => {
      controller.abort();
      finish({ status: 'failed', detail: '页面操作超时，未确认完成' });
    }, 15000);
    signal?.addEventListener('abort', abort, { once: true });
    const detail = { ...action, __signal: controller.signal, __complete: finish, __claimed: false };
    window.dispatchEvent(new CustomEvent('tracelens:assistant-ui', { detail }));
    if (!detail.__claimed) finish({ status: 'failed', detail: `当前页面未接收操作 ${String(action.type)}` });
  });
}
