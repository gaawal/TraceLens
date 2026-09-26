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
