import { useEffect, useMemo, useRef, useState } from 'react';
import * as echarts from 'echarts';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { Eye, Pause, Play, Save, Trash2, X } from 'lucide-react';
import type { ExtractedDataRow } from '../rendering/dataExtractionRules';
import type { MergedTemporaryDataRow } from '../rendering/extractedDataStore';
import {
  createDefaultVisualizationRecipe,
  datasetSources,
  deleteVisualizationRecipe,
  detectNumericFields,
  filteredVisualizationRows,
  formatAxisNumber,
  listStoredVisualizationRecipes,
  loadLastVisualizationRecipe,
  normalizeRecipeForMode,
  numericValue,
  saveLastVisualizationRecipe,
  saveVisualizationRecipe,
  sourceLabel,
  visualizationRecipeIsReady,
  type StoredVisualizationRecipe,
  type VisualizationDataset,
  type VisualizationMode,
  type VisualizationRecipe,
} from '../rendering/dataVisualization';

interface Props {
  dataset: VisualizationDataset;
  onClose: () => void;
  onOpenSourceLog?: (row: ExtractedDataRow | MergedTemporaryDataRow) => void;
}

type DataRow = ExtractedDataRow | MergedTemporaryDataRow;

interface NumericPoint {
  row: DataRow;
  rowIndex: number;
  x: number;
  y: number;
  z?: number;
  value?: number;
}

interface SixDofPoint extends NumericPoint {
  z: number;
  roll: number;
  pitch: number;
  yaw: number;
}

interface Bounds {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

interface HeatCell { x: number; y: number; value: number; ratio: number; }

const MODE_OPTIONS: Array<{ value: VisualizationMode; label: string; group: string }> = [
  { value: 'trend', label: '时间趋势', group: '趋势分析' },
  { value: 'scatter', label: '散点关系', group: '趋势分析' },
  { value: 'xy', label: 'XY轨迹', group: '空间分析' },
  { value: 'xyz', label: 'XYZ轨迹', group: '空间分析' },
  { value: 'heatmap', label: '热力图 / 等高线', group: '空间分析' },
  { value: 'sixdof', label: '6DoF轨迹', group: '空间分析' },
];

const SERIES_COLORS = ['#2563eb', '#dc2626', '#059669', '#7c3aed', '#d97706', '#0891b2', '#db2777', '#4f46e5', '#65a30d', '#ea580c', '#0f766e', '#9333ea'];

function expandRange(min: number, max: number): [number, number] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
  if (min === max) {
    const delta = Math.abs(min || 1) * 0.05 || 1;
    return [min - delta, max + delta];
  }
  return [min, max];
}

function boundsOf(points: NumericPoint[]): Bounds {
  let rawMinX = Number.POSITIVE_INFINITY;
  let rawMaxX = Number.NEGATIVE_INFINITY;
  let rawMinY = Number.POSITIVE_INFINITY;
  let rawMaxY = Number.NEGATIVE_INFINITY;
  for (const point of points) {
    if (point.x < rawMinX) rawMinX = point.x;
    if (point.x > rawMaxX) rawMaxX = point.x;
    if (point.y < rawMinY) rawMinY = point.y;
    if (point.y > rawMaxY) rawMaxY = point.y;
  }
  const [minX, maxX] = expandRange(rawMinX, rawMaxX);
  const [minY, maxY] = expandRange(rawMinY, rawMaxY);
  return { minX, maxX, minY, maxY };
}

function minMax(values: number[]): [number, number] {
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  for (const value of values) {
    if (value < min) min = value;
    if (value > max) max = value;
  }
  return [min, max];
}

function timestampLabel(value: number): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return formatAxisNumber(value);
  return date.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function pointTitle(point: NumericPoint, extra = ''): string {
  const prefix = `${point.row.timestamp}\n${sourceLabel(point.row)}\n${point.row.sourceFile}:${point.row.lineNumber}`;
  return `${prefix}${extra ? `\n${extra}` : ''}`;
}

function escapeHtml(value: unknown): string {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('\"', '&quot;')
    .replaceAll("'", '&#39;');
}

function baseCartesianOption(xName: string, yName: string, xIsTime = false): echarts.EChartsOption {
  return {
    animation: false,
    color: SERIES_COLORS,
    grid: { left: 68, right: 34, top: 46, bottom: 64, containLabel: false },
    tooltip: { trigger: 'item', confine: true, appendToBody: false },
    toolbox: {
      right: 18,
      top: 8,
      feature: { dataZoom: { yAxisIndex: false }, restore: {}, saveAsImage: { pixelRatio: 2 } },
    },
    xAxis: {
      type: xIsTime ? 'time' : 'value',
      name: xName,
      nameLocation: 'middle',
      nameGap: 34,
      axisLine: { lineStyle: { color: '#9aaabc' } },
      axisTick: { lineStyle: { color: '#b8c4cf' } },
      axisLabel: xIsTime ? { formatter: (value: number) => timestampLabel(value), color: '#64748b' } : { color: '#64748b' },
      splitLine: { lineStyle: { color: '#e8edf3', type: 'dashed' } },
    },
    yAxis: {
      type: 'value',
      name: yName,
      nameLocation: 'middle',
      nameGap: 50,
      axisLine: { show: true, lineStyle: { color: '#9aaabc' } },
      axisTick: { show: true, lineStyle: { color: '#b8c4cf' } },
      axisLabel: { color: '#64748b' },
      splitLine: { lineStyle: { color: '#e8edf3', type: 'dashed' } },
    },
    dataZoom: [
      { type: 'inside', xAxisIndex: 0, filterMode: 'none' },
      { type: 'slider', xAxisIndex: 0, height: 18, bottom: 14, borderColor: '#d9e2ea', fillerColor: 'rgba(37,99,235,.10)', handleSize: '75%' },
    ],
  };
}

function EChartsCanvas({ option, rows, onSelect }: { option: echarts.EChartsOption; rows: DataRow[]; onSelect: (row: DataRow) => void }) {
  const hostRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return undefined;
    const chart = echarts.init(host, undefined, { renderer: 'canvas' });
    chart.setOption(option, { notMerge: true, lazyUpdate: false });
    const handleClick = (params: { data?: unknown }) => {
      const customIndex = typeof params.data === 'object' && params.data !== null && '__rowIndex' in params.data
        ? Number((params.data as { __rowIndex?: number }).__rowIndex)
        : undefined;
      if (Number.isInteger(customIndex) && rows[customIndex!]) onSelect(rows[customIndex!]);
    };
    chart.on('click', handleClick);
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(host);
    return () => {
      observer.disconnect();
      chart.off('click', handleClick);
      chart.dispose();
    };
  }, [onSelect, option, rows]);

  return <div ref={hostRef} className="data-viz-echart"/>;
}

function TrendChart({ rows, recipe, onSelect }: { rows: DataRow[]; recipe: VisualizationRecipe; onSelect: (row: DataRow) => void }) {
  const option = useMemo<echarts.EChartsOption>(() => {
    const series = recipe.yFields.map((field) => {
      const data = rows.flatMap((row, rowIndex) => {
        const x = numericValue(row, recipe.xField);
        const y = numericValue(row, field);
        return x === undefined || y === undefined ? [] : [{ value: [x, y], __rowIndex: rowIndex }];
      });
      return { field, data };
    }).filter((item) => item.data.length > 0);
    if (!series.length) return {};
    const base = baseCartesianOption(recipe.xField, series.map((item) => item.field).join(' / '), recipe.xField === 'timestamp');
    return {
      ...base,
      legend: { type: 'scroll', top: 8, left: 18, right: 118, itemWidth: 18, itemHeight: 8, pageIconSize: 11, pageButtonItemGap: 5, textStyle: { color: '#526579' } },
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross', snap: false }, confine: true },
      series: series.map((item, index) => ({
        name: item.field,
        type: 'line',
        data: item.data,
        showSymbol: false,
        smooth: false,
        connectNulls: false,
        progressive: 5000,
        progressiveThreshold: 5000,
        lineStyle: { width: 2.2, color: SERIES_COLORS[index % SERIES_COLORS.length] },
        emphasis: { focus: 'series' },
      })),
    };
  }, [recipe.xField, recipe.yFields, rows]);
  if (!recipe.yFields.length) return <EmptyChart text="请选择至少一个 Y 轴字段。"/>;
  return <EChartsCanvas option={option} rows={rows} onSelect={onSelect}/>;
}

function ScatterOrXYChart({ rows, recipe, trajectory, progressIndex, onSelect }: { rows: DataRow[]; recipe: VisualizationRecipe; trajectory: boolean; progressIndex: number; onSelect: (row: DataRow) => void }) {
  const points = useMemo(() => rows.flatMap((row, rowIndex) => {
    const x = numericValue(row, recipe.xField);
    const y = numericValue(row, recipe.yField);
    const value = numericValue(row, recipe.valueField);
    return x === undefined || y === undefined ? [] : [{ row, rowIndex, x, y, value } as NumericPoint];
  }), [recipe.valueField, recipe.xField, recipe.yField, rows]);

  const option = useMemo<echarts.EChartsOption>(() => {
    if (!points.length) return {};
    const base = baseCartesianOption(recipe.xField, recipe.yField);
    const progressive = trajectory && Boolean(recipe.progressiveTrajectory);
    const activePoints = progressive ? points.filter((point) => point.rowIndex <= progressIndex) : points;
    const values = points.flatMap((point) => point.value === undefined ? [] : [point.value]);
    const [minValue, maxValue] = values.length ? expandRange(...minMax(values)) : [0, 1];
    const pointData = activePoints.map((point) => ({
      value: point.value === undefined ? [point.x, point.y] : [point.x, point.y, point.value],
      __rowIndex: point.rowIndex,
    }));
    const lineData = activePoints.map((point) => ({ value: [point.x, point.y, point.value ?? 0], __rowIndex: point.rowIndex }));
    return {
      ...base,
      tooltip: {
        trigger: 'item',
        confine: true,
        formatter: (raw: unknown) => {
          const params = raw as { data?: { __rowIndex?: number; value?: number[] } };
          const index = params.data?.__rowIndex;
          const row = index === undefined ? undefined : rows[index];
          const valuesNow = params.data?.value || [];
          return row ? `${escapeHtml(row.timestamp)}<br/>${escapeHtml(recipe.xField)}: ${formatAxisNumber(valuesNow[0])}<br/>${escapeHtml(recipe.yField)}: ${formatAxisNumber(valuesNow[1])}${recipe.valueField && valuesNow[2] !== undefined ? `<br/>${escapeHtml(recipe.valueField)}: ${formatAxisNumber(valuesNow[2])}` : ''}` : '';
        },
      },
      visualMap: recipe.valueField && values.length ? {
        min: minValue,
        max: maxValue,
        dimension: 2,
        orient: 'vertical',
        right: 14,
        top: 72,
        calculable: true,
        text: [recipe.valueField, ''],
        seriesIndex: trajectory ? 1 : 0,
        inRange: { color: ['#2563eb', '#06b6d4', '#22c55e', '#f59e0b', '#dc2626'] },
      } : undefined,
      series: trajectory ? [
        {
          name: '轨迹',
          type: 'line',
          data: lineData,
          showSymbol: false,
          connectNulls: false,
          smooth: false,
          lineStyle: { width: 2.4, color: '#315f89' },
          emphasis: { lineStyle: { width: 3 } },
          // XY 轨迹只强调当前/终点，不再绘制任何“相对起点”的辅助视觉元素。
          // 轨迹线本身仍严格按采样时间顺序连接，不额外连接回起点。
          markPoint: activePoints.length > 1 ? {
            symbol: 'circle',
            symbolSize: 30,
            label: { fontSize: 10, color: '#fff', fontWeight: 700 },
            data: [
              { name: progressive ? '当前' : '终点', value: progressive ? '进' : '终', coord: [activePoints[activePoints.length - 1].x, activePoints[activePoints.length - 1].y], itemStyle: { color: progressive ? '#f59e0b' : '#dc5b5b' } },
            ],
          } : undefined,
        },
        {
          name: '采样点',
          type: 'scatter',
          data: pointData,
          symbolSize: activePoints.length > 1500 ? 4 : 6,
          itemStyle: { opacity: activePoints.length > 1500 ? 0.45 : 0.75 },
          progressive: 2000,
        },
      ] : [{
        name: '数据点',
        type: 'scatter',
        data: pointData,
        symbolSize: points.length > 1500 ? 5 : 8,
        itemStyle: { opacity: 0.8 },
        progressive: 3000,
      }],
    };
  }, [points, progressIndex, recipe.progressiveTrajectory, recipe.valueField, recipe.xField, recipe.yField, rows, trajectory]);

  if (!points.length) return <EmptyChart text="X / Y 字段没有可绘制的数值数据。"/>;
  return <EChartsCanvas option={option} rows={rows} onSelect={onSelect}/>;
}

function buildHeatGrid(points: NumericPoint[], gridSize: number) {
  const bounds = boundsOf(points);
  const cols = Math.max(16, Math.min(60, Math.round(gridSize)));
  const rows = Math.max(12, Math.round(cols * 0.68));
  const dx = (bounds.maxX - bounds.minX) / cols;
  const dy = (bounds.maxY - bounds.minY) / rows;
  const cells: HeatCell[] = [];
  const values: number[] = [];
  const source = points;
  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < cols; col += 1) {
      const x = bounds.minX + (col + 0.5) * dx;
      const y = bounds.minY + (row + 0.5) * dy;
      let numerator = 0; let denominator = 0; let exact: number | undefined;
      for (const point of source) {
        const vx = (point.x - x) / (bounds.maxX - bounds.minX || 1);
        const vy = (point.y - y) / (bounds.maxY - bounds.minY || 1);
        const dist2 = vx * vx + vy * vy;
        if (dist2 < 1e-10) { exact = point.value ?? 0; break; }
        const weight = 1 / Math.pow(dist2, 1.15);
        numerator += (point.value ?? 0) * weight;
        denominator += weight;
      }
      const value = exact ?? (denominator ? numerator / denominator : 0);
      values.push(value);
      cells.push({ x, y, value, ratio: 0 });
    }
  }
  const [minValue, maxValue] = expandRange(...minMax(values));
  cells.forEach((cell) => { cell.ratio = (cell.value - minValue) / (maxValue - minValue || 1); });
  return { bounds, cols, rows, dx, dy, cells, minValue, maxValue };
}

function contourSegments(grid: ReturnType<typeof buildHeatGrid>, levels = 7): Array<{ x1: number; y1: number; x2: number; y2: number; level: number }> {
  const result: Array<{ x1: number; y1: number; x2: number; y2: number; level: number }> = [];
  const { cols, rows, cells, minValue, maxValue } = grid;
  const valueAt = (row: number, col: number) => cells[row * cols + col]?.value ?? 0;
  const pointAt = (row: number, col: number) => ({ x: grid.bounds.minX + (col + 0.5) * grid.dx, y: grid.bounds.minY + (row + 0.5) * grid.dy });
  const interpolate = (a: { x: number; y: number }, b: { x: number; y: number }, va: number, vb: number, level: number) => {
    const t = Math.max(0, Math.min(1, (level - va) / (vb - va || 1)));
    return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
  };
  for (let levelIndex = 1; levelIndex <= levels; levelIndex += 1) {
    const level = minValue + (levelIndex / (levels + 1)) * (maxValue - minValue);
    for (let row = 0; row < rows - 1; row += 1) {
      for (let col = 0; col < cols - 1; col += 1) {
        const p00 = pointAt(row, col); const p10 = pointAt(row, col + 1); const p11 = pointAt(row + 1, col + 1); const p01 = pointAt(row + 1, col);
        const v00 = valueAt(row, col); const v10 = valueAt(row, col + 1); const v11 = valueAt(row + 1, col + 1); const v01 = valueAt(row + 1, col);
        const intersections: Array<{ x: number; y: number }> = [];
        const cross = (va: number, vb: number) => (va <= level && vb > level) || (vb <= level && va > level);
        if (cross(v00, v10)) intersections.push(interpolate(p00, p10, v00, v10, level));
        if (cross(v10, v11)) intersections.push(interpolate(p10, p11, v10, v11, level));
        if (cross(v11, v01)) intersections.push(interpolate(p11, p01, v11, v01, level));
        if (cross(v01, v00)) intersections.push(interpolate(p01, p00, v01, v00, level));
        if (intersections.length >= 2) {
          for (let index = 0; index + 1 < intersections.length; index += 2) result.push({ x1: intersections[index].x, y1: intersections[index].y, x2: intersections[index + 1].x, y2: intersections[index + 1].y, level });
        }
      }
    }
  }
  return result;
}

function HeatmapChart({ rows, recipe, progressIndex, onSelect }: { rows: DataRow[]; recipe: VisualizationRecipe; progressIndex: number; onSelect: (row: DataRow) => void }) {
  const points = useMemo(() => rows.flatMap((row, rowIndex) => {
    const x = numericValue(row, recipe.xField); const y = numericValue(row, recipe.yField); const value = numericValue(row, recipe.valueField);
    return x === undefined || y === undefined || value === undefined ? [] : [{ row, rowIndex, x, y, value } as NumericPoint];
  }), [recipe.valueField, recipe.xField, recipe.yField, rows]);

  const option = useMemo<echarts.EChartsOption>(() => {
    if (points.length < 3) return {};
    const grid = buildHeatGrid(points, recipe.heatmapGridSize);
    const contours = recipe.showContours ? contourSegments(grid) : [];
    const base = baseCartesianOption(recipe.xField, recipe.yField);
    const heatData = grid.cells.map((cell) => [cell.x, cell.y, cell.value]);
    const clickData = points.map((point) => ({ value: [point.x, point.y, point.value], __rowIndex: point.rowIndex }));
    const series: unknown[] = [
      {
        name: recipe.valueField,
        type: 'custom',
        coordinateSystem: 'cartesian2d',
        data: heatData,
        dimensions: [recipe.xField, recipe.yField, recipe.valueField],
        encode: { x: 0, y: 1, tooltip: [0, 1, 2] },
        silent: false,
        renderItem: (_params: unknown, api: { value: (dimension: number) => number; coord: (value: number[]) => number[]; size: (value: number[]) => number[]; visual: (key: string) => string }) => {
          const x = api.value(0);
          const y = api.value(1);
          const coord = api.coord([x, y]);
          const size = api.size([grid.dx, grid.dy]);
          return {
            type: 'rect',
            shape: {
              x: coord[0] - Math.abs(size[0]) / 2 - 0.35,
              y: coord[1] - Math.abs(size[1]) / 2 - 0.35,
              width: Math.abs(size[0]) + 0.7,
              height: Math.abs(size[1]) + 0.7,
            },
            style: { fill: api.visual('color') },
            emphasis: { style: { stroke: '#334155', lineWidth: 1 } },
          };
        },
        progressive: 3000,
        z: 1,
      },
    ];
    if (recipe.showContours && contours.length) {
      series.push({
        name: '等高线',
        type: 'custom',
        coordinateSystem: 'cartesian2d',
        data: contours.map((segment) => [segment.x1, segment.y1, segment.x2, segment.y2, segment.level]),
        silent: true,
        renderItem: (_params: unknown, api: { value: (dimension: number) => number; coord: (value: number[]) => number[] }) => {
          const start = api.coord([api.value(0), api.value(1)]);
          const end = api.coord([api.value(2), api.value(3)]);
          return {
            type: 'line',
            shape: { x1: start[0], y1: start[1], x2: end[0], y2: end[1] },
            style: { stroke: 'rgba(30,41,59,.70)', lineWidth: 1.05, opacity: 0.8 },
          };
        },
        z: 4,
      });
    }
    if (recipe.showTrajectory) {
      const trajectoryPoints = recipe.progressiveTrajectory ? points.filter((point) => point.rowIndex <= progressIndex) : points;
      series.push({
        name: 'XY轨迹',
        type: 'line',
        data: trajectoryPoints.map((point) => [point.x, point.y]),
        showSymbol: false,
        connectNulls: false,
        smooth: false,
        silent: true,
        lineStyle: { color: '#ffffff', width: 2.4, shadowColor: 'rgba(15,23,42,.45)', shadowBlur: 4 },
        z: 5,
      });
    }
    series.push({
      name: '原始采样',
      type: 'scatter',
      data: clickData,
      symbolSize: 8,
      itemStyle: { opacity: 0.03, color: '#fff' },
      progressive: 5000,
      progressiveThreshold: 5000,
      z: 6,
    });
    return {
      ...base,
      visualMap: {
        min: grid.minValue,
        max: grid.maxValue,
        calculable: true,
        orient: 'vertical',
        right: 12,
        top: 72,
        precision: 3,
        text: [recipe.valueField, ''],
        seriesIndex: 0,
        inRange: { color: ['#1e3a8a', '#2563eb', '#06b6d4', '#22c55e', '#fde047', '#f97316', '#dc2626'] },
      },
      tooltip: {
        trigger: 'item',
        confine: true,
        formatter: (raw: unknown) => {
          const params = raw as { seriesName?: string; data?: { __rowIndex?: number; value?: number[] } | number[]; value?: number[] };
          if (typeof params.data === 'object' && params.data !== null && !Array.isArray(params.data) && params.data.__rowIndex !== undefined) {
            const row = rows[params.data.__rowIndex];
            return row ? `${escapeHtml(row.timestamp)}<br/>${escapeHtml(recipe.xField)}: ${formatAxisNumber(params.data.value?.[0] ?? 0)}<br/>${escapeHtml(recipe.yField)}: ${formatAxisNumber(params.data.value?.[1] ?? 0)}<br/>${escapeHtml(recipe.valueField)}: ${formatAxisNumber(params.data.value?.[2] ?? 0)}` : '';
          }
          const value = Array.isArray(params.value) ? params.value : [];
          return `${escapeHtml(recipe.xField)}: ${formatAxisNumber(value[0] ?? 0)}<br/>${escapeHtml(recipe.yField)}: ${formatAxisNumber(value[1] ?? 0)}<br/>${escapeHtml(recipe.valueField)}: ${formatAxisNumber(value[2] ?? 0)}`;
        },
      },
      series: series as never,
    };
  }, [points, progressIndex, recipe.heatmapGridSize, recipe.progressiveTrajectory, recipe.showContours, recipe.showTrajectory, recipe.valueField, recipe.xField, recipe.yField, rows]);

  if (points.length < 3) return <EmptyChart text="热力图至少需要 3 个包含 X / Y / 数值的采样点。"/>;
  return <EChartsCanvas option={option} rows={rows} onSelect={onSelect}/>;
}

function rotateEuler(vector: [number, number, number], roll: number, pitch: number, yaw: number): [number, number, number] {
  const cr = Math.cos(roll); const sr = Math.sin(roll); const cp = Math.cos(pitch); const sp = Math.sin(pitch); const cy = Math.cos(yaw); const syaw = Math.sin(yaw);
  const [x, y, z] = vector;
  return [
    (cy * cp) * x + (cy * sp * sr - syaw * cr) * y + (cy * sp * cr + syaw * sr) * z,
    (syaw * cp) * x + (syaw * sp * sr + cy * cr) * y + (syaw * sp * cr - cy * sr) * z,
    (-sp) * x + (cp * sr) * y + (cp * cr) * z,
  ];
}

interface ThreeRuntime {
  currentMarker: THREE.Mesh;
  arrows: THREE.ArrowHelper[];
  trajectoryGeometry: THREE.BufferGeometry;
  pointGeometry: THREE.BufferGeometry;
  endMarker: THREE.Mesh;
  rawCenter: THREE.Vector3;
  sceneScale: number;
}

function createCircularPointTexture(): THREE.CanvasTexture {
  const canvas = document.createElement('canvas');
  canvas.width = 32;
  canvas.height = 32;
  const context = canvas.getContext('2d');
  if (context) {
    context.clearRect(0, 0, 32, 32);
    context.beginPath();
    context.arc(16, 16, 12, 0, Math.PI * 2);
    context.fillStyle = '#ffffff';
    context.fill();
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.needsUpdate = true;
  return texture;
}

function toSceneVector(point: NumericPoint, rawCenter: THREE.Vector3, sceneScale: number): THREE.Vector3 {
  return new THREE.Vector3(point.x, point.y, point.z ?? 0).sub(rawCenter).multiplyScalar(sceneScale);
}

function ThreeTrajectoryScene({ points, currentIndex, visibleCount, orientationUnit, showOrientation = false, onSelect }: { points: NumericPoint[] | SixDofPoint[]; currentIndex?: number; visibleCount?: number; orientationUnit?: 'degree' | 'radian'; showOrientation?: boolean; onSelect: (row: DataRow) => void }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const runtimeRef = useRef<ThreeRuntime>();

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !points.length) return undefined;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf8fafc);

    // 3D 仅使用局部显示坐标：先移除绝对位置基值，再把真实跨度等比例映射到固定场景空间。
    // 这样微米级位移不会被 Float32 精度和 near clipping 吞掉，同时仍保持 XYZ 相对比例不变。
    const rawVectors = points.map((point) => new THREE.Vector3(point.x, point.y, point.z ?? 0));
    const rawBox = new THREE.Box3().setFromPoints(rawVectors);
    const rawCenter = rawBox.getCenter(new THREE.Vector3());
    const rawSize = rawBox.getSize(new THREE.Vector3());
    const rawMaxSpan = Math.max(rawSize.x, rawSize.y, rawSize.z);
    const targetSceneSpan = 12;
    const sceneScale = Number.isFinite(rawMaxSpan) && rawMaxSpan > Number.EPSILON ? targetSceneSpan / rawMaxSpan : 1;
    const vectors = points.map((point) => toSceneVector(point, rawCenter, sceneScale));
    const box = new THREE.Box3().setFromPoints(vectors);
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const maxSpan = Math.max(size.x, size.y, size.z, 1);
    const radius = Math.max(size.length() / 2, maxSpan * 0.55, 1);

    const camera = new THREE.PerspectiveCamera(46, 1, Math.max(radius / 2000, 0.001), Math.max(radius * 50, 100));
    camera.up.set(0, 0, 1);
    const distance = radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.04;
    camera.position.copy(center).add(new THREE.Vector3(1.25, -1.45, 1.05).normalize().multiplyScalar(distance));
    camera.lookAt(center);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.domElement.className = 'data-viz-three-canvas';
    host.replaceChildren(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.copy(center);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.zoomToCursor = true;
    controls.minDistance = radius * 0.08;
    controls.maxDistance = distance * 8;
    controls.update();

    const ambient = new THREE.HemisphereLight(0xffffff, 0x94a3b8, 1.35);
    scene.add(ambient);

    const gridSize = Math.max(size.x, size.y, maxSpan * 0.8);
    const grid = new THREE.GridHelper(gridSize, 12, 0x94a3b8, 0xdbe4ec);
    grid.rotation.x = Math.PI / 2;
    grid.position.set(center.x, center.y, Number.isFinite(box.min.z) ? box.min.z : center.z);
    scene.add(grid);

    const axes = new THREE.AxesHelper(maxSpan * 0.18);
    axes.position.set(box.min.x, box.min.y, Number.isFinite(box.min.z) ? box.min.z : center.z);
    scene.add(axes);

    const trajectoryGeometry = new THREE.BufferGeometry().setFromPoints(vectors);
    const initialVisibleCount = Math.max(0, Math.min(points.length, visibleCount === undefined ? points.length : Math.floor(visibleCount)));
    trajectoryGeometry.setDrawRange(0, initialVisibleCount);
    const trajectoryMaterial = new THREE.LineBasicMaterial({ color: 0x2563eb, transparent: true, opacity: 0.92 });
    const trajectory = new THREE.Line(trajectoryGeometry, trajectoryMaterial);
    scene.add(trajectory);

    const pointGeometry = new THREE.BufferGeometry().setFromPoints(vectors);
    pointGeometry.setDrawRange(0, initialVisibleCount);
    const circularPointTexture = createCircularPointTexture();
    const pointMaterial = new THREE.PointsMaterial({
      color: 0x0f5f9f,
      map: circularPointTexture,
      alphaTest: 0.18,
      size: points.length > 1800 ? 3.5 : points.length > 700 ? 4 : 4.5,
      sizeAttenuation: false,
      transparent: true,
      opacity: 0.82,
      depthWrite: false,
    });
    const pointCloud = new THREE.Points(pointGeometry, pointMaterial);
    scene.add(pointCloud);

    const endpointGeometry = new THREE.SphereGeometry(Math.max(maxSpan * 0.014, 0.0001), 18, 14);
    const startMaterial = new THREE.MeshBasicMaterial({ color: 0x16a36b });
    const endMaterial = new THREE.MeshBasicMaterial({ color: 0xdc5b5b });
    const startMarker = new THREE.Mesh(endpointGeometry, startMaterial);
    startMarker.position.copy(vectors[0]);
    scene.add(startMarker);
    const endMarker = new THREE.Mesh(endpointGeometry, endMaterial);
    endMarker.visible = initialVisibleCount > 0;
    endMarker.position.copy(vectors[Math.max(0, initialVisibleCount - 1)]);
    scene.add(endMarker);

    const currentGeometry = new THREE.SphereGeometry(Math.max(maxSpan * 0.020, 0.0001), 20, 16);
    const currentMaterial = new THREE.MeshBasicMaterial({ color: 0xf59e0b });
    const currentMarker = new THREE.Mesh(currentGeometry, currentMaterial);
    currentMarker.visible = showOrientation;
    scene.add(currentMarker);

    const arrows = [
      new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), center, maxSpan * 0.12, 0xef4444, maxSpan * 0.025, maxSpan * 0.014),
      new THREE.ArrowHelper(new THREE.Vector3(0, 1, 0), center, maxSpan * 0.12, 0x22c55e, maxSpan * 0.025, maxSpan * 0.014),
      new THREE.ArrowHelper(new THREE.Vector3(0, 0, 1), center, maxSpan * 0.12, 0x3b82f6, maxSpan * 0.025, maxSpan * 0.014),
    ];
    arrows.forEach((arrow) => { arrow.visible = showOrientation; scene.add(arrow); });
    runtimeRef.current = { currentMarker, arrows, trajectoryGeometry, pointGeometry, endMarker, rawCenter, sceneScale };

    const raycaster = new THREE.Raycaster();
    raycaster.params.Points = { threshold: Math.max(maxSpan * 0.028, 0.0001) };
    const pointer = new THREE.Vector2();
    const handlePointer = (event: PointerEvent) => {
      const rect = renderer.domElement.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hit = raycaster.intersectObject(pointCloud, false)[0];
      if (hit?.index !== undefined && points[hit.index]) onSelect(points[hit.index].row);
    };
    renderer.domElement.addEventListener('pointerdown', handlePointer);

    const resize = () => {
      const width = Math.max(1, host.clientWidth);
      const height = Math.max(1, host.clientHeight);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height, false);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    resize();

    renderer.setAnimationLoop(() => {
      controls.update();
      renderer.render(scene, camera);
    });

    return () => {
      runtimeRef.current = undefined;
      observer.disconnect();
      renderer.setAnimationLoop(null);
      renderer.domElement.removeEventListener('pointerdown', handlePointer);
      controls.dispose();
      trajectoryGeometry.dispose(); trajectoryMaterial.dispose();
      pointGeometry.dispose(); pointMaterial.dispose(); circularPointTexture.dispose();
      endpointGeometry.dispose(); startMaterial.dispose(); endMaterial.dispose();
      currentGeometry.dispose(); currentMaterial.dispose();
      grid.geometry.dispose();
      if (Array.isArray(grid.material)) grid.material.forEach((material) => material.dispose()); else grid.material.dispose();
      axes.geometry.dispose();
      if (Array.isArray(axes.material)) axes.material.forEach((material) => material.dispose()); else axes.material.dispose();
      arrows.forEach((arrow) => {
        arrow.line.geometry.dispose();
        (arrow.line.material as THREE.Material).dispose();
        arrow.cone.geometry.dispose();
        (arrow.cone.material as THREE.Material).dispose();
      });
      renderer.dispose();
      host.replaceChildren();
    };
  }, [onSelect, points, showOrientation]);


  useEffect(() => {
    if (!points.length || !runtimeRef.current) return;
    const requested = visibleCount === undefined ? points.length : visibleCount;
    const safeCount = Math.max(0, Math.min(points.length, Math.floor(requested)));
    runtimeRef.current.trajectoryGeometry.setDrawRange(0, safeCount);
    runtimeRef.current.pointGeometry.setDrawRange(0, safeCount);
    runtimeRef.current.endMarker.visible = safeCount > 0;
    if (safeCount > 0) {
      const point = toSceneVector(points[safeCount - 1], runtimeRef.current.rawCenter, runtimeRef.current.sceneScale);
      runtimeRef.current.endMarker.position.copy(point);
    }
  }, [points, visibleCount]);

  useEffect(() => {
    if (!showOrientation || !points.length || !runtimeRef.current) return;
    const safeIndex = Math.max(0, Math.min(points.length - 1, currentIndex ?? 0));
    const point = points[safeIndex] as SixDofPoint;
    const unit = orientationUnit === 'radian' ? 1 : Math.PI / 180;
    const scenePoint = toSceneVector(point, runtimeRef.current.rawCenter, runtimeRef.current.sceneScale);
    runtimeRef.current.currentMarker.position.copy(scenePoint);
    const directions: Array<[number, number, number]> = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
    directions.forEach((vector, index) => {
      const rotated = rotateEuler(vector, point.roll * unit, point.pitch * unit, point.yaw * unit);
      const arrow = runtimeRef.current!.arrows[index];
      arrow.position.copy(scenePoint);
      arrow.setDirection(new THREE.Vector3(rotated[0], rotated[1], rotated[2]).normalize());
    });
  }, [currentIndex, orientationUnit, points, showOrientation]);

  return <div className="data-viz-three-wrap">
    <div ref={hostRef} className="data-viz-three-host"/>
    <div className="data-viz-three-axis-legend"><span className="x">X</span><span className="y">Y</span><span className="z">Z</span><small>左键旋转 · 滚轮缩放 · 右键平移</small></div>
  </div>;
}

function XYZChart({ rows, recipe, progressIndex, onSelect }: { rows: DataRow[]; recipe: VisualizationRecipe; progressIndex: number; onSelect: (row: DataRow) => void }) {
  const points = useMemo(() => rows.flatMap((row, rowIndex) => {
    const x = numericValue(row, recipe.xField); const y = numericValue(row, recipe.yField); const z = numericValue(row, recipe.zField);
    return x === undefined || y === undefined || z === undefined ? [] : [{ row, rowIndex, x, y, z } as NumericPoint];
  }), [recipe.xField, recipe.yField, recipe.zField, rows]);
  if (!points.length) return <EmptyChart text="X / Y / Z 字段没有完整的数值数据。"/>;
  const visibleCount = recipe.progressiveTrajectory ? points.filter((point) => point.rowIndex <= progressIndex).length : undefined;
  return <ThreeTrajectoryScene points={points} visibleCount={visibleCount} onSelect={onSelect}/>;
}

function SixDofChart({ rows, recipe, index, onSelect }: { rows: DataRow[]; recipe: VisualizationRecipe; index: number; onSelect: (row: DataRow) => void }) {
  const points = useMemo(() => rows.flatMap((row, rowIndex) => {
    const x = numericValue(row, recipe.xField); const y = numericValue(row, recipe.yField); const z = numericValue(row, recipe.zField);
    const roll = numericValue(row, recipe.rollField); const pitch = numericValue(row, recipe.pitchField); const yaw = numericValue(row, recipe.yawField);
    return [x, y, z, roll, pitch, yaw].some((value) => value === undefined) ? [] : [{ row, rowIndex, x: x!, y: y!, z: z!, roll: roll!, pitch: pitch!, yaw: yaw! } as SixDofPoint];
  }), [recipe.pitchField, recipe.rollField, recipe.xField, recipe.yField, recipe.yawField, recipe.zField, rows]);
  if (!points.length) return <EmptyChart text="6DoF 需要 X / Y / Z / Roll / Pitch / Yaw 六个完整数值字段。"/>;
  const progressedCount = points.filter((point) => point.rowIndex <= index).length;
  const safeIndex = Math.max(0, Math.min(points.length - 1, progressedCount - 1));
  const current = points[safeIndex];
  const visibleCount = recipe.progressiveTrajectory ? progressedCount : undefined;
  return <div className="data-viz-sixdof-stage">
    <ThreeTrajectoryScene points={points} currentIndex={safeIndex} visibleCount={visibleCount} orientationUnit={recipe.orientationUnit} showOrientation onSelect={onSelect}/>
    <div className="data-viz-sixdof-readout"><b>{current.row.timestamp}</b><span>位置：{formatAxisNumber(current.x)}, {formatAxisNumber(current.y)}, {formatAxisNumber(current.z)}</span><span>姿态：R {formatAxisNumber(current.roll)} · P {formatAxisNumber(current.pitch)} · Y {formatAxisNumber(current.yaw)} {recipe.orientationUnit === 'degree' ? '°' : 'rad'}</span></div>
  </div>;
}

function EmptyChart({ text }: { text: string }) {
  return <div className="data-viz-empty"><strong>暂无可绘制数据</strong><span>{text}</span></div>;
}

function FieldSelect({ label, value, fields, onChange, allowTimestamp = false, optional = false }: { label: string; value: string; fields: string[]; onChange: (value: string) => void; allowTimestamp?: boolean; optional?: boolean }) {
  const choices = allowTimestamp ? ['timestamp', ...fields.filter((field) => field !== 'timestamp')] : fields;
  return <label className="data-viz-field"><span>{label}</span><select value={value} onChange={(event) => onChange(event.target.value)}>{optional && <option value="">不使用</option>}{!value && !optional && <option value="">请选择字段</option>}{choices.map((field) => <option value={field} key={field}>{field}</option>)}</select></label>;
}

function modeLabel(mode: VisualizationMode): string {
  return MODE_OPTIONS.find((item) => item.value === mode)?.label || mode;
}

export function DataVisualizationDialog({ dataset, onClose, onOpenSourceLog }: Props) {
  const numericFields = useMemo(() => detectNumericFields(dataset), [dataset]);
  const sources = useMemo(() => datasetSources(dataset), [dataset]);
  const [recipe, setRecipe] = useState<VisualizationRecipe>(() => {
    const defaults = createDefaultVisualizationRecipe(dataset);
    const stored = loadLastVisualizationRecipe(dataset.signature);
    return stored ? { ...defaults, ...stored, yFields: Array.isArray(stored.yFields) ? [...stored.yFields] : defaults.yFields, progressiveTrajectory: stored.progressiveTrajectory ?? false, playbackSpeed: stored.playbackSpeed || 1 } : defaults;
  });
  const [storedRecipes, setStoredRecipes] = useState<StoredVisualizationRecipe[]>(() => listStoredVisualizationRecipes(dataset.signature));
  const [selectedRecipeId, setSelectedRecipeId] = useState('');
  const [selectedRow, setSelectedRow] = useState<DataRow>();
  const [playing, setPlaying] = useState(false);
  const [playIndex, setPlayIndex] = useState(0);

  useEffect(() => { saveLastVisualizationRecipe(dataset.signature, recipe); }, [dataset.signature, recipe]);
  const rows = useMemo(() => filteredVisualizationRows(dataset, recipe), [dataset, recipe]);

  useEffect(() => {
    setPlaying(false);
    setPlayIndex(0);
  }, [recipe.mode, recipe.sourceFilter, recipe.startPercent, recipe.endPercent, recipe.sampleLimit, recipe.progressiveTrajectory]);

  const supportsProgressiveTrajectory = recipe.mode === 'xy' || recipe.mode === 'xyz' || recipe.mode === 'sixdof' || (recipe.mode === 'heatmap' && recipe.showTrajectory);
  const showPlaybackControls = recipe.mode === 'sixdof' || (supportsProgressiveTrajectory && recipe.progressiveTrajectory);

  useEffect(() => {
    if (!playing || !showPlaybackControls || rows.length < 2) return undefined;
    const speed = Math.max(0.25, recipe.playbackSpeed || 1);
    const timer = window.setInterval(() => {
      setPlayIndex((current) => current >= rows.length - 1 ? 0 : current + 1);
    }, Math.max(24, Math.round(90 / speed)));
    return () => window.clearInterval(timer);
  }, [playing, recipe.playbackSpeed, rows.length, showPlaybackControls]);

  function patch(patchValue: Partial<VisualizationRecipe>) {
    setRecipe((current) => ({ ...current, ...patchValue }));
  }

  function changeMode(mode: VisualizationMode) {
    setRecipe((current) => normalizeRecipeForMode(dataset, current, mode));
  }

  function saveCurrentRecipe() {
    const name = window.prompt('可视化方案名称', `${dataset.name}-${modeLabel(recipe.mode)}`)?.trim();
    if (!name) return;
    const saved = saveVisualizationRecipe(dataset.signature, name, recipe);
    setStoredRecipes(listStoredVisualizationRecipes(dataset.signature));
    setSelectedRecipeId(saved.id);
  }

  function loadStoredRecipe(id: string) {
    setSelectedRecipeId(id);
    const stored = storedRecipes.find((item) => item.id === id);
    if (stored) {
      const defaults = createDefaultVisualizationRecipe(dataset, stored.recipe.mode);
      setRecipe({ ...defaults, ...stored.recipe, yFields: Array.isArray(stored.recipe.yFields) ? [...stored.recipe.yFields] : defaults.yFields, progressiveTrajectory: stored.recipe.progressiveTrajectory ?? false, playbackSpeed: stored.recipe.playbackSpeed || 1 });
    }
  }

  function removeStoredRecipe() {
    if (!selectedRecipeId) return;
    const stored = storedRecipes.find((item) => item.id === selectedRecipeId);
    if (!stored || !window.confirm(`删除可视化方案“${stored.name}”吗？`)) return;
    deleteVisualizationRecipe(selectedRecipeId);
    setSelectedRecipeId('');
    setStoredRecipes(listStoredVisualizationRecipes(dataset.signature));
  }

  function toggleTrendField(field: string) {
    setRecipe((current) => {
      const exists = current.yFields.includes(field);
      const next = exists ? current.yFields.filter((item) => item !== field) : [...current.yFields, field];
      return { ...current, yFields: next };
    });
  }

  const isReady = visualizationRecipeIsReady(recipe);
  const selectedSource = selectedRow ? sourceLabel(selectedRow) : '';

  return <div className="data-viz-backdrop" onMouseDown={onClose}>
    <section className={`data-viz-dialog${recipe.mode === 'sixdof' ? ' data-viz-dialog-sixdof' : ''}`} onMouseDown={(event) => event.stopPropagation()}>
      <header className="data-viz-header">
        <div><span className="eyebrow">DATA VISUALIZATION</span><h2>{dataset.name}</h2><p>{dataset.rows.length.toLocaleString()} 行原始提取数据</p></div>
        <div className="data-viz-header-actions"><select value={selectedRecipeId} onChange={(event) => loadStoredRecipe(event.target.value)}><option value="">已保存方案…</option>{storedRecipes.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select><button className="button secondary compact" onClick={saveCurrentRecipe}><Save size={14}/> 保存方案</button><button className="icon-button" disabled={!selectedRecipeId} onClick={removeStoredRecipe}><Trash2 size={15}/></button><button className="icon-button" onClick={onClose}><X size={18}/></button></div>
      </header>

      <div className="data-viz-layout">
        <aside className="data-viz-config">
          <section><strong>可视化类型</strong><div className="data-viz-mode-grid">{MODE_OPTIONS.map((item) => <button key={item.value} className={recipe.mode === item.value ? 'active' : ''} onClick={() => changeMode(item.value)}><span>{item.label}</span><small>{item.group}</small></button>)}</div></section>

          <section><strong>字段映射</strong>
            {recipe.mode === 'trend' && <><FieldSelect label="X轴" value={recipe.xField} fields={numericFields} allowTimestamp onChange={(xField) => patch({ xField })}/><div className="data-viz-multi-fields"><div className="data-viz-multi-fields-head"><span>Y轴</span><small>已选 {recipe.yFields.length} / {numericFields.length}</small><div><button type="button" onClick={() => patch({ yFields: [...numericFields] })} disabled={!numericFields.length || recipe.yFields.length === numericFields.length}>全选</button><button type="button" onClick={() => patch({ yFields: [] })} disabled={!recipe.yFields.length}>清空</button></div></div>{numericFields.map((field) => <label key={field}><input type="checkbox" checked={recipe.yFields.includes(field)} onChange={() => toggleTrendField(field)}/><span>{field}</span></label>)}</div></>}
            {(recipe.mode === 'scatter' || recipe.mode === 'xy') && <><FieldSelect label="X轴" value={recipe.xField} fields={numericFields} onChange={(xField) => patch({ xField })}/><FieldSelect label="Y轴" value={recipe.yField} fields={numericFields} onChange={(yField) => patch({ yField })}/><FieldSelect label="颜色数值" value={recipe.valueField} fields={numericFields} optional onChange={(valueField) => patch({ valueField })}/></>}
            {recipe.mode === 'xyz' && <><FieldSelect label="X位置" value={recipe.xField} fields={numericFields} onChange={(xField) => patch({ xField })}/><FieldSelect label="Y位置" value={recipe.yField} fields={numericFields} onChange={(yField) => patch({ yField })}/><FieldSelect label="Z位置" value={recipe.zField} fields={numericFields} onChange={(zField) => patch({ zField })}/><div className="data-viz-engine-hint">真实 Three.js 空间场景，可直接鼠标旋转、缩放和平移。</div></>}
            {recipe.mode === 'heatmap' && <><FieldSelect label="X位置" value={recipe.xField} fields={numericFields} onChange={(xField) => patch({ xField })}/><FieldSelect label="Y位置" value={recipe.yField} fields={numericFields} onChange={(yField) => patch({ yField })}/><FieldSelect label="热点数值" value={recipe.valueField} fields={numericFields} onChange={(valueField) => patch({ valueField })}/><label className="data-viz-check"><input type="checkbox" checked={recipe.showContours} onChange={(event) => patch({ showContours: event.target.checked })}/><span>叠加等高线</span></label><label className="data-viz-check"><input type="checkbox" checked={recipe.showTrajectory} onChange={(event) => patch({ showTrajectory: event.target.checked })}/><span>叠加 XY 轨迹</span></label><label className="data-viz-field"><span>插值网格</span><select value={recipe.heatmapGridSize} onChange={(event) => patch({ heatmapGridSize: Number(event.target.value) })}><option value={24}>快速 24</option><option value={34}>标准 34</option><option value={46}>精细 46</option><option value={58}>高精 58</option></select></label></>}
            {recipe.mode === 'sixdof' && <><FieldSelect label="Position X" value={recipe.xField} fields={numericFields} onChange={(xField) => patch({ xField })}/><FieldSelect label="Position Y" value={recipe.yField} fields={numericFields} onChange={(yField) => patch({ yField })}/><FieldSelect label="Position Z" value={recipe.zField} fields={numericFields} onChange={(zField) => patch({ zField })}/><FieldSelect label="Roll" value={recipe.rollField} fields={numericFields} onChange={(rollField) => patch({ rollField })}/><FieldSelect label="Pitch" value={recipe.pitchField} fields={numericFields} onChange={(pitchField) => patch({ pitchField })}/><FieldSelect label="Yaw" value={recipe.yawField} fields={numericFields} onChange={(yawField) => patch({ yawField })}/><label className="data-viz-field"><span>角度单位</span><select value={recipe.orientationUnit} onChange={(event) => patch({ orientationUnit: event.target.value as 'degree' | 'radian' })}><option value="degree">度 °</option><option value="radian">弧度 rad</option></select></label></>}
          </section>

          {supportsProgressiveTrajectory && <section><strong>轨迹显示</strong>
            <label className="data-viz-check"><input type="checkbox" checked={Boolean(recipe.progressiveTrajectory)} onChange={(event) => { setPlaying(false); setPlayIndex(0); patch({ progressiveTrajectory: event.target.checked }); }}/><span>逐步绘制轨迹</span></label>
            <div className="data-viz-engine-hint">{recipe.progressiveTrajectory ? '仅绘制当前进度之前的轨迹，适合密集路径逐段查看。' : '关闭时一次显示完整轨迹。'}</div>
            {recipe.progressiveTrajectory && <label className="data-viz-field"><span>播放速度</span><select value={recipe.playbackSpeed || 1} onChange={(event) => patch({ playbackSpeed: Number(event.target.value) })}><option value={0.5}>0.5×</option><option value={1}>1×</option><option value={2}>2×</option><option value={4}>4×</option></select></label>}
          </section>}

          <section><strong>数据范围</strong>
            {sources.length > 1 && <label className="data-viz-field"><span>数据来源</span><select value={recipe.sourceFilter} onChange={(event) => patch({ sourceFilter: event.target.value })}><option value="">全部来源</option>{sources.map((source) => <option value={source} key={source}>{source}</option>)}</select></label>}
            <div className="data-viz-range"><span>时间/行范围</span><div className="data-viz-range-control"><span className="data-viz-range-track"/><input type="range" min="0" max="100" value={recipe.startPercent} onChange={(event) => patch({ startPercent: Math.min(Number(event.target.value), recipe.endPercent - 1) })}/><input type="range" min="0" max="100" value={recipe.endPercent} onChange={(event) => patch({ endPercent: Math.max(Number(event.target.value), recipe.startPercent + 1) })}/></div><small>{recipe.startPercent}% → {recipe.endPercent}%</small></div>
            <label className="data-viz-field"><span>绘图点数</span><select value={recipe.sampleLimit} onChange={(event) => patch({ sampleLimit: Number(event.target.value) })}><option value={500}>500</option><option value={1000}>1,000</option><option value={1500}>1,500</option><option value={2500}>2,500</option><option value={5000}>5,000</option><option value={10000}>10,000</option><option value={20000}>20,000</option><option value={0}>All（全部）</option></select></label>
            <div className="data-viz-engine-hint">选择 All 时当前范围内全部数据参与绘图；选择具体点数时会在整个范围内均匀取点并保留首尾。</div>
          </section>
        </aside>

        <main className="data-viz-main">
          <div className="data-viz-stage">
            {!isReady ? <EmptyChart text="请在左侧完成当前图表所需字段映射。"/> : recipe.mode === 'trend' ? <TrendChart rows={rows} recipe={recipe} onSelect={setSelectedRow}/>
              : recipe.mode === 'scatter' ? <ScatterOrXYChart rows={rows} recipe={recipe} trajectory={false} progressIndex={playIndex} onSelect={setSelectedRow}/>
              : recipe.mode === 'xy' ? <ScatterOrXYChart rows={rows} recipe={recipe} trajectory progressIndex={playIndex} onSelect={setSelectedRow}/>
              : recipe.mode === 'xyz' ? <XYZChart rows={rows} recipe={recipe} progressIndex={playIndex} onSelect={setSelectedRow}/>
              : recipe.mode === 'heatmap' ? <HeatmapChart rows={rows} recipe={recipe} progressIndex={playIndex} onSelect={setSelectedRow}/>
              : <SixDofChart rows={rows} recipe={recipe} index={playIndex} onSelect={setSelectedRow}/>} 
          </div>

          {showPlaybackControls && isReady && rows.length > 0 && <div className="data-viz-player"><button className="button secondary compact" onClick={() => setPlaying((value) => !value)}>{playing ? <Pause size={14}/> : <Play size={14}/>} {playing ? '暂停' : '播放'}</button><input type="range" min="0" max={Math.max(0, rows.length - 1)} value={Math.min(playIndex, Math.max(0, rows.length - 1))} onChange={(event) => { setPlaying(false); setPlayIndex(Number(event.target.value)); }}/><span>{Math.min(playIndex + 1, rows.length)} / {rows.length}{recipe.progressiveTrajectory ? ` · ${recipe.playbackSpeed || 1}×` : ''}</span></div>}

          <div className="data-viz-footer-info">
            <div><strong>{modeLabel(recipe.mode)}</strong><span>{rows.length.toLocaleString()} 个绘图点{recipe.sourceFilter ? ` · ${recipe.sourceFilter}` : ''}</span></div>
            {selectedRow ? <div className="data-viz-selected"><div><b>{selectedRow.timestamp}</b><span>{selectedSource} · {selectedRow.sourceFile}:{selectedRow.lineNumber}</span></div>{onOpenSourceLog && <button className="button secondary compact" onClick={() => onOpenSourceLog(selectedRow)}><Eye size={14}/> 查看原日志</button>}<button className="button ghost compact" onClick={() => setSelectedRow(undefined)}>取消选中</button></div> : <span>{recipe.mode === 'xyz' || recipe.mode === 'sixdof' ? '点击 3D 采样点可查看原始日志；拖拽旋转、滚轮缩放。' : '点击图上的采样点可查看对应原始日志位置。'}</span>}
          </div>
        </main>
      </div>
    </section>
  </div>;
}
