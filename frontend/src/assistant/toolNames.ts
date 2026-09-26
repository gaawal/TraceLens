/**
 * 原子工具的中文名。
 *
 * 后端 stream 里的 `trace.title` 已经是中文，但步骤上还会带一个 `tool_id`（英文原子名）——
 * 那是给人核对用的，不该直接顶在界面上。这里把 id 映射成中文名显示，
 * 英文 id 仍保留在悬停提示 / data 属性里，方便排查时对照。
 *
 * 与 backend/apps/tooling/registry.py 的 ToolDefinition(name=...) 保持一致；
 * 新加工具时在后端补 name 后，同步往这张表里加一行即可（查不到就回退显示 id，不会显示空白）。
 */
export const TOOL_DISPLAY_NAMES: Record<string, string> = {
  get_current_time: '获取当前时间',
  get_environment_info: '获取环境信息',
  get_log_catalog: '获取日志目录',
  resolve_environment_event: '解析环境事件码归属',
  resolve_log_components: '解析事件组件到日志模块',
  build_log_plan: '生成日志检索计划',
  query_logs: '查询日志',
  get_search_progress: '获取检索进度',
  recognize_semantic_source: '识别单条日志源码语义',
  recognize_semantic_sources_batch: '批量识别日志源码语义',
  get_log_time_range: '获取日志时间范围',
  search_errors: '检索错误日志',
  search_keyword: '检索日志关键字',
  get_log_context: '获取日志上下文',
  build_timeline: '构建结构化时间线',
  find_event_clusters: '查找异常事件簇',
  extract_process_events: '提取进程生命周期事件',
  get_related_modules: '获取关联模块',
  match_cases: '检索历史异常案例',
  analyze_atlog_case: '分析 ATLog 自动化用例',
  query_atlog_logs: '定向查询 ATLog debug 日志',
  query_atlog_event: '查询 ATLog event.log',
  list_environments: '列出环境',
  find_environment: '查找环境',
  get_environment_runtime_status: '获取环境运行状态',
  refresh_environment: '刷新环境拓扑',
  query_environment_version: '查询环境版本',
  get_deployment_defaults: '获取部署默认参数',
  preview_deployment: '预览部署',
  list_environment_deployments: '查看部署历史',
  get_latest_deployment: '获取最近部署',
  get_deployment_detail: '获取部署详情',
  get_active_deployments: '获取进行中部署',
  start_environment_deployment: '启动环境部署',
  stop_environment_deployment: '停止环境部署',
  retry_environment_deployment_step: '重试部署步骤',
  ensure_deployment_ssh_trust: '修复部署 SSH 互信',
  sync_deployment_time: '同步部署目标时间',
  resolve_environment_component: '精确解析组件日志目标',
  query_environment_logs: 'AI 定向读取环境日志',
  list_log_query_skills: '读取日志查询 Skill',
  match_log_query_skills: '匹配子系统日志查询 Skill',
  create_log_query_skill: '创建日志查询 Skill',
  query_log_query_skill_step: '执行日志查询 Skill 步骤',
  list_log_semantic_rules: '读取日志语义与标签规则',
  set_log_semantic_labels: '切换日志语义标签',
  create_log_semantic_rule: '创建日志语义规则',
  bulk_generate_log_rules: '批量生成日志语义/标签规则',
  create_log_anomaly_rule: '创建日志异常规则',
  list_data_extraction_rules: '读取数据提取能力',
  create_data_extraction_capability: '创建数据提取能力',
  run_data_extraction: '从当前日志提取数据',
  open_log_rule_settings: '打开日志规则配置',
  open_workspace_page: '打开功能页面',
  open_environment_page: '打开环境页面',
  open_log_locator: '打开日志定位',
};

/** 工具 id → 中文名；没登记过就原样返回 id（绝不显示空白）。 */
export function toolDisplayName(toolId?: string | null): string {
  const id = String(toolId || '').trim();
  if (!id) return '';
  return TOOL_DISPLAY_NAMES[id] || id;
}
