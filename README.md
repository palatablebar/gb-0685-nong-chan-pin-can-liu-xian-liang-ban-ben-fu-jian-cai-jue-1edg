# 农产品残留限量版本与复检裁决

面向农产品检测站复核员，处理采信、判定、核算、复核与归档。

## 启动

```bash
docker compose up --build -d
```

访问 http://localhost:18254 。初次启动自动运行脏样例，可切换 clean.csv 与 legacy.csv。顶部“运行批次”会重新计算，同源输入复用批次。

## 业务模块

- 报告采信：`/m1_ingest`、`/api/m1_ingest`
- 项目映射：`/m2_mapping`、`/api/m2_mapping`
- 限量选版：`/m3_versions`、`/api/m3_versions`
- 检出量换算：`/m4_segments`、`/api/m4_segments`
- 复检裁决：`/m5_calculate`、`/api/m5_calculate`
- 批次判级：`/m6_reconcile`、`/api/m6_reconcile`
- 报告归档：`/m7_archive`、`/api/m7_archive`

## 核心规则

按采样日期选择有效限量，低于检出限保留区间；复检不得越过样本关联与方法适用范围。完整数值、边界和异常定义见 `docs/DATA-DICTIONARY.md`。

上游每天重发整份台账。每次重传都会与上一份已发布批次逐条比对：新增报告直接进入可发布批次；已采信报告被更正、换版或撤回时挂起并沿用旧结论；只有沿复检 relation 显式指向已采信报告的换代才放行。逐条裁决带源行号与行摘要，见 `/m6_reconcile`、`/api/published`。

## 验证

```bash
docker compose exec -T app pytest -q
docker compose config --quiet
```

原始样例在 `sample_data/`。测试在 `tests/`。封存文件是单份CSV，含运行清单、重传裁决、双台账原始字节留档、计算证据、隔离原因与对账，可逐字节复核。复核需提交当前版本，陈旧复核返回409。

封存件可脱离数据库核对：

```bash
python -m app.verify evidence.csv
```

逐项核对双台账摘要、每条变更的源行内容、变更计数、可发布批次与项目计数独立求和；任一不符返回非零。

## 数据与限制

SQLite保存在records命名卷，普通重启与构建不会清除。确认可丢弃本项目数据后，运行 `docker compose down -v` 再启动可重置。单文件上限2MB；不接入外部业务系统；历史规则由本项目样例定义，不代表实际行业标准。
