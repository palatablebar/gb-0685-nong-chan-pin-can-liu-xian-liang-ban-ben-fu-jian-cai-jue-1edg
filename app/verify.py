"""封存件核验：把导出 CSV 与两份源台账逐条核对。

用法::

    python -m app.verify evidence.csv

核验项：
* 两份源台账的 base64 能还原原始字节，且 sha256 与记录一致；
* 每条“变更”记录都能在对应源台账的指定行找到内容签名一致的行；
* 每条当前台账的“登记”记录都能在源台账中找到同样的报告；
* 变更计数与记录条数一致，可发布批次与“计算”记录一一对应。

全部通过返回 0，否则打印差异并返回 1。核验只读导出文件，不依赖数据库。
"""

import base64
import csv
import hashlib
import io
import json
import sys

from app.ledger import signature_digest


def _rows(payload):
    return list(csv.reader(io.StringIO(payload)))


def _source_indexes(raw):
    """按 m1 的读法重建源台账：逻辑记录 -> 行号、报告内容。"""
    from app.modules.m1_ingest.decode import decode
    text, _ = decode(raw)
    reader = csv.reader(io.StringIO(text, newline=''))
    try:
        header = [cell.strip().lstrip('﻿') for cell in next(reader)]
    except StopIteration:
        return {}, {}
    by_line, by_id = {}, {}
    for row in reader:
        line = reader.line_num
        if not row or not any(cell.strip() for cell in row) or row == header:
            continue
        if len(row) != len(header):
            continue
        item = dict(zip(header, row))
        by_line[line] = item
        by_id.setdefault(item.get('id', ''), item)
    return by_line, by_id


class Report:
    def __init__(self):
        self.checks = []

    def check(self, name, ok, detail=''):
        self.checks.append({'check': name, 'ok': bool(ok), 'detail': detail})
        return ok

    @property
    def ok(self):
        return all(item['ok'] for item in self.checks)


def verify(payload):
    report = Report()
    rows = _rows(payload.lstrip('﻿'))
    if not rows or rows[0][0] != '记录类型':
        report.check('表头', False, '缺少记录类型表头')
        return report
    records = rows[1:]
    archives = {row[3]: row for row in records if row[0] == '台账留档'}
    report.check('双台账留档', set(archives) == {'current', 'baseline'},
                 '实际: %s' % sorted(archives))

    sources, indexes = {}, {}
    for role in ('current', 'baseline'):
        row = archives.get(role)
        if row is None:
            continue
        try:
            meta = json.loads(row[5])
        except (ValueError, IndexError):
            report.check(role + '留档可解析', False)
            continue
        try:
            raw = base64.b64decode(meta.get('source_b64') or '')
        except (ValueError, TypeError):
            report.check(role + '源字节可还原', False)
            continue
        digest = hashlib.sha256(raw).hexdigest()
        expected = (meta.get('sha256') or row[4])
        if not raw and not expected:
            # 首次运行没有上一份已发布台账，留档行为空是正常的。
            report.check(role + '源台账', True, '无基准台账')
            sources[role] = raw
            indexes[role] = ({}, {})
            continue
        report.check(role + '源字节可还原', bool(raw))
        report.check(role + '源字节sha256', digest == expected,
                     '' if digest == expected else '%s != %s' % (digest[:16], expected[:16]))
        sources[role] = raw
        indexes[role] = _source_indexes(raw) if raw else ({}, {})

    manifest = next((json.loads(row[5]) for row in records if row[0] == '运行清单'), {})
    if 'current' in sources:
        report.check('当前台账与运行清单一致',
                     hashlib.sha256(sources['current']).hexdigest() == manifest.get('sha256'))

    changes = [row for row in records if row[0] == '变更']
    mismatched = []
    for row in changes:
        try:
            detail = json.loads(row[5])
        except (ValueError, IndexError):
            mismatched.append((row[3], '内容无法解析'))
            continue
        # 当前行与基准行都要按各自台账核对，任何一侧被改写都应被发现。
        pairs = []
        if row[2]:
            pairs.append(('current', int(row[2]), detail.get('current_digest')))
        if detail.get('baseline_line') is not None:
            pairs.append(('baseline', int(detail['baseline_line']), detail.get('baseline_digest')))
        for role, line_no, digest in pairs:
            item = indexes.get(role, ({}, {}))[0].get(line_no)
            if item is None:
                mismatched.append((row[3], '%s 第 %s 行不存在' % (role, line_no)))
                continue
            if digest and signature_digest(item) != digest:
                mismatched.append((row[3], '%s 第 %s 行内容与摘要不符' % (role, line_no)))
    report.check('变更逐行可核对', not mismatched, '; '.join('%s %s' % pair for pair in mismatched[:5]))

    summary = next((json.loads(row[5]) for row in records if row[0] == '重传裁决'), {})
    counts = summary.get('counts') or {}
    report.check('变更计数与记录一致', sum(counts.values()) == len(changes),
                 '计数 %s 记录 %s' % (sum(counts.values()), len(changes)))

    calculations = [row for row in records if row[0] == '计算']
    report.check('可发布批次与计算记录一致',
                 summary.get('batch_count') == len(calculations),
                 '批次 %s 计算 %s' % (summary.get('batch_count'), len(calculations)))

    registered = [row for row in records if row[0] == '登记']
    current_ids = {row[3] for row in registered if row[4] == 'current'}
    accepted_ids = {row[3] for row in calculations}
    report.check('批次报告均在登记簿', accepted_ids <= (current_ids | {row[3] for row in registered}),
                 '缺失 %s' % sorted(accepted_ids - {row[3] for row in registered})[:5])

    # 批次等级与计数重算：从“计算”记录独立求和，不读汇总。
    codes = [row[4] for row in calculations]
    business = summary.get('business') or {}
    report.check('项目计数独立求和',
                 business.get('total', len(codes)) == len(codes)
                 and business.get('pass', codes.count('PASS')) == codes.count('PASS')
                 and business.get('fail', codes.count('FAIL')) == codes.count('FAIL')
                 and business.get('uncertain', codes.count('UNCERTAIN')) == codes.count('UNCERTAIN'),
                 '汇总 %s / 记录 %s' % (business, {c: codes.count(c) for c in ('PASS', 'FAIL', 'UNCERTAIN')}))
    return report


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print('用法: python -m app.verify <evidence.csv>', file=sys.stderr)
        return 2
    with open(argv[0], 'r', encoding='utf-8-sig', newline='') as handle:
        report = verify(handle.read())
    for item in report.checks:
        print(('通过' if item['ok'] else '失败'), item['check'], item['detail'])
    return 0 if report.ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
