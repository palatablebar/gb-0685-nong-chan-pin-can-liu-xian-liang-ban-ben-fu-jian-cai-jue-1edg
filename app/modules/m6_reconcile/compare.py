from collections import defaultdict
RANK={'PASS':0,'UNCERTAIN':1,'FAIL':2}

def balance(state):
    accepted=len(state['rows']);isolated=len(state['quarantine']);accounted=accepted+isolated+state['blank_count']+state['header_count']+state['duplicate_count']
    # 判级与计数只读重传裁决后的可发布批次，而不是原始计算清单。
    calculations=state.get('calculations') or []
    batches=defaultdict(list)
    for r in calculations:batches[r['batch']].append(r)
    grades=[{'batch':b,'code':max((r['code'] for r in rows),key=lambda c:RANK[c]),'projects':len(rows),'samples':len({r['sample_id'] for r in rows})} for b,rows in sorted(batches.items())]
    ledger=state.get('ledger') or {}
    return {'input':state['input_count'],'accepted':accepted,'isolated':isolated,'blank':state['blank_count'],'headers':state['header_count'],'duplicates':state['duplicate_count'],'accounted':accounted,'balanced':accounted==state['input_count'],'business':state['business'],'grades':grades,
        'projects':len(calculations),'samples':len({r['sample_id'] for r in calculations}),
        'publishable':bool(ledger.get('publishable')),'held':ledger.get('held_count',0)}
