import { useState } from 'react';
import { AlertTriangle, Check, LoaderCircle, Sparkles } from 'lucide-react';

export interface AiAutoconfigOutcome {
  /** 一句话说清 AI 填了什么。 */
  message: string;
  /** 模型没把握、或后端按样例校验后丢弃的东西。 */
  warnings?: string[];
  confidence?: string;
}

interface Props {
  /**
   * 执行一次识别并**就地填好**配置。返回填了什么、丢了什么。
   * 抛错表示识别失败（样例不足、模型不可用等），按钮会把原话显示出来。
   */
  onRun: () => Promise<AiAutoconfigOutcome>;
  disabled?: boolean;
  /** 按钮禁用时说明为什么（例如还没粘贴样例），避免用户以为按钮坏了。 */
  disabledHint?: string;
  label?: string;
  title?: string;
  /** 紧凑版：塞进字段行 / 配置框标题里。 */
  compact?: boolean;
  className?: string;
}

const CONFIDENCE_LABELS: Record<string, string> = { high: '高', medium: '中', low: '低' };

/**
 * 「AI 一键自动配置」按钮 —— 新增数据提取器 / 新增语义规则 / 单位换算等配置框共用。
 *
 * 刻意做成一个共用组件而不是各处各写一遍：识别结果必须始终以同一种方式回显
 * （填了什么 + 哪些被丢弃 + 置信度），否则「AI 给的东西能不能信」就只能靠用户猜。
 */
export function AiAutoconfigButton({
  onRun,
  disabled,
  disabledHint,
  label = 'AI 一键自动配置',
  title = '根据上面的真实日志样例识别字段/参数并自动填好这份配置',
  compact,
  className,
}: Props) {
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<AiAutoconfigOutcome>();
  const [error, setError] = useState('');

  async function run() {
    setBusy(true);
    setError('');
    setOutcome(undefined);
    try {
      setOutcome(await onRun());
    } catch (exc) {
      const name = exc instanceof Error ? exc.name : '';
      setError(
        name === 'TimeoutError'
          ? 'AI 识别超时（超过 120 秒）；可以只保留一条最有代表性的样例后重试。'
          : exc instanceof Error
            ? exc.message
            : String(exc),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`ai-autoconfig ${compact ? 'is-compact' : ''} ${className || ''}`}>
      <button
        type="button"
        className={`button secondary semantic-auto-button ai-autoconfig-button ${compact ? 'compact-button' : 'compact'}`}
        onClick={() => void run()}
        disabled={disabled || busy}
        title={disabled ? disabledHint || title : title}
      >
        {busy ? <LoaderCircle className="spin" size={compact ? 13 : 14} /> : <Sparkles size={compact ? 13 : 14} />}
        {busy ? 'AI 识别中…' : label}
      </button>
      {disabled && disabledHint && <small className="ai-autoconfig-hint">{disabledHint}</small>}
      {error && <div className="ai-autoconfig-result error"><AlertTriangle size={13} /><span>{error}</span></div>}
      {outcome && (
        <div className="ai-autoconfig-result success">
          <Check size={13} />
          <div>
            <span>
              {outcome.message}
              {outcome.confidence && <em className={`ai-autoconfig-confidence is-${outcome.confidence}`}>置信度 {CONFIDENCE_LABELS[outcome.confidence] || outcome.confidence}</em>}
            </span>
            {outcome.warnings && outcome.warnings.length > 0 && (
              <ul>
                {outcome.warnings.slice(0, 6).map((item, index) => <li key={`${item}-${index}`}><AlertTriangle size={11} />{item}</li>)}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
