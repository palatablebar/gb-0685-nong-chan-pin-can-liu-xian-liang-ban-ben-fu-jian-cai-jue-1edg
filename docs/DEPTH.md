# 深度登记 农产品残留限量版本与复检裁决

规模：中型。核心规则：按采样日期选择有效限量，低于检出限保留区间；复检不得越过样本关联与方法适用范围。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 报告采信 | 查看并复算报告采信 | `/api/m1_ingest` | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py` |
| M2 项目映射 | 查看并复算项目映射 | `/api/m2_mapping` | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py` |
| M3 限量选版 | 查看并复算限量选版 | `/api/m3_versions` | `app/modules/m3_versions/rules.py` | `tests/test_versions.py` |
| M4 检出量换算 | 查看并复算检出量换算 | `/api/m4_segments` | `app/modules/m4_segments/rules.py` | `tests/test_segments.py` |
| M5 复检裁决 | 查看并复算复检裁决 | `/api/m5_calculate` | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py` |
| M6 批次判级 | 查看并复算批次判级 | `/api/m6_reconcile` | `app/modules/m6_reconcile/compare.py`、`app/ledger.py` | `tests/test_reconcile.py`、`tests/test_ledger.py` |
| M7 报告归档 | 查看并复算报告归档 | `/api/m7_archive` | `app/modules/m7_archive/export_out.py` | `tests/test_archive.py`、`tests/test_ledger.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030中文 | 固定UTF-8失败 | 按编码链回退 | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_legacy_encoding` |
| TRAP-02 | 表头BOM | 首列键失配 | 去除BOM | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_bom` |
| TRAP-03 | 引号内换行 | 分行误计 | csv逻辑记录 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_preserves_multiline_note` |
| TRAP-05 | 列数错位 | 静默补列 | WIDTH隔离 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_width_isolation` |
| TRAP-06 | 全角千分位 | float失败 | NFKC与Decimal | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_thousands` |
| TRAP-07 | 空值哨兵 | 空值当零 | MISSING隔离 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_missing_numeric` |
| TRAP-08 | 前导零ID | 编码失真 | 字符串保形 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_identifier_zeroes` |
| TRAP-10 | 浮点误差 | 累计失准 | Decimal求和 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_decimal_precision` |
| TRAP-14 | 同版冲突 | 最后覆盖 | 整组隔离 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_real_conflict_quarantine` |
| TRAP-18 | 三种空态 | 除零或伪成功 | 三个错误码 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_empty_three_states` |
| TRAP-26 | 旧版本复核 | 覆盖新结论 | 事务条件更新 | `app/store.py` | `tests/test_archive.py::test_stale_review` |
| TRAP-15 | ug/kg与mg/kg | 差1000倍 | 浓度统一mg/kg | `app/modules/m4_segments/rules.py` | `tests/test_segments.py::test_unit_conversion` |
| TRAP-25 | 标准时间边界 | 双版或错版 | 采样日半开区间选版 | `app/modules/m3_versions/standards.py` | `tests/test_versions.py::test_halfopen_effective` |
| TRAP-20 | 复检引用环 | 遍历无法结束 | 引用路径检测 | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py::test_cycle_rejected` |
| TRAP-30 | 整份台账重传 | 新文件覆盖旧结论 | 以已发布批次为基准逐槽位裁决，挂起改用旧结论 | `app/ledger.py` | `tests/test_ledger.py::test_same_version_correction_cannot_replace_published` |
| TRAP-31 | 撤回历史行 | 报告消失无从核对 | WITHDRAWN保留旧结论并登记留档 | `app/ledger.py` | `tests/test_ledger.py::test_withdrawn_report_is_retained` |
| TRAP-32 | 静默换代 | 新复检绕开已采信报告 | CONFLICTED挂起，仅显式relation可换代 | `app/ledger.py` | `tests/test_ledger.py::test_silent_swap_without_relation_is_conflicted` |
| TRAP-33 | 重跑重复增删 | 每次重发都算新报告 | 按可发布内容指纹幂等、登记簿只增不删 | `app/store.py`、`app/ledger.py` | `tests/test_ledger.py::test_same_ledger_rerun_does_not_duplicate_reports` |
| TRAP-34 | 处理中断 | 半途覆盖已发布 | 发布指针只在可发布时前移，失败保留旧版 | `app/store.py` | `tests/test_ledger.py::test_interrupted_run_keeps_previous_publication` |

## 对账规则

输入行数=采纳+隔离+空行+重复表头+完全重复；业务总数与计算证据独立求和。隔离行保留来源行、ID、原因码与原值。

重传裁决另有一本账：本次台账中每个报告ID都要有且只有一条裁决，基准台账中缺失的历史报告按撤回留档；可发布批次每条结论必须能追到当前台账的行或已发布登记簿的ID。两项任一不成立即整批回退旧版发布。

## 已知不覆盖

不做多租户权限和外部服务联调。行业规则以项目数据字典为准。文件上限2MB，逐行解析后在上限内完成分组计算。
