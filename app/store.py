import sqlite3,json,os,hashlib
from pathlib import Path
from app.errors import DomainError


def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核", stamp TEXT NOT NULL DEFAULT "")')
    # 旧库升级：早期 runs 表没有 stamp 列，补列后按需重算。
    columns={row['name'] for row in db.execute('PRAGMA table_info(runs)')}
    if 'stamp' not in columns:db.execute('ALTER TABLE runs ADD COLUMN stamp TEXT NOT NULL DEFAULT ""')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    # 已发布批次审计：每个可发布内容指纹留存完整封存体，供回溯与双台账留档核对。
    db.execute('CREATE TABLE IF NOT EXISTS publications (run_id TEXT NOT NULL, stamp TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(run_id,stamp))')
    return db


def stamp(body):
    """可发布内容指纹：只覆盖会被出证的内容（批次结论、判级、计数、隔离凭据），
    不覆盖运行时时间戳、报告来源与比对过程，保证同一台账重跑幂等。"""
    ledger=body.get('ledger') or {}
    reconcile=body.get('reconcile') or {}
    material={'accepted':[{k:row.get(k) for k in ('id','sample_id','batch','analyte','code',
            'lower','upper','upper_exclusive','limit')} for row in ledger.get('accepted') or []],
        'business':reconcile.get('business') or ledger.get('business') or {},
        'grades':reconcile.get('grades') or [],
        'quarantine':[[row.get('line'),row.get('id'),row.get('code')] for row in body.get('quarantine') or []],
        'rule':(body.get('manifest') or {}).get('rule_version','')}
    return hashlib.sha256(json.dumps(material,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def publishable(body):
    """只有当批次账目、封存与重传裁决都可核对时才推进发布指针。"""
    return bool((body.get('reconcile') or {}).get('balanced')
        and (body.get('ledger') or {}).get('publishable')
        and (body.get('archive') or {}).get('ready'))


def save(run_id, body):
    """保存一次裁决。

    * 裁决体每次都留存（含挂起项与重传证据），避免中断或重跑丢失记录；
    * 只有可发布内容指纹变化时才使旧复核失效，同一台账重跑不重复增删报告；
    * 只有可发布内容确实变化且可发布时才推进发布指针。
    """
    fingerprint=stamp(body)
    encoded=json.dumps(body,ensure_ascii=False)
    with connection() as db:
        row=db.execute('SELECT stamp FROM runs WHERE id=?',(run_id,)).fetchone()
        if row is None:
            db.execute('INSERT INTO runs(id,body,version,decision,stamp) VALUES (?,?,1,?,?)',
                (run_id,encoded,'待复核',fingerprint))
        elif row['stamp']!=fingerprint:
            # 可发布内容变化：旧复核失效，回到待复核。
            db.execute('UPDATE runs SET body=?,version=version+1,decision=?,stamp=? WHERE id=?',
                (encoded,'待复核',fingerprint,run_id))
        else:
            db.execute('UPDATE runs SET body=? WHERE id=?',(encoded,run_id))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
        current=db.execute("SELECT value FROM meta WHERE key='published_stamp'").fetchone()
        if publishable(body) and (current is None or current['value']!=fingerprint):
            # 与已发布批次内容一致时不重复发布，发布指针保持不动。
            db.execute('INSERT INTO publications(run_id,stamp,body) VALUES (?,?,?) ON CONFLICT(run_id,stamp) DO UPDATE SET body=excluded.body',
                (run_id,fingerprint,encoded))
            db.execute("INSERT INTO meta(key,value) VALUES ('published',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
            db.execute("INSERT INTO meta(key,value) VALUES ('published_stamp',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(fingerprint,))
    return get(run_id)


def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision']);return body


def published():
    """当前已发布批次；处理中断或账目不符时仍返回上一份成功发布。"""
    with connection() as db:
        row=db.execute("SELECT p.body FROM meta m JOIN publications p ON p.run_id=m.value WHERE m.key='published' ORDER BY p.rowid DESC LIMIT 1").fetchone()
    if not row:return None
    body=json.loads(row['body'])
    # 以已发布封存体为事实来源：批次等级与样本计数都从这份发布体读取。
    body.update(run_id=body.get('run_id',''),published=True)
    return body


def published_sha():
    with connection() as db:
        row=db.execute("SELECT value FROM meta WHERE key='published_stamp'").fetchone()
    return row['value'] if row else ''


def publications():
    with connection() as db:rows=db.execute('SELECT run_id,stamp FROM publications ORDER BY rowid').fetchall()
    return [dict(r) for r in rows]


def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)
