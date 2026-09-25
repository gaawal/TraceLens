/**
 * 模块配色的**唯一**来源。
 *
 * 日志列表里的模块徽标（`.component-badge`）和日志行最左侧的色条都读 `--component-hue`，
 * 现在实时标签流也要按模块上色，就必须和它们用同一个 hash —— 各写一份的结果是
 * 同一个模块在列表里是紫色、在时间线上是蓝色，颜色就不再是「模块」的信息了。
 */
export function componentHue(component: string): number {
  // 统一小写再 hash：同一份日志里模块名的大小写并不一致 ——
  // 日志行的 component 是 `CPFR`，而实时监听命中的 fm 是 `cpfr`，
  // 大小写不同 → hash 不同 → 同一个模块在列表里一个颜色、在标签流里另一个颜色。
  // 颜色既然是用来表达「哪个模块」的，就不能对大小写敏感。
  const key = String(component || '').trim().toLowerCase();
  let hash = 0;
  for (let index = 0; index < key.length; index += 1) {
    hash = (hash * 31 + key.charCodeAt(index)) % 360;
  }
  return hash;
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
