import type { LogFormatParserRuleConfig } from '../types';
// Default to same-origin /api. In development Vite proxies it to Django, so LAN
// clients only need to reach the frontend port and never call :8000 directly.
// VITE_TRACELENS_API_BASE remains available for deployments with an explicit API URL.
const configuredApiBase = String(import.meta.env.VITE_TRACELENS_API_BASE || '').trim();
const browserHost = typeof window !== 'undefined' ? window.location.hostname.toLowerCase() : '';
const browserIsLoopback = browserHost === 'localhost' || browserHost === '127.0.0.1' || browserHost === '::1';
const configuredPointsToLoopback = /^https?:\/\/(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:\/|$)/i.test(configuredApiBase);
// A stale localhost API URL works on the developer PC but is invalid in a colleague's browser.
const effectiveApiBase = configuredApiBase && !(!browserIsLoopback && configuredPointsToLoopback)
  ? configuredApiBase
  : '/api';
export const API_BASE = effectiveApiBase.replace(/\/$/, '');

export interface MachineSummary {
  id: number;
  name: string;
  host: string;
  ssh_port: number;
  username: string;
  role: 'upper' | 'lower';
  role_label: string;
  auth_type?: 'none' | 'password' | 'private_key';
  has_credential?: boolean;
  station_id?: string;
  station_type?: string;
  station_name?: string;
  connection_status?: 'unknown' | 'online' | 'offline';
  last_connection_checked_at?: string;
  software_version?: string;
  version_checked_at?: string;
  environment_id?: number;
}

export interface EnvironmentSummary {
  id: number;
  name: string;
  folder?: number | null;
  folder_name?: string | null;
  upper_machine: MachineSummary;
  lower_machines: Array<MachineSummary & { station?: Record<string, unknown> }>;
  dhh_machine?: (MachineSummary & { station?: Record<string, unknown> }) | null;
  is_dhh_environment?: boolean;
  status: string;
  status_label: string;
  station_user_id: string;
  software_version: string;
  version_checked_at?: string;
  version_mismatch?: boolean;
  version_mismatch_hosts?: string[];
  last_discovered_at?: string;
  description: string;
}

export type DeploymentStatus = 'scheduled' | 'pending' | 'running' | 'stopping' | 'stopped' | 'success' | 'failed';
export type DeploymentStepStatus = 'pending' | 'running' | 'stopped' | 'success' | 'failed' | 'skipped';

export interface EnvironmentDeploymentStep {
  id: number;
  key: 'stop' | 'deploy' | 'tb_deploy' | 'install' | 'start' | string;
  name: string;
  sort_order: number;
  status: DeploymentStepStatus;
  status_label: string;
  command: string;
  success_marker: string;
  stdout: string;
  stderr: string;
  process_log?: string;
  logs_included?: boolean;
  parameters?: Record<string, unknown>;
  exit_status?: number | null;
  retry_count: number;
  message: string;
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface EnvironmentDeployment {
  id: number;
  environment: number;
  task_name: string;
  target_version: string;
  simulation_mode: 'sim0_sil' | 'sim2' | 'sim0_real';
  include_sdk: boolean;
  precheck_stop_lower?: boolean;
  upper_ip: string;
  gpb_ips: string[];
  tb_mode: string;
  install_mode: string;
  install_port: number;
  display_env?: string;
  include_dhh: boolean;
  dhh_ip: string;
  dhh_user: string;
  dhh_machine_id: string;
  configuration: EnvironmentDeploymentDraft;
  command_snapshot: Array<{ key: string; name: string; command: string; skipped?: boolean }>;
  status: DeploymentStatus;
  status_label: string;
  current_step: string;
  message: string;
  trigger_operator?: string;
  trigger_client_ip?: string;
  trigger_source?: string;
  scheduled_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  steps: EnvironmentDeploymentStep[];
  created_at: string;
  updated_at: string;
}

export interface EnvironmentDeploymentSummary {
  id: number;
  environment: number;
  task_name: string;
  target_version: string;
  simulation_mode: 'sim0_sil' | 'sim2' | 'sim0_real';
  include_sdk: boolean;
  upper_ip: string;
  gpb_ips: string[];
  tb_mode: string;
  install_mode: string;
  install_port: number;
  include_dhh: boolean;
  dhh_ip: string;
  dhh_user: string;
  dhh_machine_id: string;
  status: DeploymentStatus;
  status_label: string;
  current_step: string;
  message: string;
  trigger_operator?: string;
  trigger_client_ip?: string;
  trigger_source?: string;
  scheduled_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DeploymentSshTrustCheck {
  environment_id: number;
  upper_ip: string;
  gpb_ips: string[];
  include_dhh: boolean;
  dhh_ip: string;
  dhh_user: string;
  target_signature: string;
  all_trusted: boolean;
  checked_at: string;
  auto_trust_attempted?: boolean;
  auto_trust_succeeded?: boolean;
  results: Array<{
    host: string;
    user: string;
    role: 'gpb' | 'dhh' | string;
    trusted: boolean;
    forward_trusted?: boolean;
    reverse_trusted?: boolean | null;
    forward_message?: string;
    reverse_message?: string;
    message: string;
    copy_command: string;
    auto_trust_attempted?: boolean;
    auto_trust_succeeded?: boolean;
  }>;
}

export interface DeploymentTimeSyncCheck {
  environment_id: number;
  upper_ip: string;
  gpb_ips: string[];
  include_dhh: boolean;
  dhh_ip: string;
  dhh_user: string;
  target_signature: string;
  tolerance_seconds: number;
  all_synced: boolean;
  checked_at: string;
  results: Array<{
    host: string;
    user: string;
    role: 'gpb' | 'dhh' | string;
    success: boolean;
    synchronized: boolean;
    before_delta_seconds?: number | null;
    after_delta_seconds?: number | null;
    message: string;
  }>;
}

export interface EnvironmentDeploymentDraft {
  precheck_stop_lower?: boolean;
  task_name: string;
  target_version: string;
  recent_versions?: string[];
  simulation_mode: 'sim0_sil' | 'sim2' | 'sim0_real';
  include_sdk: boolean;
  upper_ip: string;
  available_gpb_ips?: string[];
  available_gpb_modes?: string[];
  available_gpb_slots?: Array<{ slot: number; ip: string; station_name?: string }>;
  real_slot_ips?: string[];
  gpb_ips: string[];
  gpb_mode: string;
  required_gpb_count?: number;
  install_port: number;
  display_env?: string;
  post_start_script?: string;
  saved_post_start_scripts?: string[];
  save_post_start_script?: boolean;
  // Legacy fields are optional so old deployment-history snapshots can still be reused.
  tb_mode?: string; // 单 GPB 时仅用于 tb_deploy.sh 的定制模式
  install_mode?: string;
  include_dhh?: boolean;
  dhh_ip?: string;
  dhh_user?: string;
  dhh_machine_id?: string;
  selected_steps?: string[]; // 复制重试部署时，仅执行选中的步骤
  commands?: Array<{ key: string; name: string; command: string; skipped?: boolean }>;
}

export interface EnvironmentFolder {
  id: number;
  name: string;
  parent?: number | null;
  sort_order: number;
  environment_count?: number;
}

export interface EnvironmentRuntimeStatus {
  environment_id: number;
  upper: { machine_id?: number; host?: string; online: boolean; message?: string; remote_epoch?: number | null; remote_date?: string };
  lowers: Array<{
    machine_id: number;
    host: string;
    username?: string;
    online: boolean;
    tb_simulator_running: boolean;
    skipped?: boolean;
    skip_reason?: 'small_network' | string;
    message?: string;
    remote_epoch?: number | null;
    remote_date?: string;
    time_sync_state?: 'synced' | 'out_of_sync' | 'unknown' | 'skipped' | string;
    time_sync_required?: boolean;
    time_delta_seconds?: number | null;
  }>;
  dhh?: { machine_id: number; host: string; username?: string; online: boolean; service_running: boolean; service_name?: string; process_command?: string; skipped?: boolean; skip_reason?: 'small_network' | string; message?: string } | null;
  checked_at?: string;
  cache_status?: 'hit' | 'miss' | 'refreshed' | string;
  message?: string;
}

export interface LowerTimeSyncResult {
  success: boolean;
  environment_id: number;
  upper_epoch?: number;
  upper_date?: string;
  results: Array<{
    machine_id: number;
    host?: string;
    success: boolean;
    message: string;
    before_epoch?: number;
    before_date?: string;
    after_epoch?: number;
    after_date?: string;
    time_sync_state?: string;
    time_sync_required?: boolean;
    time_delta_seconds?: number | null;
  }>;
  runtime_status: EnvironmentRuntimeStatus;
}

export interface LogPathProfile {
  id?: number;
  category: string;
  category_label: string;
  display_name: string;
  path_template: string;
  enabled: boolean;
  scope: 'upper_only' | 'lower_only' | 'each_machine' | 'upper_for_lower';
  scope_label?: string;
  match_rules: Array<'fm' | 'fm_timestamp' | 'archive' | 'executor_tree' | 'run_flat'>;
  sort_order: number;
}

export interface ResourceSettings {
  id: number;
  station_xml_path: string;
  version_file_path: string;
  source_code_path_template: string;
  source_code_public_paths: string;
  log_root_template?: string;
  dhh_debug_log_root: string;
  dhh_executor_log_root: string;
  dhh_run_log_root: string;
  lower_username: string;
  lower_ssh_port: number;
  lower_auth_type: 'none' | 'password' | 'private_key';
  has_lower_credential: boolean;
  display_rules?: unknown[] | null;
  display_rules_initialized?: boolean;
  data_extraction_rules?: unknown[] | null;
  log_paths: LogPathProfile[];
  path_parameters?: Array<{ token: string; meaning: string; example: string }>;
  lower_password?: string;
  lower_private_key?: string;
  lower_private_key_passphrase?: string;
}


export interface GlobalLogFm {
  id: number;
  subsystem: number;
  name: string;
  kind: 'normal' | 'executor';
  display_name: string;
  effective_name: string;
  enabled: boolean;
  sort_order: number;
  query_priority: number;
  description: string;
  target_module_ids: number[];
  target_module_names: string[];
  event_component: boolean;
  event_config_files: string[];
  event_display_codes: string[];
  event_code_count: number;
  last_event_discovered_at?: string | null;
  matched_count: number;
  last_matched_at?: string | null;
  last_discovered_at?: string;
  created_at?: string;
  updated_at?: string;
}

export interface GlobalLogSubsystem {
  id: number;
  name: string;
  display_name: string;
  effective_name: string;
  enabled: boolean;
  sort_order: number;
  description: string;
  last_discovered_at?: string;
  fm_count?: number;
  fms: GlobalLogFm[];
  created_at?: string;
  updated_at?: string;
}

export type LogQuerySkillWhen = 'always' | 'no_match' | 'source_empty' | 'source_not_found' | 'insufficient_evidence' | 'keyword_match';
export type LogQuerySkillSourceType = 'standard' | 'custom_path';
export type LogQuerySkillMachineScope = 'upper' | 'lower' | 'all_lower' | 'dhh';

export interface LogQuerySkillStep {
  name: string;
  when: LogQuerySkillWhen;
  source_type: LogQuerySkillSourceType;
  machine_scope: LogQuerySkillMachineScope;
  source_category: string;
  module: string;
  path_template: string;
  file_pattern: string;
  keywords: string[];
  time_before_seconds: number;
  time_after_seconds: number;
  note: string;
}

export interface LogQuerySkill {
  id: number;
  subsystem: number;
  subsystem_name: string;
  subsystem_display_name: string;
  name: string;
  enabled: boolean;
  priority: number;
  trigger_modules: string[];
  trigger_keywords: string[];
  description: string;
  steps: LogQuerySkillStep[];
  created_at?: string;
  updated_at?: string;
}

export interface SubsystemInfo {
  name: string;
  fms: string[];
  archive_count: number;
}

export interface EnvironmentLogSource {
  machine_id: number;
  machine_name: string;
  role: 'upper' | 'lower';
  source_category: string;
  source_name: string;
  root: string;
  status: 'success' | 'error';
  scan_status?: 'success' | 'error';
  cache_state?: 'cached' | 'refreshed' | 'stale' | 'error' | string;
  scanned_at?: string;
  message: string;
  subsystems: SubsystemInfo[];
}

export interface EnvironmentLogTree {
  environment_id: number;
  subsystems: string[];
  global_catalog: GlobalLogSubsystem[];
  cache?: { refresh_requested: boolean; hits: number; refreshed: number; stale: number; added_subsystems: number; added_fms: number };
  sources: EnvironmentLogSource[];
  machines: EnvironmentLogSource[];
}

export interface LogWindowRequest extends Record<string, unknown> {
  start_time: string;
  end_time: string;
  source_categories: string[];
  subsystems: string[];
  fms: string[];
  fm_targets?: Array<{ subsystem: string; fm: string; kind?: 'normal' | 'executor' }>;
  keyword?: string;
  /** 仅返回日志原文，不注入 TraceLens 内部来源标记。 */
  raw_text?: boolean;
}

export interface LogPlan {
  environment_id: number;
  count: number;
  artifacts: Array<{
    machine_id: number;
    machine_name: string;
    source_category: string;
    source_name: string;
    subsystem: string;
    fm: string;
    kind: 'current' | 'archived' | 'tar_member';
    path: string;
    member_name: string;
    boundary_time?: string;
    size: number;
  }>;
}

export interface LogSearchProgress {
  operation_id: string;
  stage: 'pending' | 'planning' | 'indexing' | 'reading' | 'cancelling' | 'cancelled' | 'complete' | 'error';
  percent: number;
  current_subsystem?: string;
  current_module_count?: number;
  current_host?: string;
  current_username?: string;
  current_source_category?: string;
  current_root?: string;
  current_directory?: string;
  current_action?: string;
  target_total?: number;
  target_current?: number;
  current_directory_candidates?: number;
  current_directory_selected?: number;
  current_subsystem_files?: number;
  current_subsystem_checked?: number;
  discovered_files?: number;
  checked_files?: number;
  selected_files?: number;
  artifact_total?: number;
  artifact_done?: number;
  artifact_current?: number;
  current_artifact?: string;
  current_file?: string;
  current_file_path?: string;
  current_module?: string;
  message?: string;
  done?: boolean;
  cancel_requested?: boolean;
  cancelled?: boolean;
  error?: string;
}


export interface LogAuditTarget {
  subsystem: string;
  module: string;
  kind: 'normal' | 'executor' | string;
}

export interface LogAuditDiagnostics {
  version?: number;
  query?: {
    start_time?: string;
    end_time?: string;
    source_categories?: string[];
    targets?: Array<{ subsystem?: string; module?: string; kind?: string }>;
    keyword?: string;
  };
  strategy?: {
    mode?: 'direct' | 'upper_then_dhh' | string;
    label?: string;
    upper?: { id?: number; name?: string; host?: string };
    dhh?: { id?: number; name?: string; host?: string };
  };
  cache?: { hit?: boolean; message?: string };
  plan?: {
    status?: string;
    selected_file_count?: number;
    role_counts?: Record<string, number>;
    dhh_decision?: string;
    message?: string;
  };
  time_filter?: {
    status?: string;
    requested_start?: string;
    requested_end?: string;
    selected_file_count?: number;
    matched_lines?: number;
    output_bytes?: number;
    client_result?: boolean;
  };
  conclusion?: { code?: string; message?: string };
}

export interface LogAuditRecord {
  id: number;
  operation_id: string;
  action: 'log_search' | string;
  operator_username: string;
  client_ip: string;
  environment?: number | null;
  environment_name: string;
  target_host: string;
  target_username: string;
  start_time: string;
  end_time: string;
  source_categories: string[];
  keyword: string;
  request_payload: LogWindowRequest & Record<string, unknown>;
  targets: LogAuditTarget[];
  result: 'running' | 'success' | 'no_result' | 'failed' | 'cancelled' | string;
  result_display: string;
  error_message: string;
  artifact_count: number;
  matched_files: Array<{
    machine_id?: number;
    machine?: string;
    machine_role?: 'upper' | 'lower' | 'dhh' | 'unknown' | string;
    subsystem?: string;
    module?: string;
    source_category?: string;
    path: string;
    member?: string;
    kind?: string;
    size?: number;
    indexed_start?: string;
    indexed_end?: string;
    boundary_time?: string;
  }>;
  diagnostics?: LogAuditDiagnostics;
  result_count: number;
  output_bytes: number;
  created_at: string;
  finished_at?: string | null;
  duration_ms?: number | null;
  data_extraction_count?: number;
}

export interface LogAuditList {
  count: number;
  next?: string | null;
  previous?: string | null;
  results: LogAuditRecord[];
  facets?: {
    environments: Array<{ environment_id?: number | null; environment_name: string; target_host: string; target_username: string }>;
    targets: Array<{ targets__subsystem: string; targets__module: string; targets__kind?: string }>;
  };
}

export async function listLogAudits(params: {
  page: number;
  pageSize: number;
  startTime?: string;
  endTime?: string;
  result?: string;
  environmentId?: number;
  subsystem?: string;
  module?: string;
  sourceCategory?: string;
  query?: string;
  order?: 'asc' | 'desc';
}): Promise<LogAuditList> {
  const search = new URLSearchParams({ page: String(params.page), page_size: String(params.pageSize) });
  if (params.startTime) search.set('start_time', params.startTime.replace(' ', 'T'));
  if (params.endTime) search.set('end_time', params.endTime.replace(' ', 'T'));
  if (params.result) search.set('result', params.result);
  if (params.environmentId) search.set('environment_id', String(params.environmentId));
  if (params.subsystem) search.set('subsystem', params.subsystem);
  if (params.module) search.set('module', params.module);
  if (params.sourceCategory) search.set('source_category', params.sourceCategory);
  if (params.query) search.set('query', params.query);
  search.set('order', params.order === 'asc' ? 'asc' : 'desc');
  return api(`/log-audits/?${search.toString()}`);
}

export async function updateLogAuditClientResult(operationId: string, resultCount: number): Promise<LogAuditRecord> {
  return api('/log-audits/client-result/', {
    method: 'POST',
    body: JSON.stringify({ operation_id: operationId, result_count: Math.max(0, Math.trunc(resultCount)) }),
  });
}



export interface DataExtractionResultSummary {
  rule_id: string;
  rule_name: string;
  row_count: number;
  fields: string[];
  output_format: 'table' | 'text';
}

export interface DataExtractionHourSummary {
  start_time: string;
  end_time: string;
  row_count: number;
  status: 'success' | 'no_result' | 'failed' | 'cancelled';
}

export interface DataExtractionRecord {
  id: number;
  source_audit?: number | null;
  source_operation_id: string;
  environment?: number | null;
  environment_name: string;
  task_name: string;
  name: string;
  query_snapshot: LogWindowRequest & Record<string, unknown>;
  rule_snapshots: Array<Record<string, unknown>>;
  status: 'running' | 'success' | 'no_result' | 'failed' | 'cancelled' | string;
  status_display: string;
  matched_rule_count: number;
  row_count: number;
  result_summary: DataExtractionResultSummary[];
  hour_summary: DataExtractionHourSummary[];
  error_message: string;
  created_at: string;
  updated_at: string;
  finished_at?: string | null;
}

export interface DataExtractionRecordList {
  count: number;
  next?: string | null;
  previous?: string | null;
  results: DataExtractionRecord[];
}

export async function listDataExtractionRecords(params: {
  page?: number;
  pageSize?: number;
  environmentId?: number;
  sourceOperationId?: string;
  status?: string;
  query?: string;
} = {}): Promise<DataExtractionRecordList> {
  const search = new URLSearchParams({ page: String(params.page || 1), page_size: String(params.pageSize || 100) });
  if (params.environmentId) search.set('environment_id', String(params.environmentId));
  if (params.sourceOperationId) search.set('source_operation_id', params.sourceOperationId);
  if (params.status) search.set('status', params.status);
  if (params.query) search.set('query', params.query);
  return api(`/data-extractions/?${search.toString()}`);
}

export async function createDataExtractionRecord(payload: Partial<DataExtractionRecord> & { name: string }): Promise<DataExtractionRecord> {
  return api('/data-extractions/', { method: 'POST', body: JSON.stringify(payload) });
}

export async function updateDataExtractionRecord(id: number, payload: Partial<DataExtractionRecord>): Promise<DataExtractionRecord> {
  return api(`/data-extractions/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) });
}

export async function deleteDataExtractionRecord(id: number): Promise<void> {
  await api(`/data-extractions/${id}/`, { method: 'DELETE' });
}


export interface AbnormalCaseErrorCode {
  key: string;
  value: string;
  raw: string;
}

export interface AbnormalCaseAnomalyRule {
  id: string;
  keyword: string;
  case_sensitive: boolean;
  whole_word: boolean;
}

export interface AbnormalCaseEvidence {
  source_entry_id?: string;
  timestamp: string;
  raw: string;
  message: string;
  level: string;
  severity: string;
  subsystem: string;
  module: string;
  component: string;
  function_name: string;
  source_file: string;
  source_line?: number;
  source_category: string;
  process_id?: string;
  thread_id?: string;
  trace_id?: string;
  anomaly_rules: AbnormalCaseAnomalyRule[];
  template: string;
  tokens: string[];
  error_codes: AbnormalCaseErrorCode[];
}

export interface AbnormalCaseFeatureGroup {
  id: string;
  title: string;
  enabled: boolean;
  created_at: string;
  source_operation_id: string;
  source_task_name: string;
  environment_name: string;
  note: string;
  evidences: AbnormalCaseEvidence[];
}

export interface AbnormalCaseGovernanceEvent {
  action: 'case_created' | 'feature_added' | 'feature_enabled' | 'feature_disabled' | 'feature_deleted' | string;
  created_at: string;
  feature_group_id?: string;
  feature_group_title?: string;
  evidence_count?: number;
}

export interface AbnormalCase {
  id: number;
  name: string;
  category: string;
  symptom: string;
  root_cause: string;
  solution: string;
  description: string;
  tags: string[];
  enabled: boolean;
  source_operation_id: string;
  source_task_name: string;
  environment?: number | null;
  environment_name: string;
  query_snapshot: Record<string, unknown>;
  evidences: AbnormalCaseEvidence[];
  feature_groups?: AbnormalCaseFeatureGroup[];
  governance_history?: AbnormalCaseGovernanceEvent[];
  fingerprint_version: number;
  evidence_count: number;
  matched_count: number;
  last_matched_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AbnormalCaseList {
  count: number;
  next?: string | null;
  previous?: string | null;
  results: AbnormalCase[];
}

export async function listAbnormalCases(params: {
  page?: number;
  pageSize?: number;
  enabled?: boolean;
  environmentId?: number;
  category?: string;
  module?: string;
  query?: string;
} = {}): Promise<AbnormalCaseList> {
  const search = new URLSearchParams({ page: String(params.page || 1), page_size: String(params.pageSize || 200) });
  if (params.enabled !== undefined) search.set('enabled', params.enabled ? 'true' : 'false');
  if (params.environmentId) search.set('environment_id', String(params.environmentId));
  if (params.category) search.set('category', params.category);
  if (params.module) search.set('module', params.module);
  if (params.query) search.set('query', params.query);
  return api(`/abnormal-cases/?${search.toString()}`);
}

export async function createAbnormalCase(payload: Omit<Partial<AbnormalCase>, 'id' | 'created_at' | 'updated_at'> & { name: string; evidences: AbnormalCaseEvidence[] }): Promise<AbnormalCase> {
  return api('/abnormal-cases/', { method: 'POST', body: JSON.stringify(payload) });
}

export async function updateAbnormalCase(id: number, payload: Partial<AbnormalCase>): Promise<AbnormalCase> {
  return api(`/abnormal-cases/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) });
}

export async function deleteAbnormalCase(id: number): Promise<void> {
  await api(`/abnormal-cases/${id}/`, { method: 'DELETE' });
}

export async function markAbnormalCaseMatched(id: number): Promise<AbnormalCase> {
  return api(`/abnormal-cases/${id}/mark-match/`, { method: 'POST', body: JSON.stringify({}) });
}

export interface CpdReportTree {
  environment_id: number;
  root: string;
  subsystem_count: number;
  module_count: number;
  report_count: number;
  subsystems: Array<{ name: string; module_count: number; report_count: number; modules: Array<{ name: string; report_count: number }> }>;
}

export interface CpdReportSummary {
  file_name: string;
  full_path: string;
  subsystem?: string;
  module?: string;
  cpd_name?: string;
  operator?: string;
  software_ver?: string;
  report_date?: string;
  report_time?: string;
  measure_log?: string;
  start_time?: string;
  stop_time?: string;
  start_time_raw?: string;
  stop_time_raw?: string;
  execution_time?: string;
  test_run_result?: string;
  results_validation?: string;
  measurement_quality?: string;
  mcs_status?: string;
  modified_at?: string;
  parse_status?: 'success' | 'error';
  parse_message?: string;
}

export interface CpdReportList {
  environment_id: number;
  subsystem: string;
  module: string;
  page: number;
  page_size: number;
  count: number;
  results: CpdReportSummary[];
}

export interface CpdReportDataset {
  environment_id: number;
  start_time: string;
  end_time: string;
  count: number;
  candidate_count: number;
  results: CpdReportSummary[];
  range_tree: CpdReportTree;
  all_history_tree: CpdReportTree;
  parsed_now: number;
  summary_cache_hits: number;
  catalog_cache_status?: string;
  dataset_cache_status?: string;
  fingerprint?: string;
  cached_at?: string;
}

export interface EnvironmentVersionQueryResult {
  environment_id: number;
  version: string;
  checked_at?: string;
  lower_versions: Array<{
    machine_id: number;
    host: string;
    version?: string;
    mismatch?: boolean;
    skipped?: boolean;
    message?: string;
  }>;
  version_mismatch: boolean;
  mismatched_machine_ids: number[];
}

export interface EnvironmentOverviewRefreshResult {
  environment_id: number;
  runtime_status: EnvironmentRuntimeStatus;
  version?: string;
  version_error?: string;
  version_result?: EnvironmentVersionQueryResult | null;
}

export interface BulkActionResult {
  action: string;
  requested: number;
  affected: number;
  deleted?: number;
  updated?: number;
  skipped_ids?: number[];
  results?: EnvironmentRuntimeStatus[];
  overview_results?: EnvironmentOverviewRefreshResult[];
}


export interface MachineDraftPayload {
  name?: string;
  host: string;
  ssh_port: number;
  username: string;
  auth_type: 'none' | 'password' | 'private_key';
  password?: string;
  private_key?: string;
  private_key_passphrase?: string;
  role: 'upper' | 'lower';
  connection_test_token?: string;
}

function emitApiActivity(type: 'start' | 'finish', detail: { path: string; method: string }): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent(`tracelens:api-${type}`, { detail }));
}

/**
 * AI 对话会话 ID。
 * 同一个浏览器会话复用同一个 session id，后端可通过 X-Session-Id 保持上下文。
 */
function getTraceLensSessionId(): string {
  if (typeof window === 'undefined') return '';
  const key = 'tracelens-ai-session-id-v1';
  let id = window.localStorage.getItem(key);
  if (!id) {
    const suffix = crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '').slice(0, 20) : `${Date.now()}${Math.random().toString(36).slice(2, 10)}`;
    id = `ses_${suffix}`;
    window.localStorage.setItem(key, id);
  }
  return id;
}

export function buildApiHeaders(extra?: HeadersInit, sessionIdOverride?: string): Headers {
  const headers = new Headers(extra);
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const sessionId = sessionIdOverride || getTraceLensSessionId();
  if (sessionId && !headers.has('X-Session-Id')) {
    headers.set('X-Session-Id', sessionId);
  }
  return headers;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const detail = { path, method: String(init?.method || 'GET').toUpperCase() };
  emitApiActivity('start', detail);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: buildApiHeaders(init?.headers),
    });
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try {
        const body = await response.clone().json();
        message = body.message ?? body.detail ?? JSON.stringify(body);
      } catch {
        message = await response.text() || message;
      }
      throw new Error(message);
    }
    if (response.status === 204) return undefined as T;
    return response.json() as Promise<T>;
  } finally {
    emitApiActivity('finish', detail);
  }
}

function listPayload<T>(value: T[] | { results: T[] }): T[] {
  return Array.isArray(value) ? value : value.results;
}

export interface TraceLensToolDefinition {
  id: string;
  name: string;
  description: string;
  category: string;
  tags: string[];
  read_only: boolean;
  risk_level: string;
  transport: 'json' | 'stream' | string;
  endpoint_template: string;
  implementation: string;
  version: string;
  status: string;
  invokable: boolean;
  agent_available: boolean;
  atomic_id: string;
  kind: 'context' | 'query' | 'action' | 'navigation' | 'mutation' | string;
  domain: string;
  skills: string[];
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
}

export interface TraceLensSkillDefinition {
  id: string;
  name: string;
  description: string;
  domains: string[];
}

export interface TraceLensToolList {
  count: number;
  capability_count?: number;
  agent_ready_count: number;
  read_only_count: number;
  kind_counts?: Record<string, number>;
  domain_counts?: Record<string, number>;
  skills?: TraceLensSkillDefinition[];
  tools: TraceLensToolDefinition[];
}

export async function listTraceLensTools(): Promise<TraceLensToolList> {
  return api('/tools/');
}

export async function invokeTraceLensTool<T = unknown>(toolId: string, payload: Record<string, unknown>): Promise<{ tool_id: string; success: boolean; data: T }> {
  return api(`/tools/${encodeURIComponent(toolId)}/invoke/`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export interface TraceLensAssistantStep {
  tool_id: string;
  name: string;
  status: 'success' | 'failed' | 'confirm' | string;
  message: string;
}

export interface TraceLensAssistantConfirmation {
  confirmation_token: string;
  tool_id: string;
  tool_name: string;
  risk_level: string;
  summary: Record<string, unknown>;
}

export interface TraceLensAssistantResponse {
  message: string;
  steps: TraceLensAssistantStep[];
  ui_actions: Array<Record<string, unknown>>;
  confirmations: TraceLensAssistantConfirmation[];
  result_cards?: TraceLensAssistantResultCard[];
  suggested_actions?: string[];
}

export async function chatTraceLensAssistant(payload: {
  message: string;
  history?: Array<{ role: 'user' | 'assistant'; content: string }>;
  context?: Record<string, unknown>;
  skill_id?: string;
  memory?: string;
}): Promise<TraceLensAssistantResponse> {
  return api('/tools/assistant-chat/', { method: 'POST', body: JSON.stringify(payload) });
}

export interface TraceLensAssistantTrace {
  id: string;
  stage: 'planning' | 'llm' | 'tool' | 'ui' | 'answer' | string;
  /** Which atomic tool this step ran. The backend has always sent it; the UI used to drop it. */
  tool_id?: string;
  title: string;
  status: 'running' | 'success' | 'failed' | 'confirm' | string;
  detail?: string;
  input?: unknown;
  output?: unknown;
}

export interface TraceLensAssistantResultCard {
  id?: string;
  kind: 'logs' | 'environment' | 'component' | 'data' | string;
  title: string;
  status?: 'success' | 'warning' | 'danger' | 'info' | 'neutral' | string;
  summary?: string;
  facts?: Array<{ label: string; value: string }>;
  actions?: Array<{ label: string; kind: 'ui' | 'prompt' | string; action?: Record<string, unknown>; prompt?: string }>;
}

export interface TraceLensAssistantTokenUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated?: boolean;
}

/** Structured fields the case editor can accept directly (same contract as the
 *  `draft_diagnosis_case` tool, so the preview and the editor cannot disagree). */
export interface TraceLensCaseDraftFields {
  name?: string;
  category?: string;
  symptom?: string;
  root_cause?: string;
  solution?: string;
  description?: string;
  tags?: string[];
}

export interface TraceLensCaseDraftResponse {
  ok: boolean;
  markdown: string;
  case_draft: TraceLensCaseDraftFields;
  evidences: Array<{ raw: string; component?: string; timestamp?: string; source?: string }>;
  confidence: 'high' | 'medium' | 'low' | string;
  components?: string[];
  /** Machine field names, for gating. Display text is `missing_labels`. */
  missing?: string[];
  /** Human-readable "what is still unconfirmed", derived server-side. */
  missing_labels?: string[];
  open_questions?: string[];
  importable?: boolean;
  source?: string;
  error?: string;
}

/**
 * 「整理成案例」 — deterministic trigger, model-assisted extraction.
 *
 * The frontend owns *when* to summarise (an explicit button) and the backend owns the
 * extraction, so a user never depends on the model deciding to save on its own.
 */
export async function draftCaseFromConversation(payload: {
  messages: Array<{ role: string; content: string }>;
  context?: Record<string, unknown>;
  evidence?: string[];
}): Promise<TraceLensCaseDraftResponse> {
  return api<TraceLensCaseDraftResponse>('/tools/case-draft/', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export interface TraceLensLogWatch {
  id: number;
  environment: number;
  environment_name?: string;
  name: string;
  extraction_rule_id?: string;
  source_rule_id?: string;
  capture_config?: Record<string, unknown>;
  targets?: Array<{ subsystem: string; fm: string; kind?: string }>;
  source_categories?: string[];
  enabled: boolean;
  hit_count: number;
  dropped_count: number;
  last_error?: string;
  last_hit_at?: string | null;
}

export interface TraceLensCaptureSyncResult {
  kind?: 'semantic' | 'extraction' | string;
  environment_id: number;
  enabled_rule_ids: string[];
  stopped_rule_ids: string[];
  problems: Array<{ rule_id: string; reason: string }>;
  targets: Array<{ subsystem: string; fm: string; kind?: string }>;
  watches: TraceLensLogWatch[];
}

/**
 * 实时采集 — make the server-side capture watches match the opted-in extractors.
 *
 * One write path shared by the 实时监听 toggle and the assistant's own tool, so "what gets
 * captured" can never mean two different things depending on who turned monitoring on.
 */
export async function syncCaptureWatches(payload: {
  environment_id: number;
  /** 语义规则 (timeline ribbon) or 数据提取器 (collector panel). Defaults to extraction. */
  kind?: 'semantic' | 'extraction';
  rule_ids: string[];
  targets: Array<{ subsystem: string; fm: string; kind?: string }>;
  source_categories?: string[];
  enable: boolean;
  target_rows?: number;
}): Promise<TraceLensCaptureSyncResult> {
  return api<TraceLensCaptureSyncResult>('/log-watches/sync-capture/', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export type TraceLensAssistantStreamEvent =
  | { type: 'agent_start'; title: string; detail?: string; percentage?: number }
  | { type: 'task'; status: 'running' | 'done' | 'confirm' | string; phase: string; title: string; detail?: string; progress?: number }
  | { type: 'thinking'; id: string; stage?: string; title: string; detail?: string; status?: string; percentage?: number }
  | { type: 'progress'; id: string; stage?: string; title: string; detail?: string; status?: string; percentage?: number; current_step?: number; total_steps?: number }
  | { type: 'tool_call'; id: string; tool_id?: string; title: string; detail?: string; status?: string; input?: unknown }
  | { type: 'tool_result'; id: string; tool_id?: string; title: string; detail?: string; status?: string; output?: unknown }
  | { type: 'trace'; trace: TraceLensAssistantTrace }
  | { type: 'token'; delta: string }
  | { type: 'token_reset'; text: string }
  | { type: 'token_usage'; usage: TraceLensAssistantTokenUsage }
  | { type: 'confirmation'; confirmation: TraceLensAssistantConfirmation }
  | { type: 'ui_action'; action: Record<string, unknown>; action_id?: string; run_id?: string }
  | { type: 'done'; message: string; confirmation_count?: number; memory?: string; result_cards?: TraceLensAssistantResultCard[]; suggested_actions?: string[] }
  | { type: 'error'; message: string };

export interface TraceLensAiModelOption {
  id: string;
  name: string;
  context_window?: number | null;
  efforts: string[];
  default_effort: string;
  is_default?: boolean;
}

export interface TraceLensAiModelCatalog {
  models: TraceLensAiModelOption[];
  source: 'gateway' | 'configured' | 'unavailable' | string;
  default_model: string;
  current: { model: string; effort: string };
}

/** Model + reasoning-effort options for the composer picker. The backend degrades to the
 *  configured model when the gateway is unreachable, so the picker never blocks a chat. */
export async function listTraceLensAiModels(refresh = false): Promise<TraceLensAiModelCatalog> {
  const response = await fetch(`${API_BASE}/tools/ai-models/${refresh ? '?refresh=1' : ''}`, { headers: buildApiHeaders() });
  if (!response.ok) throw new Error(`获取模型列表失败：${response.status} ${response.statusText}`);
  return (await response.json()) as TraceLensAiModelCatalog;
}

export async function streamTraceLensAssistant(
  payload: {
    message: string;
    history?: Array<{ role: 'user' | 'assistant'; content: string }>;
    context?: Record<string, unknown>;
    skill_id?: string;
    memory?: string;
    run_id?: string;
    session_id?: string;
    /** Composer model picker. Empty string means "use the server default". */
    model?: string;
    effort?: string;
  },
  options: {
    resumeTaskId?: string;
    signal?: AbortSignal;
    onEvent: (event: TraceLensAssistantStreamEvent) => void | Promise<void>;
  },
): Promise<void> {
  const path = options.resumeTaskId ? `/ai/tasks/${encodeURIComponent(options.resumeTaskId)}/events?session_id=${encodeURIComponent(payload.session_id || '')}` : '/tools/assistant-chat-stream/';
  const detail = { path, method: 'POST' };
  emitApiActivity('start', detail);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method: options.resumeTaskId ? 'GET' : 'POST',
      headers: buildApiHeaders(undefined, payload.session_id),
      body: options.resumeTaskId ? undefined : JSON.stringify(payload),
      signal: options.signal,
    });
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try {
        const body = await response.clone().json();
        message = body.message ?? body.detail ?? message;
      } catch {
        message = await response.text() || message;
      }
      throw new Error(message);
    }
    if (!response.body) throw new Error('浏览器未返回 AI SSE 响应流。');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer = `${buffer}${decoder.decode(value, { stream: true })}`.replace(/\r\n/g, '\n');
      while (true) {
        const boundary = buffer.indexOf('\n\n');
        if (boundary < 0) break;
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const dataLines: string[] = [];
        frame.split('\n').forEach((line) => {
          if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
        });
        if (!dataLines.length) continue;
        const event = JSON.parse(dataLines.join('\n')) as TraceLensAssistantStreamEvent;
        options.signal?.throwIfAborted();
        await options.onEvent(event);
        // A single network chunk can contain several SSE frames. React batches
        // synchronous state updates from that loop, which made live steps look as
        // if they appeared only at the end. Give task/trace frames one paint turn
        // so every execution/evidence update becomes visible as it arrives.
        if (['agent_start', 'task', 'thinking', 'progress', 'tool_call', 'tool_result', 'trace'].includes(event.type)) {
          await new Promise<void>((resolve) => {
            if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
              window.requestAnimationFrame(() => resolve());
            } else {
              setTimeout(resolve, 0);
            }
          });
        }
      }
    }
  } finally {
    emitApiActivity('finish', detail);
  }
}


export interface TraceLensVoiceCapabilities {
  stt: boolean;
  tts: boolean;
}

export async function getTraceLensVoiceCapabilities(): Promise<TraceLensVoiceCapabilities> {
  return api('/tools/assistant-voice-capabilities/');
}

export async function transcribeTraceLensVoice(blob: Blob, fileName = 'tracepilot-voice.webm'): Promise<{ text: string }> {
  const path = '/tools/assistant-transcribe/';
  const detail = { path, method: 'POST' };
  const form = new FormData();
  form.append('file', blob, fileName);
  emitApiActivity('start', detail);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      body: form,
    });
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try {
        const body = await response.clone().json();
        message = body.message ?? body.detail ?? message;
      } catch {
        message = await response.text() || message;
      }
      throw new Error(message);
    }
    return response.json() as Promise<{ text: string }>;
  } finally {
    emitApiActivity('finish', detail);
  }
}

export async function cancelTraceLensAssistant(runId: string): Promise<{ cancelled: boolean; run_id: string }> {
  return api('/tools/assistant-cancel/', {
    method: 'POST',
    body: JSON.stringify({ run_id: runId }),
  });
}

export async function acknowledgeAssistantUi(runId: string, actionId: string, receipt: Record<string, unknown>): Promise<void> {
  await api('/tools/assistant-ui-receipt/', { method: 'POST', body: JSON.stringify({ run_id: runId, action_id: actionId, receipt }) });
}


export async function guideTraceLensAssistant(runId: string, message: string): Promise<{ accepted: boolean; run_id: string }> {
  return api('/tools/assistant-guide/', {
    method: 'POST',
    body: JSON.stringify({ run_id: runId, message }),
  });
}


export async function confirmTraceLensAssistant(
  confirmationToken: string,
  options: { instruction?: string; context?: Record<string, unknown> } = {},
): Promise<{
  tool_id: string;
  tool_name: string;
  message: string;
  data: unknown;
  ui_actions: Array<Record<string, unknown>>;
}> {
  return api('/tools/assistant-confirm/', {
    method: 'POST',
    body: JSON.stringify({
      confirmation_token: confirmationToken,
      instruction: options.instruction || '',
      context: options.context || {},
    }),
  });
}


export interface LogFormatRuleTestLine {
  line_number: number;
  raw: string;
  matched: boolean;
  groups: Record<string, string>;
  fields: Record<string, string>;
  timestamp_valid: boolean | null;
  message: string;
}

export interface LogFormatRuleTestResult {
  line_count: number;
  matched_count: number;
  unmatched_count: number;
  match_rate: number;
  timestamp_valid_count: number;
  client_pattern: string;
  group_names: string[];
  results: LogFormatRuleTestLine[];
}

export type LogFormatRuleDraft = Omit<LogFormatParserRuleConfig, 'id' | 'client_pattern' | 'group_names' | 'built_in' | 'created_at' | 'updated_at'>;

export async function listLogFormatRules(): Promise<LogFormatParserRuleConfig[]> {
  return listPayload(await api<LogFormatParserRuleConfig[] | { results: LogFormatParserRuleConfig[] }>('/log-format-rules/?page_size=500'));
}

export async function listRuntimeLogFormatRules(): Promise<LogFormatParserRuleConfig[]> {
  return api<LogFormatParserRuleConfig[]>('/log-format-rules/runtime/');
}

export async function createLogFormatRule(payload: LogFormatRuleDraft): Promise<LogFormatParserRuleConfig> {
  return api('/log-format-rules/', { method: 'POST', body: JSON.stringify(payload) });
}

export async function updateLogFormatRule(id: number, payload: Partial<LogFormatRuleDraft>): Promise<LogFormatParserRuleConfig> {
  return api(`/log-format-rules/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) });
}

export async function deleteLogFormatRule(id: number): Promise<void> {
  return api(`/log-format-rules/${id}/`, { method: 'DELETE' });
}

export async function restoreDefaultLogFormatRule(id: number): Promise<LogFormatParserRuleConfig> {
  return api(`/log-format-rules/${id}/restore-default/`, { method: 'POST', body: '{}' });
}

export async function testLogFormatRuleDraft(payload: LogFormatRuleDraft, text: string): Promise<LogFormatRuleTestResult> {
  return api('/log-format-rules/test-draft/', { method: 'POST', body: JSON.stringify({ ...payload, text }) });
}

export interface UrlLogImportMember {
  name: string;
  filename: string;
  fm: string;
  size: number;
}

export interface UrlLogImportItem {
  status: 'success' | 'error';
  url: string;
  filename: string;
  fm: string;
  kind: 'log' | 'archive';
  members: UrlLogImportMember[];
  message?: string;
}

export async function inspectUrlLogImports(urls: string[]): Promise<UrlLogImportItem[]> {
  const payload = await api<{ results: UrlLogImportItem[] }>('/log-url-imports/', {
    method: 'POST',
    body: JSON.stringify({ urls }),
  });
  return payload.results ?? [];
}

export async function fetchUrlLogImportContent(url: string, member?: string): Promise<Blob> {
  const path = '/log-url-imports/content/';
  const detail = { path, method: 'POST' };
  emitApiActivity('start', detail);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: buildApiHeaders(),
      body: JSON.stringify({ url, member: member || '' }),
    });
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try {
        const body = await response.clone().json();
        message = body.message ?? body.detail ?? JSON.stringify(body);
      } catch {
        message = await response.text() || message;
      }
      throw new Error(message);
    }
    return response.blob();
  } finally {
    emitApiActivity('finish', detail);
  }
}

export async function listEnvironments(): Promise<EnvironmentSummary[]> {
  return listPayload(await api<EnvironmentSummary[] | { results: EnvironmentSummary[] }>('/environments/?page_size=200'));
}

export async function listActiveEnvironmentDeployments(): Promise<EnvironmentDeploymentSummary[]> {
  return listPayload(await api<EnvironmentDeploymentSummary[] | { results: EnvironmentDeploymentSummary[] }>('/environments/active-deployments/'));
}

export async function getEnvironmentDeploymentDefaults(id: number): Promise<EnvironmentDeploymentDraft> {
  return api(`/environments/${id}/deployment-defaults/`);
}

export async function checkEnvironmentDeploymentSshTrust(
  id: number,
  gpbIps: string[],
  dhh?: { include_dhh?: boolean; dhh_ip?: string; dhh_user?: string },
): Promise<DeploymentSshTrustCheck> {
  return api(`/environments/${id}/deployment-ssh-trust/`, {
    method: 'POST',
    body: JSON.stringify({ gpb_ips: gpbIps, ...dhh }),
  });
}

export async function syncEnvironmentDeploymentTime(
  id: number,
  gpbIps: string[],
  dhh?: { include_dhh?: boolean; dhh_ip?: string; dhh_user?: string },
): Promise<DeploymentTimeSyncCheck> {
  return api(`/environments/${id}/deployment-time-sync/`, {
    method: 'POST',
    body: JSON.stringify({ gpb_ips: gpbIps, ...dhh }),
  });
}

export async function previewEnvironmentDeployment(id: number, payload: EnvironmentDeploymentDraft): Promise<EnvironmentDeploymentDraft> {
  return api(`/environments/${id}/deployment-preview/`, { method: 'POST', body: JSON.stringify(payload) });
}

export async function parseEnvironmentDeploymentCommands(id: number, commands: string): Promise<EnvironmentDeploymentDraft> {
  return api(`/environments/${id}/deployment-parse/`, { method: 'POST', body: JSON.stringify({ commands }) });
}

export async function startEnvironmentDeployment(id: number, payload: EnvironmentDeploymentDraft, scheduledAt?: string | null): Promise<EnvironmentDeployment> {
  return api(`/environments/${id}/deployments/`, {
    method: 'POST',
    body: JSON.stringify({ ...payload, scheduled_at: scheduledAt || null }),
  });
}

export async function listEnvironmentDeployments(id: number): Promise<EnvironmentDeploymentSummary[]> {
  return api(`/environments/${id}/deployments/`);
}

export async function getLatestEnvironmentDeployment(id: number): Promise<EnvironmentDeploymentSummary | null> {
  return api(`/environments/${id}/latest-deployment/`);
}

export async function getEnvironmentDeployment(id: number, deploymentId: number, stepKey?: string): Promise<EnvironmentDeployment> {
  const query = stepKey ? `?step_key=${encodeURIComponent(stepKey)}` : '';
  return api(`/environments/${id}/deployments/${deploymentId}/${query}`);
}

export async function retryEnvironmentDeploymentStep(id: number, deploymentId: number, stepKey: string): Promise<EnvironmentDeployment> {
  return api(`/environments/${id}/deployments/${deploymentId}/retry/`, {
    method: 'POST',
    body: JSON.stringify({ step_key: stepKey }),
  });
}

export async function stopEnvironmentDeployment(id: number, deploymentId: number): Promise<EnvironmentDeployment> {
  return api(`/environments/${id}/deployments/${deploymentId}/stop/`, { method: 'POST' });
}


export async function listEnvironmentFolders(): Promise<EnvironmentFolder[]> {
  return listPayload(await api<EnvironmentFolder[] | { results: EnvironmentFolder[] }>('/environment-folders/?page_size=500'));
}

export async function createEnvironmentFolder(payload: { name: string; parent?: number | null; sort_order?: number }): Promise<EnvironmentFolder> {
  return api('/environment-folders/', { method: 'POST', body: JSON.stringify(payload) });
}

export async function updateEnvironmentFolder(id: number, payload: Partial<EnvironmentFolder>): Promise<EnvironmentFolder> {
  return api(`/environment-folders/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) });
}

export async function deleteEnvironmentFolder(id: number): Promise<void> {
  return api(`/environment-folders/${id}/`, { method: 'DELETE' });
}

export async function getAllEnvironmentRuntimeStatuses(): Promise<EnvironmentRuntimeStatus[]> {
  return api('/environments/runtime-status/');
}

export async function getEnvironmentRuntimeStatus(id: number, refresh = false): Promise<EnvironmentRuntimeStatus> {
  return api(`/environments/${id}/runtime-status/${refresh ? '?refresh=true' : ''}`);
}

export async function syncLowerMachineTime(environmentId: number, machineIds: number[]): Promise<LowerTimeSyncResult> {
  return api(`/environments/${environmentId}/sync-lower-time/`, {
    method: 'POST',
    body: JSON.stringify({ machine_ids: machineIds }),
  });
}

export async function listMachines(): Promise<MachineSummary[]> {
  return listPayload(await api<MachineSummary[] | { results: MachineSummary[] }>('/machines/?page_size=200'));
}

export async function testMachineDraft(payload: MachineDraftPayload): Promise<{ success: boolean; hostname?: string; system?: string; message?: string; connection_test_token?: string }> {
  return api('/machines/connect-test-draft/', { method: 'POST', body: JSON.stringify(payload) });
}

export async function createMachine(payload: MachineDraftPayload): Promise<MachineSummary> {
  return api('/machines/', { method: 'POST', body: JSON.stringify(payload) });
}

export async function updateMachine(id: number, payload: Partial<MachineDraftPayload>): Promise<MachineSummary> {
  return api(`/machines/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) });
}

export async function testMachine(id: number): Promise<{ success: boolean; hostname?: string; system?: string; message?: string }> {
  return api(`/machines/${id}/connect-test/`, { method: 'POST', body: '{}' });
}

export async function disconnectMachineSession(id: number): Promise<void> {
  return api(`/machines/${id}/disconnect-session/`, { method: 'POST', body: '{}' });
}

export async function updateEnvironment(id: number, payload: { name?: string; description?: string; folder?: number | null }): Promise<EnvironmentSummary> {
  return api(`/environments/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) });
}

export async function deleteEnvironment(id: number): Promise<void> {
  return api(`/environments/${id}/`, { method: 'DELETE' });
}

export async function bulkEnvironmentAction(ids: number[], action: 'delete' | 'runtime_status' | 'overview_refresh'): Promise<BulkActionResult> {
  return api('/environments/bulk/', { method: 'POST', body: JSON.stringify({ ids, action }) });
}

export async function discoverEnvironment(id: number): Promise<Record<string, unknown>> {
  return api(`/environments/${id}/discover/`, { method: 'POST', body: '{}' });
}

export async function queryVersion(id: number): Promise<EnvironmentVersionQueryResult> {
  return api(`/environments/${id}/query-version/`, { method: 'POST', body: '{}' });
}

export async function getResourceSettings(): Promise<ResourceSettings> {
  return api('/resource-settings/current/');
}

export async function updateResourceSettings(payload: Record<string, unknown>): Promise<ResourceSettings> {
  return api('/resource-settings/current/', { method: 'PATCH', body: JSON.stringify(payload) });
}


export interface SemanticSourceResult {
  description: string;
  function_name: string;
  qualified_name: string;
  function_line?: number;
  function_end_line?: number;
  source_path: string;
  source_file: string;
  subsystem: string;
  module: string;
  cache_hit: boolean;
  cache_ttl: number;
}

export interface SemanticSourceMiss {
  found: false;
  message: string;
}

export async function recognizeSemanticSource(environmentId: number, payload: {
  source_file: string;
  source_line?: number;
  function_name?: string;
  fm_targets: Array<{ subsystem: string; module: string }>;
}): Promise<(SemanticSourceResult & { found?: true }) | SemanticSourceMiss> {
  return api(`/environment-logs/${environmentId}/semantic-source/`, { method: 'POST', body: JSON.stringify(payload) });
}


export interface SemanticSourceBatchItem {
  key: string;
  source_file: string;
  source_line?: number;
  function_name?: string;
}

export interface SemanticSourceBatchResult {
  results: Array<SemanticSourceResult & { key: string }>;
  misses: Array<{ key: string; source_file: string; function_name?: string; message: string }>;
  count: number;
}

export async function recognizeSemanticSourcesBatch(environmentId: number, payload: {
  items: SemanticSourceBatchItem[];
  fm_targets: Array<{ subsystem: string; module: string }>;
}): Promise<SemanticSourceBatchResult> {
  return api(`/environment-logs/${environmentId}/semantic-source-batch/`, { method: 'POST', body: JSON.stringify(payload) });
}

export async function getLogTree(environmentId: number, sourceCategories: string[] = [], refresh = false): Promise<EnvironmentLogTree> {
  const params = new URLSearchParams();
  sourceCategories.forEach((item) => params.append('source_category', item));
  if (refresh) params.set('refresh', 'true');
  const query = params.toString();
  return api(`/environment-logs/${environmentId}/subsystems/${query ? `?${query}` : ''}`);
}

export async function listGlobalLogSubsystems(query = ''): Promise<GlobalLogSubsystem[]> {
  const params = new URLSearchParams({ page_size: '500' });
  if (query.trim()) params.set('q', query.trim());
  return listPayload(await api<GlobalLogSubsystem[] | { results: GlobalLogSubsystem[] }>(`/log-subsystems/?${params.toString()}`));
}

export async function getGlobalLogCatalogTree(includeDisabled = true): Promise<GlobalLogSubsystem[]> {
  return api(`/log-subsystems/tree/?include_disabled=${includeDisabled ? 'true' : 'false'}`);
}

export async function createGlobalLogSubsystem(payload: Partial<GlobalLogSubsystem>): Promise<GlobalLogSubsystem> {
  return api('/log-subsystems/', { method: 'POST', body: JSON.stringify(payload) });
}

export async function updateGlobalLogSubsystem(id: number, payload: Partial<GlobalLogSubsystem>): Promise<GlobalLogSubsystem> {
  return api(`/log-subsystems/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) });
}

export async function deleteGlobalLogSubsystem(id: number): Promise<void> {
  return api(`/log-subsystems/${id}/`, { method: 'DELETE' });
}

export async function bulkGlobalLogSubsystems(ids: number[], action: 'enable' | 'disable' | 'delete'): Promise<BulkActionResult> {
  return api('/log-subsystems/bulk/', { method: 'POST', body: JSON.stringify({ ids, action }) });
}

export async function listLogQuerySkills(options: { subsystem?: number | string; query?: string; enabledOnly?: boolean } = {}): Promise<LogQuerySkill[]> {
  const params = new URLSearchParams({ page_size: '500' });
  if (options.subsystem !== undefined && String(options.subsystem).trim()) params.set('subsystem', String(options.subsystem));
  if (options.query?.trim()) params.set('q', options.query.trim());
  if (options.enabledOnly) params.set('enabled', 'true');
  return listPayload(await api<LogQuerySkill[] | { results: LogQuerySkill[] }>(`/log-query-skills/?${params.toString()}`));
}

export async function createLogQuerySkill(payload: Omit<Partial<LogQuerySkill>, 'id'> & { subsystem: number; name: string }): Promise<LogQuerySkill> {
  return api('/log-query-skills/', { method: 'POST', body: JSON.stringify(payload) });
}

export async function updateLogQuerySkill(id: number, payload: Partial<LogQuerySkill>): Promise<LogQuerySkill> {
  return api(`/log-query-skills/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) });
}

export async function deleteLogQuerySkill(id: number): Promise<void> {
  return api(`/log-query-skills/${id}/`, { method: 'DELETE' });
}

export async function bulkLogQuerySkills(ids: number[], action: 'enable' | 'disable' | 'delete'): Promise<BulkActionResult> {
  return api('/log-query-skills/bulk/', { method: 'POST', body: JSON.stringify({ ids, action }) });
}

export async function createGlobalLogFm(payload: Partial<GlobalLogFm> & { subsystem: number; name: string }): Promise<GlobalLogFm> {
  return api('/log-fms/', { method: 'POST', body: JSON.stringify(payload) });
}

export async function updateGlobalLogFm(id: number, payload: Partial<GlobalLogFm>): Promise<GlobalLogFm> {
  return api(`/log-fms/${id}/`, { method: 'PATCH', body: JSON.stringify(payload) });
}

export async function deleteGlobalLogFm(id: number): Promise<void> {
  return api(`/log-fms/${id}/`, { method: 'DELETE' });
}

export async function bulkGlobalLogFms(ids: number[], action: 'enable' | 'disable' | 'delete'): Promise<BulkActionResult> {
  return api('/log-fms/bulk/', { method: 'POST', body: JSON.stringify({ ids, action }) });
}

export async function bulkSetGlobalLogFmTargets(ids: number[], targetModuleIds: number[]): Promise<BulkActionResult> {
  return api('/log-fms/bulk/', {
    method: 'POST',
    body: JSON.stringify({ ids, action: 'set_target_modules', target_module_ids: targetModuleIds }),
  });
}


export async function createLogScene(scene: unknown): Promise<{ id: string }> {
  return api('/log-scenes/', { method: 'POST', body: JSON.stringify(scene) });
}

export async function getLogScene<T = unknown>(sceneId: string): Promise<{ id: string; scene: T }> {
  return api(`/log-scenes/${encodeURIComponent(sceneId)}/`);
}

export async function updateLogScene(sceneId: string, scene: unknown): Promise<{ id: string }> {
  return api(`/log-scenes/${encodeURIComponent(sceneId)}/`, { method: 'PUT', body: JSON.stringify(scene) });
}

export async function buildLogPlan(environmentId: number, payload: LogWindowRequest): Promise<LogPlan> {
  return api(`/environment-logs/${environmentId}/plan/`, { method: 'POST', body: JSON.stringify(payload) });
}

export async function getLogSearchProgress(environmentId: number, operationId: string): Promise<LogSearchProgress> {
  return api(`/environment-logs/${environmentId}/progress/?operation_id=${encodeURIComponent(operationId)}`);
}

export async function cancelLogSearch(environmentId: number, operationId: string): Promise<LogSearchProgress> {
  return api(`/environment-logs/${environmentId}/cancel/`, { method: 'POST', body: JSON.stringify({ operation_id: operationId }) });
}

export interface FetchLogWindowOptions {
  operationId?: string;
  signal?: AbortSignal;
  onProgress?: (info: LogSearchProgress & { receivedBytes?: number }) => void;
}

export type LiveLogStreamEvent =
  | {
      type: 'ready';
      transport?: 'ssh_tail' | 'sftp_poll';
      interval_seconds: number;
      heartbeat_seconds?: number;
      artifacts: Array<{ machine_id: number; source_category: string; subsystem: string; fm: string; path: string }>;
    }
  | {
      type: 'logs';
      chunks: Array<{ source_category: string; subsystem: string; fm: string; path: string; line_count: number; text: string }>;
      bytes: number;
      lines: number;
      server_time: string;
    }
  | { type: 'heartbeat'; server_time: string }
  | { type: 'reconnecting'; attempt: number; retry_in_seconds: number; message?: string; server_time: string }
  | { type: 'reconnected'; transport?: 'ssh_tail'; server_time: string }
  | { type: 'fallback'; transport: 'sftp_poll'; message: string; server_time: string }
  | { type: 'error'; message: string };

export interface StreamLiveLogOptions {
  signal?: AbortSignal;
  onEvent: (event: LiveLogStreamEvent) => void;
}

export async function streamLiveLogWindow(
  environmentId: number,
  payload: LogWindowRequest,
  options: StreamLiveLogOptions,
): Promise<void> {
  const response = await fetch(`${API_BASE}/environment-logs/${environmentId}/live/`, {
      method: 'POST',
      headers: buildApiHeaders(),
      body: JSON.stringify({
        source_categories: payload.source_categories,
        subsystems: payload.subsystems,
        fms: payload.fms,
        fm_targets: payload.fm_targets,
        keyword: payload.keyword || '',
        raw_text: false,
      }),
      signal: options.signal,
    });
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try {
        const body = await response.clone().json();
        message = body.message ?? body.detail ?? message;
      } catch {
        // Keep HTTP status text.
      }
      throw new Error(message);
    }
    if (!response.body) throw new Error('浏览器未返回实时日志响应流。');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer = `${buffer}${decoder.decode(value, { stream: true })}`.replace(/\r\n/g, '\n');
      while (true) {
        const boundary = buffer.indexOf('\n\n');
        if (boundary < 0) break;
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        let eventName = 'message';
        const dataLines: string[] = [];
        frame.split('\n').forEach((line) => {
          if (line.startsWith('event:')) eventName = line.slice(6).trim();
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
        });
        if (!dataLines.length) continue;
        try {
          const event = JSON.parse(dataLines.join('\n')) as LiveLogStreamEvent;
          if (event && typeof event === 'object' && 'type' in event) options.onEvent(event);
          else console.warn('live log SSE event missing type', eventName);
        } catch (exc) {
          console.warn('live log SSE event parse failed', eventName, exc);
        }
      }
    }
}

export async function fetchLogWindow(
  environmentId: number,
  payload: LogWindowRequest,
  options: FetchLogWindowOptions = {},
): Promise<{ blob: Blob; operationId: string }> {
  const operationId = options.operationId ?? (typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `search-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  let stopped = false;
  let latestProgress: LogSearchProgress = { operation_id: operationId, stage: 'pending', percent: 0, done: false };
  const poll = async () => {
    if (stopped || options.signal?.aborted) return;
    try {
      latestProgress = await getLogSearchProgress(environmentId, operationId);
      options.onProgress?.(latestProgress);
    } catch {
      // Search itself is authoritative; progress polling must never abort it.
    }
  };
  void poll();
  const timer = window.setInterval(() => { void poll(); }, 350);
  const streamDetail = { path: `/environment-logs/${environmentId}/stream/`, method: 'POST' };
  emitApiActivity('start', streamDetail);
  try {
    const response = await fetch(`${API_BASE}/environment-logs/${environmentId}/stream/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-TraceLens-Operation-ID': operationId },
      body: JSON.stringify(payload),
      signal: options.signal,
    });
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try {
        const body = await response.clone().json();
        message = body.message ?? body.detail ?? message;
      } catch {
        // Keep HTTP message.
      }
      throw new Error(message);
    }
    const serverOperationId = response.headers.get('X-TraceLens-Operation-ID') ?? operationId;
    if (!response.body) return { blob: await response.blob(), operationId: serverOperationId };
    const reader = response.body.getReader();
    const MAX_BROWSER_LOG_BYTES = 256 * 1024 * 1024;
    let receivedBytes = 0;
    const trackedStream = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const { done, value } = await reader.read();
          if (done) {
            controller.close();
            return;
          }
          if (!value) return;
          receivedBytes += value.byteLength;
          if (receivedBytes > MAX_BROWSER_LOG_BYTES) {
            await reader.cancel('TraceLens browser log safety limit');
            controller.error(new Error('本次日志数据超过 256 MiB 浏览器安全上限，请缩短时间范围或减少模块后重新检索。'));
            return;
          }
          options.onProgress?.({ ...latestProgress, operation_id: serverOperationId, receivedBytes });
          controller.enqueue(value);
        } catch (exc) {
          controller.error(exc);
        }
      },
      async cancel(reason) {
        await reader.cancel(reason);
      },
    });
    // Let the browser own the byte aggregation instead of retaining every
    // Uint8Array chunk in the JavaScript heap. This substantially lowers the
    // transient memory peak before the worker starts streaming the Blob.
    const blob = await new Response(trackedStream, { headers: { 'Content-Type': 'text/plain;charset=utf-8' } }).blob();
    await poll();
    return { blob, operationId: serverOperationId };
  } finally {
    stopped = true;
    window.clearInterval(timer);
    emitApiActivity('finish', streamDetail);
  }
}

export async function getCpdReportTree(environmentId: number): Promise<CpdReportTree> {
  return api(`/cpd-reports/${environmentId}/tree/`);
}

export interface CpdReportQueryResult {
  environment_id: number;
  page: number;
  page_size: number;
  count: number;
  candidate_count: number;
  range_tree?: CpdReportTree;
  range_report_count?: number;
  results: CpdReportSummary[];
  parsed_now: number;
  summary_cache_hits: number;
  index_mode: 'page' | 'range';
  cache_status?: string;
  fingerprint?: string;
  cached_at?: string;
}

export interface CpdDataQuery { subsystem: string; module: string; start_time: string; end_time: string }
export interface CpdDataFile { name: string; path: string; size: number; sheets: Array<{name: string; matched_rows: number; time_column: string | null; filter_status: string; invalid_time_rows: number}> }
export interface CpdDataIndex { root: string; files: CpdDataFile[]; errors: Array<{path: string; error: string}> }
export interface CpdSheetPreview { path: string; sheet: string; headers: string[]; rows: Array<{line: number; cells: unknown[]}>; total: number; page: number; page_size: number; time_column?: string; warning?: string; invalid_time_rows?: number }
export async function getCpdDataFiles(environmentId: number, query: CpdDataQuery): Promise<CpdDataIndex> {
  return api(`/cpd-reports/${environmentId}/data-files/?${new URLSearchParams({...query})}`);
}
export async function getCpdExcelPreview(environmentId: number, query: CpdDataQuery & { path: string; sheet: string; page: string; page_size: string; sort_column: string; descending: string }): Promise<CpdSheetPreview> {
  return api(`/cpd-reports/${environmentId}/excel-preview/?${new URLSearchParams({...query})}`);
}

export async function getCpdReportSnapshot(environmentId: number, refresh = false): Promise<{ environment_id: number; tree: CpdReportTree; reports: CpdReportSummary[]; cache_status?: string; fingerprint?: string; cached_at?: string; catalog_count?: number }> {
  return api(`/cpd-reports/${environmentId}/snapshot/${refresh ? '?refresh=true' : ''}`);
}

export async function getCpdReportDataset(environmentId: number, startTime: string, endTime: string, refresh = false): Promise<CpdReportDataset> {
  const search = new URLSearchParams();
  if (startTime) search.set('start_time', startTime);
  if (endTime) search.set('end_time', endTime);
  if (refresh) search.set('refresh', 'true');
  return api(`/cpd-reports/${environmentId}/dataset/?${search.toString()}`);
}

export async function queryCpdReports(environmentId: number, params: {
  page: number; pageSize: number; startTime?: string; endTime?: string; subsystem?: string; module?: string; file?: string;
  result?: string; validation?: string; quality?: string; mcs?: string; duration?: string; query?: string;
}): Promise<CpdReportQueryResult> {
  const search = new URLSearchParams({ page: String(params.page), page_size: String(params.pageSize) });
  if (params.startTime) search.set('start_time', params.startTime);
  if (params.endTime) search.set('end_time', params.endTime);
  if (params.subsystem) search.set('subsystem', params.subsystem);
  if (params.module) search.set('module', params.module);
  if (params.file) search.set('file', params.file);
  if (params.result) search.set('result', params.result);
  if (params.validation) search.set('validation', params.validation);
  if (params.quality) search.set('quality', params.quality);
  if (params.mcs) search.set('mcs', params.mcs);
  if (params.duration) search.set('duration', params.duration);
  if (params.query) search.set('q', params.query);
  return api(`/cpd-reports/${environmentId}/query/?${search.toString()}`);
}

export async function listCpdReports(environmentId: number, subsystem?: string, module?: string, page = 1, pageSize = 100): Promise<CpdReportList> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (subsystem) params.set('subsystem', subsystem);
  if (module) params.set('module', module);
  return api(`/cpd-reports/${environmentId}/reports/?${params.toString()}`);
}

export async function getCpdReportContent(environmentId: number, subsystem: string, module: string, fileName: string): Promise<{ file_name: string; path: string; content: string }> {
  const params = new URLSearchParams({ subsystem, module, file: fileName });
  return api(`/cpd-reports/${environmentId}/content/?${params.toString()}`);
}

export type AtLogCaseStatus = 'passed' | 'failed' | 'unknown';

export interface AtLogEvidence {
  source: string;
  url: string;
  time: string;
  level: string;
  module: string;
  excerpt: string;
}

export interface AtLogCallFrame {
  file: string;
  line: number;
  function: string;
}


export interface AtLogSavedState {
  case_url: string;
  case_id: string;
  case_name: string;
  case_status: string;
  updated_at: string;
  workspace?: {
    state?: Record<string, unknown>;
    result?: AtLogCaseLogResult | Record<string, never>;
  };
  ai?: {
    result?: AtLogAiDiagnosisResult | null;
    thinking_text?: string;
    token_usage?: AtLogAiTokenUsage | Record<string, never>;
    job_id?: string;
    completed_at?: string;
    revision?: number;
  };
}

export interface AtLogCaseAnalysis {
  case_id: string;
  case_name: string;
  case_description?: string;
  base_url: string;
  host: string;
  status: AtLogCaseStatus;
  conclusion: string;
  assertion: string;
  assertion_summary: string;
  reason_category: string;
  reason_detail: string;
  assertion_meta: {
    expect: string;
    real: string;
    relation: string;
    relation_cn: string;
    caller: string;
    error_msg: string;
  };
  start_time: string;
  end_time: string;
  failure_time: string;
  event_start_time: string;
  event_end_time: string;
  summary: Record<string, unknown> & {
    tests?: number;
    failures?: number;
    errors?: number;
    ignored?: number;
  };
  failure_location?: AtLogCallFrame | null;
  call_chain: AtLogCallFrame[];
  failure_text: string;
  report_excerpt: string;
  xytest_errors: Array<{ time: string; level: string; message: string; conclusion: string; raw: string }>;
  links: Record<string, string>;
  files: string[];
  result_files: string[];
  evidence: AtLogEvidence[];
  warnings: string[];
  saved_state?: AtLogSavedState;
  environment?: {
    created?: boolean;
    environment_id?: number | null;
    environment_name?: string;
    folder_name?: string;
    topology?: string;
    sim_mode?: string;
    warning?: string;
    upper?: AtLogEnvironmentNode | null;
    dhh?: AtLogEnvironmentNode | null;
    lowers?: AtLogEnvironmentNode[];
  };
}


export interface AtLogAiTokenUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

export interface AtLogAiDiagnosisStep {
  name: string;
  label: string;
  status: 'completed' | 'running' | 'error' | string;
  duration_ms?: number;
  detail: string;
  agent?: string;
  action?: string;
  token_usage?: AtLogAiTokenUsage;
  progress?: number;
}

export interface AtLogAiEvidence {
  time: string;
  component: string;
  source: string;
  message: string;
  why: string;
}

export interface AtLogAiDiagnosisReport {
  summary: string;
  root_cause: string;
  root_cause_category: string;
  confidence: number;
  confidence_level: '高' | '中' | '低' | string;
  causal_chain: string[];
  evidence: AtLogAiEvidence[];
  excluded_causes: string[];
  recommendations: string[];
  next_checks: string[];
  limitations: string[];
}

export interface AtLogAiDiagnosisResult {
  case_id: string;
  case_name: string;
  base_url: string;
  model: string;
  provider: string;
  report: AtLogAiDiagnosisReport;
  steps: AtLogAiDiagnosisStep[];
  retrieval_rounds: number;
  event_components?: string[];
  selected_targets?: Array<{ subsystem: string; module: string }>;
  component_candidates?: Array<{ subsystem: string; module: string; score?: number; matched_by?: string; event_component?: string }>;
  anomaly_rules?: Array<{ keyword: string; case_sensitive: boolean; whole_word: boolean }>;
  evidence_message_count?: number;
  collected_message_count: number;
  current_message_count: number;
  warnings: string[];
  token_usage: AtLogAiTokenUsage;
  duration_ms: number;
  case_description?: string;
  case_evidences?: AbnormalCaseEvidence[];
  case_draft?: {
    name?: string;
    category?: string;
    symptom?: string;
    root_cause?: string;
    solution?: string;
    description?: string;
    tags?: string[];
  };
}

export interface AtLogAiJobEvent {
  seq: number;
  type: 'stage' | 'thinking' | 'token_usage' | 'report_preview' | 'diagnosis' | string;
  timestamp: number;
  status?: string;
  detail?: string;
  text?: string;
  stage_name?: string;
  agent?: string;
  kind?: 'reason' | 'action' | 'tool' | string;
  usage?: AtLogAiTokenUsage;
  stage?: AtLogAiDiagnosisStep;
}

export interface AtLogAiDiagnosisJob {
  job_id: string;
  url: string;
  status: 'queued' | 'running' | 'completed' | 'error' | string;
  created_at: number;
  updated_at: number;
  current_stage: AtLogAiDiagnosisStep;
  token_usage: AtLogAiTokenUsage;
  report_preview: string;
  thinking_text?: string;
  cache_hit?: boolean;
  cache_source?: 'database' | 'redis' | string;
  events: AtLogAiJobEvent[];
  last_seq: number;
  result?: AtLogAiDiagnosisResult | null;
  error: string;
}

export interface AtLogKnowledgeMatch {
  case_id: number;
  name: string;
  category: string;
  symptom: string;
  root_cause: string;
  solution: string;
  score: number;
  matched_tokens: string[];
  matched_modules: string[];
  evidence_count: number;
  evidence_preview?: Array<{
    time: string;
    component: string;
    source: string;
    message: string;
  }>;
  matched_count: number;
}

export interface AtLogKnowledgeMatchResult {
  case_id: string;
  case_name: string;
  matches: AtLogKnowledgeMatch[];
  strong_match: boolean;
  top_score: number;
  recommendation: string;
}

export interface AtLogEnvironmentNode {
  label: string;
  topology_ip: string;
  ssh_host: string;
  environment_host: string;
  username: string;
  has_password: boolean;
}

export interface AtLogDirectoryEntry {
  name: string;
  is_dir: boolean;
  relative_path: string;
  url: string;
}

export interface AtLogDirectoryResult {
  base_url: string;
  relative_path: string;
  url: string;
  entries: AtLogDirectoryEntry[];
}

export interface AtLogEventRow {
  line_number: number;
  time: string;
  component: string;
  level: string;
  source: string;
  message: string;
  raw: string;
}

export interface AtLogCaseLogRow {
  record_id?: string;
  trace_id?: string;
  error_code?: string;
  correlation_kind?: string;
  line_number: number;
  time: string;
  component: string;
  level: string;
  source: string;
  message: string;
  raw: string;
  source_kind: 'event' | 'full' | string;
  source_path: string;
}

export interface AtLogCaseLogCatalogGroup {
  subsystem: string;
  modules: string[];
}

export interface AtLogCaseLogResult {
  base_url: string;
  start_time: string;
  end_time: string;
  count: number;
  truncated: boolean;
  components: string[];
  log_catalog: AtLogCaseLogCatalogGroup[];
  levels: string[];
  sources: string[];
  rows: AtLogCaseLogRow[];
  event_raw_text: string;
  event_query?: {
    requested: boolean;
    url: string;
    found: boolean;
    row_count: number;
    error?: string;
  };
  source_roots?: { event?: string; debug?: string; executor?: string };
}

export interface AtLogEventResult {
  base_url: string;
  url: string;
  start_time: string;
  end_time: string;
  count: number;
  truncated: boolean;
  read_mode: string;
  components: string[];
  levels: string[];
  rows: AtLogEventRow[];
  raw_text: string;
}

export async function reportAtLogExcelImportDebug(payload: Record<string, unknown>): Promise<{ ok: boolean }> {
  return api<{ ok: boolean }>('/atlog-analysis/excel-import-debug/', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function analyzeAtLogCase(url: string, caseContext?: { case_id?: string; case_name?: string; case_description?: string }): Promise<AtLogCaseAnalysis> {
  return api<AtLogCaseAnalysis>('/atlog-analysis/analyze/', {
    method: 'POST',
    body: JSON.stringify({ url, ...(caseContext || {}) }),
  });
}

/** 报告文件类型：后端按扩展名 + 行结构判定。 */
export type AtLogReportFileKind =
  | 'junit_xml'
  | 'summary_xml'
  | 'pytest_html'
  | 'html'
  | 'cpd_report'
  | 'ini'
  | 'xytest_log'
  | 'debug_log'
  | 'event_log'
  | 'log'
  | 'text';

export interface AtLogReportFinding {
  time: string;
  level: string;
  message: string;
}

export type AtLogReportCell = string | number | boolean | null;

export interface AtLogReportSection {
  name?: string;
  title?: string;
  /** 键值型（ini 等）：[{key, value}] */
  rows?: Array<Record<string, AtLogReportCell>>;
  /** 表格型（xlsx 等）：列名 + 数据行 */
  columns?: string[];
  table?: AtLogReportCell[][];
  row_count?: number;
  truncated?: boolean;
}

/** 单个报告文件（.rpt/.xml/.html/.log/.ini…）的 URL 分析结果。 */
export interface AtLogReportFileAnalysis {
  url: string;
  file_name: string;
  kind: AtLogReportFileKind;
  case_id: string;
  line_count: number;
  char_count: number;
  findings: AtLogReportFinding[];
  summary: (Record<string, unknown> & {
    tests?: number;
    failures?: number;
    errors?: number;
    status?: string;
    failure?: { failure_message?: string } | null;
  }) | null;
  pytest: (Record<string, unknown> & {
    failure_message?: string;
    failure_text?: string;
    location?: AtLogCallFrame | null;
    call_chain?: AtLogCallFrame[];
  }) | null;
  cpd_report: (Record<string, unknown> & {
    cpd_name?: string;
    operator?: string;
    software_ver?: string;
    report_date?: string;
    report_time?: string;
    start_time?: string;
    stop_time?: string;
    execution_time?: string;
    measure_log?: string;
    test_run_result?: string;
    results_validation?: string;
    measurement_quality?: string;
    mcs_status?: string;
  }) | null;
  sections: AtLogReportSection[];
  assertion: string;
  text_excerpt: string;
  notes: string[];
  status: AtLogCaseStatus;
  conclusion: string;
}

/** 只给一个报告文件链接，直接解析这一份报告说了什么。 */
export async function analyzeAtLogReportFile(url: string, caseId?: string): Promise<AtLogReportFileAnalysis> {
  return api<AtLogReportFileAnalysis>('/atlog-analysis/analyze-report/', {
    method: 'POST',
    body: JSON.stringify({ url, ...(caseId ? { case_id: caseId } : {}) }),
  });
}

export async function getAtLogCaseSnapshot(url: string): Promise<AtLogSavedState | Record<string, never>> {
  const query = new URLSearchParams({ url });
  return api<AtLogSavedState | Record<string, never>>(`/atlog-analysis/snapshot/?${query.toString()}`);
}


export interface AtLogAnomalyRulePayload {
  id?: string;
  keyword: string;
  case_sensitive: boolean;
  whole_word: boolean;
  enabled: boolean;
}

export async function diagnoseAtLogCaseWithAi(payload: {
  url: string;
  messages?: AtLogCaseLogRow[];
  anomaly_rules?: AtLogAnomalyRulePayload[];
  case_context?: { case_id?: string; case_name?: string; case_description?: string };
}): Promise<AtLogAiDiagnosisResult> {
  return api<AtLogAiDiagnosisResult>('/atlog-analysis/ai-diagnose/', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function startAtLogAiDiagnosis(payload: {
  url: string;
  messages?: AtLogCaseLogRow[];
  anomaly_rules?: AtLogAnomalyRulePayload[];
  case_context?: { case_id?: string; case_name?: string; case_description?: string };
  force?: boolean;
}): Promise<AtLogAiDiagnosisJob> {
  return api<AtLogAiDiagnosisJob>('/atlog-analysis/ai-diagnose-start/', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function getAtLogAiDiagnosisStatus(jobId: string, afterSeq = 0): Promise<AtLogAiDiagnosisJob> {
  const query = new URLSearchParams({ job_id: jobId, after_seq: String(afterSeq) });
  return api<AtLogAiDiagnosisJob>(`/atlog-analysis/ai-diagnose-status/?${query.toString()}`);
}

export type AtLogAiDiagnosisStreamEvent =
  | { type: 'event'; event: AtLogAiJobEvent; job: AtLogAiDiagnosisJob }
  | { type: 'state'; job: AtLogAiDiagnosisJob }
  | { type: 'snapshot'; job: AtLogAiDiagnosisJob } // backward compatibility with older backends
  | { type: 'error'; message: string };

export async function streamAtLogAiDiagnosis(
  jobId: string,
  afterSeq: number,
  options: { signal?: AbortSignal; onEvent: (event: AtLogAiDiagnosisStreamEvent) => void },
): Promise<void> {
  const query = new URLSearchParams({ job_id: jobId, after_seq: String(afterSeq || 0) });
  const path = `/atlog-analysis/ai-diagnose-stream/?${query.toString()}`;
  const response = await fetch(`${API_BASE}${path}`, { method: 'GET', signal: options.signal });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.clone().json();
      message = body.message ?? body.detail ?? message;
    } catch {
      message = await response.text() || message;
    }
    throw new Error(message);
  }
  if (!response.body) throw new Error('浏览器未返回 ATLog AI SSE 响应流。');

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer = `${buffer}${decoder.decode(value, { stream: true })}`.replace(/\r\n/g, '\n');
    while (true) {
      const boundary = buffer.indexOf('\n\n');
      if (boundary < 0) break;
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const dataLines = frame.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart());
      if (!dataLines.length) continue;
      options.onEvent(JSON.parse(dataLines.join('\n')) as AtLogAiDiagnosisStreamEvent);
      await new Promise<void>((resolve) => {
        if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') window.requestAnimationFrame(() => resolve());
        else setTimeout(resolve, 0);
      });
    }
  }
}

export async function matchAtLogKnowledge(
  url: string,
  anomalyRules: Array<{ id?: string; keyword: string; case_sensitive: boolean; whole_word: boolean; enabled: boolean }> = [],
  limit = 5,
): Promise<AtLogKnowledgeMatchResult> {
  return api<AtLogKnowledgeMatchResult>('/atlog-analysis/knowledge-match/', {
    method: 'POST',
    body: JSON.stringify({ url, limit, anomaly_rules: anomalyRules }),
  });
}

export async function acceptAtLogAiDiagnosis(jobId: string | undefined, targetCaseId?: number, url?: string): Promise<AbnormalCase> {
  return api<AbnormalCase>('/atlog-analysis/ai-accept/', {
    method: 'POST',
    body: JSON.stringify({ job_id: jobId || '', ...(url ? { url } : {}), ...(targetCaseId ? { target_case_id: targetCaseId } : {}) }),
  });
}

export interface AtLogSearchProgress {
  operation_id: string;
  stage: string;
  percent: number;
  message?: string;
  current_file?: string;
  matched_files?: number;
  read_files?: number;
  total_files?: number;
  done?: boolean;
  cancel_requested?: boolean;
  cancelled?: boolean;
  error?: string;
  result?: AtLogCaseLogResult | null;
}

export async function queryAtLogCaseLogs(payload: {
  url: string;
  start_time?: string;
  end_time?: string;
  components?: string[];
  targets?: Array<{ subsystem: string; module: string }>;
  source_categories?: string[];
  levels?: string[];
  anomaly_rules?: AtLogAnomalyRulePayload[];
  keyword?: string;
  max_lines?: number;
  workspace_state?: Record<string, unknown>;
  signal?: AbortSignal;
  onStarted?: (operationId: string) => void;
  onProgress?: (progress: AtLogSearchProgress) => void;
}): Promise<AtLogCaseLogResult> {
  const { onProgress, onStarted, signal, ...requestPayload } = payload;
  const started = await api<{ operation_id: string }>('/atlog-analysis/logs-start/', {
    method: 'POST',
    body: JSON.stringify(requestPayload),
    signal,
  });
  const operationId = started.operation_id;
  onStarted?.(operationId);
  let latest: AtLogSearchProgress = { operation_id: operationId, stage: 'planning', percent: 3, message: '正在准备日志检索', done: false };
  while (true) {
    await new Promise<void>((resolve, reject) => {
      if (signal?.aborted) { reject(new DOMException('Aborted', 'AbortError')); return; }
      const timer = window.setTimeout(() => { signal?.removeEventListener('abort', onAbort); resolve(); }, 350);
      const onAbort = () => { window.clearTimeout(timer); signal?.removeEventListener('abort', onAbort); reject(new DOMException('Aborted', 'AbortError')); };
      signal?.addEventListener('abort', onAbort, { once: true });
    });
    latest = await api<AtLogSearchProgress>(`/atlog-analysis/logs-status/?operation_id=${encodeURIComponent(operationId)}`, { signal });
    onProgress?.(latest);
    if (latest.done) {
      if (latest.cancelled) throw new Error('日志检索已停止。');
      if (latest.error) throw new Error(latest.error);
      if (latest.result) return latest.result;
      throw new Error('日志检索已完成，但没有返回结果。');
    }
  }
}

export async function cancelAtLogCaseLogs(operationId: string): Promise<AtLogSearchProgress> {
  return api<AtLogSearchProgress>('/atlog-analysis/logs-cancel/', {
    method: 'POST',
    body: JSON.stringify({ operation_id: operationId }),
  });
}

export async function queryAtLogEvent(payload: {
  url: string;
  start_time?: string;
  end_time?: string;
  modules?: string[];
  levels?: string[];
  keyword?: string;
  max_lines?: number;
}): Promise<AtLogEventResult> {
  return api<AtLogEventResult>('/atlog-analysis/event/', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}


export async function browseAtLogCaseDirectory(payload: {
  url: string;
  relative_path?: string;
}): Promise<AtLogDirectoryResult> {
  return api<AtLogDirectoryResult>('/atlog-analysis/browse/', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function readAtLogCaseFile(payload: {
  url: string;
  relative_path: string;
  max_bytes?: number;
}): Promise<{ base_url: string; relative_path: string; url: string; content: string; size: number }> {
  return api('/atlog-analysis/read-file/', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}
