from apps.tooling.evidence import LogAnchor, merge_anchor_intervals, build_evidence_pack, structured_diagnosis

def test_source_and_window_safe_union():
    anchors = [LogAnchor('debug',100,'error','t','a'),LogAnchor('debug',180,'error','t','a'),LogAnchor('event',100,'error','t','a'),LogAnchor('debug',180,'error','t','b'),LogAnchor('debug',180,'error','t','a','window','second')]
    result = merge_anchor_intervals(anchors)
    assert len(result)==4
    assert (result[0]['start_line'],result[0]['end_line'])==(1,280)
    assert len(result[0]['anchors'])==2

def test_union_keeps_context_and_no_duplicate_rows():
    rows = [{'source_kind':'debug','source_path':'a','line_number':i,'message':str(i),'level':'ERROR' if i in (101,150) else 'INFO'} for i in range(1,251)]
    pack = build_evidence_pack(rows,max_chars=200000)
    assert len(pack['fragments'])==1
    assert len(pack['fragments'][0]['rows'])==250
    assert not pack['missing_context']
    assert len({r['evidence_id'] for r in pack['fragments'][0]['rows']})==250

def test_sparse_lines_and_missing_trace_are_not_fabricated():
    pack = build_evidence_pack([{'source_path':'pytest.xml','component':'pytest','line_number':47,'message':'assert False'}])
    assert pack['anchors'][0]['source']=='pytest'
    assert pack['missing_context']
    assert pack['anchors'][0]['trace_id']==''

def test_budget_is_bounded_and_explicit():
    rows = [{'source_path':'x','line':i,'level':'ERROR','message':'a'*500} for i in range(1,201)]
    pack=build_evidence_pack(rows,max_lines=5,max_chars=3000)
    assert pack['rows_included']<=5
    assert pack['truncated']

def test_citations_require_exact_original_evidence():
    result = structured_diagnosis({'root_cause':'test','confidence':99,'evidence':[{'source':'real','message':'invented'}]},[{'source_path':'real','message':'observed'}])
    assert result['evidence']==[] and result['confidence']<=40
    assert result['unverified_citation_count']==1
