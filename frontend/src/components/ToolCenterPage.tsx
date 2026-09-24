import { registerPageContextReader } from '../assistant/contextRegistry';
import { useEffect, useMemo, useState } from 'react';
import { Braces, ChevronDown, ChevronRight, CircleCheck, Search, ShieldCheck, Wrench } from 'lucide-react';
import { listTraceLensTools, type TraceLensToolDefinition } from '../api/resourceApi';

function schemaFieldSummary(name: string, schema: Record<string, unknown>, required: boolean): string {
  const typeValue = schema.type;
  const type = Array.isArray(typeValue) ? typeValue.join(' | ') : String(typeValue || 'any');
  const description = String(schema.description || '').trim();
  const defaultValue = Object.prototype.hasOwnProperty.call(schema, 'default') ? ` · 默认 ${JSON.stringify(schema.default)}` : '';
  return `${name}${required ? ' *' : ''} · ${type}${description ? ` · ${description}` : ''}${defaultValue}`;
}

function ToolSchema({ tool }: { tool: TraceLensToolDefinition }) {
  const properties = (tool.input_schema?.properties || {}) as Record<string, Record<string, unknown>>;
  const required = new Set(Array.isArray(tool.input_schema?.required) ? tool.input_schema.required.map(String) : []);
  const fields = Object.entries(properties);
  return (
    <div className="tool-schema-panel">
      <div className="tool-schema-title"><Braces size={14}/> 输入参数</div>
      {fields.length ? (
        <div className="tool-schema-fields">
          {fields.map(([name, schema]) => <code key={name}>{schemaFieldSummary(name, schema, required.has(name))}</code>)}
        </div>
      ) : <div className="tool-schema-empty">无输入参数</div>}
      <div className="tool-schema-meta">
        <span>Atomic ID：<code>{tool.atomic_id || tool.id}</code></span>
        <span>兼容 Tool ID：<code>{tool.id}</code></span>
        <span>实现：<code>{tool.implementation || '-'}</code></span>
        {tool.endpoint_template && <span>接口：<code>{tool.endpoint_template}</code></span>}
      </div>
    </div>
  );
}

export function ToolCenterPage() {
  const [tools, setTools] = useState<TraceLensToolDefinition[]>([]);
  const [query, setQuery] = useState('');
  const [domain, setDomain] = useState('全部');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let alive = true;
    setLoading(true);
    listTraceLensTools()
      .then((payload) => {
        if (!alive) return;
        setTools(payload.tools || []);
        setError('');
      })
      .catch((cause) => {
        if (!alive) return;
        setError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => { alive = false; };
  }, []);

  const domains = useMemo(() => ['全部', ...Array.from(new Set(tools.map((tool) => tool.domain || 'general'))).sort()], [tools]);
  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return tools.filter((tool) => {
      if (domain !== '全部' && tool.domain !== domain) return false;
      if (!keyword) return true;
      return [tool.id, tool.atomic_id, tool.name, tool.description, tool.category, tool.domain, tool.kind, ...tool.skills, ...tool.tags]
        .some((value) => String(value).toLowerCase().includes(keyword));
    });
  }, [tools, query, domain]);

  const readyCount = tools.filter((tool) => tool.status === 'ready').length;
  const agentCount = tools.filter((tool) => tool.agent_available).length;
  const readOnlyCount = tools.filter((tool) => tool.read_only).length;

  function toggleExpanded(id: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }


  useEffect(() => {
    const handler = (event: Event) => {
      const custom = event as CustomEvent<{ context?: Record<string, unknown> }>;
      const context = custom.detail?.context;
      if (!context) return;
      const opened = filtered.find((tool) => expanded.has(tool.id));
      context.page = 'tools';
      context.page_label = '能力中心';
      context.tool_center = {
        query,
        domain,
        tool_count: tools.length,
        visible_tool_count: filtered.length,
        ready_count: readyCount,
        agent_available_count: agentCount,
        read_only_count: readOnlyCount,
        expanded_capability: opened ? {
          tool_id: opened.id,
          atomic_id: opened.atomic_id || opened.id,
          name: opened.name,
          domain: opened.domain,
          kind: opened.kind,
          skills: opened.skills,
          risk_level: opened.risk_level,
          read_only: opened.read_only,
          description: opened.description,
        } : null,
      };
    };
    return registerPageContextReader(handler, 10);
  }, [query, domain, tools, filtered, expanded, readyCount, agentCount, readOnlyCount]);

  return (
    <main className="tool-center-page">
      <section className="tool-center-hero">
        <div>
          <div className="tool-center-eyebrow"><Wrench size={15}/> 原子能力</div>
          <h1>能力中心</h1>
          <p>TracePilot 与页面操作共用同一套原子能力注册表。</p>
        </div>
        <div className="tool-center-summary">
          <div><strong>{tools.length}</strong><span>原子能力</span></div>
          <div><strong>{readyCount}</strong><span>当前可用</span></div>
          <div><strong>{agentCount}</strong><span>Agent 可发现</span></div>
          <div><strong>{readOnlyCount}</strong><span>只读工具</span></div>
        </div>
      </section>

      <section className="tool-center-toolbar">
        <label className="tool-center-search"><Search size={15}/><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索能力、Atomic ID、领域或 Skill" /></label>
        <div className="tool-category-tabs">
          {domains.map((item) => <button type="button" key={item} className={domain === item ? 'active' : ''} onClick={() => setDomain(item)}>{item}</button>)}
        </div>
      </section>

      {loading && <div className="tool-center-state">正在读取 Tool Registry…</div>}
      {!loading && error && <div className="tool-center-state error">工具注册表读取失败：{error}</div>}
      {!loading && !error && (
        <section className="tool-list">
          {filtered.map((tool) => {
            const isExpanded = expanded.has(tool.id);
            return (
              <article className="tool-card" key={tool.id}>
                <button type="button" className="tool-card-main" onClick={() => toggleExpanded(tool.id)}>
                  <span className="tool-card-expand">{isExpanded ? <ChevronDown size={17}/> : <ChevronRight size={17}/>}</span>
                  <span className="tool-card-identity">
                    <span className="tool-card-name-row"><strong>{tool.name}</strong><code>{tool.atomic_id || tool.id}</code></span>
                    <span className="tool-card-description">{tool.description}</span>
                    <span className="tool-card-tags">
                      <em>{tool.domain}</em><em>{tool.kind}</em>
                      {tool.skills.map((skill) => <em key={`skill-${skill}`}>{skill}</em>)}
                    </span>
                  </span>
                  <span className="tool-card-statuses">
                    <span className="tool-status ready"><CircleCheck size={13}/> {tool.status === 'ready' ? '可用' : tool.status}</span>
                    <span className="tool-status"><ShieldCheck size={13}/> {tool.read_only ? '只读' : tool.risk_level}</span>
                    <span className="tool-status">{tool.transport === 'stream' ? '流式' : 'JSON'}</span>
                  </span>
                </button>
                {isExpanded && <ToolSchema tool={tool}/>} 
              </article>
            );
          })}
          {!filtered.length && <div className="tool-center-state">没有匹配的工具。</div>}
        </section>
      )}
    </main>
  );
}
