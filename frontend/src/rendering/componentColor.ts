/**
 * 模块配色的**唯一**来源。
 *
 * 日志列表里的模块徽标（`.component-badge`）和日志行最左侧的色条都读 `--component-hue`，
 * 现在实时标签流也要按模块上色，就必须和它们用同一个 hash —— 各写一份的结果是
 * 同一个模块在列表里是紫色、在时间线上是蓝色，颜色就不再是「模块」的信息了。
 */
/** 模块名归一化：大小写不敏感（列表里是 `CPFR`、实时命中里是 `cpfr`，必须算同一个模块）。 */
export function componentKey(component: string): string {
  return String(component || '').trim().toLowerCase();
}

export function componentHue(component: string): number {
  // 统一小写再 hash：同一份日志里模块名的大小写并不一致 ——
  // 日志行的 component 是 `CPFR`，而实时监听命中的 fm 是 `cpfr`，
  // 大小写不同 → hash 不同 → 同一个模块在列表里一个颜色、在标签流里另一个颜色。
  // 颜色既然是用来表达「哪个模块」的，就不能对大小写敏感。
  const key = componentKey(component);
  let hash = 0;
  for (let index = 0; index < key.length; index += 1) {
    hash = (hash * 31 + key.charCodeAt(index)) % 100_000;
  }
  // 🔴 不能直接 `hash % 360`：两个模块名只差一两个字节时 hash 也只差一两，
  // 色相就挤在一起（实测 `wsp` 与另一个模块拿到 215°/217°，肉眼看是同一个颜色）。
  // 乘黄金角再取模，把相邻 hash 撒到色环的两端 —— 仍然稳定（同一模块永远同一色）。
  return Math.round((hash * 137.508) % 360);
}

export function componentStyle(component: string): React.CSSProperties {
  return { '--component-hue': componentHue(component) } as React.CSSProperties;
}

/**
 * 模块色的三种用法，和 `.component-badge` / `.log-row` 里的 hsl 取值保持一致：
 * 边框浅、文字深、圆点/色条最饱和。
 */
export function moduleColor(component: string): { border: string; dot: string; text: string } {
  const hue = componentHue(component);
  return {
    border: `hsl(${hue} 62% 72%)`,
    dot: `hsl(${hue} 66% 52%)`,
    text: `hsl(${hue} 58% 31%)`,
  };
}

/**
 * 规则没有自定义标签色时后端会填这个默认蓝。用它当「未自定义」的哨兵值：
 * 只有用户真的挑过颜色，才应该盖过模块色。
 */
export const DEFAULT_LABEL_COLOR = '#2563eb';

export function isCustomLabelColor(color?: string): boolean {
  const value = String(color || '').trim().toLowerCase();
  return Boolean(value) && value !== DEFAULT_LABEL_COLOR;
}


/**
 * 时间线调色板的**下标分配表**。
 *
 * 以前是 `|hash| % palette.length`：两个模块的 hash 只要差成调色板档数的整数倍就会
 * **撞同一个颜色**（模块一多几乎必撞），而按模块上色一旦撞色就失去了表达力。
 * 现在改成"这一屏出现的模块按名称排序后依次拿 0、1、2…"，只要模块数不超过档数就保证互不相同；
 * 表外的模块仍退回 hash，绝不会没有颜色。
 */
const timelinePaletteIndexByComponent = new Map<string, number>();

/** 用当前时间线里出现的全部模块重建分配表（父组件渲染时调用一次）。 */
export function registerTimelinePaletteComponents(components: Iterable<string>): void {
  const keys = Array.from(new Set(Array.from(components, componentKey).filter(Boolean))).sort();
  timelinePaletteIndexByComponent.clear();
  keys.forEach((key, index) => timelinePaletteIndexByComponent.set(key, index));
}

/** 某个模块在调色板里的下标（未登记时退回 hash，永远返回 `[0, size)`）。 */
export function timelinePaletteIndex(component: string, size: number): number {
  if (!Number.isFinite(size) || size <= 0) return 0;
  const assigned = timelinePaletteIndexByComponent.get(componentKey(component));
  if (assigned !== undefined) return assigned % size;
  return Math.abs(componentHue(component)) % size;
}

/** 测试/排查用：当前分配表的快照。 */
export function timelinePaletteSnapshot(): Record<string, number> {
  return Object.fromEntries([...timelinePaletteIndexByComponent.entries()].sort((left, right) => left[1] - right[1]));
}
