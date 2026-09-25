from __future__ import annotations

import json
import sqlite3
import unittest

from settlement.api import JsonApplication
from settlement.service import SettlementService


def post(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class SettlementApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(SettlementService(self.connection))
        self.app.handle("POST", "/users", body=post(
            {"user_id": "off", "display_name": "民警", "role": "officer"}))
        self.app.handle("POST", "/users", body=post(
            {"user_id": "lia", "display_name": "联络员", "role": "liaison"}))
        self.app.handle("POST", "/users", body=post(
            {"user_id": "aud", "display_name": "审计", "role": "auditor"}))

    def tearDown(self) -> None:
        self.connection.close()

    def _full_flow_until_confirmed(self) -> None:
        self.assertEqual(self.app.handle(
            "POST", "/cases", {"X-Actor-Id": "off"}, post({"case_id": "case-1"})).status, 201)
        response = self.app.handle(
            "POST", "/cases/case-1/liability", {"X-Actor-Id": "off"},
            post({"shares": {"party-A": 70, "party-B": 30}, "basis": {"decision_no": "RZ-1"}}))
        self.assertEqual(response.status, 201, response.body)
        self.assertEqual(self.app.handle(
            "POST", "/settlements", {"X-Actor-Id": "lia"},
            post({"settlement_id": "s-1", "case_id": "case-1"})).status, 201)
        items = [
            {"item_id": "repair-A", "vehicle_id": "沪A-1001", "owner_party_id": "party-A",
             "kind": "钣金", "amount": "10000.00", "deductible_cny": "500.00"},
            {"item_id": "repair-B", "vehicle_id": "沪B-2002", "owner_party_id": "party-B",
             "kind": "前杠", "amount": "3000.00", "deductible_cny": "0.00"},
        ]
        self.assertEqual(self.app.handle(
            "POST", "/settlements/s-1/quotes", {"X-Actor-Id": "lia"},
            post({"garage_id": "g-7", "items": items})) .status, 201)
        for party in ("party-A", "party-B"):
            self.assertEqual(self.app.handle(
                "POST", "/settlements/s-1/confirmations", {"X-Actor-Id": "lia"},
                post({"party_id": party, "decision": "confirmed"})).status, 201)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_actor_header_required(self) -> None:
        response = self.app.handle("GET", "/cases/case-1")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_role_forbidden_via_api(self) -> None:
        response = self.app.handle(
            "POST", "/cases", {"X-Actor-Id": "lia"}, post({"case_id": "case-x"}))
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")

    def test_end_to_end_payment_dedup_and_close_gate(self) -> None:
        self._full_flow_until_confirmed()
        payment = {"party_id": "party-A", "amount_cny": "8750.00",
                   "receipt_no": "R-1", "channel": "bank"}
        first = self.app.handle(
            "POST", "/settlements/s-1/payments", {"X-Actor-Id": "lia"}, post(payment))
        self.assertEqual(first.status, 201)
        replay = self.app.handle(
            "POST", "/settlements/s-1/payments", {"X-Actor-Id": "lia"}, post(payment))
        self.assertEqual(replay.status, 201)
        self.assertTrue(replay.body["duplicate"])
        self.assertEqual(replay.body["payment_id"], first.body["payment_id"])
        # 只收了一方的钱，结案被门禁拒绝。
        blocked = self.app.handle("POST", "/cases/case-1/close", {"X-Actor-Id": "off"})
        self.assertEqual(blocked.status, 409)
        self.assertEqual(blocked.body["error"]["code"], "invalid_state")
        # 另一方到账后才允许结案。
        paid = self.app.handle(
            "POST", "/settlements/s-1/payments", {"X-Actor-Id": "lia"},
            post({"party_id": "party-B", "amount_cny": "3750.00",
                  "receipt_no": "R-2", "channel": "bank"}))
        self.assertEqual(paid.status, 201)
        closed = self.app.handle("POST", "/cases/case-1/close", {"X-Actor-Id": "off"})
        self.assertEqual(closed.status, 200)
        self.assertEqual(closed.body["status"], "closed")

    def test_rejection_cannot_be_overwritten_via_api(self) -> None:
        self._full_flow_until_confirmed()
        # 修理厂提交 v2 报价，原确认失效。
        response = self.app.handle(
            "POST", "/settlements/s-1/quotes", {"X-Actor-Id": "lia"},
            post({"garage_id": "g-7", "items": [
                {"item_id": "repair-A", "vehicle_id": "沪A-1001", "owner_party_id": "party-A",
                 "kind": "钣金", "amount": "11000.00", "deductible_cny": "500.00"},
                {"item_id": "repair-B", "vehicle_id": "沪B-2002", "owner_party_id": "party-B",
                 "kind": "前杠", "amount": "3000.00", "deductible_cny": "0.00"}]}))
        self.assertEqual(response.status, 201, response.body)
        reject = self.app.handle(
            "POST", "/settlements/s-1/confirmations", {"X-Actor-Id": "lia"},
            post({"party_id": "party-A", "decision": "rejected", "note": "有异议"}))
        self.assertEqual(reject.status, 201)
        flip = self.app.handle(
            "POST", "/settlements/s-1/confirmations", {"X-Actor-Id": "lia"},
            post({"party_id": "party-A", "decision": "confirmed"}))
        self.assertEqual(flip.status, 409)
        self.assertEqual(flip.body["error"]["code"], "conflict")

    def test_history_endpoint_reconstructs_events(self) -> None:
        self._full_flow_until_confirmed()
        response = self.app.handle(
            "GET", "/history/settlement/s-1", {"X-Actor-Id": "aud"})
        self.assertEqual(response.status, 200)
        types = [event["event_type"] for event in response.body["events"]]
        self.assertEqual(types, ["sheet.created", "quote.versioned",
                                 "party.confirmed", "party.confirmed"])
        chain = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "aud"})
        self.assertTrue(chain.body["valid"])


if __name__ == "__main__":
    unittest.main()
