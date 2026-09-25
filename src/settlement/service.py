"""轻微事故快速结算用例：责任版本、报价版本、确认、支付台账与结案门禁。"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .money import CalcError, ZERO, canonical_json, compute_settlement, digest, money, percent
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "officer": {
        "case.register", "liability.issue", "case.close", "sheet.abandon",
        "case.read", "sheet.read", "audit.read",
    },
    "liaison": {
        "sheet.create", "quote.write", "confirmation.write", "payment.write",
        "case.read", "sheet.read",
    },
    "auditor": {"case.read", "sheet.read", "audit.read"},
}

ACTIVE_SHEET_STATES = ("awaiting_confirmation", "confirmed", "partially_paid")
# 结算单在案件关闭前并非冻结：settled 之后、结案之前仍可更正报价或退款，
# 状态机会因此回到 partially_paid；案件关闭后一切变更都被拒绝。
MUTABLE_SHEET_STATES = ACTIVE_SHEET_STATES + ("settled",)
PAYMENT_ALLOWED_STATES = ACTIVE_SHEET_STATES + ("settled",)


def _text(value: object, field: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


class SettlementService:
    """在单个 SQLite 连接上提供全部结算业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    # ----- 权限与审计 -------------------------------------------------

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM settlement_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM settlement_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = digest(body)
        self.connection.execute(
            "INSERT INTO settlement_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    # ----- 用户与案件 -------------------------------------------------

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO settlement_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_case(self, actor_id: str, case_id: str) -> dict[str, Any]:
        self._require(actor_id, "case.register")
        case_id = _text(case_id, "case_id", 64)
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO cases(case_id,status,created_by,created_at,updated_at) VALUES(?, 'open', ?,?,?)",
                    (case_id, actor_id, now, now),
                )
                self._audit("case", case_id, "case.registered", actor_id, {})
        except sqlite3.IntegrityError as exc:
            raise Conflict("案件编号已经存在") from exc
        return {"case_id": case_id, "status": "open"}

    def _case_row(self, case_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFound("案件不存在")
        return row

    def issue_liability(
        self,
        actor_id: str,
        case_id: str,
        shares: Mapping[str, object],
        basis: Mapping[str, Any] | list[Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """登记已生效责任认定；再次登记即更正，旧版本保留并标记 superseded。"""

        self._require(actor_id, "liability.issue")
        case_id = _text(case_id, "case_id", 64)
        if not isinstance(shares, Mapping) or not shares:
            raise ValidationFailed("责任比例必须是非空对象")
        if not isinstance(basis, Mapping | list) or isinstance(basis, (str, bytes)) or not basis:
            raise ValidationFailed("责任认定依据不能为空")
        parsed_shares: dict[str, str] = {}
        total = Decimal("0")
        for raw_party, raw_percent in shares.items():
            party_id = _text(raw_party, "当事人编号", 64)
            try:
                ratio = percent(raw_percent, f"责任比例 {party_id}")
            except CalcError as exc:
                raise ValidationFailed(str(exc)) from exc
            parsed_shares[party_id] = format(ratio, "f")
            total += ratio
        if total != Decimal("100"):
            raise ValidationFailed(f"责任比例之和必须等于 100，当前为 {total}")
        if idempotency_key is not None:
            _text(idempotency_key, "idempotency_key", 128)

        case = self._case_row(case_id)
        if case["status"] not in ("open", "liability_effective"):
            raise InvalidState("案件已进入结算或结案，不能再登记责任认定")
        basis_json = canonical_json(basis)
        basis_sha256 = digest(basis)
        previous = self.connection.execute(
            "SELECT * FROM liability_findings WHERE case_id=? AND state='effective' ORDER BY revision DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if previous is not None and previous["basis_sha256"] == basis_sha256 and previous["shares_json"] == canonical_json(parsed_shares):
            return {"finding_id": previous["finding_id"], "revision": previous["revision"], "duplicate": True}
        if idempotency_key:
            prior = self.connection.execute(
                "SELECT finding_id FROM liability_findings WHERE case_id=? AND idempotency_key=?",
                (case_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                raise Conflict("幂等键已用于该案件的另一责任认定")
        revision = 1 if previous is None else previous["revision"] + 1
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                if previous is not None:
                    self.connection.execute(
                        "UPDATE liability_findings SET state='superseded' WHERE finding_id=? AND state='effective'",
                        (previous["finding_id"],),
                    )
                cursor = self.connection.execute(
                    "INSERT INTO liability_findings(case_id,revision,shares_json,basis_json,basis_sha256,idempotency_key,"
                    "state,supersedes_finding_id,effective_at,issued_by,created_at) "
                    "VALUES(?,?,?,?,?,?, 'effective', ?,?,?,?)",
                    (
                        case_id,
                        revision,
                        canonical_json(parsed_shares),
                        basis_json,
                        basis_sha256,
                        idempotency_key,
                        None if previous is None else previous["finding_id"],
                        now,
                        actor_id,
                        now,
                    ),
                )
                finding_id = int(cursor.lastrowid)
                if case["status"] == "open":
                    self.connection.execute(
                        "UPDATE cases SET status='liability_effective',updated_at=? WHERE case_id=? AND status='open'",
                        (now, case_id),
                    )
                self._audit(
                    "case", case_id, "liability.issued", actor_id,
                    {"finding_id": finding_id, "revision": revision, "shares": parsed_shares,
                     "supersedes": None if previous is None else previous["finding_id"],
                     "basis_sha256": basis_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("责任认定版本或幂等键冲突") from exc
        return {"finding_id": finding_id, "revision": revision, "duplicate": False}

    # ----- 结算单与报价版本 -------------------------------------------

    def create_sheet(self, actor_id: str, settlement_id: str, case_id: str) -> dict[str, Any]:
        self._require(actor_id, "sheet.create")
        settlement_id = _text(settlement_id, "settlement_id", 64)
        case_id = _text(case_id, "case_id", 64)
        case = self._case_row(case_id)
        if case["status"] != "liability_effective":
            raise InvalidState("只有责任认定已生效的案件才能建立快速结算单")
        finding = self.connection.execute(
            "SELECT * FROM liability_findings WHERE case_id=? AND state='effective' ORDER BY revision DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        existing = self.connection.execute(
            "SELECT state FROM settlement_sheets WHERE case_id=? ORDER BY revision DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if existing is not None and existing["state"] != "abandoned":
            raise InvalidState("该案件已有未终结或已完成的结算单")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO settlement_sheets(settlement_id,case_id,liability_finding_id,current_quote_version,"
                    "state,created_by,created_at,updated_at) VALUES(?,?,?,0,'awaiting_confirmation',?,?,?)",
                    (settlement_id, case_id, finding["finding_id"], actor_id, now, now),
                )
                self.connection.execute(
                    "UPDATE cases SET status='in_settlement',updated_at=? WHERE case_id=?",
                    (now, case_id),
                )
                self._audit(
                    "settlement", settlement_id, "sheet.created", actor_id,
                    {"case_id": case_id, "liability_finding_id": finding["finding_id"]},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("结算单编号冲突") from exc
        return self.get_sheet(actor_id, settlement_id)

    def _sheet_row(self, settlement_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM settlement_sheets WHERE settlement_id=?", (settlement_id,)
        ).fetchone()
        if row is None:
            raise NotFound("结算单不存在")
        return row

    def _require_mutable_sheet(self, sheet: sqlite3.Row) -> None:
        if sheet["state"] not in MUTABLE_SHEET_STATES:
            raise InvalidState(f"结算单处于 {sheet['state']} 状态，不能再变更")

    def _require_case_open(self, sheet: sqlite3.Row) -> None:
        case = self._case_row(sheet["case_id"])
        if case["status"] == "closed":
            raise InvalidState("案件已经结案，结算内容冻结，不能再变更")

    def _require_finding_effective(self, sheet: sqlite3.Row) -> sqlite3.Row:
        finding = self.connection.execute(
            "SELECT * FROM liability_findings WHERE finding_id=?", (sheet["liability_finding_id"],)
        ).fetchone()
        if finding["state"] != "effective":
            raise InvalidState("结算单依据的责任认定已被更正版本取代，请废弃后依据新认定重建结算单")
        return finding

    def add_quote_version(
        self,
        actor_id: str,
        settlement_id: str,
        garage_id: str,
        items: list[Mapping[str, Any]],
        note: str = "",
    ) -> dict[str, Any]:
        """登记修理厂报价新版本；新版本生效后原确认失效，历史版本全部保留。"""

        self._require(actor_id, "quote.write")
        settlement_id = _text(settlement_id, "settlement_id", 64)
        garage_id = _text(garage_id, "garage_id", 64)
        if not isinstance(items, list) or not items:
            raise ValidationFailed("报价赔付项目必须是非空数组")
        note = note.strip()
        sheet = self._sheet_row(settlement_id)
        self._require_mutable_sheet(sheet)
        self._require_case_open(sheet)
        finding = self._require_finding_effective(sheet)
        shares = json.loads(finding["shares_json"])
        try:
            calc = compute_settlement(shares, items)
        except CalcError as exc:
            raise ValidationFailed(str(exc)) from exc
        normalized_items = [
            {
                "item_id": row["item_id"],
                "vehicle_id": row["vehicle_id"],
                "owner_party_id": row["owner_party_id"],
                "kind": row["kind"],
                "amount": row["amount"],
                "deductible_cny": row["deductible_cny"],
            }
            for row in calc["items"]
        ]
        quote_content = {"garage_id": garage_id, "items": normalized_items, "note": note}
        quote_sha256 = digest(quote_content)

        latest = self.connection.execute(
            "SELECT quote_version,quote_sha256 FROM sheet_quote_versions WHERE settlement_id=? "
            "ORDER BY quote_version DESC LIMIT 1",
            (settlement_id,),
        ).fetchone()
        if latest is not None and latest["quote_sha256"] == quote_sha256:
            return {"settlement_id": settlement_id, "quote_version": latest["quote_version"], "duplicate": True}
        quote_version = 1 if latest is None else latest["quote_version"] + 1
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO sheet_quote_versions(settlement_id,quote_version,garage_id,quote_json,quote_sha256,"
                "calc_json,calc_sha256,gross_total_cny,deductible_total_cny,claim_total_cny,note,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    settlement_id, quote_version, garage_id, canonical_json(normalized_items), quote_sha256,
                    canonical_json(calc), calc["calc_sha256"],
                    calc["gross_total_cny"], calc["deductible_total_cny"], calc["claim_total_cny"],
                    note, actor_id, now,
                ),
            )
            voided: list[str] = []
            if quote_version > 1:
                prior = self.connection.execute(
                    "SELECT party_id FROM party_confirmations WHERE settlement_id=? AND quote_version=? AND decision='confirmed'",
                    (settlement_id, quote_version - 1),
                ).fetchall()
                voided = [row["party_id"] for row in prior]
                self.connection.execute(
                    "UPDATE party_confirmations SET decision='void',voided_at=?,void_reason=? "
                    "WHERE settlement_id=? AND quote_version=? AND decision='confirmed'",
                    (now, f"修理厂报价更新至第 {quote_version} 版", settlement_id, quote_version - 1),
                )
            self.connection.execute(
                "UPDATE settlement_sheets SET current_quote_version=?,revision=revision+1,updated_at=? WHERE settlement_id=?",
                (quote_version, now, settlement_id),
            )
            self._audit(
                "settlement", settlement_id, "quote.versioned", actor_id,
                {"quote_version": quote_version, "garage_id": garage_id, "quote_sha256": quote_sha256,
                 "calc_sha256": calc["calc_sha256"], "gross_total_cny": calc["gross_total_cny"],
                 "deductible_total_cny": calc["deductible_total_cny"], "claim_total_cny": calc["claim_total_cny"],
                 "voided_confirmations": voided},
            )
            if voided:
                self._audit(
                    "settlement", settlement_id, "confirmation.voided", actor_id,
                    {"quote_version": quote_version - 1, "parties": voided,
                     "reason": "quote_version_changed"},
                )
            self._recompute_state(settlement_id, actor_id)
        return {"settlement_id": settlement_id, "quote_version": quote_version,
                "calc_sha256": calc["calc_sha256"], "duplicate": False}

    # ----- 当事人确认 -------------------------------------------------

    def record_confirmation(
        self,
        actor_id: str,
        settlement_id: str,
        party_id: str,
        decision: str,
        note: str = "",
    ) -> dict[str, Any]:
        """登记当事人对当前报价版本的意思表示；确认与拒签都不可覆盖或改口。"""

        self._require(actor_id, "confirmation.write")
        settlement_id = _text(settlement_id, "settlement_id", 64)
        party_id = _text(party_id, "party_id", 64)
        if decision not in ("confirmed", "rejected"):
            raise ValidationFailed("decision 必须是 confirmed 或 rejected")
        note = note.strip()
        sheet = self._sheet_row(settlement_id)
        self._require_mutable_sheet(sheet)
        self._require_case_open(sheet)
        self._require_finding_effective(sheet)
        quote_version = sheet["current_quote_version"]
        if quote_version == 0:
            raise InvalidState("修理厂尚未报价，不能登记当事人确认")
        calc = self._current_calc(sheet)
        if party_id not in {row["party_id"] for row in calc["parties"]}:
            raise ValidationFailed("该当事人不在责任认定当事人之列")
        existing = self.connection.execute(
            "SELECT * FROM party_confirmations WHERE settlement_id=? AND quote_version=? AND party_id=?",
            (settlement_id, quote_version, party_id),
        ).fetchone()
        if existing is not None:
            if existing["decision"] == decision:
                return {"confirmation_id": existing["confirmation_id"], "decision": decision, "duplicate": True}
            raise Conflict(
                f"当事人已对第 {quote_version} 版报价{self._decision_label(existing['decision'])}，"
                "同一版本不能改口覆盖；如金额变化请登记新报价版本"
            )
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO party_confirmations(settlement_id,quote_version,party_id,decision,note,"
                "decided_by,decided_at) VALUES(?,?,?,?,?,?,?)",
                (settlement_id, quote_version, party_id, decision, note, actor_id, now),
            )
            self.connection.execute(
                "UPDATE settlement_sheets SET revision=revision+1,updated_at=? WHERE settlement_id=?",
                (now, settlement_id),
            )
            self._audit(
                "settlement", settlement_id,
                "party.confirmed" if decision == "confirmed" else "party.rejected",
                actor_id,
                {"party_id": party_id, "quote_version": quote_version, "note": note},
            )
            self._recompute_state(settlement_id, actor_id)
        return {"confirmation_id": int(cursor.lastrowid), "decision": decision, "duplicate": False}

    @staticmethod
    def _decision_label(decision: str) -> str:
        return {"confirmed": "确认", "rejected": "拒绝签署", "void": "确认（已失效）"}[decision]

    # ----- 收款与退款（只追加台账） -----------------------------------

    def _replay_payment(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "payment_id": row["payment_id"], "settlement_id": row["settlement_id"],
            "party_id": row["party_id"], "direction": row["direction"],
            "amount_cny": row["amount_cny"], "receipt_no": row["receipt_no"],
            "channel": row["channel"], "received_at": row["received_at"],
            "duplicate": True,
        }

    def _post_payment(
        self,
        actor_id: str,
        settlement_id: str,
        party_id: str,
        direction: str,
        amount_raw: object,
        receipt_no: str,
        channel: str,
        received_at: str | None,
        idempotency_key: str | None,
        original_receipt_no: str | None = None,
    ) -> dict[str, Any]:
        try:
            amount = money(amount_raw, "amount_cny")
        except CalcError as exc:
            raise ValidationFailed(str(exc)) from exc
        if amount <= ZERO:
            raise ValidationFailed("amount_cny 必须大于零")
        receipt_no = _text(receipt_no, "receipt_no", 128)
        channel = _text(channel, "channel", 32)
        if received_at is None:
            received_at = self._now()
        else:
            received_at = utc_text(parse_utc(_text(received_at, "received_at", 40), "received_at"))
        if idempotency_key is not None:
            idempotency_key = _text(idempotency_key, "idempotency_key", 128)

        # 幂等重放优先于一切状态判断：即使结算单已结清、案件已结案，
        # 同一支付回执再次到达也必须原样返回，而不是再走一遍业务校验。
        sheet = self._sheet_row(settlement_id)
        prior_by_receipt = self.connection.execute(
            "SELECT * FROM payment_entries WHERE receipt_no=?", (receipt_no,)
        ).fetchone()
        if prior_by_receipt is not None:
            if (prior_by_receipt["settlement_id"], prior_by_receipt["party_id"],
                    prior_by_receipt["amount_cny"], prior_by_receipt["direction"]) != (
                settlement_id, party_id, format(amount, "f"), direction
            ):
                raise Conflict("支付回执已用于另一笔收款登记")
            return self._replay_payment(prior_by_receipt)
        if idempotency_key:
            prior_by_key = self.connection.execute(
                "SELECT * FROM payment_entries WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if prior_by_key is not None:
                if (prior_by_key["receipt_no"], prior_by_key["amount_cny"], prior_by_key["direction"]) != (
                    receipt_no, format(amount, "f"), direction
                ):
                    raise Conflict("幂等键已用于另一笔支付")
                return self._replay_payment(prior_by_key)

        if sheet["state"] not in PAYMENT_ALLOWED_STATES:
            raise InvalidState(f"结算单处于 {sheet['state']} 状态，不能登记支付")
        self._require_finding_effective(sheet)
        calc = self._current_calc(sheet)
        if calc is None:
            raise InvalidState("修理厂尚未报价，不能登记支付")
        parties = {row["party_id"] for row in calc["parties"]}
        if party_id not in parties:
            raise ValidationFailed("付款方不在责任认定当事人之列")
        if original_receipt_no is not None:
            original = self.connection.execute(
                "SELECT 1 FROM payment_entries WHERE receipt_no=? AND settlement_id=? "
                "AND party_id=? AND direction='inbound'",
                (original_receipt_no, settlement_id, party_id),
            ).fetchone()
            if original is None:
                raise ValidationFailed("退款引用的原支付回执不存在或不属于该当事人收款")

        case = self._case_row(sheet["case_id"])
        balances = self._balances(sheet, calc)
        if direction == "inbound":
            if case["status"] == "closed":
                raise InvalidState("案件已经结案，不能再登记收款；如回执到达延迟请走结案后更正流程")
            due = next(
                (Decimal(row["claim_due_cny"]) for row in calc["parties"] if row["party_id"] == party_id),
                None,
            )
            if balances[party_id] + amount > due:
                raise InvalidState(
                    f"收款后 {party_id} 到账金额将超过应付 {format(due, 'f')} 元；"
                    "超过部分应先更正报价或走退款流程，不能登记为收款"
                )
        else:
            if balances[party_id] < amount:
                raise InvalidState("退款金额不能超过该当事人已到账余额")
            if case["status"] == "closed":
                projected = dict(balances)
                projected[party_id] -= amount
                if not self._is_fully_covered(calc, projected):
                    raise InvalidState("案件已结案，退款会造成结算缺口，应先补足或履行更正流程")

        now = self._now()
        event_type = "payment.recorded" if direction == "inbound" else "payment.refunded"
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO payment_entries(settlement_id,party_id,direction,amount_cny,receipt_no,channel,"
                    "idempotency_key,received_at,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (settlement_id, party_id, direction, format(amount, "f"), receipt_no, channel,
                     idempotency_key or f"auto:{receipt_no}", received_at, actor_id, now),
                )
                payment_id = int(cursor.lastrowid)
                self.connection.execute(
                    "UPDATE settlement_sheets SET revision=revision+1,updated_at=? WHERE settlement_id=?",
                    (now, settlement_id),
                )
                self._audit(
                    "settlement", settlement_id, event_type, actor_id,
                    {"payment_id": payment_id, "party_id": party_id, "direction": direction,
                     "amount_cny": format(amount, "f"), "receipt_no": receipt_no, "channel": channel,
                     "received_at": received_at,
                     **({"original_receipt_no": original_receipt_no} if original_receipt_no else {})},
                )
                self._recompute_state(settlement_id, actor_id)
        except sqlite3.IntegrityError as exc:
            raced = self.connection.execute(
                "SELECT * FROM payment_entries WHERE receipt_no=? OR idempotency_key=?",
                (receipt_no, idempotency_key or f"auto:{receipt_no}"),
            ).fetchone()
            if raced is not None:
                if (raced["settlement_id"], raced["party_id"], raced["amount_cny"], raced["direction"]) != (
                    settlement_id, party_id, format(amount, "f"), direction
                ):
                    raise Conflict("支付回执或幂等键已用于另一笔支付") from exc
                return self._replay_payment(raced)
            raise Conflict("支付登记冲突") from exc
        return {
            "payment_id": payment_id, "settlement_id": settlement_id, "party_id": party_id,
            "direction": direction, "amount_cny": format(amount, "f"), "receipt_no": receipt_no,
            "channel": channel, "received_at": received_at, "duplicate": False,
        }

    def record_payment(
        self,
        actor_id: str,
        settlement_id: str,
        party_id: str,
        amount_cny: object,
        receipt_no: str,
        channel: str,
        received_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """登记收款；同一支付回执重复到达只返回原记录，绝不重复入账。"""

        self._require(actor_id, "payment.write")
        return self._post_payment(
            actor_id, settlement_id, party_id, "inbound", amount_cny, receipt_no, channel,
            received_at, idempotency_key,
        )

    def record_refund(
        self,
        actor_id: str,
        settlement_id: str,
        party_id: str,
        amount_cny: object,
        receipt_no: str,
        channel: str,
        original_receipt_no: str | None = None,
        received_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """登记退款；退款是新增台账记录，从不冲销或覆盖原收款。"""

        self._require(actor_id, "payment.write")
        return self._post_payment(
            actor_id, settlement_id, party_id, "refund", amount_cny, receipt_no, channel,
            received_at, idempotency_key, original_receipt_no,
        )

    # ----- 状态推导与结案门禁 -----------------------------------------

    def _current_quote(self, sheet: sqlite3.Row) -> sqlite3.Row | None:
        if sheet["current_quote_version"] == 0:
            return None
        return self.connection.execute(
            "SELECT * FROM sheet_quote_versions WHERE settlement_id=? AND quote_version=?",
            (sheet["settlement_id"], sheet["current_quote_version"]),
        ).fetchone()

    def _current_calc(self, sheet: sqlite3.Row) -> dict[str, Any] | None:
        quote = self._current_quote(sheet)
        return None if quote is None else json.loads(quote["calc_json"])

    def _balances(self, sheet: sqlite3.Row, calc: dict[str, Any] | None = None) -> dict[str, Decimal]:
        if calc is None:
            calc = self._current_calc(sheet)
        balances = {row["party_id"]: ZERO for row in (calc["parties"] if calc else [])}
        rows = self.connection.execute(
            "SELECT party_id,direction,amount_cny FROM payment_entries WHERE settlement_id=? ORDER BY payment_id",
            (sheet["settlement_id"],),
        ).fetchall()
        for row in rows:
            value = Decimal(row["amount_cny"])
            if row["party_id"] not in balances:
                # 新版本报价当事人集合变化时，旧付款仍须保留并参与总额核对。
                balances.setdefault(row["party_id"], ZERO)
            balances[row["party_id"]] += value if row["direction"] == "inbound" else -value
        return balances

    @staticmethod
    def _is_fully_covered(calc: dict[str, Any], balances: Mapping[str, Decimal]) -> bool:
        for row in calc["parties"]:
            if balances.get(row["party_id"], ZERO) < Decimal(row["claim_due_cny"]):
                return False
        return True

    @staticmethod
    def _is_exactly_settled(calc: dict[str, Any], balances: Mapping[str, Decimal]) -> bool:
        """分文不差：每一方余额都恰好等于当前报价下的应付，不欠也不多。"""

        return all(
            balances.get(row["party_id"], ZERO) == Decimal(row["claim_due_cny"])
            for row in calc["parties"]
        )

    def _recompute_state(self, settlement_id: str, actor_id: str) -> None:
        """依据当前报价的确认情况与支付台账推导结算单状态，调用方须持事务。"""

        sheet = self.connection.execute(
            "SELECT * FROM settlement_sheets WHERE settlement_id=?", (settlement_id,)
        ).fetchone()
        if sheet["state"] == "abandoned":
            return
        calc = self._current_calc(sheet)
        balances = self._balances(sheet, calc)
        total_balance = sum(balances.values(), ZERO)
        confirmed_parties: set[str] = set()
        if calc is not None:
            rows = self.connection.execute(
                "SELECT party_id FROM party_confirmations WHERE settlement_id=? AND quote_version=? AND decision='confirmed'",
                (settlement_id, sheet["current_quote_version"]),
            ).fetchall()
            confirmed_parties = {row["party_id"] for row in rows}
        all_confirmed = calc is not None and all(
            row["party_id"] in confirmed_parties for row in calc["parties"]
        )
        exactly_settled = calc is not None and self._is_exactly_settled(calc, balances)
        if all_confirmed and exactly_settled:
            new_state = "settled"
        elif total_balance > ZERO:
            new_state = "partially_paid"
        elif all_confirmed:
            new_state = "confirmed"
        else:
            new_state = "awaiting_confirmation"
        if new_state != sheet["state"]:
            self.connection.execute(
                "UPDATE settlement_sheets SET state=? WHERE settlement_id=?",
                (new_state, settlement_id),
            )
            if new_state == "settled":
                self._audit(
                    "settlement", settlement_id, "sheet.settled", actor_id,
                    {"from_state": sheet["state"], "claim_total_cny": calc["claim_total_cny"],
                     "quote_version": sheet["current_quote_version"]},
                )
            elif sheet["state"] == "settled":
                self._audit(
                    "settlement", settlement_id, "sheet.reopened", actor_id,
                    {"to_state": new_state, "total_balance_cny": format(total_balance, "f"),
                     "quote_version": sheet["current_quote_version"]},
                )

    def abandon_sheet(self, actor_id: str, settlement_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "sheet.abandon")
        reason = _text(reason, "reason", 500)
        sheet = self._sheet_row(settlement_id)
        if sheet["state"] not in ACTIVE_SHEET_STATES:
            raise InvalidState(f"结算单处于 {sheet['state']} 状态，不能废弃")
        self._require_case_open(sheet)
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE settlement_sheets SET state='abandoned',revision=revision+1,updated_at=? WHERE settlement_id=?",
                (now, settlement_id),
            )
            self.connection.execute(
                "UPDATE cases SET status='liability_effective',updated_at=? WHERE case_id=? AND status='in_settlement'",
                (now, sheet["case_id"]),
            )
            self._audit("settlement", settlement_id, "sheet.abandoned", actor_id, {"reason": reason})
        return {"settlement_id": settlement_id, "state": "abandoned"}

    def close_case(self, actor_id: str, case_id: str) -> dict[str, Any]:
        """结案门禁：只有存在状态为 settled 的结算单，案件才允许关闭。"""

        self._require(actor_id, "case.close")
        case_id = _text(case_id, "case_id", 64)
        case = self._case_row(case_id)
        if case["status"] == "closed":
            return {"case_id": case_id, "status": "closed", "duplicate": True}
        sheet = self.connection.execute(
            "SELECT * FROM settlement_sheets WHERE case_id=? AND state!='abandoned' ORDER BY revision DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if sheet is None:
            raise InvalidState("案件尚未建立结算单，不能结案")
        if sheet["state"] != "settled":
            raise InvalidState(f"结算单状态为 {sheet['state']}，结算未完成，案件不能结案")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE cases SET status='closed',updated_at=? WHERE case_id=? AND status!='closed'",
                (now, case_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("案件状态已变化")
            self._audit(
                "case", case_id, "case.closed", actor_id,
                {"settlement_id": sheet["settlement_id"], "settlement_revision": sheet["revision"]},
            )
        return {"case_id": case_id, "status": "closed", "duplicate": False}

    # ----- 查询与审计重建 ---------------------------------------------

    def get_sheet(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        self._require(actor_id, "sheet.read")
        sheet = self._sheet_row(settlement_id)
        case = self._case_row(sheet["case_id"])
        finding = self.connection.execute(
            "SELECT * FROM liability_findings WHERE finding_id=?", (sheet["liability_finding_id"],)
        ).fetchone()
        quote_rows = self.connection.execute(
            "SELECT quote_version,garage_id,quote_sha256,calc_sha256,gross_total_cny,deductible_total_cny,"
            "claim_total_cny,note,created_by,created_at FROM sheet_quote_versions WHERE settlement_id=? ORDER BY quote_version",
            (settlement_id,),
        ).fetchall()
        quote = self._current_quote(sheet)
        calc = None if quote is None else json.loads(quote["calc_json"])
        confirmation_rows = self.connection.execute(
            "SELECT * FROM party_confirmations WHERE settlement_id=? ORDER BY quote_version,confirmation_id",
            (settlement_id,),
        ).fetchall()
        payment_rows = self.connection.execute(
            "SELECT * FROM payment_entries WHERE settlement_id=? ORDER BY payment_id",
            (settlement_id,),
        ).fetchall()
        balances = self._balances(sheet, calc)
        due_map = {row["party_id"]: Decimal(row["claim_due_cny"]) for row in calc["parties"]} if calc else {}
        party_ids = sorted(set(due_map) | set(balances))
        party_balances = []
        for party_id in party_ids:
            balance = balances.get(party_id, ZERO)
            due = due_map.get(party_id)
            party_balances.append({
                "party_id": party_id,
                "claim_due_cny": None if due is None else format(due, "f"),
                "balance_cny": format(balance, "f"),
                "covered": due is not None and balance >= due,
                "overpaid_cny": format(max(ZERO, balance - (due if due is not None else ZERO)), "f"),
            })
        total_balance = sum(balances.values(), ZERO)
        current_confirmed = {
            row["party_id"] for row in confirmation_rows
            if row["quote_version"] == sheet["current_quote_version"] and row["decision"] == "confirmed"
        }
        all_confirmed = calc is not None and all(
            row["party_id"] in current_confirmed for row in calc["parties"]
        )
        return {
            "settlement_id": settlement_id,
            "case_id": sheet["case_id"],
            "case_status": case["status"],
            "state": sheet["state"],
            "revision": sheet["revision"],
            "current_quote_version": sheet["current_quote_version"],
            "liability": {
                "finding_id": finding["finding_id"],
                "revision": finding["revision"],
                "state": finding["state"],
                "shares": json.loads(finding["shares_json"]),
                "basis_sha256": finding["basis_sha256"],
                "effective_at": finding["effective_at"],
                "issued_by": finding["issued_by"],
            },
            "quote": None if quote is None else {
                "quote_version": quote["quote_version"],
                "garage_id": quote["garage_id"],
                "items": json.loads(quote["quote_json"]),
                "note": quote["note"],
                "quote_sha256": quote["quote_sha256"],
                "created_by": quote["created_by"],
                "created_at": quote["created_at"],
            },
            "calc": calc,
            "all_confirmed": all_confirmed,
            "confirmations": [
                {
                    "confirmation_id": row["confirmation_id"],
                    "quote_version": row["quote_version"],
                    "party_id": row["party_id"],
                    "decision": row["decision"],
                    "note": row["note"],
                    "decided_by": row["decided_by"],
                    "decided_at": row["decided_at"],
                    "voided_at": row["voided_at"],
                    "void_reason": row["void_reason"],
                    "active": row["quote_version"] == sheet["current_quote_version"]
                    and row["decision"] in ("confirmed", "rejected"),
                }
                for row in confirmation_rows
            ],
            "payments": [
                {
                    "payment_id": row["payment_id"],
                    "party_id": row["party_id"],
                    "direction": row["direction"],
                    "amount_cny": row["amount_cny"],
                    "receipt_no": row["receipt_no"],
                    "channel": row["channel"],
                    "received_at": row["received_at"],
                    "recorded_by": row["recorded_by"],
                    "recorded_at": row["recorded_at"],
                }
                for row in payment_rows
            ],
            "party_balances": party_balances,
            "total_balance_cny": format(total_balance, "f"),
            "quote_history": [dict(row) for row in quote_rows],
            "fully_settled": sheet["state"] == "settled",
        }

    def case_detail(self, actor_id: str, case_id: str) -> dict[str, Any]:
        self._require(actor_id, "case.read")
        case = self._case_row(case_id)
        findings = self.connection.execute(
            "SELECT finding_id,revision,state,shares_json,basis_sha256,supersedes_finding_id,effective_at,issued_by,created_at "
            "FROM liability_findings WHERE case_id=? ORDER BY revision",
            (case_id,),
        ).fetchall()
        sheets = self.connection.execute(
            "SELECT settlement_id,state,revision,current_quote_version,liability_finding_id,created_by,created_at,updated_at "
            "FROM settlement_sheets WHERE case_id=? ORDER BY revision",
            (case_id,),
        ).fetchall()
        return {
            "case_id": case_id,
            "status": case["status"],
            "liability_findings": [
                dict(row) | {"shares": json.loads(row["shares_json"])} for row in findings
            ],
            "sheets": [dict(row) for row in sheets],
        }

    def history(self, actor_id: str, entity_type: str, entity_id: str) -> dict[str, Any]:
        """按时间顺序返回某案件或结算单的全部审计事件，供办案与审计重建过程。"""

        self._require(actor_id, "audit.read")
        if entity_type not in ("case", "settlement"):
            raise ValidationFailed("entity_type 必须是 case 或 settlement")
        rows = self.connection.execute(
            "SELECT event_id,entity_type,entity_id,event_type,actor_id,payload_json,previous_hash,event_hash,created_at "
            "FROM settlement_audit_events WHERE entity_type=? AND entity_id=? ORDER BY event_id",
            (entity_type, entity_id),
        ).fetchall()
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "events": [
                {
                    "event_id": row["event_id"], "event_type": row["event_type"],
                    "actor_id": row["actor_id"], "created_at": row["created_at"],
                    "payload": json.loads(row["payload_json"]),
                    "event_hash": row["event_hash"], "previous_hash": row["previous_hash"],
                }
                for row in rows
            ],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM settlement_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = digest(body)
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
