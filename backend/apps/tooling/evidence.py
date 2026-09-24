"""Source-aware anchor unions. Missing context is explicit, never invented."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Literal

@dataclass(frozen=True)
class LogAnchor:
    source: Literal['pytest', 'xytest', 'event', 'debug']
    line: int
    reason: str
    timestamp: str
    source_path: str
    coordinate: str = 'file'
    window_id: str = ''
    trace_id: str = ''
    error_code: str = ''
    component: str = ''


def merge_anchor_intervals(anchors, radius=100):
    if not 0 <= radius <= 100: raise ValueError('上下文半径必须为 0..100')
    groups = defaultdict(list)
    for anchor in anchors:
        if anchor.line < 1: raise ValueError('行号从 1 开始')
        groups[(anchor.source,anchor.source_path,anchor.coordinate,anchor.window_id)].append(anchor)
    result = []
    for key, values in groups.items():
        merged = []
        for anchor in sorted(values,key=lambda a:a.line):
            start,end = max(1,anchor.line-radius), anchor.line+radius
            if merged and start <= merged[-1]['end_line']+1:
                merged[-1]['end_line'] = max(merged[-1]['end_line'],end)
                merged[-1]['anchors'].append(asdict(anchor))
            else:
                merged.append({'source':key[0],'source_path':key[1],'coordinate':key[2],'window_id':key[3], 'start_line':start,'end_line':end,'anchors':[asdict(anchor)]})
        result.extend(merged)
    return result


def source_kind(row):
    source = str(row.get('source_kind') or row.get('component') or row.get('source_path') or '').lower()
    for kind in ('pytest','xytest','event'):
        if kind in source: return kind
    return 'debug'


def build_evidence_pack(rows, *, max_lines=500, max_chars=16000):
    normalized, anchors = [], []
    for index,row in enumerate(rows):
        path = str(row.get('source_path') or row.get('source') or 'unknown')
        kind = source_kind(row)
        number = row.get('line_number') or row.get('line')
        valid_line = isinstance(number,int) and not isinstance(number,bool) and number > 0
        coordinate = str(row.get('line_number_kind') or ('reported' if valid_line else 'sample'))
        line = number if valid_line else index+1
        window_id = str(row.get('window_id') or row.get('retrieval_id') or '')
        text = str(row.get('raw') or row.get('message') or '')
        reason = str(row.get('evidence_reason') or '')
        if not reason and row.get('matched_anomaly_rules'): reason = '异常规则命中'
        if not reason and (str(row.get('level','')).upper() in {'ERROR','FATAL','CRITICAL','ALARM'} or re.search(r'\b(assert(?:ionerror)?|failed?|error|exception|timeout)\b',text,re.I)): reason = f'{kind} 异常/失败点'
        if not reason and kind == 'xytest' and re.search(r'\b(start|end|step|checkpoint)\b|开始|结束|关键节点',text,re.I): reason = 'xytest 过程节点'
        if not reason and row.get('evidence_selected'): reason = '用户/规则选中证据'
        value = {**row, 'source_kind':kind,'source_path':path,'line_number':line,'line_number_kind':coordinate,'window_id':window_id}
        value['evidence_id'] = hashlib.sha256(json.dumps([kind,path,coordinate,window_id,line,text],ensure_ascii=False).encode()).hexdigest()[:24]
        normalized.append(value)
        if reason:
            anchors.append(LogAnchor(kind,line,reason,str(row.get('time') or row.get('timestamp') or ''),path,coordinate,window_id,str(row.get('trace_id') or row.get('traceId') or ''),str(row.get('error_code') or row.get('display_code') or ''),str(row.get('component') or '')))
    intervals = merge_anchor_intervals(anchors)
    # Fairly distribute a bounded evidence budget across sources/windows.
    remaining_lines, remaining_chars = max_lines,max_chars
    fragments = []
    for i, interval in enumerate(intervals):
        candidates = [r for r in normalized if r['source_kind']==interval['source'] and r['source_path']==interval['source_path'] and r['line_number_kind']==interval['coordinate'] and r['window_id']==interval['window_id'] and interval['start_line']<=r['line_number']<=interval['end_line']]
        unique = {r['evidence_id']:r for r in candidates}
        candidates = list(unique.values())
        candidates.sort(key=lambda r:min(abs(r['line_number']-a['line']) for a in interval['anchors']))
        line_budget = max(0,remaining_lines//max(1,len(intervals)-i))
        char_budget = max(0,remaining_chars//max(1,len(intervals)-i))
        selected, consumed = [],0
        for row in candidates:
            text = str(row.get('raw') or row.get('message') or '')
            size = len(json.dumps(row,ensure_ascii=False,default=str))
            if len(selected)>=line_budget or consumed+size>char_budget: continue
            selected.append(row); consumed += size
        selected.sort(key=lambda r:r['line_number'])
        remaining_lines -= len(selected); remaining_chars -= consumed
        times = [str(r.get('time') or r.get('timestamp')) for r in selected if r.get('time') or r.get('timestamp')]
        expected = interval['end_line']-interval['start_line']+1
        fragments.append({**interval,'rows':selected,'time_start':min(times) if times else None,'time_end':max(times) if times else None,
                          'available_lines':len(candidates),'missing_context':len({r['line_number'] for r in candidates})<expected,
                          'truncated':len(selected)<len(candidates),'coverage':'available_rows_only'})
    timeline = [{'source':a.source,'source_path':a.source_path,'line':a.line,'timestamp':a.timestamp,'reason':a.reason,'trace_id':a.trace_id,'component':a.component} for a in anchors]
    def ts_key(item):
        try: return datetime.fromisoformat(item['timestamp'].replace('Z','+00:00')).timestamp()
        except (ValueError,TypeError): return float('inf')
    timeline.sort(key=ts_key)
    return {'schema_version':1,'radius':100,'anchors':[asdict(a) for a in anchors], 'fragments':fragments,'timeline':timeline,
            'line_budget':max_lines,'row_char_budget':max_chars,'rows_included':max_lines-remaining_lines,
            'missing_context':any(f['missing_context'] for f in fragments),'truncated':any(f['truncated'] for f in fragments)}


def structured_diagnosis(report, rows, targets=()):
    """Only retain citations found in source rows. Unverified model citations are dropped."""
    verified = []
    for cited in report.get('evidence') or []:
        if not isinstance(cited,dict): continue
        match = next((r for r in rows if str(r.get('source_path') or r.get('source') or '') == str(cited.get('source') or '') and str(r.get('message') or r.get('raw') or '')[:1800] == str(cited.get('message') or '')),None)
        if match: verified.append({**cited,'line':match.get('line_number') or match.get('line'), 'trace_id':match.get('trace_id') or '', 'error_code':match.get('error_code') or match.get('display_code') or ''})
    first = targets[0] if targets else {}
    try: confidence = max(0,min(100,int(report.get('confidence') or 0)))
    except (TypeError,ValueError): confidence = 0
    return {**report,'schema_version':1,'root_cause':str(report.get('root_cause') or ''),
            'subsystem':str(first.get('subsystem') or ''),'module':str(first.get('module') or ''),
            'evidence':verified,'timeline':build_evidence_pack(rows)['timeline'],
            'confidence':min(confidence,40) if not verified else confidence,'suggestion':report.get('recommendations') or [],
            'unverified_citation_count':len(report.get('evidence') or [])-len(verified)}
