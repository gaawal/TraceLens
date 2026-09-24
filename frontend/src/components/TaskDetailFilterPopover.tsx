import { type ReactNode, useEffect, useRef, useState } from 'react';
import { Check, Filter, X } from 'lucide-react';

interface Props {
  components: string[];
  levels: string[];
  modes: string[];
  selectedComponents: Set<string>;
  selectedLevels: Set<string>;
  selectedModes: Set<string>;
  timeRangeLabel?: string;
  durationMinMs?: string;
  durationMaxMs?: string;
  onToggleComponent: (value: string) => void;
  onToggleLevel: (value: string) => void;
  onToggleMode: (value: string) => void;
  onClearTimeRange: () => void;
  onDurationRangeChange: (minMs: string, maxMs: string) => void;
  onClearDurationRange: () => void;
  onClearAll: () => void;
}

function OptionChip({ active, children, onClick }: { active: boolean; children: ReactNode; onClick: () => void }) {
  return (
    <button type="button" className={active ? 'task-detail-option active' : 'task-detail-option'} onClick={onClick}>
      <span className="task-detail-option-check">{active && <Check size={10} />}</span>
      {children}
    </button>
  );
}

export function TaskDetailFilterPopover({
  components,
  levels,
  modes,
  selectedComponents,
  selectedLevels,
  selectedModes,
  timeRangeLabel,
  durationMinMs = '',
  durationMaxMs = '',
  onToggleComponent,
  onToggleLevel,
  onToggleMode,
  onClearTimeRange,
  onDurationRangeChange,
  onClearDurationRange,
  onClearAll,
}: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const durationActive = Boolean(durationMinMs || durationMaxMs);
  const activeCount = selectedComponents.size + selectedLevels.size + selectedModes.size + (timeRangeLabel ? 1 : 0) + (durationActive ? 1 : 0);

  useEffect(() => {
    if (!open) return undefined;
    const close = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', close, true);
    document.addEventListener('keydown', escape, true);
    return () => {
      document.removeEventListener('pointerdown', close, true);
      document.removeEventListener('keydown', escape, true);
    };
  }, [open]);

  return (
    <div className="task-detail-filter" ref={ref}>
      <button
        type="button"
        className={activeCount > 0 ? 'task-detail-filter-trigger active' : 'task-detail-filter-trigger'}
        onClick={(event) => { event.preventDefault(); setOpen((value) => !value); }}
        title="当前任务详细筛选"
        aria-expanded={open}
      >
        <Filter size={13} />
        {activeCount > 0 && <span>{activeCount}</span>}
      </button>
      {open && (
        <div className="task-detail-filter-menu">
          <div className="task-detail-filter-head">
            <div><strong>当前任务筛选</strong><span>仅影响已加载日志</span></div>
            {activeCount > 0 && <button type="button" onClick={onClearAll}><X size={11} /> 清除</button>}
          </div>
          <div className="task-detail-filter-section">
            <strong>组件</strong>
            <div>{components.map((component) => (
              <OptionChip key={component} active={selectedComponents.has(component)} onClick={() => onToggleComponent(component)}>{component}</OptionChip>
            ))}</div>
          </div>
          <div className="task-detail-filter-section compact">
            <strong>级别</strong>
            <div>{levels.map((level) => (
              <OptionChip key={level} active={selectedLevels.has(level)} onClick={() => onToggleLevel(level)}>{level}</OptionChip>
            ))}</div>
          </div>
          <div className="task-detail-filter-section compact">
            <strong>模式</strong>
            <div>{modes.map((mode) => (
              <OptionChip key={mode} active={selectedModes.has(mode)} onClick={() => onToggleMode(mode)}>
                {mode === '100' ? '100 内部' : mode === '101' ? '101 外部' : mode}
              </OptionChip>
            ))}</div>
          </div>

          <div className="task-detail-filter-section duration-range-section">
            <strong>折叠耗时范围（ms）</strong>
            <div className="duration-range-inputs">
              <label><span>最小</span><input type="number" min="0" step="0.001" value={durationMinMs} onChange={(event) => onDurationRangeChange(event.target.value, durationMaxMs)} placeholder="可空"/></label>
              <span className="duration-range-separator">—</span>
              <label><span>最大</span><input type="number" min="0" step="0.001" value={durationMaxMs} onChange={(event) => onDurationRangeChange(durationMinMs, event.target.value)} placeholder="可空"/></label>
              {durationActive && <button type="button" className="duration-range-clear" onClick={onClearDurationRange}><X size={11}/> 清除</button>}
            </div>
            <small>仅填最小值表示 ≥；仅填最大值表示 ≤；两项都填表示区间，前后顺序填反时系统自动取较小值到较大值。</small>
          </div>
          {timeRangeLabel && (
            <div className="task-detail-time-range">
              <span>{timeRangeLabel}</span>
              <button type="button" onClick={onClearTimeRange}><X size={11} /> 清除时间窗</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
