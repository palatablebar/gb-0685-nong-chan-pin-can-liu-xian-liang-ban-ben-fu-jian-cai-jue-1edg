"""台账重传裁决。

上游每天重发“整份”检测台账：文件末尾追加新报告，历史行也可能被更正或撤回。
本模块把一次重传与“上一份已发布批次”逐条比对，裁决哪些内容进入本次可发布
批次。判据围绕“已采信结论的槽位”（样本/批次/产品/残留项目）展开：

* 新槽位的新结论立即进入可发布批次，出证不必等待；
* 已采信结论原样重发视为沿用，重跑同一台账不重复增删；
* 已采信报告被改写（同ID同版本内容变化、或同ID换版本）时挂起，
  可发布批次继续沿用旧结论，禁止重传悄悄换代；
* 已采信报告在新台账中消失（撤回）时挂起，并保留旧结论；
* 新报告按复检引用链显式指向已采信报告时记为 SUPERSEDED：新结论进入批次，
  旧报告作为历史证据留在登记簿；
* 新报告若想占据已有已采信结论的槽位、却没有显式指向该报告，记为
  CONFLICTED 并挂起：这是“悄悄换代”，必须人工复核；
* 任一条无法与源台账核对时整批回退到旧版发布，处理中断不丢旧版。

每条裁决带当前台账行号（line）与基准台账行号（baseline_line），并附行内容
摘要，结果可直接与两份源台账核对。``accepted`` 是可发布批次，批次判级、
样本计数与封存都以它为准。
"""

import hashlib
from decimal import Decimal, InvalidOperation

from app.reasons import REASONS

# 报告身份字段：note 是自由文本，不参与“内容是否被更正”的判定。
CORE_FIELDS = ('id', 'sample_id', 'batch', 'product', 'analyte', 'method',
               'version', 'sampled', 'reported', 'result', 'unit', 'lod',
               'relation')
KEY_FIELDS = ('sample_id', 'batch', 'product', 'analyte')
CONCLUSION_FIELDS = ('standard', 'lower', 'upper', 'upper_exclusive', 'limit',
                     'code', 'replaces')

CHANGES = ('NEW', 'EVIDENCE', 'UNCHANGED', 'CORRECTED', 'REVISED',
           'WITHDRAWN', 'SUPERSEDED', 'CONFLICTED', 'REJECTED')


def _text(value):
    return '' if value is None else str(value)


def source_view(row):
    """回到源台账的原值：m4 会把 unit 归一为 mg/kg，裁决与留档要按原始行核对。"""
    raw = row.get('_raw') if isinstance(row, dict) else None
    return raw if isinstance(raw, dict) else row


def _version(row):
    return _text(row.get('version'))


def _version_value(row):
    try:
        return Decimal(_version(row))
    except (InvalidOperation, ValueError):
        return Decimal(-1)


def content_signature(row):
    """报告内容的可比签名，按源台账原值计算（不含来源行号与自由文本备注）。

    业务流水线会把单位归一为 mg/kg，比对和留档必须回到源行原值，
    这样裁决结果才能逐字与重传文件核对。
    """
    row = source_view(row)
    return tuple(_text(row.get(field)) for field in CORE_FIELDS)


def signature_digest(row):
    """行内容摘要，便于脱离数据库直接与源台账核对。"""
    joined = '\x1f'.join(content_signature(row))
    return hashlib.sha256(joined.encode('utf-8')).hexdigest()[:16]


def report_key(row):
    """一份最终判定所归属的样本/项目键。"""
    return tuple(_text(row.get(field)) for field in KEY_FIELDS)


def slim_report(row, source='current'):
    row = source_view(row)
    item = {field: _text(row.get(field)) for field in CORE_FIELDS}
    item['_line'] = row.get('_line')
    item['_source'] = source
    return item


def publish_entry(measurement, calculation, source='current'):
    """可发布批次条目：报告身份 + 判定结论，可独立用于判级与封存。"""
    item = slim_report(measurement, source)
    for field in CONCLUSION_FIELDS:
        if calculation.get(field) is not None:
            item[field] = _text(calculation.get(field))
    item['line'] = measurement.get('_line')
    return item


def _decision(change, code, row=None, baseline=None, identity=None,
              adopted=False, held=False, retained=False, line=None, **extra):
    anchor = row or baseline or {}
    item = {
        'change': change,
        'code': code,
        'message': REASONS.get(code, code),
        'id': _text(identity if identity is not None else anchor.get('id')),
        'version': _version(anchor),
        'sample_id': _text(anchor.get('sample_id')),
        'batch': _text(anchor.get('batch')),
        'analyte': _text(anchor.get('analyte')),
        'line': (row or {}).get('_line') if line is None else line,
        'baseline_line': (baseline or {}).get('_line'),
        # 逐行内容摘要：与源台账该行内容比对，不依赖数据库。
        'current_digest': signature_digest(row) if row else '',
        'baseline_digest': signature_digest(baseline) if baseline else '',
        'adopted': adopted,
        'held': held,
        'retained': retained,
        'confirmed': False,
    }
    item.update(extra)
    return item


def _baseline_ledger(baseline):
    return (baseline or {}).get('ledger') or {}


def _accepted_of(baseline):
    if not baseline:
        return []
    ledger = _baseline_ledger(baseline)
    if ledger.get('accepted'):
        return [dict(item) for item in ledger['accepted']]
    calculations = {_text(row.get('id')): row for row in baseline.get('calculations') or []}
    entries = []
    for measurement in baseline.get('measurements') or []:
        calculation = calculations.get(_text(measurement.get('id')))
        if calculation:
            entries.append(publish_entry(measurement, calculation, 'baseline'))
    return entries


def _register_of(baseline):
    """基准台账的已采信登记簿：历史报告留档，也是重传比对的依据。"""
    if not baseline:
        return []
    ledger = _baseline_ledger(baseline)
    if ledger.get('register'):
        return [dict(item) for item in ledger['register']]
    return [slim_report(row, 'baseline') for row in baseline.get('measurements') or []]


def _baseline_sha(baseline):
    """基准台账原始字节摘要，与 manifest 中的 sha256 一致，可直接比对源文件。"""
    if not baseline:
        return ''
    return _text((baseline.get('manifest') or {}).get('sha256'))


def _baseline_source(baseline):
    return _text((baseline or {}).get('source_b64'))


def _observed(state):
    """当前台账出现过的报告ID -> 源行号（含未能通过校验的行）。"""
    observed = {}
    for row in state.get('measurements') or []:
        identity = _text(row.get('id'))
        if identity:
            observed.setdefault(identity, row.get('_line'))
    for entry in state.get('quarantine') or []:
        raw = entry.get('raw') if isinstance(entry.get('raw'), dict) else {}
        identity = _text(raw.get('id') or entry.get('id'))
        if identity:
            observed.setdefault(identity, entry.get('line'))
    return observed


def _rejected_rows(state):
    """(报告ID, 版本) -> (原因码, 行号)；无版本的隔离行按ID兜底。"""
    by_key, by_id = {}, {}
    for entry in state.get('quarantine') or []:
        raw = entry.get('raw') if isinstance(entry.get('raw'), dict) else {}
        identity = _text(raw.get('id') or entry.get('id'))
        if not identity:
            continue
        record = (entry.get('code') or 'REJECTED', entry.get('line'))
        if raw.get('id'):
            by_key[(identity, _text(raw.get('version')))] = record
            by_id.setdefault(identity, record)
        else:
            by_id.setdefault(identity, record)
    return by_key, by_id


def _latest_by_id(measurements):
    latest = {}
    for row in measurements or []:
        identity = _text(row.get('id'))
        if identity and (identity not in latest
                         or _version_value(row) > _version_value(latest[identity])):
            latest[identity] = row
    return latest


def _ancestors(row, versions):
    """沿复检引用链回溯，返回全部上游报告ID；环或悬空引用即止。"""
    chain, seen = [], set()
    current = _text(row.get('relation'))
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        parent = versions.get(current)
        if parent is None:
            break
        current = _text(parent.get('relation'))
    return chain


def _sort_batch(rows):
    return sorted(rows, key=lambda row: (row.get('batch', ''), row.get('sample_id', ''),
                                         row.get('analyte', ''), row.get('id', '')))


def _register_entry(row, source):
    return slim_report(row, source)


def _accept_set(parameters):
    """显式确认的变更：parameters.accept 列出报告ID，或 accept_all 为真。

    没有显式确认时，被更正/换版/撤回的已采信结论一律沿用旧版，
    重传不会自己换代。确认后仍逐条留痕，可回溯。
    """
    if not parameters:
        return set(), False
    raw = parameters.get('accept') or []
    if isinstance(raw, str):
        raw = [item.strip() for item in raw.split(',')]
    return {_text(item) for item in raw if _text(item)}, bool(parameters.get('accept_all'))


def adjudicate(state, baseline=None, parameters=None):
    """裁决一次重传，返回可发布批次、逐条变更凭据与可核对账目。"""
    measurements = state.get('measurements') or []
    calculations = {_text(row.get('id')): row for row in state.get('calculations') or []}
    latest = _latest_by_id(measurements)
    observed = _observed(state)
    rejected_by_key, rejected_by_id = _rejected_rows(state)
    confirmed_ids, accept_all = _accept_set(parameters if parameters is not None
                                            else state.get('parameters'))

    prior_register = {_text(row.get('id')): row for row in _register_of(baseline)}
    prior_accepted = {report_key(row): row for row in _accepted_of(baseline)}
    prior_ids = set(prior_register)

    # 本次台账中通过业务裁决的结论，按样本/项目槽位归位。
    current_accepted = {}
    for identity, calculation in calculations.items():
        row = latest.get(identity)
        if row is not None:
            current_accepted[report_key(row)] = (row, calculation)

    decisions = {}

    def record(identity, decision):
        existing = decisions.get(identity)
        if existing is None:
            decisions[identity] = decision
        else:
            # 同一报告既是结论又是证据时，取更强的结论性描述。
            if decision['adopted']:
                decisions[identity] = decision

    # 一、逐槽位裁决已采信结论。
    retired = set()
    for key in sorted(set(prior_accepted) | set(current_accepted)):
        previous = prior_accepted.get(key)
        candidate = current_accepted.get(key)
        if candidate is None:
            identity = _text(previous.get('id'))
            confirm = accept_all or identity in confirmed_ids
            if identity in observed:
                code, line = rejected_by_id.get(identity, ('REJECTED', None))
                record(identity, _decision('REJECTED', code, latest.get(identity), previous,
                                           held=not confirm, retained=not confirm, line=line,
                                           confirmed=confirm,
                                           note='已采信报告本次未通过校验，保留旧结论'))
            else:
                record(identity, _decision('WITHDRAWN', 'WITHDRAWN', None, previous,
                                           held=not confirm, retained=not confirm,
                                           confirmed=confirm, withdrawn_confirmed=confirm,
                                           note='已采信报告被撤回' + ('（已确认）' if confirm else '，旧结论保留')))
            continue
        row, calculation = candidate
        identity = _text(row.get('id'))
        if previous is None:
            record(identity, _decision('NEW', 'NEW', row, None, adopted=True))
            continue
        prior_id = _text(previous.get('id'))
        if identity == prior_id:
            prior_row = prior_register.get(prior_id)
            confirm = accept_all or identity in confirmed_ids
            if prior_row is not None and content_signature(row) == content_signature(prior_row):
                record(identity, _decision('UNCHANGED', 'UNCHANGED', row, prior_row, adopted=True))
            elif prior_row is not None and _version(row) != _version(prior_row):
                record(identity, _decision('REVISED', 'REVISED', row, prior_row,
                                           held=not confirm, retained=not confirm,
                                           adopted=confirm, confirmed=confirm,
                                           note='换版本重发' + ('（已确认）' if confirm else '未经复核，沿用已采信版本')))
            else:
                record(identity, _decision('CORRECTED', 'CORRECTED', row, prior_row,
                                           held=not confirm, retained=not confirm,
                                           adopted=confirm, confirmed=confirm,
                                           note='同版内容被更正' + ('（已确认）' if confirm else '未经复核，沿用已采信版本')))
            continue
        # 候选结论换了一个报告ID：只有显式指向已采信报告才算换代。
        if prior_id in _ancestors(row, latest):
            record(identity, _decision('SUPERSEDED', 'SUPERSEDED', row, previous,
                                       adopted=True, supersedes=prior_id))
            record(prior_id, _decision('SUPERSEDED', 'SUPERSEDED_RETIRED', None, previous,
                                       retained=True, superseded_by=identity))
            retired.add(prior_id)
        else:
            # 确认可以指向新报告，也可以指向被替代的已采信报告。
            confirm = accept_all or identity in confirmed_ids or prior_id in confirmed_ids
            record(identity, _decision('CONFLICTED', 'CONFLICTED', row, previous,
                                       held=not confirm, retained=not confirm,
                                       adopted=confirm, confirmed=confirm,
                                       suppressed_by=prior_id,
                                       note='未显式指向已采信报告的换代' + ('（已确认）' if confirm else '，待复核')))
            record(prior_id, _decision('WITHDRAWN', 'WITHDRAWN', None, previous,
                                       held=not confirm, retained=not confirm,
                                       confirmed=confirm,
                                       superseded_by=identity if confirm else None,
                                       note='槽位出现候选换代' + ('（已确认）' if confirm else '，旧结论保留')))

    # 二、其余报告按证据处理：只登记，不改动结论。
    # 这些报告从未成为最终结论，改动不影响可发布批次，但登记簿仍保留旧版，
    # 不允许重传悄悄覆盖证据。
    for identity in sorted(observed):
        if identity in decisions:
            continue
        row = latest.get(identity)
        prior = prior_register.get(identity)
        if row is not None and (identity, _version(row)) in rejected_by_key:
            code, line = rejected_by_key[(identity, _version(row))]
            record(identity, _decision('REJECTED', code, row, prior, line=line))
        elif row is None:
            code_line = rejected_by_id.get(identity)
            code = code_line[0] if code_line else 'REJECTED'
            line = code_line[1] if code_line else observed.get(identity)
            record(identity, _decision('REJECTED', code, None, prior, identity=identity,
                                       line=line))
        elif prior is None:
            record(identity, _decision('EVIDENCE', 'EVIDENCE', row, None,
                                       note='新增报告，未成为最终判定'))
        elif content_signature(row) == content_signature(prior):
            record(identity, _decision('UNCHANGED', 'UNCHANGED', row, prior))
        elif _version(row) != _version(prior):
            record(identity, _decision('REVISED', 'REVISED', row, prior,
                                       note='历史证据换版重发，登记簿保留旧版'))
        else:
            record(identity, _decision('CORRECTED', 'CORRECTED', row, prior,
                                       note='历史证据内容变化，登记簿保留旧版'))

    # 三、基准台账中被删掉的历史报告：留档并注明撤回（不影响可发布批次）。
    for identity in sorted(prior_ids - set(observed)):
        if identity in decisions:
            continue
        record(identity, _decision('WITHDRAWN', 'WITHDRAWN', None, prior_register[identity],
                                   retained=True,
                                   note='历史报告在新台账中缺失，已留档'))

    ordered = sorted(decisions.values(),
                     key=lambda row: (row.get('batch', ''), row.get('sample_id', ''),
                                      row.get('analyte', ''), _text(row.get('id'))))

    # 可发布批次：采用本次通过裁决的结论；挂起的槽位沿用已采信结论。
    batch = {}
    for key, (row, calculation) in current_accepted.items():
        decision = decisions.get(_text(row.get('id')))
        if decision is not None and decision['adopted']:
            batch[key] = publish_entry(row, calculation, 'current')
    for key, previous in prior_accepted.items():
        identity = _text(previous.get('id'))
        decision = decisions.get(identity)
        if decision is not None and decision['adopted'] and key in batch:
            continue
        if decision is not None and decision['change'] in ('WITHDRAWN', 'REJECTED') \
                and decision['confirmed']:
            # 已确认撤回/拒收：已采信结论退出可发布批次，登记簿仍留档。
            batch.pop(key, None)
            continue
        if key in batch and batch[key]['id'] != identity and decision is not None \
                and decision['change'] in ('SUPERSEDED', 'WITHDRAWN', 'CONFLICTED') \
                and decision.get('confirmed'):
            continue
        if key not in batch:
            batch[key] = dict(previous, _source='baseline', retained=True)
    accepted = _sort_batch(batch.values())

    def decided_for(previous):
        return decisions.get(_text(previous.get('id')))

    # 已采信登记簿：历史报告永久留档、不删除。挂起的更正/换版/撤回保留旧内容，
    # 只有通过裁决的新增、沿用、显式换代与显式确认的变更才更新登记。
    register = {identity: dict(row, _source='baseline')
                for identity, row in prior_register.items()}
    for identity, row in latest.items():
        if (identity, _version(row)) in rejected_by_key or identity in rejected_by_id:
            continue
        decision = decisions.get(identity)
        change = decision['change'] if decision else 'EVIDENCE'
        confirmed = bool(decision and decision.get('confirmed'))
        if change in ('NEW', 'EVIDENCE', 'UNCHANGED', 'SUPERSEDED') \
                or (confirmed and change in ('CORRECTED', 'REVISED', 'CONFLICTED')):
            register[identity] = _register_entry(row, 'current')
    # 已确认撤回的报告退出批次，但登记簿保留并标注退役。
    for decision in ordered:
        if decision['change'] == 'WITHDRAWN' and decision['confirmed'] and decision['id'] in register:
            register[decision['id']] = dict(register[decision['id']], _retired=True)
    for identity in retired:
        if identity in register:
            register[identity] = dict(register[identity], _retired=True)
    register_rows = sorted(register.values(),
                           key=lambda row: (row.get('id', ''), _version(row)))

    counts = {change.lower(): 0 for change in CHANGES}
    for decision in ordered:
        counts[decision['change'].lower()] += 1

    business = {'total': len(accepted),
                'pass': sum(1 for row in accepted if row.get('code') == 'PASS'),
                'fail': sum(1 for row in accepted if row.get('code') == 'FAIL'),
                'uncertain': sum(1 for row in accepted if row.get('code') == 'UNCERTAIN'),
                'unit': '检测项目'}

    held = [decision for decision in ordered if decision['held']]
    held_lines = sorted({decision['line'] for decision in held
                         if decision['line'] is not None})
    decided = set(decisions) == (set(observed) | prior_ids)
    # 已采信槽位要么仍在批次里，要么是显式换代或经确认的撤回/拒收；
    # 悄无声息地消失才回退到旧版发布。
    def accounted(key, previous):
        if key in batch:
            return True
        decision = decided_for(previous)
        if decision is None:
            return False
        if decision['change'] == 'SUPERSEDED':
            return True
        return decision['confirmed'] and decision['change'] in ('WITHDRAWN', 'REJECTED')

    preserved = all(accounted(key, previous) for key, previous in prior_accepted.items())
    confirmed = [decision for decision in ordered if decision['confirmed']]
    batch_accounted = all(
        (row.get('_source') == 'current' and row.get('id') in calculations
         and decisions.get(row.get('id'), {}).get('adopted'))
        or (row.get('_source') == 'baseline' and row.get('id') in prior_register)
        for row in accepted)
    publishable = decided and preserved and batch_accounted
    if not publishable:
        # 账目无法与源文件核对时整批回退，保留旧版发布。
        accepted = _sort_batch(dict(row, _source='baseline', retained=True)
                               for row in prior_accepted.values())
        business = {'total': len(accepted),
                    'pass': sum(1 for row in accepted if row.get('code') == 'PASS'),
                    'fail': sum(1 for row in accepted if row.get('code') == 'FAIL'),
                    'uncertain': sum(1 for row in accepted if row.get('code') == 'UNCERTAIN'),
                    'unit': '检测项目'}

    summary = {
        'baseline_sha256': _baseline_sha(baseline),
        'counts': counts,
        'business': business,
        'current_reports': len(observed),
        'prior_reports': len(prior_ids),
        'register_count': len(register_rows),
        'batch_count': len(accepted),
        'held_count': len(held),
        'confirmed_count': len(confirmed),
        'confirmed': [{'id': item['id'], 'change': item['change'],
                       'line': item['line'], 'baseline_line': item['baseline_line']}
                      for item in confirmed],
        'requires_review': bool(held),
        'decided': decided,
        'preserved': preserved,
        'batch_accounted': batch_accounted,
        'publishable': publishable,
        'held_lines': held_lines,
        'decisions': ordered,
        'held': held,
        'accepted': accepted,
        'evidence': [slim_report(row, 'current') for row in measurements],
        'register': register_rows,
        'baseline_source_b64': _baseline_source(baseline),
    }
    return summary
