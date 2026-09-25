from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from settlement.clock import FrozenClock
from settlement.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from settlement.money import compute_settlement
from settlement.service import SettlementService


ITEMS_V1 = [
    {"item_id": "repair-A", "vehicle_id": "沪A-1001", "owner_party_id": "party-A",
     "kind": "钣金喷漆", "amount": "10000.00", "deductible_cny": "500.00"},
    {"item_id": "repair-B", "vehicle_id": "沪B-2002", "owner_party_id": "party-B",
     "kind": "前杠更换", "amount": "3000.00", "deductible_cny": "0.00"},
]


class MoneyCalcTests(unittest.TestCase):
    def test_deductible_self_borne_and_split_sums_exactly(self) -> None:
        result = compute_settlement({"A": 70, "B": 30}, [
            {"item_id": "x", "vehicle_id": "v", "owner_party_id": "A", "kind": "k",
             "amount": "100.00", "deductible_cny": "10.00"},
        ])
        self.assertEqual(result["gross_total_cny"], "100.00")
        self.assertEqual(result["deductible_total_cny"], "10.00")
        self.assertEqual(result["claim_total_cny"], "90.00")
        self.assertEqual(result["parties"][0]["claim_due_cny"], "63.00")
        self.assertEqual(result["parties"][1]["claim_due_cny"], "27.00")

    def test_largest_remainder_absorbs_fractional_cents(self) -> None:
        # 100.00 按 1/3、1/3、1/3 切分：33.33 + 33.33 + 33.34，合计分毫不差。
        result = compute_settlement({"A": "33.33", "B": "33.33", "C": "33.34"}, [
            {"item_id": "x", "vehicle_id": "v", "owner_party_id": "A", "kind": "k",
             "amount": "100.00", "deductible_cny": "0"},
        ])
        splits = result["items"][0]["split"]
        self.assertEqual(splits, {"A": "33.33", "B": "33.33", "C": "33.34"})

    def test_shares_must_total_one_hundred(self) -> None:
        with self.assertRaises(ValueError):
            compute_settlement({"A": 70, "B": 20}, ITEMS_V1)

    def test_deductible_cannot_exceed_item_amount(self) -> None:
        with self.assertRaises(ValueError):
            compute_settlement({"A": 70, "B": 30}, [
                {"item_id": "x", "vehicle_id": "v", "owner_party_id": "A", "kind": "k",
                 "amount": "100.00", "deductible_cny": "100.01"},
            ])

    def test_calc_is_deterministic_and_versioned(self) -> None:
        first = compute_settlement({"party-A": 70, "party-B": 30}, ITEMS_V1)
        second = compute_settlement({"party-A": 70, "party-B": 30}, list(reversed(ITEMS_V1)))
        self.assertEqual(first["calc_sha256"], second["calc_sha256"])
        self.assertEqual(first["calc_rules_version"], "1.0")


class SettlementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.service = SettlementService(self.connection, self.clock)
        for user_id, role in (("off", "officer"), ("lia", "liaison"), ("aud", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.register_case("off", "case-1")

    def tearDown(self) -> None:
        self.connection.close()

    def _liability(self, case_id: str = "case-1") -> None:
        self.service.issue_liability(
            "off", case_id, {"party-A": 70, "party-B": 30},
            {"decision_no": "RZ-1", "conclusion": "A 主 B 次"})

    def _sheet(self) -> str:
        self._liability()
        self.service.create_sheet("lia", "s-1", "case-1")
        self.service.add_quote_version("lia", "s-1", "garage-07", ITEMS_V1, "初版")
        return "s-1"

    def test_role_boundaries(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_case("lia", "case-x")
        with self.assertRaises(Forbidden):
            self.service.issue_liability("aud", "case-1", {"party-A": 100}, {"n": 1})
        with self.assertRaises(Forbidden):
            self.service.close_case("lia", "case-1")
        with self.assertRaises(Forbidden):
            self.service.record_payment("off", "s", "party-A", "1", "r", "bank")

    def test_sheet_requires_effective_liability(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.create_sheet("lia", "s-1", "case-1")

    def test_liability_revisions_append_only(self) -> None:
        self._liability()
        first = self.service.case_detail("aud", "case-1")["liability_findings"][0]
        self.service.issue_liability(
            "off", "case-1", {"party-A": 80, "party-B": 20}, {"decision_no": "RZ-2"})
        findings = self.service.case_detail("aud", "case-1")["liability_findings"]
        self.assertEqual([row["state"] for row in findings], ["superseded", "effective"])
        self.assertEqual(findings[1]["supersedes_finding_id"], first["finding_id"])
        # 完全相同的认定重复提交是幂等返回，不新增版本。
        again = self.service.issue_liability(
            "off", "case-1", {"party-A": 80, "party-B": 20}, {"decision_no": "RZ-2"})
        self.assertTrue(again["duplicate"])

    def test_requote_voids_confirmations_but_keeps_history(self) -> None:
        sheet_id = self._sheet()
        self.service.record_confirmation("lia", sheet_id, "party-A", "confirmed")
        self.service.record_confirmation("lia", sheet_id, "party-B", "confirmed")
        self.assertEqual(self.service.get_sheet("aud", sheet_id)["state"], "confirmed")
        self.clock.advance(hours=1)
        self.service.add_quote_version("lia", sheet_id, "garage-07", [
            {"item_id": "repair-A", "vehicle_id": "沪A-1001", "owner_party_id": "party-A",
             "kind": "钣金喷漆", "amount": "12000.00", "deductible_cny": "500.00"},
            {"item_id": "repair-B", "vehicle_id": "沪B-2002", "owner_party_id": "party-B",
             "kind": "前杠更换", "amount": "3000.00", "deductible_cny": "0.00"},
        ], "调价")
        view = self.service.get_sheet("aud", sheet_id)
        self.assertEqual(view["state"], "awaiting_confirmation")
        self.assertEqual(view["current_quote_version"], 2)
        decisions = {(row["quote_version"], row["party_id"]): row["decision"]
                     for row in view["confirmations"]}
        self.assertEqual(decisions[(1, "party-A")], "void")
        self.assertEqual(decisions[(1, "party-B")], "void")
        self.assertEqual(len(view["quote_history"]), 2)
        self.assertEqual(view["quote_history"][0]["gross_total_cny"], "13000.00")
        self.assertEqual(view["quote_history"][1]["gross_total_cny"], "15000.00")

    def test_rejection_is_final_and_cannot_be_overwritten(self) -> None:
        sheet_id = self._sheet()
        self.service.record_confirmation("lia", sheet_id, "party-A", "rejected", "对金额有异议")
        with self.assertRaises(Conflict):
            self.service.record_confirmation("lia", sheet_id, "party-A", "confirmed")
        # 新报价版本开启新一轮意思表示，旧拒签作为历史事实仍然保留。
        self.service.add_quote_version("lia", sheet_id, "garage-07", ITEMS_V1)
        self.service.record_confirmation("lia", sheet_id, "party-A", "confirmed", "重新报价后同意")
        view = self.service.get_sheet("aud", sheet_id)
        self.assertEqual(
            [(row["quote_version"], row["decision"]) for row in view["confirmations"] if row["party_id"] == "party-A"],
            [(1, "rejected"), (2, "confirmed")],
        )

    def test_partial_payment_blocks_close_but_does_not_lose_receipt(self) -> None:
        sheet_id = self._sheet()
        self.service.record_confirmation("lia", sheet_id, "party-A", "confirmed")
        self.service.record_confirmation("lia", sheet_id, "party-B", "confirmed")
        self.service.record_payment("lia", sheet_id, "party-A", "8650.00", "R-1", "bank")
        view = self.service.get_sheet("aud", sheet_id)
        self.assertEqual(view["state"], "partially_paid")
        with self.assertRaises(InvalidState):
            self.service.close_case("off", "case-1")

    def test_duplicate_receipt_never_double_posted(self) -> None:
        sheet_id = self._sheet()
        first = self.service.record_payment("lia", sheet_id, "party-B", "3650.00", "R-9", "bank")
        replay = self.service.record_payment("lia", sheet_id, "party-B", "3650.00", "R-9", "bank")
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["payment_id"], first["payment_id"])
        self.assertEqual(len(self.service.get_sheet("aud", sheet_id)["payments"]), 1)
        # 相同回执、不同金额/当事人是冲突，不得静默当作重复。
        with self.assertRaises(Conflict):
            self.service.record_payment("lia", sheet_id, "party-A", "1.00", "R-9", "bank")

    def test_idempotency_key_conflict_detection(self) -> None:
        sheet_id = self._sheet()
        self.service.record_payment(
            "lia", sheet_id, "party-B", "3650.00", "R-10", "bank", idempotency_key="K-10")
        with self.assertRaises(Conflict):
            self.service.record_payment(
                "lia", sheet_id, "party-B", "1.00", "R-11", "bank", idempotency_key="K-10")

    def test_full_flow_settles_and_closes_then_refund_reopens(self) -> None:
        sheet_id = self._sheet()
        self.service.record_confirmation("lia", sheet_id, "party-A", "confirmed")
        self.service.record_confirmation("lia", sheet_id, "party-B", "confirmed")
        # v1: claim A = 9500*0.7+3000*0.7 = 8750.00? 由计算模块给出，按返回值收款。
        view = self.service.get_sheet("aud", sheet_id)
        dues = {row["party_id"]: row["claim_due_cny"] for row in view["calc"]["parties"]}
        self.service.record_payment("lia", sheet_id, "party-A", dues["party-A"], "RA", "bank")
        self.service.record_payment("lia", sheet_id, "party-B", dues["party-B"], "RB", "bank")
        self.assertEqual(self.service.get_sheet("aud", sheet_id)["state"], "settled")
        self.service.close_case("off", "case-1")
        # 结案后重复回执仍然原样重放，绝不重复登记。
        replay = self.service.record_payment("lia", sheet_id, "party-A", dues["party-A"], "RA", "bank")
        self.assertTrue(replay["duplicate"])
        # 结案后不能再报价或重新确认；退款造成缺口也被拒绝。
        with self.assertRaises(InvalidState):
            self.service.add_quote_version("lia", sheet_id, "garage-07", ITEMS_V1)
        with self.assertRaises(InvalidState):
            self.service.record_refund("lia", sheet_id, "party-A", "1.00", "RF", "bank")
        # 办案民警与审计人员都能重建金额与确认过程。
        officer_history = self.service.history("off", "settlement", sheet_id)
        self.assertIn("sheet.settled", [event["event_type"] for event in officer_history["events"]])
        self.assertTrue(self.service.audit_chain("off")["valid"])
        # 结案前场景：先退款再结案。退款不覆盖原收款记录。
        self.service2_flow_with_refund_before_close()

    def service2_flow_with_refund_before_close(self) -> None:
        self.service.register_case("off", "case-2")
        self.service.issue_liability(
            "off", "case-2", {"party-A": 100}, {"decision_no": "RZ-2"})
        self.service.create_sheet("lia", "s-2", "case-2")
        self.service.add_quote_version("lia", "s-2", "garage-07", [
            {"item_id": "x", "vehicle_id": "vA", "owner_party_id": "party-A",
             "kind": "k", "amount": "1000.00", "deductible_cny": "0.00"},
        ])
        self.service.record_confirmation("lia", "s-2", "party-A", "confirmed")
        self.service.record_payment("lia", "s-2", "party-A", "1000.00", "R2", "bank")
        self.service.record_refund("lia", "s-2", "party-A", "200.00", "RF2", "bank", "R2")
        view = self.service.get_sheet("aud", "s-2")
        self.assertEqual(view["state"], "partially_paid")
        directions = [(row["direction"], row["receipt_no"]) for row in view["payments"]]
        self.assertEqual(directions, [("inbound", "R2"), ("refund", "RF2")])
        self.assertEqual(view["total_balance_cny"], "800.00")
        with self.assertRaises(InvalidState):
            self.service.close_case("off", "case-2")
        self.service.record_payment("lia", "s-2", "party-A", "200.00", "R3", "bank")
        self.service.close_case("off", "case-2")

    def test_quote_decrease_after_payment_requires_refund_before_settlement(self) -> None:
        self.service.register_case("off", "case-3")
        self.service.issue_liability("off", "case-3", {"party-A": 100}, {"decision_no": "RZ-3"})
        self.service.create_sheet("lia", "s-3", "case-3")
        self.service.add_quote_version("lia", "s-3", "garage-07", [
            {"item_id": "x", "vehicle_id": "vA", "owner_party_id": "party-A",
             "kind": "k", "amount": "1000.00", "deductible_cny": "0.00"},
        ])
        self.service.record_confirmation("lia", "s-3", "party-A", "confirmed")
        self.service.record_payment("lia", "s-3", "party-A", "1000.00", "R30", "bank")
        self.assertEqual(self.service.get_sheet("aud", "s-3")["state"], "settled")
        # 结案前修理厂下调报价至 800：原确认失效，重新确认后余额多出 200，不能直接结清。
        self.service.add_quote_version("lia", "s-3", "garage-07", [
            {"item_id": "x", "vehicle_id": "vA", "owner_party_id": "party-A",
             "kind": "k", "amount": "800.00", "deductible_cny": "0.00"},
        ], "核减工时")
        self.service.record_confirmation("lia", "s-3", "party-A", "confirmed")
        view = self.service.get_sheet("aud", "s-3")
        self.assertEqual(view["state"], "partially_paid")
        self.assertEqual(view["party_balances"][0]["overpaid_cny"], "200.00")
        with self.assertRaises(InvalidState):
            self.service.close_case("off", "case-3")
        # 退还多出的 200 后分文不差，方可结清结案。
        self.service.record_refund("lia", "s-3", "party-A", "200.00", "RF30", "bank", "R30")
        self.assertEqual(self.service.get_sheet("aud", "s-3")["state"], "settled")
        self.service.close_case("off", "case-3")

    def test_refund_cannot_exceed_balance(self) -> None:
        sheet_id = self._sheet()
        with self.assertRaises(InvalidState):
            self.service.record_refund("lia", sheet_id, "party-A", "1.00", "RF", "bank")

    def test_refund_must_reference_real_receipt(self) -> None:
        sheet_id = self._sheet()
        self.service.record_payment("lia", sheet_id, "party-B", "3650.00", "RB", "bank")
        with self.assertRaises(ValidationFailed):
            self.service.record_refund(
                "lia", sheet_id, "party-B", "10.00", "RF", "bank", "GHOST")
        # 引用他人的回执也不行。
        with self.assertRaises(ValidationFailed):
            self.service.record_refund(
                "lia", sheet_id, "party-A", "10.00", "RF2", "bank", "RB")

    def test_overpayment_is_rejected(self) -> None:
        sheet_id = self._sheet()
        with self.assertRaises(InvalidState):
            self.service.record_payment("lia", sheet_id, "party-A", "999999.00", "RX", "bank")

    def test_audit_history_reconstructs_who_what_when(self) -> None:
        sheet_id = self._sheet()
        self.service.record_confirmation("lia", sheet_id, "party-A", "confirmed")
        history = self.service.history("aud", "settlement", sheet_id)
        types = [event["event_type"] for event in history["events"]]
        self.assertEqual(types, ["sheet.created", "quote.versioned", "party.confirmed"])
        confirm_event = history["events"][-1]
        self.assertEqual(confirm_event["actor_id"], "lia")
        self.assertEqual(confirm_event["payload"]["party_id"], "party-A")
        self.assertIn("2026-09-25", confirm_event["created_at"])
        chain = self.service.audit_chain("aud")
        self.assertTrue(chain["valid"])
        self.connection.execute(
            "UPDATE settlement_audit_events SET payload_json='{\"tampered\":true}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("aud")["valid"])

    def test_unknown_user_and_case(self) -> None:
        with self.assertRaises(NotFound):
            self.service.get_sheet("aud", "missing")
        with self.assertRaises(NotFound):
            self.service.register_case("ghost", "case-9")


if __name__ == "__main__":
    unittest.main()
