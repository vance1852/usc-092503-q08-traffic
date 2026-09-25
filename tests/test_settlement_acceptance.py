from __future__ import annotations

import unittest
from pathlib import Path

from settlement.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class SettlementAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        # v1 确认后又被 v2 报价失效。
        self.assertEqual(result["confirmed_state_after_v1"], "confirmed")
        self.assertEqual(result["state_after_requote"], "awaiting_confirmation")
        self.assertEqual(len(result["voided_confirmations"]), 2)
        # 两个报价版本与金额都保留在历史中。
        self.assertEqual(result["quote_versions"], [1, 2])
        self.assertEqual(result["gross_totals"], ["13000.00", "15000.00"])
        # 部分支付阻断结案，付完后才关闭。
        self.assertEqual(result["state_after_partial_payment"], "partially_paid")
        self.assertTrue(result["close_blocked_while_partial"])
        self.assertTrue(result["case_closed"])
        # 重复回执不重复入账。
        self.assertTrue(result["payment_replay_duplicate"])
        self.assertEqual(result["payment_entries"], 2)
        # 金额分毫不差，审计链完整。
        self.assertTrue(all(row["covered"] for row in result["party_balances"]))
        self.assertTrue(result["audit"]["valid"])
        self.assertGreaterEqual(result["history_events"], 7)
        self.assertIn("sheet.settled", result["event_types"])


if __name__ == "__main__":
    unittest.main()
