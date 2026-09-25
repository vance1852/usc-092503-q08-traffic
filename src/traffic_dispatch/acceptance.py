"""贯通风险指数、道路走廊、应急资源库存、调度申请和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import TrafficDispatchService
from .settlement_service import QuickSettlementService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = TrafficDispatchService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    settlements = QuickSettlementService(connection, service.clock)
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor"), ("officer", "officer")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_risk_record("plan", {"risk_index": "COLLISION", "duty_date": f"2026-09-{index}", "index_value": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"center_id": "center-east", "name": "北部事故快处中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "500000"})
    service.create_facility("plan", {"center_id": "command-center-b", "name": "沿海终端", "kind": "command-center", "timezone": "Asia/Shanghai", "capacity_units": "800000"})
    service.create_route("plan", {"corridor_id": "corridor-east-1", "origin_center_id": "center-east", "destination_center_id": "command-center-b", "response_resource_kind": "patrol-unit", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 36})
    service.add_inventory_lot("dispatch", {"response_resource_lot_id": "lot-001", "center_id": "center-east", "response_resource_kind": "patrol-unit", "grade": "COLLISION", "quantity_units": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_dispatch("dispatch", {"dispatch_id": "nom-001", "corridor_id": "corridor-east-1", "incident_id": "medical-center-east", "duty_date": "2026-09-25", "requested_units": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "corridor-east-1", "2026-09-25")
    deployment = service.dispatch_deployment("dispatch", "deployment-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "road-section-restart", "name": "主干路恢复通行与事故需求回落", "risk_index_drop_percent": "9", "route_capacity_changes": {"corridor-east-1": "20"}, "demand_changes": {"center-east:patrol-unit": "-5"}})
    service.approve_scenario("risk", "road-section-restart", 1)
    scenario = service.run_scenario("plan", "road-section-restart", "2026-09-23")
    # 轻微事故快速结算：责任认定 -> 修理厂报价 -> 各方确认 -> 分笔支付 -> 结清 -> 结案
    settlements.record_liability("officer", {
        "determination_id": "liab-acc-042", "incident_id": "acc-2026-042",
        "parties": [
            {"party_id": "party-jia", "name": "甲车当事人", "share_bp": 7000},
            {"party_id": "party-yi", "name": "乙车当事人", "share_bp": 3000},
        ],
    })
    settlements.create_settlement("officer", {
        "settlement_id": "qs-acc-042", "determination_id": "liab-acc-042",
        "compensation_items": [{"item_id": "tow", "title": "拖车费", "amount": "300.00"}],
        "deductibles": {"party-jia": "100.00"},
    })
    settlements.submit_repair_quote("officer", "qs-acc-042", {
        "shop_id": "shop-shuntong", "shop_name": "顺通一类修理厂", "quote_ref": "QT-2026-042",
        "items": [{"item_id": "bumper", "title": "前保险杠复位", "amount": "2000.00"}],
    })
    settlements.confirm_party("officer", "qs-acc-042", "party-jia", "confirmed")
    settlements.confirm_party("officer", "qs-acc-042", "party-yi", "confirmed")
    settlements.register_payment("officer", "qs-acc-042", {
        "payment_id": "pay-042-1", "amount": "1000.00", "receipt_no": "BANK-042-1", "payer_party_id": "party-jia"})
    settlements.register_payment("officer", "qs-acc-042", {
        "payment_id": "pay-042-2", "amount": "510.00", "receipt_no": "BANK-042-2", "payer_party_id": "party-jia"})
    settlements.register_payment("officer", "qs-acc-042", {
        "payment_id": "pay-042-3", "amount": "690.00", "receipt_no": "BANK-042-3", "payer_party_id": "party-yi"})
    settlements.settle("officer", "qs-acc-042")
    settlements.close_case("officer", "qs-acc-042", "快速理赔结案")
    settlement_report = settlements.settlement_report("audit", "qs-acc-042")
    result = {"status": "ok", "index": service.risk_summary("COLLISION"), "plan_id": allocation["plan_id"], "deployment": deployment, "scenario_run_id": scenario["run_id"],
              "settlement": {
                  "settlement_id": "qs-acc-042",
                  "amount_timeline_versions": [item["quote_version"] for item in settlement_report["amount_timeline"]],
                  "payments": len(settlement_report["payment_ledger"]),
                  "events": [event["event_type"] for event in settlement_report["events"]],
              },
              "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行事故快处中心调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
