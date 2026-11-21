import io,csv,json,hashlib

def bundle(state):
    """单份封存 CSV：运行清单、双台账留档、重传裁决、对账与全部证据。

    两份源台账都以原始字节的 base64 留档，并给出 sha256，收到文件后可逐字节复核。
    """
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['记录类型','批次','来源行','业务ID','原因码','内容'])
    rid=state.get('run_id','preview')
    manifest=dict(state.get('manifest') or {})
    ledger=state.get('ledger') or {}
    writer.writerow(['运行清单',rid,'','','',json.dumps(manifest,ensure_ascii=False,sort_keys=True)])
    writer.writerow(['台账留档',rid,'','current',manifest.get('sha256',''),
        json.dumps({'role':'current','sha256':manifest.get('sha256',''),'encoding':state.get('encoding',''),
            'source_b64':state.get('source_b64','')},ensure_ascii=False,sort_keys=True)])
    writer.writerow(['台账留档',rid,'','baseline',ledger.get('baseline_sha256',''),
        json.dumps({'role':'baseline','sha256':ledger.get('baseline_sha256',''),
            'source_b64':ledger.get('baseline_source_b64','')},ensure_ascii=False,sort_keys=True)])
    writer.writerow(['重传裁决',rid,'','','',json.dumps({k:ledger.get(k) for k in
        ('counts','business','current_reports','prior_reports','register_count','batch_count',
         'held_count','requires_review','decided','preserved','batch_accounted','publishable')},
        ensure_ascii=False,sort_keys=True)])
    for row in ledger.get('decisions') or []:
        writer.writerow(['变更',rid,row.get('line') if row.get('line') is not None else '',row.get('id',''),row.get('code',''),
            json.dumps({k:row.get(k) for k in ('change','version','sample_id','batch','analyte',
                'baseline_line','current_digest','baseline_digest','adopted','held','retained',
                'supersedes','superseded_by','suppressed_by','note')},ensure_ascii=False,sort_keys=True)])
    for row in ledger.get('register') or []:
        writer.writerow(['登记',rid,row.get('_line') if row.get('_line') is not None else '',row.get('id',''),
            row.get('_source',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get('reconcile',{}),ensure_ascii=False,sort_keys=True)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',row.get('_line','')),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    for row in state['quarantine']:writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True)])
    return '﻿'+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()
