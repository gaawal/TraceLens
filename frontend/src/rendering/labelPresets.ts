/**
 * 日志标签的「语义色盘」。
 *
 * 颜色不应该让用户自己调：日志标签是给人**扫一眼**用的，同一个含义必须永远是同一个颜色
 * （异常＝红、告警＝橙、流程开始＝翠绿…），否则每个人配一套，扫视就失效了。
 * 所以这里给一组常见的日志主题色，每个都带：
 *   - 语义名（异常 / 报错、控制流程、建模流程…）和一句「什么时候用」；
 *   - 默认符号（✕ ▲ ✓ ▶ ■ …）：颜色之外再加一层区分，色弱用户也能分辨；
 *   - 颜色值（挑的是在浅色面板上对比度够的主题色）。
 * 想用调色板以外的颜色，仍然可以在「自定义颜色」里手填（老规则的颜色不会被动）。
 */
export type LabelChipStyle = 'soft' | 'solid' | 'outline';

export interface LabelPreset {
  id: string;
  /** 语义名，直接显示给用户。 */
  name: string;
  /** 什么时候用它。 */
  hint: string;
  color: string;
  /** 默认符号（空串＝不带符号）。 */
  symbol: string;
}

export const LABEL_PRESETS: readonly LabelPreset[] = [
  { id: 'error', name: '异常 / 报错', hint: 'ERROR 行、异常抛出、断言失败', color: '#dc2626', symbol: '✕' },
  { id: 'fault', name: '故障 / 阻塞', hint: '设备故障、流程中断、卡死', color: '#b91c1c', symbol: '!' },
  { id: 'warning', name: '告警 / 风险', hint: 'WARN 行、接近阈值、重试', color: '#ea580c', symbol: '▲' },
  { id: 'performance', name: '性能 / 耗时', hint: '超时、耗时偏长、节拍慢', color: '#ca8a04', symbol: '⏱' },
  { id: 'success', name: '成功 / 通过', hint: '校验通过、恢复成功、PASS', color: '#16a34a', symbol: '✓' },
  { id: 'flow_start', name: '流程开始', hint: '入口、START、进入阶段', color: '#10b981', symbol: '▶' },
  { id: 'flow_end', name: '流程结束', hint: '出口、END、离开阶段', color: '#64748b', symbol: '■' },
  { id: 'modeling', name: '建模流程', hint: '建模、标定、参数计算', color: '#8b5cf6', symbol: '⚙' },
  { id: 'control', name: '控制流程', hint: '运动控制、闭环、伺服', color: '#4f46e5', symbol: '◆' },
  { id: 'data', name: '数据采集', hint: '采集、提取、落库', color: '#06b6d4', symbol: '⇩' },
  { id: 'comm', name: '通信 / RPC', hint: '跨进程调用、超时重连', color: '#0ea5e9', symbol: '⇄' },
  { id: 'security', name: '安全 / 权限', hint: '鉴权、越权、联锁', color: '#db2777', symbol: '⛨' },
  { id: 'key', name: '关键节点', hint: '里程碑、重要状态切换', color: '#2563eb', symbol: '★' },
  { id: 'case', name: '用例 / 场景', hint: '用例步骤、场景标记', color: '#7c3aed', symbol: '◇' },
  { id: 'debug', name: '调试 / 信息', hint: 'DEBUG、普通说明性日志', color: '#6b7280', symbol: '' },
  { id: 'todo', name: '待办 / 注意', hint: '待确认、TODO、人工介入', color: '#d97706', symbol: '☰' },
] as const;

/** 可选的标签符号：颜色之外的第二层区分。 */
export const LABEL_SYMBOLS = ['', '✕', '!', '▲', '⏱', '✓', '▶', '■', '◆', '⇄', '⇩', '★', '◇', '⚙', '⛨', '☰', '●'] as const;

export const LABEL_CHIP_STYLES: ReadonlyArray<{ id: LabelChipStyle; name: string; hint: string }> = [
  { id: 'soft', name: '柔和', hint: '浅底描边，默认样式，适合大量标签并存' },
  { id: 'solid', name: '实心', hint: '实底白字，最醒目，适合异常/关键节点' },
  { id: 'outline', name: '描边', hint: '只有描边，最克制，适合信息类标签' },
];

export function labelPresetById(id?: string | null): LabelPreset | undefined {
  const key = String(id || '').trim();
  return key ? LABEL_PRESETS.find((preset) => preset.id === key) : undefined;
}

/** 按颜色反查预设：老规则只有颜色，也能在色盘里高亮成对应的那一档。 */
export function labelPresetByColor(color?: string | null): LabelPreset | undefined {
  const key = String(color || '').trim().toLowerCase();
  return key ? LABEL_PRESETS.find((preset) => preset.color.toLowerCase() === key) : undefined;
}

export const DEFAULT_LABEL_COLOR = '#2563eb';
