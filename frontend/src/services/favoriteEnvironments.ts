/**
 * 环境收藏（五角星）—— **按浏览器存**，不是存在环境上。
 *
 * 为什么不在后端：收藏是"我这个人的工作台"，不是环境的属性。以前写在
 * `Environment.is_favorite` 上，等于全团队共用一份收藏——A 收藏了，B 的列表里也亮起来，
 * 谁都没法有自己的排序。现在只落在这个浏览器的 localStorage 里。
 *
 * 约定：
 * - 存的是**环境 id 数组**（环境删了/重建后 id 变了就自然失效，不残留脏数据）；
 * - 同一浏览器多标签页靠 `storage` 事件同步，同页面内靠订阅回调同步；
 * - 读写都做容错：localStorage 不可用（隐私模式）时退化成"本次会话内存态"，不抛异常。
 */

const STORAGE_KEY = 'tracelens.favorite-environments.v1';

/** 同页面内的订阅者（多个卡片/统计条）。 */
type Listener = (ids: ReadonlySet<number>) => void;
const listeners = new Set<Listener>();

let memoryFallback: Set<number> | null = null;

function normalize(value: unknown): Set<number> {
  if (!Array.isArray(value)) return new Set();
  const ids = new Set<number>();
  value.forEach((item) => {
    const id = Number(item);
    if (Number.isFinite(id) && id > 0) ids.add(Math.trunc(id));
  });
  return ids;
}

function readStorage(): Set<number> | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) return null;
    return normalize(JSON.parse(raw));
  } catch {
    return null;
  }
}

function writeStorage(ids: Set<number>): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([...ids].sort((left, right) => left - right)));
  } catch {
    // 隐私模式/配额满：至少保证当前会话内是对的。
    memoryFallback = new Set(ids);
  }
}

function emit(ids: Set<number>): void {
  const snapshot: ReadonlySet<number> = new Set(ids);
  listeners.forEach((listener) => {
    try {
      listener(snapshot);
    } catch {
      // 单个订阅者出错不影响其它订阅者。
    }
  });
}

/** 当前浏览器收藏了哪些环境 id。 */
export function loadFavoriteEnvironmentIds(): Set<number> {
  const stored = readStorage();
  if (stored) return stored;
  if (memoryFallback) return new Set(memoryFallback);
  return new Set();
}

export function isFavoriteEnvironment(environmentId: number): boolean {
  return loadFavoriteEnvironmentIds().has(Number(environmentId));
}

/** 直接覆盖整份收藏（用于恢复/清空）。 */
export function saveFavoriteEnvironmentIds(ids: Iterable<number>): Set<number> {
  const next = normalize([...ids]);
  writeStorage(next);
  emit(next);
  return next;
}

/** 收藏/取消收藏，返回**切换后**的状态。 */
export function toggleFavoriteEnvironment(environmentId: number): boolean {
  const id = Number(environmentId);
  const current = loadFavoriteEnvironmentIds();
  const next = new Set(current);
  const nowFavorite = !next.has(id);
  if (nowFavorite) next.add(id);
  else next.delete(id);
  writeStorage(next);
  emit(next);
  return nowFavorite;
}

/** 环境被删除时顺手把收藏清掉，避免 localStorage 里越积越多。 */
export function pruneFavoriteEnvironments(existingIds: Iterable<number>): Set<number> {
  const existing = new Set([...existingIds].map((item) => Number(item)));
  const current = loadFavoriteEnvironmentIds();
  const next = new Set([...current].filter((id) => existing.has(id)));
  if (next.size === current.size) return current;
  return saveFavoriteEnvironmentIds(next);
}

/** 订阅收藏变化（同页面）。返回取消订阅函数。 */
export function subscribeFavoriteEnvironments(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** 多标签页同步：另一个标签页改了收藏，这个标签页也要跟着更新。 */
export function watchFavoriteEnvironmentsStorage(): () => void {
  if (typeof window === 'undefined') return () => undefined;
  const handler = (event: StorageEvent) => {
    if (event.key !== STORAGE_KEY) return;
    emit(loadFavoriteEnvironmentIds());
  };
  window.addEventListener('storage', handler);
  return () => window.removeEventListener('storage', handler);
}
