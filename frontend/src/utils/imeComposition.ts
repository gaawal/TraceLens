import { useCallback, useRef } from 'react';

/**
 * 输入法选词时按回车，是「确认候选词」，不是「提交」。
 *
 * 只判断 `event.isComposing` 是不够的：有些浏览器（尤其中文输入法把候选词上屏的那一次）
 * 把这次 keydown 报成 `isComposing === false`，`keyCode === 229` 也时有时无 ——
 * 结果就是话还没打完就被当成回车发出去了。
 *
 * 所以除了看事件本身，再记住 `compositionend` 的时刻：刚结束输入法的很短时间内
 * 也先不提交。宽限期只有几十毫秒，正常「打完字再按回车」不受影响。
 */
const IME_ENTER_GRACE_MS = 80;

export interface ImeCompositionGuard {
  onCompositionStart: () => void;
  onCompositionEnd: () => void;
  /** 这一次 Enter/Space 是不是输入法在选词（是的话不要提交）。 */
  isComposing: (event?: { nativeEvent?: { isComposing?: boolean }; keyCode?: number; key?: string }) => boolean;
}

/**
 * 纯判定：这一次按键要不要当成「输入法在选词」而忽略掉提交。
 * 单独抽出来是为了能直接单测（不依赖 React 渲染）。
 */
export function imeCompositionBlocksSubmit(input: {
  composing: boolean;
  lastCompositionEndAt: number;
  now: number;
  graceMs?: number;
  event?: { isComposing?: boolean; keyCode?: number };
}): boolean {
  if (input.composing) return true;
  if (input.event?.isComposing) return true;
  if (Number(input.event?.keyCode ?? 0) === 229) return true;
  const grace = input.graceMs ?? IME_ENTER_GRACE_MS;
  return input.lastCompositionEndAt > 0 && input.now - input.lastCompositionEndAt < grace;
}

export function useImeCompositionGuard(graceMs = IME_ENTER_GRACE_MS): ImeCompositionGuard {
  const composingRef = useRef(false);
  const endedAtRef = useRef(0);

  const onCompositionStart = useCallback(() => {
    composingRef.current = true;
  }, []);

  const onCompositionEnd = useCallback(() => {
    composingRef.current = false;
    endedAtRef.current = Date.now();
  }, []);

  const isComposing = useCallback((event?: { nativeEvent?: { isComposing?: boolean; keyCode?: number }; keyCode?: number }) => {
    const native = event?.nativeEvent;
    return imeCompositionBlocksSubmit({
      composing: composingRef.current,
      lastCompositionEndAt: endedAtRef.current,
      now: Date.now(),
      graceMs,
      event: {
        isComposing: Boolean(native?.isComposing),
        keyCode: Number(event?.keyCode ?? native?.keyCode ?? 0),
      },
    });
  }, [graceMs]);

  return { onCompositionStart, onCompositionEnd, isComposing };
}
