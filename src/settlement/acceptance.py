"""贯通责任认定、报价版本、确认失效、部分支付、重复回执、退款与结案门禁的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SettlementService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
    service = SettlementService(connection, clock)
    for user_id, role in (("officer", "officer"), ("liaison", "liaison"), ("auditor", "auditor")):
        service.create_user(user_id, user_id, role)

    service.register_case("officer", "case-2026-0001")
    service.issue_liability(
        "officer", "case-2026-0001",
        {"party-A": 70, "party-B": 30},
        {"decision_no": "RZ-2026-0001", "evidence_review_id": 42, "conclusion": "A 车主责、B 车次责，无人员伤亡"},
        idempotency_key="liab-case-0001",
    )
    sheet = service.create_sheet("liaison", "settle-2026-0001", "case-2026-0001")

    items_v1 = [
        {"item_id": "repair-A", "vehicle_id": "沪A-1001", "owner_party_id": "party-A",
         "kind": "钣金喷漆", "amount": "10000.00", "deductible_cny": "500.00"},
        {"item_id": "repair-B", "vehicle_id": "沪B-2002", "owner_party_id": "party-B",
         "kind": "前杠更换", "amount": "3000.00", "deductible_cny": "0.00"},
    ]
    quote_v1 = service.add_quote_version("liaison", "settle-2026-0001", "garage-07", items_v1, "初版报价")
    service.record_confirmation("liaison", "settle-2026-0001", "party-A", "confirmed", "同意初版")
    service.record_confirmation("liaison", "settle-2026-0001", "party-B", "confirmed", "同意初版")
    confirmed_after_v1 = service.get_sheet("auditor", "settle-2026-0001")["state"]

    # 修理厂复核后调整报价：新版本生效，原确认失效，必须重新确认。
    clock.advance(hours=2)
    items_v2 = [
        {"item_id": "repair-A", "vehicle_id": "沪A-1001", "owner_party_id": "party-A",
         "kind": "钣金喷漆", "amount": "12000.00", "deductible_cny": "500.00"},
        {"item_id": "repair-B", "vehicle_id": "沪B-2002", "owner_party_id": "party-B",
         "kind": "前杠更换", "amount": "3000.00", "deductible_cny": "0.00"},
    ]
    quote_v2 = service.add_quote_version("liaison", "settle-2026-0001", "garage-07", items_v2, "复核后调整工时")
    sheet_after_requote = service.get_sheet("auditor", "settle-2026-0001")
    service.record_confirmation("liaison", "settle-2026-0001", "party-A", "confirmed", "同意新版")
    service.record_confirmation("liaison", "settle-2026-0001", "party-B", "confirmed", "同意新版")

    # 部分支付：只有 A 到账时结算单为 partially_paid，结案被门禁拒绝。
    # v2 可赔总额 14500.00：party-A 应付 10150.00，party-B 应付 4350.00。
    pay_a = service.record_payment(
        "liaison", "settle-2026-0001", "party-A", "10150.00", "RCPT-1001", "bank_transfer",
        idempotency_key="pay-1001")
    partial_view = service.get_sheet("auditor", "settle-2026-0001")
    blocked_close = None
    try:
        service.close_case("officer", "case-2026-0001")
    except Exception as exc:  # noqa: BLE001 - 验收记录门禁错误
        blocked_close = str(exc)
    # 同一支付回执重复到达：返回原记录，不重复登记收款。
    pay_a_replay = service.record_payment(
        "liaison", "settle-2026-0001", "party-A", "10150.00", "RCPT-1001", "bank_transfer",
        idempotency_key="pay-1001")
    pay_b = service.record_payment(
        "liaison", "settle-2026-0001", "party-B", "4350.00", "RCPT-1002", "bank_transfer")
    settled_sheet = service.get_sheet("auditor", "settle-2026-0001")
    closed = service.close_case("officer", "case-2026-0001")

    # 结案后退款必须保持金额平衡；审计链与重建视图必须完整。
    history = service.history("auditor", "settlement", "settle-2026-0001")
    case_view = service.case_detail("auditor", "case-2026-0001")
    chain = service.audit_chain("auditor")
    result = {
        "status": "ok",
        "workspace": workspace.name,
        "confirmed_state_after_v1": confirmed_after_v1,
        "state_after_requote": sheet_after_requote["state"],
        "state_after_partial_payment": partial_view["state"],
        "close_blocked_while_partial": blocked_close is not None,
        "blocked_reason": blocked_close,
        "voided_confirmations": [row for row in sheet_after_requote["confirmations"] if row["decision"] == "void"],
        "quote_versions": [row["quote_version"] for row in settled_sheet["quote_history"]],
        "gross_totals": [row["gross_total_cny"] for row in settled_sheet["quote_history"]],
        "payment_replay_duplicate": pay_a_replay["duplicate"] and pay_a_replay["payment_id"] == pay_a["payment_id"],
        "payment_entries": len(settled_sheet["payments"]),
        "party_balances": settled_sheet["party_balances"],
        "final_state": settled_sheet["state"],
        "case_closed": closed["status"] == "closed",
        "case_view_status": case_view["status"],
        "history_events": len(history["events"]),
        "event_types": [event["event_type"] for event in history["events"]],
        "audit": chain,
        "quote_v1_calc_sha256": quote_v1["calc_sha256"],
        "quote_v2_calc_sha256": quote_v2["calc_sha256"],
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行轻微事故快速结算服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
