import type { ExtractedDataRow } from './dataExtractionRules';
import type { MergedTemporaryDataRow } from './extractedDataStore';

export type VisualizationMode = 'trend' | 'scatter' | 'xy' | 'xyz' | 'heatmap' | 'sixdof';

export interface VisualizationDataset {
  name: string;
  signature: string;
  fields: string[];
  rows: Array<ExtractedDataRow | MergedTemporaryDataRow>;
}

export interface VisualizationRecipe {
  mode: VisualizationMode;
  xField: string;
  yField: string;
  zField: string;
  valueField: string;
  yFields: string[];
  rollField: string;
  pitchField: string;
  yawField: string;
  timeField: string;
  sourceFilter: string;
  /** 0 表示 All（全量）；其它值表示当前范围内最多绘制多少个均匀采样点。 */
  sampleLimit: number;
  startPercent: number;
  endPercent: number;
  showTrajectory: boolean;
  /** 轨迹模式是否按时间/行顺序逐步绘制，而不是一次展示完整轨迹。 */
  progressiveTrajectory: boolean;
  /** 逐步绘制/6DoF 回放速度倍率。 */
  playbackSpeed: number;
  showContours: boolean;
  heatmapGridSize: number;
  orientationUnit: 'degree' | 'radian';
  cameraYaw: number;
  cameraPitch: number;
}

export interface StoredVisualizationRecipe {
  id: string;
  name: string;
  signature: string;
  recipe: VisualizationRecipe;
  updatedAt: number;
}

const STORAGE_KEY = 'tracelens.data-visualization-recipes.v1';
const LAST_RECIPE_KEY = 'tracelens.data-visualization-last.v1';

function normalizeName(value: string): string {
  return value.toLowerCase().replace(/[\s_\-.()[\]{}]/g, '');
}

function findAlias(fields: string[], aliases: string[]): string {
  const normalized = fields.map((field) => [field, normalizeName(field)] as const);
  for (const alias of aliases) {
    const target = normalizeName(alias);
    const exact = normalized.find(([, value]) => value === target);
    if (exact) return exact[0];
  }
  for (const alias of aliases) {
    const target = normalizeName(alias);
    const partial = normalized.find(([, value]) => value.endsWith(target) || value.includes(target));
    if (partial) return partial[0];
  }
  return '';
}

export function numericValue(row: ExtractedDataRow | MergedTemporaryDataRow, field: string): number | undefined {
  if (!field) return undefined;
  if (field === 'timestamp') {
    const parsed = new Date(String(row.timestamp || '').trim().replace(' ', 'T')).getTime();
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  const normalized = row.normalizedValues?.[field];
  if (typeof normalized === 'number' && Number.isFinite(normalized)) return normalized;
  const raw = row.values[field];
  if (typeof raw === 'number') return Number.isFinite(raw) ? raw : undefined;
  if (typeof raw === 'boolean') return raw ? 1 : 0;
  if (typeof raw !== 'string') return undefined;
  const text = raw.trim();
  if (!text) return undefined;
  const direct = Number(text.replace(/,/g, ''));
  if (Number.isFinite(direct)) return direct;
  const unitValue = text.match(/^[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:e[-+]?\d+)?/i)?.[0];
  if (!unitValue) return undefined;
  const parsed = Number(unitValue.replace(/,/g, ''));
  return Number.isFinite(parsed) ? parsed : undefined;
}

export function detectNumericFields(dataset: VisualizationDataset): string[] {
  const sample = dataset.rows.slice(0, 250);
  return dataset.fields.filter((field) => {
    let values = 0;
    let numeric = 0;
    for (const row of sample) {
      const raw = row.values[field];
      if (raw === undefined || raw === null || raw === '') continue;
      values += 1;
      if (numericValue(row, field) !== undefined) numeric += 1;
    }
    return values > 0 && numeric / values >= 0.65;
  });
}

export function sourceLabel(row: ExtractedDataRow | MergedTemporaryDataRow): string {
  const merged = row as MergedTemporaryDataRow;
  return merged.ruleName || row.module || row.subsystem || row.sourceFile || '默认来源';
}

export function datasetSources(dataset: VisualizationDataset): string[] {
  return [...new Set(dataset.rows.map(sourceLabel).filter(Boolean))].sort((a, b) => a.localeCompare(b));
}

export function createDefaultVisualizationRecipe(dataset: VisualizationDataset, mode: VisualizationMode = 'trend'): VisualizationRecipe {
  const numeric = detectNumericFields(dataset);
  const x = findAlias(numeric, ['x', 'posX', 'positionX', 'position_x', 'px']) || numeric[0] || '';
  const y = findAlias(numeric, ['y', 'posY', 'positionY', 'position_y', 'py']) || numeric.find((field) => field !== x) || '';
  const z = findAlias(numeric, ['z', 'posZ', 'positionZ', 'position_z', 'pz']) || numeric.find((field) => field !== x && field !== y) || '';
  const roll = findAlias(numeric, ['roll', 'rx', 'rotationX', 'angleX']);
  const pitch = findAlias(numeric, ['pitch', 'ry', 'rotationY', 'angleY']);
  const yaw = findAlias(numeric, ['yaw', 'rz', 'rotationZ', 'heading', 'angleZ']);
  const value = findAlias(numeric, ['temperature', 'temp', 'error', 'errorValue', 'strength', 'latency', 'value', 'speed']) || z || numeric[0] || '';
  return {
    mode,
    xField: mode === 'trend' ? 'timestamp' : x,
    yField: y,
    zField: z,
    valueField: value,
    yFields: numeric.slice(0, 3),
    rollField: roll,
    pitchField: pitch,
    yawField: yaw,
    timeField: 'timestamp',
    sourceFilter: '',
    sampleLimit: 0,
    startPercent: 0,
    endPercent: 100,
    showTrajectory: true,
    progressiveTrajectory: false,
    playbackSpeed: 1,
    showContours: true,
    heatmapGridSize: 34,
    orientationUnit: 'degree',
    cameraYaw: 38,
    cameraPitch: 28,
  };
}

export function normalizeRecipeForMode(dataset: VisualizationDataset, current: VisualizationRecipe, mode: VisualizationMode): VisualizationRecipe {
  const guessed = createDefaultVisualizationRecipe(dataset, mode);
  return {
    ...guessed,
    sourceFilter: current.sourceFilter,
    sampleLimit: Number.isFinite(current.sampleLimit) ? current.sampleLimit : 0,
    startPercent: current.startPercent,
    endPercent: current.endPercent,
    heatmapGridSize: current.heatmapGridSize,
    showTrajectory: current.showTrajectory,
    progressiveTrajectory: current.progressiveTrajectory ?? false,
    playbackSpeed: current.playbackSpeed || 1,
    showContours: current.showContours,
    orientationUnit: current.orientationUnit,
    cameraYaw: current.cameraYaw,
    cameraPitch: current.cameraPitch,
  };
}

export function filteredVisualizationRows(dataset: VisualizationDataset, recipe: VisualizationRecipe): Array<ExtractedDataRow | MergedTemporaryDataRow> {
  let rows = recipe.sourceFilter ? dataset.rows.filter((row) => sourceLabel(row) === recipe.sourceFilter) : dataset.rows.slice();
  if (!rows.length) return [];
  const start = Math.max(0, Math.min(100, Math.min(recipe.startPercent, recipe.endPercent)));
  const end = Math.max(0, Math.min(100, Math.max(recipe.startPercent, recipe.endPercent)));
  const startIndex = Math.floor((rows.length - 1) * start / 100);
  const endIndex = Math.max(startIndex + 1, Math.ceil((rows.length - 1) * end / 100) + 1);
  rows = rows.slice(startIndex, endIndex);
  const limit = Math.max(0, Math.floor(Number(recipe.sampleLimit) || 0));
  // 0 = All：保留当前范围内全部数据点。其它选项按整个范围均匀取点，并始终保留首尾点。
  if (!limit || rows.length <= limit) return rows;
  if (limit === 1) return [rows[0]];
  const result: typeof rows = [];
  const step = (rows.length - 1) / (limit - 1);
  let previousIndex = -1;
  for (let index = 0; index < limit; index += 1) {
    const sourceIndex = Math.min(rows.length - 1, Math.round(index * step));
    if (sourceIndex !== previousIndex) result.push(rows[sourceIndex]);
    previousIndex = sourceIndex;
  }
  return result;
}

export function visualizationRecipeIsReady(recipe: VisualizationRecipe): boolean {
  if (recipe.mode === 'trend') return Boolean(recipe.xField && recipe.yFields.length);
  if (recipe.mode === 'scatter' || recipe.mode === 'xy') return Boolean(recipe.xField && recipe.yField);
  if (recipe.mode === 'xyz') return Boolean(recipe.xField && recipe.yField && recipe.zField);
  if (recipe.mode === 'heatmap') return Boolean(recipe.xField && recipe.yField && recipe.valueField);
  return Boolean(recipe.xField && recipe.yField && recipe.zField && recipe.rollField && recipe.pitchField && recipe.yawField);
}

function readStoredRecipes(): StoredVisualizationRecipe[] {
  if (typeof window === 'undefined') return [];
  try {
    const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function writeStoredRecipes(items: StoredVisualizationRecipe[]): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items.slice(0, 80)));
}

export function listStoredVisualizationRecipes(signature: string): StoredVisualizationRecipe[] {
  return readStoredRecipes().filter((item) => item.signature === signature).sort((a, b) => b.updatedAt - a.updatedAt);
}

export function saveVisualizationRecipe(signature: string, name: string, recipe: VisualizationRecipe): StoredVisualizationRecipe {
  const items = readStoredRecipes();
  const existing = items.find((item) => item.signature === signature && item.name === name.trim());
  const saved: StoredVisualizationRecipe = {
    id: existing?.id || `viz-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    name: name.trim() || '未命名可视化',
    signature,
    recipe: { ...recipe, yFields: [...recipe.yFields] },
    updatedAt: Date.now(),
  };
  const next = items.filter((item) => item.id !== saved.id);
  next.unshift(saved);
  writeStoredRecipes(next);
  return saved;
}

export function deleteVisualizationRecipe(id: string): void {
  writeStoredRecipes(readStoredRecipes().filter((item) => item.id !== id));
}

export function formatAxisNumber(value: number): string {
  if (!Number.isFinite(value)) return '—';
  const abs = Math.abs(value);
  if (abs >= 1000000 || (abs > 0 && abs < 0.001)) return value.toExponential(2);
  if (abs >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 1 });
  return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

export function loadLastVisualizationRecipe(signature: string): VisualizationRecipe | undefined {
  if (typeof window === 'undefined') return undefined;
  try {
    const parsed = JSON.parse(window.localStorage.getItem(LAST_RECIPE_KEY) || '{}') as Record<string, VisualizationRecipe>;
    const recipe = parsed?.[signature];
    return recipe ? { ...recipe, yFields: Array.isArray(recipe.yFields) ? [...recipe.yFields] : [] } : undefined;
  } catch {
    return undefined;
  }
}

export function saveLastVisualizationRecipe(signature: string, recipe: VisualizationRecipe): void {
  if (typeof window === 'undefined') return;
  try {
    const parsed = JSON.parse(window.localStorage.getItem(LAST_RECIPE_KEY) || '{}') as Record<string, VisualizationRecipe>;
    parsed[signature] = { ...recipe, yFields: [...recipe.yFields] };
    window.localStorage.setItem(LAST_RECIPE_KEY, JSON.stringify(parsed));
  } catch {
    // Ignore unavailable/private localStorage.
  }
}
