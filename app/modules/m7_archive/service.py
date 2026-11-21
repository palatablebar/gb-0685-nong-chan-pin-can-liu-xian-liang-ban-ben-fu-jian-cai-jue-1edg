from app.modules.m7_archive.export_out import checksum

def execute(state):
    ledger=state.get('ledger') or {}
    reconcile=state.get('reconcile') or {}
    # 封存证据：可发布批次结论、隔离报告与逐条重传裁决；重传裁决本身也是证据。
    evidence=len(state.get('calculations',[]))+len(state['quarantine'])+len(ledger.get('decisions') or [])
    archive={'sha256':'','evidence_count':evidence,
        'ledger_count':len(ledger.get('decisions') or []),
        # 只有对账相符且重传裁决可发布时才允许封存出证。
        'ready':bool(reconcile.get('balanced') and ledger.get('publishable'))}
    state['archive']=archive
    archive['sha256']=checksum(state)
    return state
