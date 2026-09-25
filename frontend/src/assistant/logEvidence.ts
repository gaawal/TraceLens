import type { LogEntry } from '../types';
import { normalizeAbnormalMessage } from '../rendering/abnormalKnowledge';

/**
 * 投喂给 AI 的日志证据：**异常锚点 ±N 行**，先看文本长度再决定压不压缩。
 *
 * 为什么不是「把当前页若干行原样丢给模型」
 * ----------------------------------------
 * 1. 单个异常行本身几乎无法解释原因 —— 根因通常在它前后几十行的状态变化里。
 *    所以每个异常行向外扩 N 行（默认 100），多个异常各自扩散后**合并重叠区间**：
 *    重叠部分只投喂一次，既不重复烧 token，也不会因为两次窗口拼接而错位。
 * 2. **先量再压**：扩完之后量一下原文长度（含每个异常锚点自身），
 *    没超过上限（默认 40000 字符，可配置）就**原样直送**——模型看到的是真实日志行，
 *    没有任何模板占位符或代号，判断力最好；只有超过上限时才走下面的压缩。
 *    这里刻意只比字符长度，不做 token 估算：日志基本都是 ASCII，数量级一致，
 *    而估算器本身也会漂，反而不好解释「为什么这次压了、上次没压」。
 * 3. 压缩是确定性的、不调用模型：
 *    - **字典抽取**：子系统/模块/来源文件/级别这些反复出现的值各给一个短代号
 *      （`c1=cpfr`、`E=ERROR`、`f1=cpfr.log`），正文里只写代号；
 *    - **模板归并**：把时间戳/进程号/十六进制/数字抽掉得到模板，**连续且同模板**的行
 *      合并成一条并标 `×N`；同类心跳、轮询、参数回显几百行会塌成一行；
 *    - 压缩是**无损语义**的：代号表在文首给出，模板里的可变段用 `<N>` 之类占位符标出。
 * 4. 输出用接近 YAML 的紧凑文本，而不是 JSON —— 同内容 JSON 的键名和引号要贵得多。
 *
 * 返回结构化的 `windows`/`anchors`（供工具按行号回查原始日志）与 `text`（给模型读）。
 */

/** 异常锚点向外扩散的行数。 */
export const EVIDENCE_RADIUS = 100;
/** 最多取多少个异常锚点：再多就应该先收敛筛选条件，而不是继续加 token。 */
const MAX_ANCHORS = 12;

/** 锚点上下文原文的长度上限：没超过就直送原文，超过才压缩。 */
export const DEFAULT_EVIDENCE_MAX_CHARS = 40000;
export const EVIDENCE_MAX_CHARS_KEY = 'tracelens-ai-evidence-max-chars-v1';
export const EVIDENCE_MAX_CHARS_MIN = 4000;
export const EVIDENCE_MAX_CHARS_MAX = 400000;
/** 上限下拉里给的档位：够用就好，不鼓励把上下文塞满。 */
export const EVIDENCE_MAX_CHARS_PRESETS = [20000, 40000, 80000, 160000] as const;

export interface EvidenceAnchor {
  /** 在当前有序视图里的下标（0 基）。 */
  index: number;
  line: number;
  time: string;
  component: string;
  level: string;
  message: string;
}

export interface LogEvidencePayload {
  radius: number;
  /** raw = 原文直送（没超长度上限）；compressed = 走了字典 + 模板归并。 */
  mode: 'raw' | 'compressed';
  /** 这次判定用的原文长度上限（字符）。 */
  max_chars: number;
  /** 实际投喂文本的长度（字符）。 */
  text_chars: number;
  /** 锚点上下文原文的长度（字符，压缩模式下用它和上限比）。 */
  raw_text_chars: number;
  anchors: EvidenceAnchor[];
  /** 合并后的行号区间（闭区间，1 基，按当前视图编号）。 */
  windows: Array<{ from: number; to: number }>;
  /** 区间内的行数（压缩前的规模）。 */
  window_lines: number;
  /** raw 模式=原文行数；compressed 模式=归纳后的模板种数。 */
  groups: number;
  dict: Record<string, Record<string, string>>;
  text: string;
  stats: {
    raw_chars: number;
    packed_chars: number;
    /** 压缩后 / 压缩前，越小越省。 */
    ratio: number;
    anchors_total: number;
    anchors_used: number;
    mode?: 'raw' | 'compressed';
    max_chars?: number;
    text_chars?: number;
    raw_text_chars?: number;
    /**
     * 后端放行这段证据文本用的字符上限（= 用户配置的长度上限 + 头部余量）：
     * 页面证据已经量过了，后端不能再按通用的 3000 字符上限二次切掉。
     */
    char_budget?: number;
  };
}

export function normalizeEvidenceMaxChars(value: unknown): number {
  const parsed = Math.round(Number(value));
  if (!Number.isFinite(parsed) || parsed <= 0) return DEFAULT_EVIDENCE_MAX_CHARS;
  return Math.min(EVIDENCE_MAX_CHARS_MAX, Math.max(EVIDENCE_MAX_CHARS_MIN, parsed));
}

/** 读取用户配置的证据长度上限；没配过 / 存坏了都退回默认 40000 字符。 */
export function loadEvidenceMaxChars(): number {
  if (typeof window === 'undefined') return DEFAULT_EVIDENCE_MAX_CHARS;
  try {
    const stored = window.localStorage.getItem(EVIDENCE_MAX_CHARS_KEY);
    if (!stored) return DEFAULT_EVIDENCE_MAX_CHARS;
    return normalizeEvidenceMaxChars(stored);
  } catch {
    return DEFAULT_EVIDENCE_MAX_CHARS;
  }
}

export function saveEvidenceMaxChars(value: unknown): number {
  const normalized = normalizeEvidenceMaxChars(value);
  try {
    window.localStorage.setItem(EVIDENCE_MAX_CHARS_KEY, String(normalized));
  } catch {
    // 存储不可用（隐私模式等）时仍然按本次选择生效，不阻断对话。
  }
  return normalized;
}

/** 把字符数说成人话：40000 → 4 万字符。 */
export function formatEvidenceMaxChars(value: number): string {
  if (value >= 10000) return `${Number((value / 10000).toFixed(value % 10000 === 0 ? 0 : 1))} 万字符`;
  return `${value} 字符`;
}
function short(value: string, limit: number): string {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function compactTime(value: string): string {
  const match = String(value || '').match(/(\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)/);
  return match ? match[1] : short(value, 12);
}

const LEVEL_CODES: Record<string, string> = {
  error: 'E', fatal: 'E', critical: 'E', alarm: 'E',
  warn: 'W', warning: 'W', wrn: 'W',
  info: 'I', debug: 'D', trace: 'T',
};

function levelCode(level: string): string {
  const key = String(level || '').trim().toLowerCase();
  return LEVEL_CODES[key] || (key ? key.slice(0, 1).toUpperCase() : '?');
}

/** 把下标区间按重叠合并成尽量少的闭区间。 */
function mergeIntervals(intervals: Array<[number, number]>): Array<[number, number]> {
  if (!intervals.length) return [];
  const sorted = [...intervals].sort((left, right) => left[0] - right[0]);
  const merged: Array<[number, number]> = [sorted[0]];
  for (const [from, to] of sorted.slice(1)) {
    const last = merged[merged.length - 1];
    // 相邻（to + 1 === from）也并起来：两段中间没有别的行，分开写只是多一行记录。
    if (from <= last[1] + 1) last[1] = Math.max(last[1], to);
    else merged.push([from, to]);
  }
  return merged;
}

/** 一个可复用的短代号表。 */
class TokenDict {
  private readonly map = new Map<string, string>();
  private readonly prefix: string;

  constructor(prefix: string) {
    this.prefix = prefix;
  }

  code(value: string): string {
    const key = String(value || '').trim();
    if (!key) return '-';
    const existing = this.map.get(key);
    if (existing) return existing;
    const code = `${this.prefix}${this.map.size + 1}`;
    this.map.set(key, code);
    return code;
  }

  dump(): Record<string, string> {
    return Object.fromEntries([...this.map.entries()].map(([value, code]) => [code, value]));
  }
}

/**
 * 构建「异常锚点 ±N 行」的证据。
 *
 * 先量后压：锚点上下文原文的长度没超过 `maxChars` 就原样直送，
 * 超过才做字典 + 模板归并（返回 payload 的 `mode` 说明走了哪条路）。
 *
 * @param entries 当前视图的**有序**日志（时间序），下标即视图行号。
 * @param radius 锚点向外扩散的行数。
 * @param maxChars 原文长度上限（字符）；默认读用户配置（没配过 = 40000）。
 */
export function buildLogEvidence(
  entries: readonly LogEntry[],
  radius = EVIDENCE_RADIUS,
  maxChars = loadEvidenceMaxChars(),
): LogEvidencePayload | undefined {
  const limit = normalizeEvidenceMaxChars(maxChars);
  if (!entries.length) return undefined;

  const anchors: EvidenceAnchor[] = [];
  entries.forEach((entry, index) => {
    if (entry.severity !== 'error') return;
    anchors.push({
      index,
      line: entry.lineNumber ?? index + 1,
      time: compactTime(entry.timestamp),
      component: String(entry.component || entry.logModule || ''),
      level: String(entry.level || ''),
      message: short(entry.message || entry.raw || '', 240),
    });
  });
  if (!anchors.length) return undefined;

  const usedAnchors = anchors.slice(0, MAX_ANCHORS);
  const windows = mergeIntervals(usedAnchors.map((anchor) => (
    [Math.max(0, anchor.index - radius), Math.min(entries.length - 1, anchor.index + radius)]
  )));

  const lineOf = (index: number) => entries[index]?.lineNumber ?? index + 1;

  // ── 锚点上下文的原文（去重、按视图顺序）─────────────────────────────────
  // 这份原文既是「直送 AI」的候选，也是决定要不要压缩的依据：
  // 先看它的长度，超过上限才去构造压缩版本。
  const rawMembers = new Set<string>();
  const rawLines: string[] = [];
  let rawChars = 0;
  for (const [from, to] of windows) {
    for (let index = from; index <= to; index += 1) {
      const entry = entries[index];
      if (!entry || rawMembers.has(entry.id)) continue;
      rawMembers.add(entry.id);
      const body = String(entry.raw || entry.message || '').replace(/\s+/g, ' ').trim();
      rawChars += body.length;
      const component = String(entry.component || entry.logModule || '').trim() || '-';
      const level = String(entry.level || '').trim() || '-';
      rawLines.push(`L${lineOf(index)} ${compactTime(entry.timestamp)} ${component} ${level} ${body}`);
    }
  }
  const windowLines = rawMembers.size;

  const rawHeader = [
    `# 日志证据 · 异常锚点 ±${radius} 行（原文直送，未压缩）`,
    `规模: 原文 ${windowLines} 行 / ${rawChars} 字符（上限 ${limit} 字符）`,
    `区间(视图行号): ${windows.map(([from, to]) => `${from + 1}-${to + 1}`).join(', ')}`,
    `锚点(${usedAnchors.length}${anchors.length > usedAnchors.length ? `/${anchors.length}` : ''}):`,
    ...usedAnchors.map((anchor) => (
      `- L${anchor.line} ${anchor.time} ${anchor.component} ${anchor.level} ${anchor.message}`
    )),
    '上下文原文（L=视图行号，逐行真实日志）:',
  ].join('\n');
  const rawText = `${rawHeader}\n${rawLines.join('\n')}`;
  // 量的是**最终要投喂的那段文本**（含表头），不是逐条消息之和：判断和实际投喂一致。
  const rawTextChars = rawText.length;

  if (rawTextChars <= limit) {
    return {
      radius,
      mode: 'raw',
      max_chars: limit,
      text_chars: rawTextChars,
      raw_text_chars: rawTextChars,
      anchors: usedAnchors,
      windows: windows.map(([from, to]) => ({ from: from + 1, to: to + 1 })),
      window_lines: windowLines,
      groups: rawLines.length,
      dict: {},
      text: rawText,
      stats: {
        raw_chars: rawChars,
        packed_chars: rawTextChars,
        ratio: rawChars > 0 ? Number((rawTextChars / rawChars).toFixed(3)) : 1,
        anchors_total: anchors.length,
        anchors_used: usedAnchors.length,
        mode: 'raw',
        max_chars: limit,
        text_chars: rawTextChars,
        raw_text_chars: rawTextChars,
        // 后端按这个上限放行文本，不headroom：留一点头部余量，避免差几个字符被切。
        char_budget: limit + 512,
      },
    };
  }

  /**
   * 先扫一遍区间内的 `key=value`，找出**取值很多**的键（wafer=W01…W08、target=…）。
   *
   * 这些是「实例标识」，每条都不同，但区分度为零 —— 不归一化的话同一个模板会因为
   * `wafer=W08` / `wafer=W07` 变成 8 个模板，图上的重复行一条都合并不了。
   * 只归一化取值 ≥3 种的键：`recipe=SPM-V2026.09.23` 这种固定值必须原样保留，
   * 它是语义的一部分，抹掉就等于丢失功能描述。
   */
  const keyValues = new Map<string, Set<string>>();
  for (const [from, to] of windows) {
    for (let index = from; index <= to; index += 1) {
      const text = String(entries[index]?.message || entries[index]?.raw || '');
      for (const match of text.matchAll(/([A-Za-z_][\w]*)=("?)([A-Za-z0-9._:\/-]{1,40})\2/g)) {
        const key = match[1].toLowerCase();
        const values = keyValues.get(key) ?? new Set<string>();
        values.add(match[3]);
        keyValues.set(key, values);
      }
    }
  }
  const volatileKeys = new Set([...keyValues.entries()].filter(([, values]) => values.size >= 3).map(([key]) => key));

  const normalizeVolatile = (template: string): string => {
    if (!volatileKeys.size) return template;
    return template.replace(/([A-Za-z_][\w]*)=("?)([A-Za-z0-9._:\/-]{1,40})\2/g, (whole, key: string) => (
      volatileKeys.has(String(key).toLowerCase()) ? `${key}=<V>` : whole
    ));
  };

  const componentDict = new TokenDict('c');
  const levelDict = new TokenDict('l');
  const sourceDict = new TokenDict('f');
  const members = new Set<string>();
  const groups: Array<{ from: number; to: number; count: number; comp: string; lvl: string; src: string; tmpl: string }> = [];

  for (const [from, to] of windows) {
    for (let index = from; index <= to; index += 1) {
      const entry = entries[index];
      if (!entry || members.has(entry.id)) continue;
      members.add(entry.id);

      const component = String(entry.component || entry.logModule || '').trim() || '-';
      const level = levelCode(entry.level || '');
      const source = String(entry.source?.fileName || entry.sourceFile || '').trim();
      // 模板 = 去掉时间戳/PID/十六进制/数字后的消息；同一模板的连续行会合并成一条。
      const template = normalizeVolatile(normalizeAbnormalMessage(entry.message || entry.raw || '')) || '-';
      const key = `${component}\u0000${level}\u0000${source}\u0000${template}`;
      const last = groups[groups.length - 1];
      const lastKey = last ? `${last.comp}\u0000${last.lvl}\u0000${last.src}\u0000${last.tmpl}` : '';
      if (last && lastKey === key && last.to === index - 1) {
        last.to = index;
        last.count += 1;
        continue;
      }
      groups.push({
        from: index,
        to: index,
        count: 1,
        comp: componentDict.code(component),
        lvl: levelDict.code(level),
        src: sourceDict.code(source),
        tmpl: short(template, 200),
      });
    }
  }

  /**
   * 第二次归并：**跨区间**按 (模块, 级别, 模板) 再合一次。
   *
   * 只合并「连续同模板」是不够的 —— 周期性日志里同一串模板每个周期重复一遍，
   * 40 个模板 × 8 个周期 = 320 条，但真正不同的信息只有 40 条。
   * 这里把它们合成一条，`×N` 记出现次数，并保留最多 3 个代表行号用来回查原文。
   * 这就是用户说的「同类的提取出来，不用一行一行描述」。
   */
  const aggregated = new Map<string, { count: number; comp: string; lvl: string; src: string; tmpl: string; samples: number[] }>();
  for (const group of groups) {
    const key = `${group.comp}\u0000${group.lvl}\u0000${group.src}\u0000${group.tmpl}`;
    const current = aggregated.get(key);
    if (current) {
      current.count += group.count;
      if (current.samples.length < 3) current.samples.push(lineOf(group.from));
      continue;
    }
    aggregated.set(key, {
      count: group.count,
      comp: group.comp,
      lvl: group.lvl,
      src: group.src,
      tmpl: group.tmpl,
      samples: [lineOf(group.from)],
    });
  }
  const finalGroups = [...aggregated.values()].sort((left, right) => right.count - left.count || left.samples[0] - right.samples[0]);

  // 极长日志（几千个不同模板）也可能把文本撑大，留一个硬上限并如实标注截断。
  const MAX_GROUPS = 400;
  const MAX_TEXT = 12000;
  const totalGroups = finalGroups.length;
  const shownGroups = finalGroups.slice(0, MAX_GROUPS);
  const truncated = totalGroups > shownGroups.length;

  const componentNames = componentDict.dump();
  const levelNames = levelDict.dump();
  const sourceNames = sourceDict.dump();

  const textLines = [
    `# 日志证据 · 异常锚点 ±${radius} 行（原文 ${rawTextChars} 字符 > 上限 ${limit} 字符，已压缩：重叠区间合并 + 模板归并）`,
    `规模: 原文 ${windowLines} 行 / ${rawChars} 字符 → 去重 ${groups.length} 段 → 归纳 ${totalGroups} 种`,
    `区间(视图行号): ${windows.map(([from, to]) => `${from + 1}-${to + 1}`).join(', ')}`,
    `可变键(取值≥3种，已归一化为 <V>): ${volatileKeys.size ? [...volatileKeys].join(' ') : '无'}`,
    `字典: ${[
      Object.keys(componentNames).length ? `模块 ${Object.entries(componentNames).map(([code, name]) => `${code}=${name}`).join(' ')}` : '',
      Object.keys(levelNames).length ? `级别 ${Object.entries(levelNames).map(([code, name]) => `${code}=${name}`).join(' ')}` : '',
      Object.keys(sourceNames).length > 1 ? `来源 ${Object.entries(sourceNames).map(([code, name]) => `${code}=${name}`).join(' ')}` : '',
    ].filter(Boolean).join(' | ')}`,
    `锚点(${usedAnchors.length}${anchors.length > usedAnchors.length ? `/${anchors.length}` : ''}):`,
    ...usedAnchors.map((anchor) => (
      `- L${anchor.line} ${anchor.time} ${componentDict.code(anchor.component)} ${levelDict.code(levelCode(anchor.level))} ${anchor.message}`
    )),
    '上下文分组（L=行号区间 ×=重复次数，<N>/<HEX> 等为可变段占位符）:',
    ...shownGroups.map((group) => (
      `- ${group.samples.map((line) => `L${line}`).join(',')}${group.count > 1 ? ` ×${group.count}` : ''} ${group.comp} ${group.lvl} ${group.tmpl}`
    )),
    ...(truncated ? [`（已截断：仅列出出现最多的 ${shownGroups.length} 种，共 ${totalGroups} 种）`] : []),
  ];
  const joined = textLines.join('\n');
  const text = joined.length > MAX_TEXT ? `${joined.slice(0, MAX_TEXT)}\n（已达长度上限，剩余分组省略）` : joined;

  return {
    radius,
    mode: 'compressed',
    max_chars: limit,
    text_chars: text.length,
    raw_text_chars: rawTextChars,
    anchors: usedAnchors,
    // 用**视图行号**（1 基）而不是文件行号：合并区间常常横跨多个日志文件，
    // 而每个文件的行号都从 1 开始，直接报行号会看起来像「只有一个区间」。
    windows: windows.map(([from, to]) => ({ from: from + 1, to: to + 1 })),
    window_lines: windowLines,
    groups: totalGroups,
    dict: { component: componentNames, level: levelNames, source: sourceNames },
    text,
    stats: {
      raw_chars: rawChars,
      packed_chars: text.length,
      ratio: rawChars > 0 ? Number((text.length / rawChars).toFixed(3)) : 1,
      anchors_total: anchors.length,
      anchors_used: usedAnchors.length,
      mode: 'compressed',
      max_chars: limit,
      text_chars: text.length,
      raw_text_chars: rawTextChars,
      // 压缩后的文本本来就比上限小得多，这里给同样的放行额度即可。
      char_budget: limit + 512,
    },
  };
}
