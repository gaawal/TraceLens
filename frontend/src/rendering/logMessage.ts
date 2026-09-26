import type { LogEntry } from '../types';

/**
 * 折叠起来的函数卡片，展开后要在同一行显示入口日志的正文。
 *
 * 但日志原文里已经带了一次函数名和调用边界符，例如：
 *   `Stage_WSP_HOME() >() enter stage homing stage start step=1/6 wafer=W01`
 * 函数名（`Stage_WSP_HOME()`）卡片头上本来就有，边界符 `>()` 只是折叠器的标记，
 * 两个再重复一遍既啰嗦又把这一行撑得很长，所以只留下真正的信息：
 *   `enter stage homing stage start step=1/6 wafer=W01`
 *
 * 只在开头剥一次，正文中间出现的函数名不动（比如 `call Stage_WSP_HOME() failed` 保持原样）。
 * 如果剥完什么都不剩（原文只有函数名），就退回原文，避免展开后一片空白。
 */
export function stripFunctionPrefixFromMessage(message: string, functionName?: string): string {
  const raw = String(message ?? '').trim();
  if (!raw) return raw;
  let text = raw;

  const bare = String(functionName ?? '')
    .replace(/\(\s*\)$/, '')
    .trim();
  if (bare) {
    const escaped = bare.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    // 可选方括号包裹（部分模块打印成 `[Stage_WSP_HOME]`）+ 可选空括号
    text = text.replace(new RegExp(`^\\s*\\[?${escaped}\\]?\\s*(?:\\(\\s*\\))?\\s*`), '');
  }
  // 兜底：即使拿不到函数名，也剥掉开头的 `Name()` / `[name]()` 这种纯标识
  text = text.replace(/^\[?[A-Za-z_~][\w:<>~.\-]*\]?\s*\(\s*\)\s*/, '');
  // 调用边界符：>() 进入 / <() 退出，以及 `< ()` 这类带空格的写法
  text = text.replace(/^[<>]\s*\(\s*\)\s*/, '');

  const trimmed = text.trim();
  return trimmed || raw;
}

/** 入口行的边界符：`>()` 表示调用进入、`<()` 表示退出；允许日志里写成 `> ( )` 这种带空格的形态。 */
const BOUNDARY_MARKER_REGEX = /[<>]\s*\(\s*\)/;

/**
 * 折叠栏（函数名那个位置）**展开后**要显示什么。
 *
 * 折叠是**按入口行里的「函数名 + 边界符」**成立的：`MoveAbsolute() >()` 是进入、
 * `MoveAbsolute() <()` 是退出，而连续行 / 尾随行的折叠根本没有边界符。
 * 所以展开后就把**折叠依据的那一段原样还原**出来：
 *
 *   折叠: `MoveAbsolute()`            （只有函数名，说明"这里折了一段"）
 *   展开: `MoveAbsolute() >()`        （还原成日志里真实的入口形态）
 *
 * 依据什么折的就显示什么 —— 是 `<()` 就显示 `<()`，没有边界符就保持纯函数名，
 * 不要一律补成 `>()`（那会把出口折成的块写成入口）。
 */
export function foldEntrySignature(entry: LogEntry | undefined, fallbackName: string): string {
  const name = String(fallbackName ?? '').trim();
  const message = String(entry?.message ?? '');
  const matched = message.match(BOUNDARY_MARKER_REGEX);
  if (!matched) return name;
  const prefix = message.slice(0, matched.index ?? 0).trim();
  return `${prefix || name} ${matched[0].replace(/\s+/g, '')}`;
}
