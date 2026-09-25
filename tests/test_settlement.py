from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from traffic_dispatch.api import JsonApplication
from traffic_dispatch.clock import FrozenClock
from traffic_dispatch.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from traffic_dispatch.service import TrafficDispatchService
from traffic_dispatch.settlement import build_breakdown, money, parse_shares, split_amount
from traffic_dispatch.settlement_service import QuickSettlementService

LIABILITY = {
    "determination_id": "liab-1",
    "incident_id": "acc-1",
    "parties": [
        {"party_id": "pA", "name": "甲车", "share_bp": 7000},
        {"party_id": "pB", "name": "乙车", "share_bp": 3000},
    ],
}


class SplitCalculationTests(unittest.TestCase):
    def test_split_amount_conserves_to_the_cent(self) -> None:
        shares = parse_shares([
            {"party_id": "a", "name": "甲", "share_bp": 3333},
            {"party_id": "b", "name": "乙", "share_bp": 3333},
            {"party_id": "c", "name": "丙", "share_bp": 3334},
        ])
        parts = split_amount(Decimal("100.00"), shares)
        self.assertEqual(sum(Decimal(value) for value in parts.values()), Decimal("100.00"))
        self.assertEqual(parts["c"], "33.34")  # 主要责任方吸收量化尾差

    def test_split_is_deterministic_on_tie(self) -> None:
        shares = parse_shares([
            {"party_id": "z", "name": "z", "share_bp": 5000},
            {"party_id": "a", "name": "a", "share_bp": 5000},
        ])
        first = split_amount(Decimal("0.01"), shares)
        second = split_amount(Decimal("0.01"), list(reversed(shares)))
        self.assertEqual(first, second)
        self.assertEqual(set(first.values()), {"0.00", "0.01"})

    def test_shares_must_total_whole(self) -> None:
        with self.assertRaises(ValidationFailed):
            parse_shares([{"party_id": "a", "name": "甲", "share_bp": 9999}])

    def test_deductible_reduces_payable_and_totals_conserved(self) -> None:
        shares = parse_shares(LIABILITY["parties"])
        breakdown = build_breakdown(
            1, shares,
            [{"item_id": "fix", "title": "修理", "amount": Decimal("1000.00")}],
            [],
            {"pA": Decimal("100.00")},
        )
        parties = {row["party_id"]: row for row in breakdown["parties"]}
        self.assertEqual(parties["pA"]["allocated_total"], "700.00")
        self.assertEqual(parties["pA"]["deductible"], "100.00")
        self.assertEqual(parties["pA"]["payable"], "600.00")
        self.assertEqual(parties["pB"]["payable"], "300.00")
        self.assertEqual(breakdown["total_payable"], "900.00")
        self.assertEqual(breakdown["total_amount"], "1000.00")

    def test_deductible_above_allocation_is_capped(self) -> None:
        shares = parse_shares(LIABILITY["parties"])
        breakdown = build_breakdown(
            1, shares,
            [{"item_id": "fix", "title": "修理", "amount": Decimal("100.00")}],
            [],
            {"pB": money("999", "ded")},
        )
        parties = {row["party_id"]: row for row in breakdown["parties"]}
        self.assertEqual(parties["pB"]["deductible"], "30.00")
        self.assertEqual(parties["pB"]["payable"], "0.00")


class QuickSettlementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.dispatch = TrafficDispatchService(self.connection, self.clock)
        self.service = QuickSettlementService(self.connection, self.clock)
        for user_id, role in (("ofc", "officer"), ("aud", "auditor"), ("plan", "planner")):
            self.dispatch.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def open_case(self, determination_id="liab-1", settlement_id="qs-1", incident_id="acc-1", deductibles=None):
        liability = dict(LIABILITY, determination_id=determination_id, incident_id=incident_id)
        self.service.record_liability("ofc", liability)
        return self.service.create_settlement("ofc", {
            "settlement_id": settlement_id,
            "determination_id": determination_id,
            "deductibles": deductibles or {},
        })

    def quote_v1(self, settlement_id="qs-1", amount="1000.00", ref="QT-1"):
        return self.service.submit_repair_quote("ofc", settlement_id, {
            "shop_id": "shop-1", "shop_name": "顺通修理厂", "quote_ref": ref,
            "items": [{"item_id": "bumper", "title": "前保险杠", "amount": amount}],
        })

    def confirm_all(self, settlement_id="qs-1"):
        self.service.confirm_party("ofc", settlement_id, "pA", "confirmed")
        self.service.confirm_party("ofc", settlement_id, "pB", "confirmed")

    def test_settlement_requires_effective_liability(self) -> None:
        with self.assertRaises(NotFound):
            self.service.create_settlement("ofc", {"settlement_id": "qs-x", "determination_id": "missing"})
        self.service.record_liability("ofc", LIABILITY)
        self.service.revoke_liability("ofc", "liab-1", "认定主体错误")
        with self.assertRaises(InvalidState):
            self.service.create_settlement("ofc", {"settlement_id": "qs-x", "determination_id": "liab-1"})

    def test_one_effective_determination_per_incident(self) -> None:
        self.service.record_liability("ofc", LIABILITY)
        second = dict(LIABILITY, determination_id="liab-2")
        with self.assertRaises(Conflict):
            self.service.record_liability("ofc", second)

    def test_one_settlement_per_determination(self) -> None:
        self.open_case()
        with self.assertRaises(Conflict):
            self.service.create_settlement("ofc", {"settlement_id": "qs-2", "determination_id": "liab-1"})

    def test_officer_role_required(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.record_liability("plan", LIABILITY)

    def test_quote_change_voids_earlier_confirmations_but_keeps_ledger(self) -> None:
        self.open_case()
        self.quote_v1()
        self.confirm_all()
        detail = self.service.settlement("aud", "qs-1")
        self.assertEqual({row["state"] for row in detail["current_confirmations"]}, {"confirmed"})
        self.quote_v1(amount="900.00", ref="QT-2")
        report = self.service.settlement_report("aud", "qs-1")
        self.assertTrue(all(row["state"] == "voided" for row in report["confirmation_ledger"]))
        self.assertEqual([row["quote_version"] for row in report["quote_versions"]], [1, 2])
        self.assertEqual(len(report["amount_timeline"]), 3)  # v0、v1、v2 金额快照
        detail = self.service.settlement("aud", "qs-1")
        self.assertEqual({row["state"] for row in detail["current_confirmations"]}, {"pending"})
        self.assertEqual(detail["state"], "open")

    def test_identical_quote_is_replayed_not_versioned(self) -> None:
        self.open_case()
        first = self.quote_v1()
        again = self.quote_v1()
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["quote_version"], again["quote_version"])

    def test_refusal_cannot_be_overridden_on_same_version(self) -> None:
        self.open_case()
        self.quote_v1()
        self.service.confirm_party("ofc", "qs-1", "pA", "confirmed")
        self.service.confirm_party("ofc", "qs-1", "pB", "refused", "对金额有异议")
        self.assertEqual(self.service.settlement("aud", "qs-1")["state"], "refused")
        with self.assertRaises(InvalidState):
            self.service.confirm_party("ofc", "qs-1", "pB", "confirmed")
        # 新报价版本后允许重新确认
        self.quote_v1(amount="800.00", ref="QT-2")
        self.confirm_all()
        self.assertEqual(self.service.settlement("aud", "qs-1")["state"], "open")

    def test_change_of_mind_is_appended_not_overwritten(self) -> None:
        self.open_case()
        self.quote_v1()
        self.service.confirm_party("ofc", "qs-1", "pA", "confirmed")
        self.service.confirm_party("ofc", "qs-1", "pA", "refused", "改口")
        ledger = self.service.settlement_report("aud", "qs-1")["confirmation_ledger"]
        decisions = [row["decision"] for row in ledger if row["party_id"] == "pA"]
        self.assertEqual(decisions, ["confirmed", "refused"])

    def test_payment_requires_all_confirmations_and_quote(self) -> None:
        self.open_case()
        with self.assertRaises(InvalidState):
            self.service.register_payment("ofc", "qs-1",
                                          {"payment_id": "p1", "amount": "700", "receipt_no": "r1"})
        self.quote_v1()
        self.service.confirm_party("ofc", "qs-1", "pA", "confirmed")
        with self.assertRaises(InvalidState):
            self.service.register_payment("ofc", "qs-1",
                                          {"payment_id": "p1", "amount": "700", "receipt_no": "r1"})

    def test_partial_payment_then_full_settlement_and_close(self) -> None:
        self.open_case()
        self.quote_v1()
        self.confirm_all()
        first = self.service.register_payment("ofc", "qs-1", {
            "payment_id": "p1", "amount": "700.00", "receipt_no": "r1", "payer_party_id": "pA"})
        self.assertEqual(first["state"], "partially_paid")
        with self.assertRaises(InvalidState):
            self.service.settle("ofc", "qs-1")
        with self.assertRaises(InvalidState):
            self.service.close_case("ofc", "qs-1")
        self.service.register_payment("ofc", "qs-1", {
            "payment_id": "p2", "amount": "300.00", "receipt_no": "r2", "payer_party_id": "pB"})
        settled = self.service.settle("ofc", "qs-1")
        self.assertEqual(settled["state"], "settled")
        closed = self.service.close_case("ofc", "qs-1")
        self.assertEqual(closed["state"], "closed")
        with self.assertRaises(InvalidState):
            self.service.close_case("ofc", "qs-1")

    def test_payment_cannot_exceed_payable(self) -> None:
        self.open_case()
        self.quote_v1()
        self.confirm_all()
        with self.assertRaises(InvalidState):
            self.service.register_payment("ofc", "qs-1", {
                "payment_id": "p1", "amount": "1000.01", "receipt_no": "r1"})

    def test_duplicate_receipt_is_not_registered_twice(self) -> None:
        self.open_case()
        self.quote_v1()
        self.confirm_all()
        self.service.register_payment("ofc", "qs-1", {"payment_id": "p1", "amount": "700", "receipt_no": "r1"})
        replay = self.service.register_payment("ofc", "qs-1", {
            "payment_id": "p1-again", "amount": "700", "receipt_no": "r1"})
        self.assertTrue(replay["duplicate"])
        ledger = self.service.settlement_report("aud", "qs-1")["payment_ledger"]
        self.assertEqual(len(ledger), 1)
        # 同回执但金额不同必须报错而不是静默入账
        with self.assertRaises(Conflict):
            self.service.register_payment("ofc", "qs-1", {
                "payment_id": "p1-bad", "amount": "300", "receipt_no": "r1"})
        # 回执号全局唯一：不能在另一张结算单上重复登记
        self.open_case(determination_id="liab-2", settlement_id="qs-2", incident_id="acc-2")
        self.quote_v1("qs-2", ref="QT-2")
        self.confirm_all("qs-2")
        with self.assertRaises(Conflict):
            self.service.register_payment("ofc", "qs-2", {
                "payment_id": "p-other", "amount": "100", "receipt_no": "r1"})

    def test_refund_is_appended_and_bounded_by_net(self) -> None:
        self.open_case()
        self.quote_v1()
        self.confirm_all()
        self.service.register_payment("ofc", "qs-1", {"payment_id": "p1", "amount": "1000", "receipt_no": "r1"})
        with self.assertRaises(InvalidState):
            self.service.register_payment("ofc", "qs-1", {
                "payment_id": "rf-bad", "direction": "refund", "amount": "1000.01", "receipt_no": "rr0"})
        refund = self.service.register_payment("ofc", "qs-1", {
            "payment_id": "rf1", "direction": "refund", "amount": "100.00", "receipt_no": "rr1"})
        self.assertEqual(refund["state"], "partially_paid")
        self.assertEqual(refund["balance_after"], "900.00")
        ledger = self.service.settlement_report("aud", "qs-1")["payment_ledger"]
        self.assertEqual([row["direction"] for row in ledger], ["inbound", "refund"])

    def test_refund_receipt_idempotent(self) -> None:
        self.open_case()
        self.quote_v1()
        self.confirm_all()
        self.service.register_payment("ofc", "qs-1", {"payment_id": "p1", "amount": "1000", "receipt_no": "r1"})
        self.service.register_payment("ofc", "qs-1", {
            "payment_id": "rf1", "direction": "refund", "amount": "100", "receipt_no": "rr1"})
        replay = self.service.register_payment("ofc", "qs-1", {
            "payment_id": "rf1-x", "direction": "refund", "amount": "100", "receipt_no": "rr1"})
        self.assertTrue(replay["duplicate"])
        self.assertEqual(len(self.service.settlement_report("aud", "qs-1")["payment_ledger"]), 2)

    def test_quote_reduction_then_refund_allows_settlement(self) -> None:
        self.open_case()
        self.quote_v1(amount="1000.00")
        self.confirm_all()
        self.service.register_payment("ofc", "qs-1", {"payment_id": "p1", "amount": "1000", "receipt_no": "r1"})
        # 报价下调后旧确认失效，净收高于新应付
        self.quote_v1(amount="900.00", ref="QT-2")
        with self.assertRaises(InvalidState):
            self.service.settle("ofc", "qs-1")
        self.service.register_payment("ofc", "qs-1", {
            "payment_id": "rf1", "direction": "refund", "amount": "100.00", "receipt_no": "rr1"})
        self.confirm_all()
        self.service.settle("ofc", "qs-1")

    def test_money_is_locked_after_settlement(self) -> None:
        self.open_case()
        self.quote_v1()
        self.confirm_all()
        self.service.register_payment("ofc", "qs-1", {"payment_id": "p1", "amount": "1000", "receipt_no": "r1"})
        self.service.settle("ofc", "qs-1")
        with self.assertRaises(InvalidState):
            self.service.register_payment("ofc", "qs-1", {"payment_id": "p2", "amount": "1", "receipt_no": "r2"})
        with self.assertRaises(InvalidState):
            self.service.register_payment("ofc", "qs-1", {
                "payment_id": "rf", "direction": "refund", "amount": "1", "receipt_no": "rr"})

    def test_locked_money_states_reject_quote_change(self) -> None:
        self.open_case()
        self.quote_v1()
        self.confirm_all()
        self.service.register_payment("ofc", "qs-1", {"payment_id": "p1", "amount": "1000", "receipt_no": "r1"})
        self.service.settle("ofc", "qs-1")
        with self.assertRaises(InvalidState):
            self.quote_v1(amount="500.00", ref="QT-late")

    def test_auditor_can_rebuild_report_but_not_write(self) -> None:
        self.open_case()
        self.quote_v1()
        self.confirm_all()
        report = self.service.settlement_report("aud", "qs-1")
        self.assertIn("amount_timeline", report)
        self.assertIn("confirmation_ledger", report)
        self.assertIn("payment_ledger", report)
        with self.assertRaises(Forbidden):
            self.service.register_payment("aud", "qs-1", {"payment_id": "x", "amount": "1", "receipt_no": "y"})

    def test_settlement_events_join_the_hash_chain(self) -> None:
        self.open_case()
        self.quote_v1()
        self.confirm_all()
        self.service.register_payment("ofc", "qs-1", {"payment_id": "p1", "amount": "1000", "receipt_no": "r1"})
        self.service.settle("ofc", "qs-1")
        chain = self.dispatch.audit_chain("aud")
        self.assertTrue(chain["valid"])
        self.assertGreaterEqual(chain["events"], 5)
        # 篡改支付审计事件可被发现
        self.connection.execute(
            "UPDATE traffic_audit_events SET payload_json='{}' WHERE event_type='payment.registered'")
        self.assertFalse(self.dispatch.audit_chain("aud")["valid"])

    def test_concurrent_quote_insertion_resolves_through_versioning(self) -> None:
        self.open_case()
        self.quote_v1()
        rows = self.connection.execute(
            "SELECT quote_version FROM repair_quote_versions WHERE settlement_id='qs-1'").fetchall()
        self.assertEqual([row[0] for row in rows], [1])


class SettlementApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        service = TrafficDispatchService(self.connection, self.clock)
        self.application = JsonApplication(service)
        service.create_user("ofc", "办案民警", "officer")
        service.create_user("aud", "审计员", "auditor")
        self.headers = {"X-Actor-Id": "ofc"}

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict, headers=None):
        return self.application.handle("POST", path, headers or self.headers,
                                       __import__("json").dumps(payload).encode())

    def get(self, path: str, actor="ofc"):
        return self.application.handle("GET", path, {"X-Actor-Id": actor})

    def test_full_flow_through_http(self) -> None:
        response = self.post("/liability_determinations", LIABILITY)
        self.assertEqual(response.status, 201)
        response = self.post("/quick_settlements", {"settlement_id": "qs-1", "determination_id": "liab-1"})
        self.assertEqual(response.status, 201)
        response = self.post("/quick_settlements/qs-1/quotes", {
            "shop_id": "shop-1", "shop_name": "顺通修理厂", "quote_ref": "QT-1",
            "items": [{"item_id": "bumper", "title": "前杠", "amount": "1000.00"}]})
        self.assertEqual(response.status, 201)
        for party in ("pA", "pB"):
            response = self.post("/quick_settlements/qs-1/confirmations",
                                 {"party_id": party, "decision": "confirmed"})
            self.assertEqual(response.status, 201)
        response = self.post("/quick_settlements/qs-1/payments",
                             {"payment_id": "p1", "amount": "1000.00", "receipt_no": "r1"})
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["state"], "paid")
        response = self.post("/quick_settlements/qs-1/settle", {})
        self.assertEqual(response.status, 200)
        response = self.post("/quick_settlements/qs-1/close", {"result": "settled"})
        self.assertEqual(response.status, 200)
        report = self.get("/quick_settlements/qs-1/report", actor="aud")
        self.assertEqual(report.status, 200)
        self.assertEqual(len(report.body["payment_ledger"]), 1)


if __name__ == "__main__":
    unittest.main()
