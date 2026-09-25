# 道路交通事故快处与处罚协同服务

本项目是一套可离线运行的 Python 后台，用于事故受理、道路风险研判、警力与拖车调度、结构化证据复核、责任认定、轻微事故快速结算、处罚执行和审计追溯。系统把同一事故从报警到结案的关键状态保存在 SQLite 中，角色权限覆盖接警员、调度员、事故处理民警、保险联络员、复核人员和审计人员。

## 目录

- `src/traffic_dispatch/`：事故风险指数、快处中心、道路走廊、应急资源、调度申请和响应情景；
- `src/evidence_review/`：采集设备、证据规范、结构化记录导入、一致性分析、复核租约和采信决定；
- `src/penalty_ops/`：事故案件、违法记录、风险告警、处置工单、处罚流转和审计；
- `src/settlement/`：无人员伤亡轻微事故的快速结算单——责任认定版本、修理厂报价版本、各方确认（含拒签）、免赔与责任分摊计算、收款/退款只追加台账和结案门禁；
- `fixtures/`：离线验收使用的证据规范与结构化事故记录；
- `tests/`：领域规则、事务边界、权限、HTTP API 和 CLI 验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅依赖 Python 标准库与 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m traffic_dispatch.acceptance --workspace .
PYTHONPATH=src python3 -m evidence_review.acceptance --workspace .
PYTHONPATH=src python3 -m penalty_ops.acceptance
PYTHONPATH=src python3 -m settlement.acceptance --workspace .
```

验收会建立临时 SQLite 数据库，登记事故风险记录、快处中心、道路走廊和应急资源，完成调度与证据复核；快速结算验收会贯通责任认定、报价调价导致确认失效、部分支付阻断结案、重复回执去重、退款留痕和结案，并输出 JSON 结果。命令不会访问公网，也不需要额外数据库、队列或常驻服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m traffic_dispatch.api --database traffic.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m evidence_review.api --database evidence.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m penalty_ops.api --database penalties.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m settlement.api --database settlement.sqlite3 --host 127.0.0.1 --port 8083
```

四个服务均提供 `GET /health`，其余接口使用 JSON（`X-Actor-Id` 头标识操作人）。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可继续查询。

### 快速结算服务（端口 8083）

- `POST /cases`、`POST /cases/{case_id}/liability`（办案民警登记案件与已生效责任认定，认定只追加、更正以新版本取代）；
- `POST /settlements`（在已生效责任认定之上建立快速结算单）；
- `POST /settlements/{id}/quotes`（修理厂报价版本，只追加；新版本生效后旧确认置为 `void` 并留痕，需重新确认）；
- `POST /settlements/{id}/confirmations`（当事人 `confirmed`/`rejected`；拒签与确认均不可覆盖，同一版本不能改口）；
- `POST /settlements/{id}/payments`、`POST /settlements/{id}/refunds`（收款/退款只追加；同一支付回执重复到达返回原记录，不重复入账）；
- `POST /cases/{case_id}/close`（结案门禁：结算单未到 `settled` 一律拒绝）；
- `GET /settlements/{id}`、`GET /cases/{case_id}`、`GET /history/{case|settlement}/{id}`（重建报价版本、免赔与分摊金额、确认人与时间、支付台账）；
- `GET /audit/chain`（哈希链校验，发现任何篡改）。
