# 道路交通事故快处与处罚协同服务

本项目是一套可离线运行的 Python 后台，用于事故受理、道路风险研判、警力与拖车调度、结构化证据复核、责任认定、处罚执行和审计追溯。系统把同一事故从报警到结案的关键状态保存在 SQLite 中，角色权限覆盖接警员、调度员、事故处理民警、复核人员和审计人员。

## 目录

- `src/traffic_dispatch/`：事故风险指数、快处中心、道路走廊、应急资源、调度申请、响应情景，以及无人员伤亡轻微事故的责任认定与快速结算单；
- `src/evidence_review/`：采集设备、证据规范、结构化记录导入、一致性分析、复核租约和采信决定；
- `src/penalty_ops/`：事故案件、违法记录、风险告警、处置工单、处罚流转和审计；
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
```

验收会建立临时 SQLite 数据库，登记事故风险记录、快处中心、道路走廊和应急资源，完成调度与证据复核，并输出 JSON 结果。命令不会访问公网，也不需要额外数据库、队列或常驻服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m traffic_dispatch.api --database traffic.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m evidence_review.api --database evidence.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m penalty_ops.api --database penalties.sqlite3 --host 127.0.0.1 --port 8082
```

三个服务均提供 `GET /health`，其余接口使用 JSON，写接口需要 `X-Actor-Id` 头。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可继续查询。

### 轻微事故快速结算（traffic_dispatch 服务，端口 8080）

在已生效的责任认定之上建立快速结算单，覆盖「责任认定 → 建立结算单 → 修理厂报价版本 → 各方确认/拒签 → 登记收款/退款 → 结清 → 结案」全流程，由 `officer`（办案人员）角色操作，`auditor` 可重建全部金额与确认历史。

- `POST /liability_determinations`：登记已生效的责任认定（责任比例以基点表示，合计 10000）；同一事故同时只有一份生效认定。
- `POST /quick_settlements`：在责任认定之上建立结算单，可带赔付项目（拖车费等）与按方免赔额。
- `POST /quick_settlements/{id}/quotes`：提交修理厂报价。每次内容变化产生新版本，旧版确认自动失效（`voided`）但完整保留；内容未变的重复提交回放旧版本，不产生版本。
- `POST /quick_settlements/{id}/confirmations`：当事人对当前报价版本确认或拒签。同一版本拒签后不能被确认覆盖，须等新报价版本；改口以追加记录形式保留。
- `POST /quick_settlements/{id}/payments`：登记收款（`inbound`）或退款（`refund`），只追加。同一回执号重复到达只回放结果、绝不重复入账；收款不得超过剩余应付，退款不得超过净收金额。
- `POST /quick_settlements/{id}/settle`：各方当前版本全部确认且净收恰好等于应付才允许结清。
- `POST /quick_settlements/{id}/close`：结清后才允许案件进入结案状态。
- `GET  /quick_settlements/{id}`：当前版本明细（分摊、免赔、各方状态、支付台账）。
- `GET  /quick_settlements/{id}/report`：办案人员与审计人员重建金额为何变化（每个报价版本对应一份守恒的分摊快照）、谁在何时确认/拒签/改口、每笔收支与回执，以及责任认定与结算单的全部哈希链审计事件。

所有结算写操作都会向与调度服务共享的 SHA-256 哈希链追加事件，篡改任一支付或确认事件会被 `GET /audit/chain` 发现。金额一律以 `Decimal` 计算并量化到分，按责任比例分摊后各方合计严格等于项目金额，尾差归集到主要责任方。
