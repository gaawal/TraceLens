import type { LogEntry } from '../types';
import type {
  AbnormalCase,
  AbnormalCaseAnomalyRule,
  AbnormalCaseEvidence,
  AbnormalCaseFeatureGroup,
} from '../api/resourceApi';
import { matchingErrorRules, type ErrorMatchRule } from '../parser/logParser';

export interface ExtractedErrorCode {
  key: string;
  value: string;
  raw: string;
}

export interface AbnormalCaseEvidenceMatch {
  evidence: AbnormalCaseEvidence;
  observable: boolean;
  score: number;
  matchedEntry?: LogEntry;
  matchedFeatures: Array<{ label: string; score: number; maxScore: number; detail: string }>;
  missingFeatures: Array<{ label: string; maxScore: number; detail: string }>;
  neutralFeatures: Array<{ label: string; detail: string }>;
}

export interface AbnormalCaseMatchResult {
  case: AbnormalCase;
  similarity: number;
  coverage: number;
  confidence: 'high' | 'suspected' | 'related' | 'local' | 'none';
  evidenceMatches: AbnormalCaseEvidenceMatch[];
  observedModules: string[];
  observableEvidenceCount: number;
  abnormalCandidateCount: number;
  matchedFeatureGroupId?: string;
  matchedFeatureGroupTitle?: string;
}

export interface AbnormalCaseDuplicateCandidate {
  case: AbnormalCase;
  similarity: number;
  coverage: number;
  matchedFeatureGroupId: string;
  matchedFeatureGroupTitle: string;
}

// 只把“显式错误码字段 + 十六进制值”视为错误码。普通地址、句柄、指针等 0x... 不进入错误码强特征。
const ERROR_CODE_REGEX = /\b(error\s*code|error[_-]?code|err\s*code|err[_-]?code|errno|error\s*no|err\s*no)\b\s*(?:=|:|：|\[|\(|\{|<|is\s+)?\s*["']?(0x[0-9a-fA-F]+)\b/gi;
const UUID_REGEX = /\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b/g;
const IPV4_REGEX = /\b(?:\d{1,3}\.){3}\d{1,3}\b/g;
const TRACEISH_REGEX = /\b(trace(?:id)?|span(?:id)?|father(?:span)?(?:id)?|request(?:id)?|req(?:id)?|session(?:id)?|task(?:id)?|transaction(?:id)?)\s*[=:：]\s*[A-Za-z0-9_.:-]+/gi;
const KEYED_NUMBER_REGEX = /\b(pid|tid|thread(?:id)?|process(?:id)?|request(?:id)?|req(?:id)?|sequence|seq|index|idx)\s*[=:：]\s*[-+]?\d+\b/gi;
const LONG_HEX_REGEX = /\b0x[0-9a-fA-F]{4,}\b/g;
const FLOAT_REGEX = /(?<![\w.])[-+]?(?:\d+\.\d+|\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?(?![\w.])/g;
const INTEGER_REGEX = /(?<![\w.])[-+]?\d+(?![\w.])/g;
const PLACEHOLDER_TOKEN_REGEX = /^<(?:uuid|id|n|num|hex|ip|error_code)>$/i;
const TOKEN_SOURCE_REGEX = /<[^>]+>|[\p{L}\p{N}_:.+\-/]+/gu;
const GENERIC_ERROR_TOKENS = new Set([
  'error', 'errors', 'failed', 'fail', 'failure', 'exception', 'fatal', 'warn', 'warning',
]);
const ERROR_CODE_KEY_TOKENS = new Set(['errorcode', 'errcode', 'errno', 'errorno', 'errno']);
const DYNAMIC_KEY_TOKENS = new Set(['requestid', 'reqid', 'traceid', 'spanid', 'sessionid', 'taskid', 'transactionid', 'pid', 'tid', 'threadid', 'processid']);
const TOKEN_STOP_WORDS = new Set([
  'a', 'an', 'the', 'to', 'of', 'for', 'from', 'with', 'and', 'or', 'is', 'was', 'were', 'be', 'been',
  'on', 'at', 'in', 'by', 'as', 'this', 'that', 'it', 'true', 'false',
]);

function normalizeModule(value: string | undefined): string {
  return String(value || '').trim().toLocaleLowerCase();
}

function normalizeRuleKeyword(value: string | undefined): string {
  return String(value || '').trim().replace(/\s+/g, ' ').toLocaleLowerCase();
}

export function normalizeHexErrorCode(raw: string): string {
  const text = String(raw || '').trim().toLocaleLowerCase();
  if (!/^0x[0-9a-f]+$/.test(text)) return text;
  try {
    return `0x${BigInt(text).toString(16)}`;
  } catch {
    const body = text.slice(2).replace(/^0+/, '') || '0';
    return `0x${body}`;
  }
}

export function extractExplicitErrorCodes(text: string): ExtractedErrorCode[] {
  const source = String(text || '');
  const results: ExtractedErrorCode[] = [];
  const seen = new Set<string>();
  ERROR_CODE_REGEX.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = ERROR_CODE_REGEX.exec(source)) !== null) {
    const key = match[1].replace(/[\s_-]+/g, '').toLocaleLowerCase();
    const value = normalizeHexErrorCode(match[2]);
    const signature = `${key}:${value}`;
    if (seen.has(signature)) continue;
    seen.add(signature);
    results.push({ key, value, raw: match[0] });
  }
  return results;
}

function replaceErrorCodeValues(text: string): string {
  ERROR_CODE_REGEX.lastIndex = 0;
  return text.replace(ERROR_CODE_REGEX, (_full, key) => `${String(key).replace(/[\s_-]+/g, '')}=<ERROR_CODE>`);
}

/**
 * 把日志里的动态值泛化为占位符，保留稳定的技术语义。
 * ErrorCode 的具体值单独保存在 error_codes 中，正文只保留“这里存在错误码字段”。
 */
export function normalizeAbnormalMessage(message: string): string {
  let text = String(message || '').trim();
  text = replaceErrorCodeValues(text);
  text = text.replace(UUID_REGEX, '<UUID>');
  text = text.replace(IPV4_REGEX, '<IP>');
  text = text.replace(TRACEISH_REGEX, (_full, key: string) => `${String(key).toLocaleLowerCase()}=<ID>`);
  text = text.replace(KEYED_NUMBER_REGEX, (_full, key: string) => `${String(key).toLocaleLowerCase()}=<N>`);
  // 非显式 ErrorCode 的十六进制多为地址、句柄、动态 ID；统一泛化。
  text = text.replace(LONG_HEX_REGEX, '<HEX>');
  text = text.replace(FLOAT_REGEX, '<NUM>');
  text = text.replace(INTEGER_REGEX, '<N>');
  return text.replace(/\s+/g, ' ').trim();
}

function camelParts(value: string): string[] {
  const split = value
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .split(/[_.:/\\+\-]+|\s+/)
    .map((item) => item.trim().toLocaleLowerCase())
    .filter(Boolean);
  return split;
}

function pushToken(target: string[], seen: Set<string>, raw: string): void {
  const token = raw.trim().toLocaleLowerCase();
  if (!token || token.length < 2 || PLACEHOLDER_TOKEN_REGEX.test(token) || TOKEN_STOP_WORDS.has(token) || /^[-+]?\d+(?:\.\d+)?$/.test(token)) return;
  if (seen.has(token)) return;
  seen.add(token);
  target.push(token);
}

/**
 * 面向日志的技术分词：保留完整技术标识符，同时拆 camelCase/snake_case/命名空间。
 * 中文连续文本保留原词，并补充二元片段，使少量文字变化仍能产生重叠证据。
 */
export function fingerprintTokens(template: string): string[] {
  const tokens: string[] = [];
  const seen = new Set<string>();
  const sourceTokens = String(template || '').match(TOKEN_SOURCE_REGEX) || [];

  sourceTokens.forEach((sourceToken) => {
    if (PLACEHOLDER_TOKEN_REGEX.test(sourceToken)) return;
    const compactKey = sourceToken.replace(/[\s_-]+/g, '').toLocaleLowerCase();
    // ErrorCode 的字段名和值由独立强特征处理，不重复进入 Token 主评分；动态追踪字段名也不作为故障语义。
    if (ERROR_CODE_KEY_TOKENS.has(compactKey) || DYNAMIC_KEY_TOKENS.has(compactKey)) return;
    pushToken(tokens, seen, sourceToken);

    camelParts(sourceToken).forEach((part) => pushToken(tokens, seen, part));

    if (/^[\p{Script=Han}]+$/u.test(sourceToken)) {
      const chars = Array.from(sourceToken);
      if (chars.length >= 4) {
        for (let index = 0; index < chars.length - 1; index += 1) {
          pushToken(tokens, seen, `${chars[index]}${chars[index + 1]}`);
        }
      }
    }
  });

  return tokens;
}

function tokenWeight(token: string): number {
  if (GENERIC_ERROR_TOKENS.has(token)) return 0.35;
  if (token === 'errorcode' || token === 'errno' || token === 'errcode') return 0.55;
  if (/^[\p{Script=Han}]{2}$/u.test(token)) return 0.65;
  if (/[_:./\\-]/.test(token) || token.length >= 12) return 1.3;
  if (token.length >= 7) return 1.15;
  return 1;
}

function weightedTokenJaccard(left: readonly string[], right: readonly string[]): number {
  if (!left.length || !right.length) return 0;
  const a = new Set(left);
  const b = new Set(right);
  const union = new Set([...a, ...b]);
  let intersectionWeight = 0;
  let unionWeight = 0;
  union.forEach((token) => {
    const weight = tokenWeight(token);
    unionWeight += weight;
    if (a.has(token) && b.has(token)) intersectionWeight += weight;
  });
  return unionWeight ? intersectionWeight / unionWeight : 0;
}

function lcsRatio(left: readonly string[], right: readonly string[]): number {
  if (!left.length || !right.length) return 0;
  const a = left.slice(0, 48);
  const b = right.slice(0, 48);
  const dp = new Array<number>(b.length + 1).fill(0);
  for (let i = 1; i <= a.length; i += 1) {
    let diagonal = 0;
    for (let j = 1; j <= b.length; j += 1) {
      const previous = dp[j];
      if (a[i - 1] === b[j - 1]) dp[j] = diagonal + 1;
      else dp[j] = Math.max(dp[j], dp[j - 1]);
      diagonal = previous;
    }
  }
  return dp[b.length] / Math.max(1, Math.min(a.length, b.length));
}

interface TokenSimilarity {
  score: number;
  overlap: string[];
  missing: string[];
  setScore: number;
  orderScore: number;
}

function tokenSimilarity(
  expectedTemplate: string,
  actualTemplate: string,
  expectedTokens?: readonly string[],
  actualTokens?: readonly string[],
): TokenSimilarity {
  const expected = expectedTokens?.length ? [...expectedTokens] : fingerprintTokens(expectedTemplate);
  const actual = actualTokens?.length ? [...actualTokens] : fingerprintTokens(actualTemplate);
  const actualSet = new Set(actual);
  const overlap = expected.filter((token) => actualSet.has(token)).sort((a, b) => tokenWeight(b) - tokenWeight(a));
  const missing = expected.filter((token) => !actualSet.has(token)).sort((a, b) => tokenWeight(b) - tokenWeight(a));
  const setScore = weightedTokenJaccard(expected, actual);
  const orderScore = lcsRatio(expected, actual);
  return {
    score: setScore * 0.8 + orderScore * 0.2,
    overlap,
    missing,
    setScore,
    orderScore,
  };
}

export function anomalyRulesForEntry(entry: LogEntry, errorRules: readonly ErrorMatchRule[]): AbnormalCaseAnomalyRule[] {
  const text = `${entry.level} ${entry.message}`;
  return matchingErrorRules(text, errorRules).map((rule) => ({
    id: rule.id,
    keyword: rule.keyword,
    case_sensitive: rule.caseSensitive,
    whole_word: rule.wholeWord,
  }));
}

export function isAbnormalRuleMatchedEntry(entry: LogEntry, errorRules: readonly ErrorMatchRule[]): boolean {
  return anomalyRulesForEntry(entry, errorRules).length > 0;
}

export function createAbnormalEvidence(entry: LogEntry, errorRules: readonly ErrorMatchRule[]): AbnormalCaseEvidence {
  const anomalyRules = anomalyRulesForEntry(entry, errorRules);
  const template = normalizeAbnormalMessage(entry.message || entry.raw);
  return {
    source_entry_id: entry.id,
    timestamp: entry.timestamp,
    raw: entry.raw,
    message: entry.message,
    level: entry.level,
    severity: entry.severity,
    subsystem: entry.logSubsystem || '',
    module: entry.logModule || entry.component || '',
    component: entry.component || '',
    function_name: entry.functionName || entry.boundaryFunctionName || '',
    source_file: entry.source.fileName || entry.sourceFile || '',
    source_line: entry.source.lineNumber || entry.lineNumber || undefined,
    source_category: entry.logCategory || '',
    process_id: entry.processId || '',
    thread_id: entry.threadId || '',
    trace_id: entry.rpc?.traceId || '',
    anomaly_rules: anomalyRules,
    template,
    tokens: fingerprintTokens(template),
    error_codes: extractExplicitErrorCodes(`${entry.level} ${entry.message} ${entry.raw}`),
  };
}

function errorCodeScore(expected: readonly ExtractedErrorCode[], actual: readonly ExtractedErrorCode[]): { score: number; detail: string } {
  const actualValues = new Set(actual.map((item) => item.value));
  const matched = expected.filter((item) => actualValues.has(item.value));
  return {
    score: expected.length ? matched.length / expected.length : 0,
    detail: matched.length
      ? `共同错误码 ${matched.map((item) => item.value).join(', ')}`
      : `案例 ${expected.map((item) => item.value).join(', ')} / 当前 ${actual.map((item) => item.value).join(', ')}`,
  };
}

function anomalyRuleScore(expected: readonly AbnormalCaseAnomalyRule[], actual: readonly AbnormalCaseAnomalyRule[]): { score: number; detail: string } {
  if (!expected.length || !actual.length) return { score: 0, detail: '没有可比较的异常规则关键字' };
  const actualKeywords = new Set(actual.map((item) => normalizeRuleKeyword(item.keyword)).filter(Boolean));
  const matched = expected.filter((item) => actualKeywords.has(normalizeRuleKeyword(item.keyword)));
  return {
    score: matched.length / expected.length,
    detail: matched.length
      ? `共同命中：${matched.map((item) => item.keyword).join(', ')}`
      : `案例：${expected.map((item) => item.keyword).join(', ')}；当前：${actual.map((item) => item.keyword).join(', ')}`,
  };
}

interface CurrentAbnormalCandidate {
  entry: LogEntry;
  evidence: AbnormalCaseEvidence;
}

function scoreEvidence(expected: AbnormalCaseEvidence, candidate: CurrentAbnormalCandidate): Omit<AbnormalCaseEvidenceMatch, 'evidence' | 'observable'> {
  const actual = candidate.evidence;
  const matchedFeatures: AbnormalCaseEvidenceMatch['matchedFeatures'] = [];
  const missingFeatures: AbnormalCaseEvidenceMatch['missingFeatures'] = [];
  const neutralFeatures: AbnormalCaseEvidenceMatch['neutralFeatures'] = [];
  let weightedScore = 0;
  let maxScore = 0;

  // 1) Token 指纹是主评分。异常规则只负责先筛出候选异常语句。
  const expectedTokens = expected.tokens?.length ? expected.tokens : fingerprintTokens(expected.template || '');
  const actualTokens = actual.tokens?.length ? actual.tokens : fingerprintTokens(actual.template || '');
  const lexical = tokenSimilarity(expected.template || '', actual.template || '', expectedTokens, actualTokens);
  if (expectedTokens.length && actualTokens.length) {
    const weight = 70;
    maxScore += weight;
    const gained = weight * lexical.score;
    weightedScore += gained;
    const overlapText = lexical.overlap.slice(0, 8).join(', ') || '无稳定词重叠';
    const detail = `稳定词重叠 ${(lexical.setScore * 100).toFixed(0)}%，顺序 ${(lexical.orderScore * 100).toFixed(0)}%；命中 ${overlapText}`;
    if (lexical.score >= 0.45) matchedFeatures.push({ label: 'Token 指纹', score: gained, maxScore: weight, detail });
    else missingFeatures.push({ label: 'Token 指纹', maxScore: weight - gained, detail: `${detail}${lexical.missing.length ? `；缺少 ${lexical.missing.slice(0, 6).join(', ')}` : ''}` });
  }

  // 2) 模块属于稳定结构证据，但不推断上下游关系。
  const expectedModule = normalizeModule(expected.module || expected.component || '');
  const actualModule = normalizeModule(actual.module || actual.component || '');
  if (expectedModule && actualModule) {
    const weight = 15;
    maxScore += weight;
    if (expectedModule === actualModule) {
      weightedScore += weight;
      matchedFeatures.push({ label: '模块', score: weight, maxScore: weight, detail: expected.module || expected.component || '' });
    } else {
      missingFeatures.push({ label: '模块', maxScore: weight, detail: `案例 ${expected.module || expected.component || '—'} / 当前 ${actual.module || actual.component || '—'}` });
    }
  } else if (expectedModule) {
    neutralFeatures.push({ label: '模块', detail: '当前日志未解析出模块，不参与评分' });
  }

  // 3) 函数只在双方都有结构化函数名时比较，缺失不扣分。
  const expectedFunction = String(expected.function_name || '').trim();
  const actualFunction = String(actual.function_name || '').trim();
  if (expectedFunction && actualFunction) {
    const weight = 10;
    maxScore += weight;
    if (expectedFunction.toLocaleLowerCase() === actualFunction.toLocaleLowerCase()) {
      weightedScore += weight;
      matchedFeatures.push({ label: '函数', score: weight, maxScore: weight, detail: expectedFunction });
    } else {
      missingFeatures.push({ label: '函数', maxScore: weight, detail: `案例 ${expectedFunction} / 当前 ${actualFunction}` });
    }
  } else if (expectedFunction) {
    neutralFeatures.push({ label: '函数', detail: '当前语句没有函数信息，不参与评分' });
  }

  // 4) 异常规则关键字是“为什么这条日志进入异常候选”的举证，只占很小权重。
  const expectedRules = expected.anomaly_rules || [];
  const actualRules = actual.anomaly_rules || [];
  if (expectedRules.length && actualRules.length) {
    const weight = 5;
    maxScore += weight;
    const rule = anomalyRuleScore(expectedRules, actualRules);
    const gained = weight * rule.score;
    weightedScore += gained;
    if (rule.score > 0) matchedFeatures.push({ label: '异常规则', score: gained, maxScore: weight, detail: rule.detail });
    else missingFeatures.push({ label: '异常规则', maxScore: weight, detail: rule.detail });
  }

  // 5) ErrorCode 不是必要条件。双方都有显式十六进制错误码时才参与评分；一方没有时保持中立。
  const expectedCodes = (expected.error_codes || []) as ExtractedErrorCode[];
  const actualCodes = (actual.error_codes || []) as ExtractedErrorCode[];
  if (expectedCodes.length && actualCodes.length) {
    const weight = 15;
    maxScore += weight;
    const code = errorCodeScore(expectedCodes, actualCodes);
    const gained = weight * code.score;
    weightedScore += gained;
    if (code.score > 0) matchedFeatures.push({ label: '错误码', score: gained, maxScore: weight, detail: code.detail });
    else missingFeatures.push({ label: '错误码', maxScore: weight, detail: code.detail });
  } else if (expectedCodes.length && !actualCodes.length) {
    neutralFeatures.push({ label: '错误码', detail: `案例包含 ${expectedCodes.map((item) => item.value).join(', ')}，当前语句没有显式错误码，因此不扣分` });
  } else if (!expectedCodes.length && actualCodes.length) {
    neutralFeatures.push({ label: '错误码', detail: `当前语句出现 ${actualCodes.map((item) => item.value).join(', ')}，案例未固化错误码，因此不参与评分` });
  }

  return {
    score: maxScore ? Math.max(0, Math.min(100, weightedScore / maxScore * 100)) : 0,
    matchedEntry: candidate.entry,
    matchedFeatures,
    missingFeatures,
    neutralFeatures,
  };
}

function evidenceModule(evidence: AbnormalCaseEvidence): string {
  return normalizeModule(evidence.module || evidence.component || '');
}

function queryScopeModules(entries: readonly LogEntry[], selectedModules?: readonly string[]): Set<string> {
  const modules = new Set((selectedModules || []).map(normalizeModule).filter(Boolean));
  if (!modules.size) {
    entries.forEach((entry) => {
      const value = normalizeModule(entry.logModule || entry.component);
      if (value) modules.add(value);
    });
  }
  return modules;
}

export function effectiveAbnormalCaseFeatureGroups(caseItem: AbnormalCase): AbnormalCaseFeatureGroup[] {
  const groups = Array.isArray(caseItem.feature_groups) ? caseItem.feature_groups.filter((item) => item && Array.isArray(item.evidences) && item.evidences.length > 0) : [];
  if (groups.length) return groups;
  return [{
    id: `legacy-${caseItem.id}`,
    title: '历史现场',
    enabled: true,
    created_at: caseItem.created_at || '',
    source_operation_id: caseItem.source_operation_id || '',
    source_task_name: caseItem.source_task_name || '',
    environment_name: caseItem.environment_name || '',
    note: '',
    evidences: caseItem.evidences || [],
  }];
}

export function activeAbnormalCaseFeatureGroups(caseItem: AbnormalCase): AbnormalCaseFeatureGroup[] {
  return effectiveAbnormalCaseFeatureGroups(caseItem).filter((item) => item.enabled !== false);
}

export function activeAbnormalCaseEvidences(caseItem: AbnormalCase): AbnormalCaseEvidence[] {
  return activeAbnormalCaseFeatureGroups(caseItem).flatMap((item) => item.evidences || []);
}

function confidenceFor(similarity: number, coverage: number): AbnormalCaseMatchResult['confidence'] {
  if (similarity >= 90 && coverage >= 70) return 'high';
  if (similarity >= 90) return 'local';
  if (similarity >= 75) return 'suspected';
  if (similarity >= 60) return 'related';
  return 'none';
}

function matchFeatureGroupPrepared(
  caseItem: AbnormalCase,
  group: AbnormalCaseFeatureGroup,
  abnormalCandidates: readonly CurrentAbnormalCandidate[],
  scope: Set<string>,
): AbnormalCaseMatchResult {
  const evidences = group.evidences || [];
  const observable = evidences.map((evidence) => {
    const module = evidenceModule(evidence);
    return !module || !scope.size || scope.has(module);
  });
  const observableCount = observable.filter(Boolean).length;
  const coverage = evidences.length ? observableCount / evidences.length * 100 : 0;
  const usedEntries = new Set<string>();

  const matches: AbnormalCaseEvidenceMatch[] = evidences.map((evidence, index) => {
    if (!observable[index]) {
      return { evidence, observable: false, score: 0, matchedFeatures: [], missingFeatures: [], neutralFeatures: [] };
    }

    const expectedModule = evidenceModule(evidence);
    const candidates = abnormalCandidates.filter(({ entry }) => {
      if (usedEntries.has(entry.id)) return false;
      if (!expectedModule) return true;
      return normalizeModule(entry.logModule || entry.component) === expectedModule;
    });

    if (!candidates.length) {
      return {
        evidence,
        observable: true,
        score: 0,
        matchedFeatures: [],
        missingFeatures: [{ label: '异常候选', maxScore: 100, detail: expectedModule ? '当前已查询该模块，但没有命中启用异常规则的可比较语句' : '当前查询没有命中启用异常规则的可比较语句' }],
        neutralFeatures: [],
      };
    }

    let best: ReturnType<typeof scoreEvidence> | undefined;
    for (const candidate of candidates) {
      const current = scoreEvidence(evidence, candidate);
      if (!best || current.score > best.score) best = current;
    }
    if (!best) return { evidence, observable: true, score: 0, matchedFeatures: [], missingFeatures: [], neutralFeatures: [] };
    if (best.matchedEntry && best.score >= 45) usedEntries.add(best.matchedEntry.id);
    return { evidence, observable: true, ...best };
  });

  const scored = matches.filter((item) => item.observable);
  const similarity = scored.length ? scored.reduce((sum, item) => sum + item.score, 0) / scored.length : 0;
  return {
    case: caseItem,
    similarity: Math.round(similarity * 10) / 10,
    coverage: Math.round(coverage * 10) / 10,
    confidence: confidenceFor(similarity, coverage),
    evidenceMatches: matches,
    observedModules: Array.from(scope),
    observableEvidenceCount: observableCount,
    abnormalCandidateCount: abnormalCandidates.length,
    matchedFeatureGroupId: group.id,
    matchedFeatureGroupTitle: group.title,
  };
}

function matchAbnormalCasePrepared(
  caseItem: AbnormalCase,
  abnormalCandidates: readonly CurrentAbnormalCandidate[],
  scope: Set<string>,
): AbnormalCaseMatchResult {
  const groups = activeAbnormalCaseFeatureGroups(caseItem);
  const results = groups.map((group) => matchFeatureGroupPrepared(caseItem, group, abnormalCandidates, scope));
  if (!results.length) {
    return { case: caseItem, similarity: 0, coverage: 0, confidence: 'none', evidenceMatches: [], observedModules: Array.from(scope), observableEvidenceCount: 0, abnormalCandidateCount: abnormalCandidates.length };
  }
  return results.sort((left, right) => right.similarity - left.similarity || right.coverage - left.coverage)[0];
}

function scoreEvidencePair(expected: AbnormalCaseEvidence, actual: AbnormalCaseEvidence): number {
  const result = scoreEvidence(expected, { entry: undefined as unknown as LogEntry, evidence: actual });
  return result.score;
}

function duplicateScoreForGroup(group: AbnormalCaseFeatureGroup, candidates: readonly AbnormalCaseEvidence[]): { similarity: number; coverage: number } {
  const expected = group.evidences || [];
  if (!expected.length || !candidates.length) return { similarity: 0, coverage: 0 };

  // 重复治理关注“本次新现场是否已被历史案例覆盖”。因此从新现场出发，
  // 每条新证据寻找历史特征中的最佳匹配；历史案例比本次现场更完整时不会被额外证据稀释。
  const scores = candidates.map((actual) => {
    const actualModule = evidenceModule(actual);
    const comparable = expected.filter((item) => !actualModule || !evidenceModule(item) || evidenceModule(item) === actualModule);
    const pool = comparable.length ? comparable : expected;
    return pool.reduce((best, item) => Math.max(best, scoreEvidencePair(item, actual)), 0);
  });
  const similarity = scores.reduce((sum, item) => sum + item, 0) / Math.max(1, scores.length);

  const expectedModules = new Set(expected.map((item) => evidenceModule(item)).filter(Boolean));
  const candidateModules = new Set(candidates.map((item) => evidenceModule(item)).filter(Boolean));
  const coveredModules = [...expectedModules].filter((module) => candidateModules.has(module)).length;
  const coverage = expectedModules.size ? coveredModules / expectedModules.size * 100 : 100;
  return {
    similarity: Math.round(similarity * 10) / 10,
    coverage: Math.round(coverage * 10) / 10,
  };
}


export function rankDuplicateAbnormalCases(cases: readonly AbnormalCase[], candidateEvidences: readonly AbnormalCaseEvidence[]): AbnormalCaseDuplicateCandidate[] {
  if (!candidateEvidences.length) return [];
  return cases.filter((item) => item.enabled).flatMap((caseItem) => {
    const groups = activeAbnormalCaseFeatureGroups(caseItem);
    if (!groups.length) return [];
    const best = groups.map((group) => ({ group, ...duplicateScoreForGroup(group, candidateEvidences) }))
      .sort((left, right) => right.similarity - left.similarity || right.coverage - left.coverage)[0];
    if (!best || best.similarity < 75) return [];
    return [{
      case: caseItem,
      similarity: best.similarity,
      coverage: best.coverage,
      matchedFeatureGroupId: best.group.id,
      matchedFeatureGroupTitle: best.group.title,
    }];
  }).sort((left, right) => right.similarity - left.similarity || right.coverage - left.coverage);
}

function prepareAbnormalCandidates(
  entries: readonly LogEntry[],
  errorRules: readonly ErrorMatchRule[],
): CurrentAbnormalCandidate[] {
  return entries.flatMap((entry) => {
    const evidence = createAbnormalEvidence(entry, errorRules);
    return evidence.anomaly_rules.length ? [{ entry, evidence }] : [];
  });
}

export function matchAbnormalCase(
  caseItem: AbnormalCase,
  currentEntries: readonly LogEntry[],
  errorRules: readonly ErrorMatchRule[],
  selectedModules?: readonly string[],
): AbnormalCaseMatchResult {
  const abnormalCandidates = prepareAbnormalCandidates(currentEntries, errorRules);
  const scope = queryScopeModules(currentEntries, selectedModules);
  return matchAbnormalCasePrepared(caseItem, abnormalCandidates, scope);
}

export function rankAbnormalCases(
  cases: readonly AbnormalCase[],
  entries: readonly LogEntry[],
  errorRules: readonly ErrorMatchRule[],
  selectedModules?: readonly string[],
): AbnormalCaseMatchResult[] {
  // 当前日志的异常候选只预处理一次，避免“案例数 × 日志数”重复做异常规则匹配和 Token 化。
  const abnormalCandidates = prepareAbnormalCandidates(entries, errorRules);
  if (!abnormalCandidates.length) return [];
  const scope = queryScopeModules(entries, selectedModules);

  return cases
    .filter((item) => item.enabled)
    .map((item) => matchAbnormalCasePrepared(item, abnormalCandidates, scope))
    .filter((item) => item.observableEvidenceCount > 0 && item.similarity >= 35)
    .sort((left, right) => {
      if (right.similarity !== left.similarity) return right.similarity - left.similarity;
      if (right.coverage !== left.coverage) return right.coverage - left.coverage;
      return right.case.matched_count - left.case.matched_count;
    });
}
