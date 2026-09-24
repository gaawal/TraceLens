from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_langgraph_ai_diagnosis_is_wired_as_bounded_state_machine():
    source = (ROOT / "apps" / "atlog" / "ai_agent.py").read_text(encoding="utf-8")
    assert "StateGraph(AiDiagnosisState)" in source
    assert 'graph.add_node("context"' in source
    assert 'graph.add_node("triage"' in source
    assert 'graph.add_node("retrieve"' in source
    assert 'graph.add_node("review"' in source
    assert 'graph.add_node("report"' in source
    assert 'int(state.get("retrieval_round") or 0) < 2' in source
    assert 'config={"recursion_limit": 12}' in source
    assert "ChatOpenAI" not in source
    assert "get_llm_client().chat(messages)" in source


def test_ai_provider_configuration_stays_server_side_and_frontend_has_ai_button():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    settings = (ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    frontend = (ROOT.parent / "frontend" / "src" / "components" / "AtLogAnalysisPage.tsx").read_text(encoding="utf-8")
    api = (ROOT.parent / "frontend" / "src" / "api" / "resourceApi.ts").read_text(encoding="utf-8")
    assert "TRACELENS_AI_BASE_URL=http://xxxx:3000/v1" in env_example
    assert "TRACELENS_AI_API_KEY=xxxx" in env_example
    assert 'os.getenv("TRACELENS_AI_API_KEY"' in settings
    assert "TRACELENS_AI_API_KEY" not in frontend
    assert "AI 诊断" in frontend
    assert "startAtLogAiDiagnosis" in frontend
    assert "AiDiagnosisConversation" in frontend
    assert "诊断结论" in frontend
    assert "保存案例" in frontend
    assert "Token：" in frontend
    assert "'/atlog-analysis/ai-diagnose-start/'" in api
    assert "/atlog-analysis/ai-diagnose-status/" in api
    assert "'/atlog-analysis/knowledge-match/'" in api
    assert "'/atlog-analysis/ai-accept/'" in api


def test_llm_transport_matches_known_good_openai_compatible_shape():
    # apps/atlog/llm_client.py was an unimported wrapper and has been removed; the live
    # transport is apps/tooling/llm/client.py.
    llm_source = (ROOT / "apps" / "tooling" / "llm" / "client.py").read_text(encoding="utf-8")
    settings_source = (ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert 'base_url=base_url' in llm_source
    assert 'api_key=api_key' in llm_source
    assert '"model":' in llm_source
    assert '"messages": messages' in llm_source
    assert 'kwargs["proxy"] = proxy' in llm_source
    assert '"trust_env": use_env_proxy' in llm_source
    assert 'TRACELENS_AI_TIMEOUT = float(os.getenv("TRACELENS_AI_TIMEOUT", "600"))' in settings_source
    assert 'TRACELENS_AI_PROXY = os.getenv("TRACELENS_AI_PROXY", "").strip()' in settings_source
    assert 'TRACELENS_AI_USE_ENV_PROXY' in settings_source
    assert 'TRACELENS_AI_TIMEOUT=600' in env_example
    assert 'TRACELENS_AI_PROXY=' in env_example
    assert 'TRACELENS_AI_USE_ENV_PROXY=false' in env_example


def test_minimal_ai_connectivity_probe_is_packaged():
    probe = (ROOT / "examples" / "ai_connection_test.py").read_text(encoding="utf-8")
    assert 'messages=[{"role": "user", "content": "只回复 OK"}]' in probe
    assert 'client.chat.completions.create(' in probe
    assert 'temperature' not in probe


def test_ai_diagnosis_jobs_expose_public_progress_and_persist_server_side():
    jobs = (ROOT / "apps" / "atlog" / "ai_jobs.py").read_text(encoding="utf-8")
    agent = (ROOT / "apps" / "atlog" / "ai_agent.py").read_text(encoding="utf-8")
    assert "start_diagnosis_job" in jobs
    assert "get_diagnosis_job" in jobs
    assert "current_stage" in jobs
    assert "token_usage" in jobs
    assert "progress_callback" in agent
    assert "_emit_progress" in agent
    assert '"Context Agent"' in agent
    assert '"Component Resolver"' in agent
    assert '"Component Decision Agent"' in agent
    assert '"Log Evidence Agent"' in agent
    assert '"Evidence Gate"' in agent
    assert '"Report Agent"' in agent


def test_ai_knowledge_acceptance_only_uses_verified_log_evidence():
    bridge = (ROOT / "apps" / "atlog" / "knowledge_bridge.py").read_text(encoding="utf-8")
    agent = (ROOT / "apps" / "atlog" / "ai_agent.py").read_text(encoding="utf-8")
    evidence = (ROOT / "apps" / "knowledge" / "evidence.py").read_text(encoding="utf-8")
    assert "_verified_evidence_rows" in agent
    assert 'verified_rows = result.get("_verified_evidence_rows")' in bridge
    assert "build_ai_case_evidences" in bridge
    assert '"source": "atlog_ai_diagnosis"' in bridge
    assert "match_case_knowledge" in bridge
    assert "target_case_id" in bridge
    assert '"AI补充现场"' in bridge
    assert '"feature_groups": groups' in bridge
    # 采纳时仍然只保存可回链的真实证据行；但不再用「必须有运行日志异常证据」
    # 这条硬校验把用例报告执行类案例挡在知识库外 —— 那类案例本来就没有日志行。
    # 「能不能参与指纹比对」改为写在每条举证上的 matchable 质量信号。
    assert "缺少可回链的运行日志异常证据" not in bridge
    assert "require_runtime" in bridge
    assert "def is_matchable(" in evidence
    assert "def resolve_evidence_kind(" in evidence
    assert "EVIDENCE_KIND_CASE_REPORT" in evidence


def test_ai_agent_resolves_db_components_then_extracts_targeted_debug_evidence():
    agent = (ROOT / "apps" / "atlog" / "ai_agent.py").read_text(encoding="utf-8")
    registry = (ROOT / "apps" / "tooling" / "registry.py").read_text(encoding="utf-8")
    tooling = (ROOT / "apps" / "tooling" / "services.py").read_text(encoding="utf-8")
    atlog = (ROOT / "apps" / "atlog" / "services.py").read_text(encoding="utf-8")
    assert 'graph.add_node("component_map"' in agent
    assert 'graph.add_edge("context", "component_map")' in agent
    assert 'graph.add_conditional_edges("component_map", _route_after_component_map' in agent
    assert 'invoke_tool("resolve_log_components"' in agent
    assert 'invoke_tool("query_atlog_logs"' in agent
    assert 'invoke_tool("search_errors"' not in agent
    assert 'invoke_tool("search_keyword"' not in agent
    assert 'invoke_tool("get_log_context"' not in agent
    assert '"anomaly_rules": active_rules' in agent
    assert '"include_event": False' in agent
    assert 'id="resolve_log_components"' in registry
    assert 'id="query_atlog_logs"' in registry
    assert 'def resolve_log_components(' in tooling
    assert 'LogFmDefinition.objects.filter(enabled=True' in tooling
    assert 'LogSubsystemDefinition.objects.filter(enabled=True)' in tooling
    assert 'def query_case_logs_tool(' in atlog
    assert 'targets=payload.get("targets") or []' in atlog
    assert 'TRACELENS_AI_MAX_EVIDENCE_NODES' in agent
    assert 'TRACELENS_AI_MAX_EVIDENCE_CHARS' in agent


def test_ai_agent_emits_public_thinking_reason_stream():
    agent = (ROOT / "apps" / "atlog" / "ai_agent.py").read_text(encoding="utf-8")
    frontend = (ROOT.parent / "frontend" / "src" / "components" / "AtLogAnalysisPage.tsx").read_text(encoding="utf-8")
    api = (ROOT.parent / "frontend" / "src" / "api" / "resourceApi.ts").read_text(encoding="utf-8")
    assert "def _emit_thinking(" in agent
    assert '"thinking"' in agent
    assert "决定优先补查" in agent
    assert "Evidence Gate" in agent
    assert "异常规则命中" in agent
    assert "GrowingTypewriterText" in frontend
    assert "compactReasonTranscript" in frontend
    assert "atlog-ai-process-toggle" in frontend
    assert "aria-expanded={processOpen}" in frontend
    assert 'LoaderCircle className="spin"' in frontend
    assert "event.type === 'thinking'" in frontend
    assert "'thinking'" in api


def test_ai_ui_is_compact_conversation_and_hides_nonessential_report_sections():
    frontend = (ROOT.parent / "frontend" / "src" / "components" / "AtLogAnalysisPage.tsx").read_text(encoding="utf-8")
    agent = (ROOT / "apps" / "atlog" / "ai_agent.py").read_text(encoding="utf-8")
    assert "AiDiagnosisConversation" in frontend
    assert "案例库已找到相似问题" in frontend
    assert "evidence_preview" in frontend
    for obsolete in ("处理建议</strong>", "下一步确认</strong>", "边界与排除项</strong>", "组件决策路径</strong>", "Thinking / 实时诊断过程"):
        assert obsolete not in frontend
    assert "recommendations" in agent
    assert "处理建议不得伪装成事实证据" in agent


def test_root_cause_component_table_has_priority_over_llm_component_decision():
    models = (ROOT / "apps" / "logsources" / "models.py").read_text(encoding="utf-8")
    tooling = (ROOT / "apps" / "tooling" / "services.py").read_text(encoding="utf-8")
    agent = (ROOT / "apps" / "atlog" / "ai_agent.py").read_text(encoding="utf-8")
    urls = (ROOT / "config" / "urls.py").read_text(encoding="utf-8")
    frontend = (ROOT.parent / "frontend" / "src" / "components" / "LogCatalogSettingsPage.tsx").read_text(encoding="utf-8")
    assert "class RootCauseComponentMapping" in models
    assert 'event_component = models.CharField("Event组件名"' in models
    assert "RootCauseComponentMapping.objects.filter(enabled=True)" in tooling
    assert '"manual_targets"' in tooling
    assert "def _route_after_component_map" in agent
    assert 'return "retrieve" if state.get("manual_component_targets")' in agent
    assert 'remaining = [] if state.get("manual_component_targets")' in agent
    assert 'router.register("root-cause-components"' in urls
    assert "根因组件表" in frontend
    assert "Event组件名" in frontend
    assert 'modules = models.ManyToManyField(' in models
    assert '.prefetch_related("modules")' in tooling
    assert 'target_modules = list(row.modules.all())' in tooling
    assert 'HierarchyModuleSelect catalog={rootModuleCatalog}' in frontend
    assert 'moduleNameOnly' in frontend
    assert 'selected={rootModuleSelection}' in frontend
    assert 'onChange={setRootModuleSelection}' in frontend
    assert 'ROOT CAUSE COMPONENT MAP' not in frontend
    assert '人工确认 Event 组件名对应的 debug 子系统/模块' not in frontend


def test_root_cause_multi_module_serializer_does_not_require_legacy_module_field():
    serializers = (ROOT / "apps" / "logsources" / "serializers.py").read_text(encoding="utf-8")
    start = serializers.index("class RootCauseComponentMappingSerializer")
    end = serializers.index("class LogFmTargetSerializer", start)
    source = serializers[start:end]
    assert "module = serializers.PrimaryKeyRelatedField(" in source
    assert "required=False" in source
    assert 'attrs["module"] = selected_modules[0]' in source


def test_atlog_ai_result_uses_redis_scene_cache_and_thinking_snapshot():
    jobs = (ROOT / "apps" / "atlog" / "ai_jobs.py").read_text(encoding="utf-8")
    settings_source = (ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    frontend = (ROOT.parent / "frontend" / "src" / "components" / "AtLogAnalysisPage.tsx").read_text(encoding="utf-8")
    assert "RedisLogStore.get_json(cache_key)" in jobs
    assert "RedisLogStore.set_json(cache_key, cache_payload, ttl)" in jobs
    assert "_diagnosis_cache_identity" in jobs
    assert '"failure_time"' in jobs and '"assertion"' in jobs
    assert '"thinking_text"' in jobs
    assert "TRACELENS_ATLOG_AI_CACHE_TTL" in settings_source
    assert "TRACELENS_ATLOG_AI_CACHE_TTL=604800" in env_example
    assert "job?.thinking_text" in frontend
    assert "force = Boolean(aiResult)" in frontend


def test_atlog_ai_uses_only_user_anomaly_rules_for_debug_evidence():
    agent = (ROOT / "apps" / "atlog" / "ai_agent.py").read_text(encoding="utf-8")
    atlog = (ROOT / "apps" / "atlog" / "services.py").read_text(encoding="utf-8")
    jobs = (ROOT / "apps" / "atlog" / "ai_jobs.py").read_text(encoding="utf-8")
    frontend = (ROOT.parent / "frontend" / "src" / "components" / "AtLogAnalysisPage.tsx").read_text(encoding="utf-8")
    api = (ROOT.parent / "frontend" / "src" / "api" / "resourceApi.ts").read_text(encoding="utf-8")
    assert "normalize_anomaly_rules" in agent
    assert "matching_compiled_anomaly_rules" in atlog
    assert 'parsed["matched_anomaly_rules"]' in atlog
    assert "search_errors" not in agent
    assert 'invoke_tool("search_keyword"' not in agent
    assert '"anomaly_rules": anomaly_rule_identity(anomaly_rules or [])' in jobs
    assert "currentAtLogAiAnomalyRules" in frontend
    assert "anomaly_rules: aiAnomalyRules" in frontend
    assert "AtLogAnomalyRulePayload" in api
    assert "read_url_log_window(artifact, start, end" in atlog
    assert "不会使用任何内置关键字兜底" in agent


def test_evidence_review_is_deterministic_to_reduce_llm_calls():
    agent = (ROOT / "apps" / "atlog" / "ai_agent.py").read_text(encoding="utf-8")
    assert 'agent="Evidence Gate"' in agent
    assert "REVIEW_SYSTEM" not in agent
    assert "Deterministic: anomaly-rule evidence gate (0 LLM tokens)" in agent


def test_ai_uses_case_intent_report_evidence_and_opens_editor_before_case_save():
    agent = (ROOT / "apps" / "atlog" / "ai_agent.py").read_text(encoding="utf-8")
    jobs = (ROOT / "apps" / "atlog" / "ai_jobs.py").read_text(encoding="utf-8")
    serializer = (ROOT / "apps" / "knowledge" / "serializers.py").read_text(encoding="utf-8")
    frontend = (ROOT.parent / "frontend" / "src" / "components" / "AtLogAnalysisPage.tsx").read_text(encoding="utf-8")
    editor = (ROOT.parent / "frontend" / "src" / "components" / "AbnormalCaseEditorDialog.tsx").read_text(encoding="utf-8")
    assert '"case_description"' in agent
    assert '"case_context"' in jobs
    assert "_case_context_catalog_candidates" in agent
    assert "_case_report_evidence_rows" in agent
    assert '"case_report_evidence"' in agent and '"runtime_evidence"' in agent
    assert '"case_evidences"' in agent and '"case_draft"' in agent
    # 「哪些来源算用例报告证据」只有一处定义：apps/knowledge/evidence.py。
    # 序列化器不再自己维护一份副本（两份副本正是「AI 草稿永远存不进案例库」的成因之一）。
    evidence = (ROOT / "apps" / "knowledge" / "evidence.py").read_text(encoding="utf-8")
    assert 'REPORT_SOURCE_CATEGORIES = {"case_report", "pytest", "xytest", "case-metadata", "case_metadata"}' in evidence
    assert "normalize_evidence_item" in serializer
    assert "evidence_text" in serializer
    assert "AbnormalCaseEditorDialog" in frontend
    assert "setCaseEditorOpen(true)" in frontend
    assert "presetEvidences={aiResult.case_evidences || []}" in frontend
    assert "initialDraft" in editor and "presetEvidences" in editor
    assert "只有点击保存后才会写入案例库" not in editor
    assert "ABNORMAL KNOWLEDGE" not in editor

def test_ai_selected_targets_are_persisted_into_case_workspace():
    snapshots = (ROOT / "apps" / "atlog" / "snapshots.py").read_text(encoding="utf-8")
    frontend = (ROOT.parent / "frontend" / "src" / "components" / "AtLogAnalysisPage.tsx").read_text(encoding="utf-8")
    assert "def _ai_selected_target_keys" in snapshots
    assert 'workspace_state["selectedTargets"] = selected_target_keys' in snapshots
    assert 'workspace_state["selectedTargetSource"] = "ai"' in snapshots
    assert "aiSelectedTargets={aiResult?.selected_targets}" in frontend
    assert "atLogAiTargetKeys(aiSelectedTargets)" in frontend
    assert "setSelectedTargets(next)" in frontend
    assert "aiRefreshToken" in frontend
    assert "nextSources.add('debug')" in frontend
    assert "void runQuery(nextTargets, nextSources)" in frontend
    assert "ai_evidence_revision" in frontend

