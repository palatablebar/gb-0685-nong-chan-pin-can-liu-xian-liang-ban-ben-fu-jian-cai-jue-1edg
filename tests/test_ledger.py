"""重传裁决：可发布批次、源文件核对、中断保留与双台账留档。"""

import base64
import csv
import hashlib
import io
import json
from copy import deepcopy

import pytest

from app import store
from app.errors import DomainError
from app.modules.m7_archive.export_out import bundle
from app.verify import verify
from app.workflow import load, run

FIELDS = ['id', 'sample_id', 'batch', 'product', 'analyte', 'method', 'version',
          'sampled', 'reported', 'result', 'unit', 'lod', 'relation', 'note']


def rows_of(raw):
    return list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))


def encode(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=FIELDS, lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, '') for field in FIELDS})
    return out.getvalue().encode('utf-8')


def change(rows, identity, **fields):
    target = next(row for row in rows if row['id'] == identity)
    target.update(fields)
    return rows


@pytest.fixture
def baseline():
    return run(load('clean.csv'), {}, 7)


@pytest.fixture
def source():
    return rows_of(load('clean.csv'))


def decisions(state):
    return {row['id']: row for row in state['ledger']['decisions']}


def slot(state, sample_id, analyte):
    return [row for row in state['calculations']
            if row['sample_id'] == sample_id and row['analyte'] == analyte]


# —— 本次重传中哪些内容算入可发布批次 ——

def test_first_run_accepts_every_valid_report(baseline):
    ledger = baseline['ledger']
    assert ledger['batch_count'] == 24 and ledger['publishable'] is True
    assert ledger['counts']['new'] == 24 and ledger['prior_reports'] == 0
    assert baseline['business'] == {'total': 24, 'pass': 12, 'fail': 12,
                                    'uncertain': 0, 'unit': '检测项目'}


def test_unchanged_resend_does_not_add_or_drop_reports(baseline, source):
    again = run(encode(source), {}, 7, baseline)
    assert again['ledger']['counts']['unchanged'] == 36
    assert again['ledger']['batch_count'] == baseline['ledger']['batch_count']
    assert again['business'] == baseline['business']
    assert {row['id'] for row in again['calculations']} == {row['id'] for row in baseline['calculations']}


def test_appended_report_joins_batch(baseline, source):
    rows = deepcopy(source)
    rows.append(dict(source[0], id='00090', sample_id='S090', batch='B090',
                     relation='', note='新报告'))
    state = run(encode(rows), {}, 7, baseline)
    assert decisions(state)['00090']['change'] == 'NEW'
    assert decisions(state)['00090']['adopted'] is True
    assert state['ledger']['batch_count'] == baseline['ledger']['batch_count'] + 1
    assert state['ledger']['publishable'] is True


def test_same_version_correction_cannot_replace_published(baseline, source):
    rows = change(deepcopy(source), '00002', result='0.006')
    state = run(encode(rows), {}, 7, baseline)
    decision = decisions(state)['00002']
    assert decision['change'] == 'CORRECTED' and decision['held'] is True
    assert decision['published' if 'published' in decision else 'adopted'] is False
    accepted = slot(state, 'S001', 'P01')[0]
    assert accepted['id'] == '00002' and accepted['_source'] == 'baseline'
    assert state['business'] == baseline['business']
    assert state['ledger']['requires_review'] is True


def test_new_version_resend_is_held_not_swapped(baseline, source):
    rows = change(deepcopy(source), '00002', version='3', result='0.006')
    state = run(encode(rows), {}, 7, baseline)
    decision = decisions(state)['00002']
    assert decision['change'] == 'REVISED' and decision['held'] is True
    assert slot(state, 'S001', 'P01')[0]['_source'] == 'baseline'
    assert state['ledger']['register_count'] == baseline['ledger']['register_count']


def test_withdrawn_report_is_retained(baseline, source):
    rows = [row for row in deepcopy(source) if row['id'] != '00002']
    state = run(encode(rows), {}, 7, baseline)
    assert decisions(state)['00002']['change'] == 'WITHDRAWN'
    assert decisions(state)['00002']['retained'] is True
    assert slot(state, 'S001', 'P01')[0]['id'] == '00002'
    assert state['business'] == baseline['business']


def test_silent_swap_without_relation_is_conflicted(baseline, source):
    # 新复检只指向初检根报告，绕开已采信的 00002：不能悄悄换代。
    rows = deepcopy(source)
    rows.append(dict(source[0], id='00098', version='3', result='0.006', lod='0.001',
                     relation='00001', reported='2026-08-06', note='绕开已采信复检'))
    state = run(encode(rows), {}, 7, baseline)
    assert decisions(state)['00098']['change'] == 'CONFLICTED'
    assert decisions(state)['00098']['held'] is True
    assert decisions(state)['00002']['change'] == 'WITHDRAWN'
    assert slot(state, 'S001', 'P01')[0]['id'] == '00002'
    assert slot(state, 'S001', 'P01')[0]['_source'] == 'baseline'


def test_explicit_retest_supersedes_with_audit_trail(baseline, source):
    rows = deepcopy(source)
    rows.append(dict(source[0], id='00099', version='3', result='0.003', lod='0.001',
                     relation='00002', reported='2026-08-05', note='第三次复检'))
    state = run(encode(rows), {}, 7, baseline)
    assert decisions(state)['00099']['change'] == 'SUPERSEDED'
    assert decisions(state)['00099']['supersedes'] == '00002'
    assert decisions(state)['00002']['code'] == 'SUPERSEDED_RETIRED'
    accepted = slot(state, 'S001', 'P01')[0]
    assert accepted['id'] == '00099' and accepted['_source'] == 'current'
    retired = [row for row in state['ledger']['register'] if row['id'] == '00002']
    assert retired and retired[0]['_retired'] is True


def test_resend_ledger_is_idempotent_across_reruns(baseline, source):
    first = run(encode(source), {}, 7, baseline)
    second = run(encode(source), {}, 7, first)
    assert first['ledger']['batch_count'] == second['ledger']['batch_count']
    assert first['business'] == second['business']
    assert second['ledger']['counts']['unchanged'] == 36


# —— 批次等级、样本计数与封存采用裁决结果 ——

def test_grades_and_counts_read_publishable_batch(baseline, source):
    rows = change(deepcopy(source), '00002', result='0.006')
    state = run(encode(rows), {}, 7, baseline)
    reconcile = state['reconcile']
    assert reconcile['projects'] == len(state['calculations']) == 24
    assert reconcile['samples'] == len({row['sample_id'] for row in state['calculations']})
    assert sum(g['projects'] for g in reconcile['grades']) == reconcile['projects']
    assert all(g['code'] in ('PASS', 'FAIL', 'UNCERTAIN') for g in reconcile['grades'])


def test_archive_seals_decisions_and_both_ledgers(baseline, source):
    rows = change(deepcopy(source), '00002', result='0.006')
    state = run(encode(rows), {}, 7, baseline)
    text = bundle(state)
    kinds = [row[0] for row in csv.reader(io.StringIO(text.lstrip('﻿')))]
    assert kinds.count('台账留档') == 2
    archived = list(csv.reader(io.StringIO(text.lstrip('﻿'))))
    roles = {row[3] for row in archived if row[0] == '台账留档'}
    assert roles == {'current', 'baseline'}
    assert any(row[4] == 'CORRECTED' for row in archived if row[0] == '变更')
    assert state['archive']['ready'] is True


def test_decision_rows_are_checkable_against_source(baseline, source):
    rows = change(deepcopy(source), '00002', result='0.006')
    raw = encode(rows)
    state = run(raw, {}, 7, baseline)
    physical = raw.decode('utf-8').count('\n')
    for decision in state['ledger']['decisions']:
        if decision['line'] is None:
            # 撤回/换代只出现在基准台账里，用基准行号核对。
            assert decision['baseline_line'] is not None
            continue
        assert 1 <= decision['line'] <= physical
        assert decision['current_digest'] or decision['baseline_digest']


# —— 处理中断保留旧版发布；同台账重跑不重复增删 ——

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'records.db'))
    return tmp_path


def submit(raw):
    state = run(raw, {}, 7, store.published())
    return store.save(state['run_id'], state)


def test_interrupted_run_keeps_previous_publication(db, baseline, source):
    first = submit(load('clean.csv'))
    assert store.published()['run_id'] == first['run_id']
    bad = b'\xef\xbb\xbfid,sample_id\n1,2\n'
    with pytest.raises(DomainError):
        run(bad, {}, 7, store.published())
    assert store.published()['run_id'] == first['run_id']
    assert store.published()['business'] == first['business']


def test_non_publishable_result_does_not_advance_pointer(db, baseline, source):
    first = submit(load('clean.csv'))
    state = run(load('clean.csv'), {}, 7, store.published())
    state['ledger'] = dict(state['ledger'], publishable=False)
    store.save(state['run_id'], state)
    assert store.published()['run_id'] == first['run_id']


def test_same_ledger_rerun_does_not_duplicate_reports(db, source):
    first = submit(encode(source))
    second = submit(encode(source))
    assert first['run_id'] == second['run_id']
    assert len(store.history()) == 1
    assert len(store.publications()) == 1
    assert second['review_version'] == first['review_version']
    assert second['ledger']['batch_count'] == first['ledger']['batch_count']


def test_changed_ledger_republishes_once_with_new_review(db, source):
    first = submit(encode(source))
    rows = deepcopy(source)
    rows.append(dict(source[0], id='00090', sample_id='S090', batch='B090',
                     relation='', note='新报告'))
    second = submit(encode(rows))
    assert second['run_id'] != first['run_id']
    assert second['review_version'] == 1
    assert store.published()['run_id'] == second['run_id']
    third = submit(encode(rows))
    assert third['review_version'] == second['review_version']
    assert len(store.publications()) == 2


# —— 封存件可与两份源台账逐条核对 ——

def test_sealed_bundle_passes_verification(baseline, source):
    rows = change(deepcopy(source), '00002', result='0.006')
    state = run(encode(rows), {}, 7, baseline)
    report = verify(bundle(state))
    assert report.ok, [item for item in report.checks if not item['ok']]


def test_verifier_detects_tampered_digest(baseline, source):
    state = run(encode(source), {}, 7, baseline)
    text = bundle(state)
    lines = text.splitlines()
    target = next(i for i, line in enumerate(lines)
                  if line.startswith('变更') and state['ledger']['decisions'][0]['current_digest'] in line)
    digest = state['ledger']['decisions'][0]['current_digest']
    lines[target] = lines[target].replace(digest, 'deadbeefdeadbeef', 1)
    report = verify('\n'.join(lines))
    assert not report.ok
    assert any(item['check'] == '变更逐行可核对' and not item['ok'] for item in report.checks)


def test_verifier_detects_dropped_calculation(baseline, source):
    state = run(encode(source), {}, 7, baseline)
    lines = [line for line in bundle(state).splitlines()
             if not (line.startswith('计算') and state['calculations'][0]['id'] in line)]
    report = verify('\n'.join(lines))
    assert any(item['check'] == '可发布批次与计算记录一致' and not item['ok']
               for item in report.checks)


def test_bundle_archives_both_raw_ledgers(baseline, source):
    state = run(encode(source), {}, 7, baseline)
    text = bundle(state)
    rows = list(csv.reader(io.StringIO(text.lstrip('﻿'))))
    archived = {row[3]: row for row in rows if row[0] == '台账留档'}
    current = json.loads(archived['current'][5])
    prior = json.loads(archived['baseline'][5])
    assert base64.b64decode(current['source_b64']) == encode(source)
    assert base64.b64decode(prior['source_b64']) == load('clean.csv')
    assert current['sha256'] == hashlib.sha256(encode(source)).hexdigest()


# —— 显式确认才换版；确认有据可查 ——

def test_explicit_accept_switches_version_with_audit(baseline, source):
    rows = change(deepcopy(source), '00002', result='0.006')
    state = run(encode(rows), {'accept': ['00002']}, 7, baseline)
    decision = decisions(state)['00002']
    assert decision['confirmed'] is True and decision['adopted'] is True
    assert decision['held'] is False
    assert state['ledger']['confirmed'] == [{'id': '00002', 'change': 'CORRECTED',
                                             'line': decision['line'],
                                             'baseline_line': decision['baseline_line']}]
    accepted = slot(state, 'S001', 'P01')[0]
    assert accepted['id'] == '00002' and accepted['_source'] == 'current'
    assert accepted['code'] == 'FAIL'


def test_unconfirmed_withdrawal_stays_published(baseline, source):
    rows = [row for row in deepcopy(source) if row['batch'] != 'B004']
    state = run(encode(rows), {}, 7, baseline)
    assert state['ledger']['preserved'] is True
    assert state['ledger']['held_count'] > 0
    assert {grade['batch'] for grade in state['reconcile']['grades']} >= {'B004'}
    assert state['ledger']['batch_count'] == baseline['ledger']['batch_count']


def test_confirmed_withdrawal_shrinks_publishable_batch(baseline, source):
    rows = [row for row in deepcopy(source) if row['batch'] != 'B004']
    withdrawn = [row['id'] for row in source if row['batch'] == 'B004']
    state = run(encode(rows), {'accept': withdrawn}, 7, baseline)
    assert state['ledger']['publishable'] is True
    assert state['ledger']['batch_count'] == 18
    assert {grade['batch'] for grade in state['reconcile']['grades']} == {'B001', 'B002', 'B003'}
    assert state['business']['total'] == 18
    retired = [row for row in state['ledger']['register']
               if row['batch'] == 'B004' and row.get('_retired')]
    assert retired, '撤回的报告仍应留在登记簿'


def test_evidence_change_does_not_raise_review_load(baseline, source):
    # 从未成为结论的历史证据被改写：只留档，不虚增挂起项。
    # 00001 是历史证据（结论已被 00002 取代），改写它不影响批次。
    rows = change(deepcopy(source), '00001', result='0.02')
    state = run(encode(rows), {}, 7, baseline)
    decision = decisions(state)['00001']
    assert decision['change'] == 'CORRECTED' and decision['held'] is False
    assert state['ledger']['held_count'] == 0
    assert state['ledger']['batch_count'] == baseline['ledger']['batch_count']


# —— API 层：接受参数与已发布批次 ——

def test_api_accept_parameter_switches_version(client):
    first = client.post('/api/runs', json={'sample': 'clean.csv'}).get_json()
    rows = rows_of(load('clean.csv'))
    change(rows, '00002', result='0.006')
    held = client.post('/api/runs', json={'csv': encode(rows).decode('utf-8')}).get_json()
    accepted = client.post('/api/runs', json={'csv': encode(rows).decode('utf-8'),
                                              'parameters': {'accept': ['00002']}}).get_json()
    assert held['ledger']['held_count'] == 1 and held['business']['fail'] == 12
    assert accepted['ledger']['held_count'] == 0 and accepted['business']['fail'] == 13
    published = client.get('/api/published').get_json()
    assert published['run_id'] == accepted['run_id']
    assert published['ledger']['confirmed'][0]['id'] == '00002'
    assert first['run_id'] != published['run_id']


def test_published_export_survives_held_resend(client):
    client.post('/api/runs', json={'sample': 'clean.csv'})
    rows = rows_of(load('clean.csv'))
    change(rows, '00002', result='0.006')
    client.post('/api/runs', json={'csv': encode(rows).decode('utf-8')})
    response = client.get('/api/archive/published/export.csv')
    assert response.status_code == 200
    assert '台账留档' in response.data.decode('utf-8-sig')
