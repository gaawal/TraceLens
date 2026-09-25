import { useEffect, useState, type ReactNode } from 'react';
import { BookOpenCheck, Wand2, X } from 'lucide-react';
import type { AbnormalCase, AbnormalCaseEvidence } from '../api/resourceApi';
import type { LogEntry } from '../types';
import type { LogWindowRequest } from '../api/resourceApi';
import type { ErrorMatchRule } from '../parser/logParser';
import { AbnormalCaseAnalysisDialog } from './AbnormalCaseAnalysisDialog';
import { AbnormalCaseEditorDialog } from './AbnormalCaseEditorDialog';

export type SmartAnalysisTab = 'cases' | 'analysis';

interface Props {
  tab: SmartAnalysisTab;
  onTabChange: (tab: SmartAnalysisTab) => void;
  onClose: () => void;

  // 案例录入
  initialDraft?: Partial<AbnormalCase>;
  presetEvidences?: AbnormalCaseEvidence[];
  defaultSelectedEntryId?: string;
  environmentName?: string;
  sourceOperationId?: string;
  sourceTaskName?: string;
  querySnapshot?: LogWindowRequest;
  onSaved: (item: AbnormalCase) => void;

  // 相似案例匹配
  analysisEntries: readonly LogEntry[];
  selectedModules?: readonly string[];
  onLocateEntry: (entry: LogEntry) => void;
  aiPanel?: ReactNode;

  entries?: readonly LogEntry[];
  environmentId?: number;
  errorRules: readonly ErrorMatchRule[];
}

const TABS: Array<{ id: SmartAnalysisTab; label: string; hint: string }> = [
  { id: 'analysis', label: '相似案例匹配', hint: '用当前异常日志匹配知识库里已启用的案例，给出相似度与证据对照' },
  { id: 'cases', label: '案例录入', hint: '把这次现场（异常日志 + 现场特征）保存成可复用的案例' },
];

/**
 * 智能分析 — one window, two tabs.
 *
 * 「匹配已有案例」和「把这次现象录成案例」是同一条工作流的进出两端：看一眼历史经验，
 * 或者沉淀一条新的。以前它们在工具栏上是两个按钮、两个对话框，用户得先猜哪个是自己要的。
 * 这里只做窗口与标签，两个标签的内容仍是原来那两个组件（embedded 模式），
 * 表单与匹配逻辑没有任何第二份实现。
 */
export function SmartAnalysisDialog(props: Props) {
  const { tab, onTabChange, onClose } = props;
  // 案例录入的草稿由外部注入：从对话「整理成案例」过来时必须先落在录入页。
  const [announced, setAnnounced] = useState<SmartAnalysisTab>(tab);
  useEffect(() => { setAnnounced(tab); }, [tab]);

  const active = TABS.find((item) => item.id === announced) || TABS[0];

  return (
    <div className="knowledge-dialog-backdrop" onMouseDown={onClose}>
      <section className="knowledge-analysis-dialog smart-analysis-dialog" role="dialog" aria-modal="true" aria-label="案例" onMouseDown={(event) => event.stopPropagation()}>
        <header className="knowledge-dialog-header">
          <div>
            <h2 id="smart-analysis-title"><Wand2 size={18} /> 案例</h2>
            <p>{active.hint}</p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭案例窗口"><X size={18} /></button>
        </header>

        <nav className="smart-analysis-tabs" role="tablist" aria-label="案例功能">
          {TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              id={`smart-analysis-tab-${item.id}`}
              aria-selected={item.id === announced}
              aria-controls={`smart-analysis-panel-${item.id}`}
              className={item.id === announced ? 'active' : ''}
              onClick={() => onTabChange(item.id)}
            >
              {item.id === 'cases' ? <BookOpenCheck size={15} /> : <Wand2 size={15} />}
              {item.label}
            </button>
          ))}
        </nav>

        <div
          className="smart-analysis-panel"
          role="tabpanel"
          id={`smart-analysis-panel-${announced}`}
          aria-labelledby={`smart-analysis-tab-${announced}`}
        >
          {announced === 'cases' ? (
            <AbnormalCaseEditorDialog
              embedded
              caseItem={undefined}
              initialDraft={props.initialDraft}
              presetEvidences={props.presetEvidences}
              entries={props.entries}
              defaultSelectedEntryId={props.defaultSelectedEntryId}
              environmentId={props.environmentId}
              environmentName={props.environmentName}
              sourceOperationId={props.sourceOperationId}
              sourceTaskName={props.sourceTaskName}
              querySnapshot={props.querySnapshot}
              errorRules={props.errorRules}
              onClose={onClose}
              onSaved={props.onSaved}
            />
          ) : (
            <AbnormalCaseAnalysisDialog
              embedded
              entries={props.analysisEntries}
              selectedModules={props.selectedModules}
              environmentId={props.environmentId}
              errorRules={props.errorRules}
              onClose={onClose}
              onLocateEntry={props.onLocateEntry}
              aiPanel={props.aiPanel}
            />
          )}
        </div>
      </section>
    </div>
  );
}
