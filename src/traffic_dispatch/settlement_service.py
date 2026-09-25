"""轻微事故快速结算单用例。

在已生效的责任认定之上建立快速结算单，保存各方确认、赔付项目、免赔与
分摊计算、修理厂报价版本和支付结果。所有写操作与 ``TrafficDispatchService``
共用同一 SQLite 连接、用户表和哈希链审计表，时间源可注入。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .planning import canonical_json, digest
from .service import ROLE_PERMISSIONS
from .settlement import (
    ZERO,
    breakdown_conservation,
    build_breakdown,
    money,
    parse_deductibles,
    parse_items,
    parse_shares,
    text_value,
)
from .storage import transaction

# 允许提交新修理厂报价的结算单状态；settled/closed/void 不可再改报价。
# paid（已付清但尚未完成结算对账）仍允许更正报价，多收部分通过退款冲平，
# 支付台账只追加、不会被覆盖。
QUOTE_OPEN_STATES = {"open", "partially_paid", "paid", "refused"}
# 结算完成后的资金状态。
MONEY_LOCKED_STATES = {"settled", "closed"}


class QuickSettlementService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM traffic_users WHERE user_id=?", (user_id,)
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
        """向与调度服务共享的哈希链追加审计事件。"""
        previous = self.connection.execute(
            "SELECT event_hash FROM traffic_audit_events ORDER BY event_id DESC LIMIT 1"
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
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO traffic_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
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

    # ----- 责任认定 -------------------------------------------------------

    def record_liability(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "liability.write")
        determination_id = text_value(raw.get("determination_id"), "determination_id")
        incident_id = text_value(raw.get("incident_id"), "incident_id")
        shares = parse_shares(raw.get("parties"))
        content_sha256 = digest({"incident_id": incident_id, "parties": shares})
        try:
            with transaction(self.connection, immediate=True):
                existing = self.connection.execute(
                    "SELECT determination_id FROM liability_determinations WHERE incident_id=? AND state='effective'",
                    (incident_id,),
                ).fetchone()
                if existing is not None:
                    raise Conflict("该事故已有生效责任认定，需先撤销再重新认定")
                self.connection.execute(
                    "INSERT INTO liability_determinations(determination_id,incident_id,shares_json,content_sha256,"
                    "state,decided_by,decided_at) VALUES(?,?,?,?, 'effective',?,?)",
                    (
                        determination_id,
                        incident_id,
                        canonical_json(shares),
                        content_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("liability", determination_id, "liability.determined", actor_id, {
                    "incident_id": incident_id,
                    "parties": [{"party_id": p["party_id"], "share_bp": p["share_bp"]} for p in shares],
                    "content_sha256": content_sha256,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("责任认定编号冲突") from exc
        return {
            "determination_id": determination_id,
            "incident_id": incident_id,
            "state": "effective",
            "content_sha256": content_sha256,
        }

    def revoke_liability(self, actor_id: str, determination_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "liability.write")
        if not reason.strip():
            raise ValidationFailed("撤销原因不能为空")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT state FROM liability_determinations WHERE determination_id=?",
                (determination_id,),
            ).fetchone()
            if row is None:
                raise NotFound("责任认定不存在")
            if row["state"] != "effective":
                raise InvalidState("责任认定不是生效状态")
            payments = self.connection.execute(
                "SELECT count(*) FROM settlement_payments p JOIN quick_settlements s "
                "ON s.settlement_id=p.settlement_id WHERE s.determination_id=?",
                (determination_id,),
            ).fetchone()[0]
            if payments:
                raise InvalidState("结算单已发生支付，责任认定不能撤销")
            self.connection.execute(
                "UPDATE liability_determinations SET state='revoked',revision=revision+1,revoked_at=? "
                "WHERE determination_id=?",
                (self._now(), determination_id),
            )
            self.connection.execute(
                "UPDATE quick_settlements SET state='void',revision=revision+1 "
                "WHERE determination_id=? AND state NOT IN ('void','closed','settled','paid')",
                (determination_id,),
            )
            self._audit("liability", determination_id, "liability.revoked", actor_id, {"reason": reason})
        return {"determination_id": determination_id, "state": "revoked"}

    # ----- 结算单 ---------------------------------------------------------

    def _load_settlement(self, settlement_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM quick_settlements WHERE settlement_id=?", (settlement_id,)
        ).fetchone()
        if row is None:
            raise NotFound("快速结算单不存在")
        return row

    def _load_determination(self, determination_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM liability_determinations WHERE determination_id=?",
            (determination_id,),
        ).fetchone()
        if row is None:
            raise NotFound("责任认定不存在")
        if row["state"] != "effective":
            raise InvalidState("责任认定尚未生效，不能建立结算单")
        return row

    def create_settlement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "settlement.write")
        settlement_id = text_value(raw.get("settlement_id"), "settlement_id")
        determination_id = text_value(raw.get("determination_id"), "determination_id")
        determination = self._load_determination(determination_id)
        shares = json.loads(determination["shares_json"])
        party_ids = {party["party_id"] for party in shares}
        fixed_items = parse_items(raw.get("compensation_items"), "compensation_items", allow_empty=True)
        deductibles = parse_deductibles(raw.get("deductibles"), party_ids)
        breakdown = build_breakdown(0, shares, fixed_items, [], deductibles)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO quick_settlements(settlement_id,incident_id,determination_id,fixed_items_json,"
                    "deductibles_json,state,quote_version,total_amount,total_deductible,total_payable,"
                    "created_by,created_at) VALUES(?,?,?,?,?, 'open', 0,?,?,?,?,?)",
                    (
                        settlement_id,
                        determination["incident_id"],
                        determination_id,
                        canonical_json([{**item, "amount": format(item["amount"], "f")} for item in fixed_items]),
                        canonical_json({key: format(value, "f") for key, value in sorted(deductibles.items())}),
                        breakdown["total_amount"],
                        breakdown["total_deductible"],
                        breakdown["total_payable"],
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO settlement_breakdowns(settlement_id,quote_version,breakdown_json,created_at) "
                    "VALUES(?,?,?,?)",
                    (settlement_id, 0, canonical_json(breakdown), self._now()),
                )
                self._audit("settlement", settlement_id, "settlement.created", actor_id, {
                    "determination_id": determination_id,
                    "incident_id": determination["incident_id"],
                    "total_payable": breakdown["total_payable"],
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("结算单编号冲突或该责任认定已建立结算单") from exc
        return self.settlement(actor_id, settlement_id)

    def submit_repair_quote(self, actor_id: str, settlement_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        shop_id = text_value(raw.get("shop_id"), "shop_id")
        shop_name = text_value(raw.get("shop_name"), "shop_name", 128)
        quote_ref = text_value(raw.get("quote_ref"), "quote_ref")
        items = parse_items(raw.get("items"), "修理厂报价项目")
        with transaction(self.connection, immediate=True):
            settlement = self._load_settlement(settlement_id)
            if settlement["state"] not in QUOTE_OPEN_STATES:
                raise InvalidState(f"结算单状态 {settlement['state']} 下不能变更修理厂报价")
            determination = self._load_determination(settlement["determination_id"])
            shares = json.loads(determination["shares_json"])
            fixed_items = [
                {"item_id": item["item_id"], "title": item["title"], "amount": Decimal(item["amount"])}
                for item in json.loads(settlement["fixed_items_json"])
            ]
            deductibles = {key: Decimal(value) for key, value in json.loads(settlement["deductibles_json"]).items()}
            version = int(settlement["quote_version"]) + 1
            content_input = {
                "shop_id": shop_id,
                "shop_name": shop_name,
                "quote_ref": quote_ref,
                "items": [{**item, "amount": format(item["amount"], "f")} for item in items],
            }
            content_sha256 = digest(content_input)
            # 与上一版内容一致时按重复提交处理，不产生新版本。
            previous = self.connection.execute(
                "SELECT content_sha256,quote_version FROM repair_quote_versions "
                "WHERE settlement_id=? ORDER BY quote_version DESC LIMIT 1",
                (settlement_id,),
            ).fetchone()
            if previous is not None and previous["content_sha256"] == content_sha256:
                return {
                    "settlement_id": settlement_id,
                    "quote_version": previous["quote_version"],
                    "duplicate": True,
                    "content_sha256": content_sha256,
                }
            breakdown = build_breakdown(version, shares, fixed_items, items, deductibles)
            breakdown_conservation(breakdown)
            self.connection.execute(
                "UPDATE repair_quote_versions SET state='superseded' "
                "WHERE settlement_id=? AND state='active'",
                (settlement_id,),
            )
            # 报价变化后，针对旧版本的确认与拒签一律失效，但记录保留可审计。
            self.connection.execute(
                "UPDATE party_confirmations SET state='voided',voided_at=?,void_reason='修理厂报价已更新' "
                "WHERE settlement_id=? AND quote_version=? AND state='active'",
                (self._now(), settlement_id, settlement["quote_version"]),
            )
            self.connection.execute(
                "INSERT INTO repair_quote_versions(quote_version,settlement_id,shop_id,shop_name,quote_ref,"
                "items_json,content_sha256,state,submitted_by,created_at) VALUES(?,?,?,?,?,?,?, 'active',?,?)",
                (
                    version,
                    settlement_id,
                    shop_id,
                    shop_name,
                    quote_ref,
                    canonical_json(content_input["items"]),
                    content_sha256,
                    actor_id,
                    self._now(),
                ),
            )
            self.connection.execute(
                "INSERT INTO settlement_breakdowns(settlement_id,quote_version,breakdown_json,created_at) "
                "VALUES(?,?,?,?)",
                (settlement_id, version, canonical_json(breakdown), self._now()),
            )
            net_paid = Decimal(settlement["amount_paid"]) - Decimal(settlement["amount_refunded"])
            new_state = self._derive_state(net_paid, Decimal(breakdown["total_payable"]), readiness=None)
            self.connection.execute(
                "UPDATE quick_settlements SET quote_version=?,total_amount=?,total_deductible=?,total_payable=?,"
                "state=?,revision=revision+1 WHERE settlement_id=?",
                (
                    version,
                    breakdown["total_amount"],
                    breakdown["total_deductible"],
                    breakdown["total_payable"],
                    new_state,
                    settlement_id,
                ),
            )
            self._audit("settlement", settlement_id, "quote.submitted", actor_id, {
                "quote_version": version,
                "shop_id": shop_id,
                "quote_ref": quote_ref,
                "content_sha256": content_sha256,
                "previous_version": settlement["quote_version"],
                "total_payable": breakdown["total_payable"],
            })
        return self.settlement(actor_id, settlement_id)

    # ----- 当事人确认 -----------------------------------------------------

    def _readiness(self, settlement_id: str, version: int, party_ids: set[str]) -> dict[str, dict[str, Any]]:
        """返回当前报价版本下每方的最新有效确认。"""
        rows = self.connection.execute(
            "SELECT party_id,decision,created_at,actor_id FROM party_confirmations "
            "WHERE settlement_id=? AND quote_version=? AND state='active' "
            "ORDER BY confirmation_id",
            (settlement_id, version),
        ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest[row["party_id"]] = dict(row)
        return {party_id: latest.get(party_id) for party_id in party_ids}

    @staticmethod
    def _derive_state(net_paid: Decimal, payable: Decimal, readiness: Mapping[str, Any] | None) -> str:
        if net_paid > ZERO:
            return "paid" if net_paid >= payable else "partially_paid"
        if readiness is not None and any(
            entry is not None and entry["decision"] == "refused" for entry in readiness.values()
        ):
            return "refused"
        return "open"

    def confirm_party(
        self, actor_id: str, settlement_id: str, party_id: str, decision: str, note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "confirmation.write")
        party_id = text_value(party_id, "party_id")
        if decision not in {"confirmed", "refused"}:
            raise ValidationFailed("确认决定必须是 confirmed 或 refused")
        note = (note or "")[:1000]
        with transaction(self.connection, immediate=True):
            settlement = self._load_settlement(settlement_id)
            if settlement["state"] in {"settled", "closed", "void"}:
                raise InvalidState(f"结算单状态 {settlement['state']} 下不能再追加确认或拒签")
            version = int(settlement["quote_version"])
            if version <= 0:
                raise InvalidState("修理厂尚未提交报价版本，不能确认结算单")
            determination = self._load_determination(settlement["determination_id"])
            shares = json.loads(determination["shares_json"])
            party_ids = {party["party_id"] for party in shares}
            if party_id not in party_ids:
                raise ValidationFailed("确认方不在责任认定当事人名单中")
            prior = self.connection.execute(
                "SELECT decision FROM party_confirmations WHERE settlement_id=? AND quote_version=? "
                "AND party_id=? AND state='active' ORDER BY confirmation_id DESC LIMIT 1",
                (settlement_id, version, party_id),
            ).fetchone()
            if prior is not None and prior["decision"] == decision:
                return {"settlement_id": settlement_id, "party_id": party_id, "decision": decision, "duplicate": True}
            if prior is not None and prior["decision"] == "refused":
                raise InvalidState("该方已在本报价版本拒绝签署，拒绝不能被覆盖；需提交新报价版本")
            content_sha256 = digest({
                "settlement_id": settlement_id,
                "quote_version": version,
                "party_id": party_id,
                "decision": decision,
                "note": note,
            })
            cursor = self.connection.execute(
                "INSERT INTO party_confirmations(settlement_id,quote_version,party_id,decision,note,"
                "content_sha256,state,actor_id,created_at) VALUES(?,?,?,?,?,?, 'active',?,?)",
                (settlement_id, version, party_id, decision, note, content_sha256, actor_id, self._now()),
            )
            readiness = self._readiness(settlement_id, version, party_ids)
            net_paid = Decimal(settlement["amount_paid"]) - Decimal(settlement["amount_refunded"])
            if net_paid == ZERO:
                new_state = self._derive_state(net_paid, Decimal(settlement["total_payable"]), readiness)
                self.connection.execute(
                    "UPDATE quick_settlements SET state=?,revision=revision+1 WHERE settlement_id=?",
                    (new_state, settlement_id),
                )
            self._audit("settlement", settlement_id, "party.confirmed", actor_id, {
                "confirmation_id": cursor.lastrowid,
                "quote_version": version,
                "party_id": party_id,
                "decision": decision,
            })
        return {"settlement_id": settlement_id, "party_id": party_id, "decision": decision, "duplicate": False}

    def _all_confirmed(self, settlement: sqlite3.Row, shares: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
        readiness = self._readiness(settlement["settlement_id"], int(settlement["quote_version"]),
                                    {party["party_id"] for party in shares})
        all_confirmed = bool(readiness) and all(
            entry is not None and entry["decision"] == "confirmed" for entry in readiness.values()
        )
        return all_confirmed, readiness

    # ----- 支付与退款（只追加台账）----------------------------------------

    def register_payment(self, actor_id: str, settlement_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "payment.write")
        payment_id = text_value(raw.get("payment_id"), "payment_id")
        direction = text_value(raw.get("direction", "inbound"), "direction")
        if direction not in {"inbound", "refund"}:
            raise ValidationFailed("支付方向必须是 inbound 或 refund")
        amount = money(raw.get("amount"), "amount", allow_zero=False)
        receipt_no = text_value(raw.get("receipt_no"), "receipt_no")
        method = text_value(raw.get("method", "bank_transfer"), "method", 32)
        payer_party_id = raw.get("payer_party_id")
        if payer_party_id is not None:
            payer_party_id = text_value(payer_party_id, "payer_party_id")
        note = str(raw.get("note", ""))[:1000]
        with transaction(self.connection, immediate=True):
            settlement = self._load_settlement(settlement_id)
            # 同一支付回执重复到达：校验关键字段一致后回放既有登记，绝不重复入账。
            existing = self.connection.execute(
                "SELECT * FROM settlement_payments WHERE receipt_no=?",
                (receipt_no,),
            ).fetchone()
            if existing is not None:
                if existing["settlement_id"] != settlement_id:
                    raise Conflict("支付回执已登记在另一张结算单上")
                if existing["direction"] != direction or Decimal(existing["amount"]) != amount:
                    raise Conflict("同一支付回执对应了不同的金额或方向")
                return {
                    "payment_id": existing["payment_id"],
                    "settlement_id": settlement_id,
                    "receipt_no": receipt_no,
                    "duplicate": True,
                    "balance_after": existing["balance_after"],
                }
            if settlement["state"] in {"void", "settled", "closed"}:
                raise InvalidState(f"结算单状态 {settlement['state']} 下不能登记收支；结算完成后资金已锁定")
            determination = self._load_determination(settlement["determination_id"])
            shares = json.loads(determination["shares_json"])
            if payer_party_id is not None and payer_party_id not in {p["party_id"] for p in shares}:
                raise ValidationFailed("付款方不在责任认定当事人名单中")
            paid = Decimal(settlement["amount_paid"])
            refunded = Decimal(settlement["amount_refunded"])
            net = paid - refunded
            payable = Decimal(settlement["total_payable"])
            if direction == "inbound":
                all_confirmed, _ = self._all_confirmed(settlement, shares)
                if int(settlement["quote_version"]) <= 0:
                    raise InvalidState("修理厂报价版本未形成，不能登记赔付")
                if not all_confirmed:
                    raise InvalidState("各方尚未在当前报价版本上全部确认，不能登记赔付")
                if settlement["state"] in MONEY_LOCKED_STATES and net >= payable:
                    raise InvalidState("结算单应付金额已结清，不能继续登记收款")
                if amount > payable - net:
                    raise InvalidState(
                        f"本次收款超过剩余应付金额 {format(payable - net, 'f')}；多收资金须走退款"
                    )
                paid += amount
            else:
                if net <= ZERO:
                    raise InvalidState("没有可退的结算资金")
                if amount > net:
                    raise InvalidState("退款金额不能超过结算单净收金额")
                refunded += amount
            net_after = paid - refunded
            state = self._derive_state(
                net_after, payable,
                None if net_after > ZERO else self._readiness(
                    settlement_id, int(settlement["quote_version"]),
                    {p["party_id"] for p in shares},
                ),
            )
            try:
                self.connection.execute(
                    "INSERT INTO settlement_payments(payment_id,settlement_id,direction,amount,receipt_no,"
                    "payer_party_id,method,note,balance_after,registered_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        payment_id,
                        settlement_id,
                        direction,
                        format(amount, "f"),
                        receipt_no,
                        payer_party_id,
                        method,
                        note,
                        format(net_after, "f"),
                        actor_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("支付回执号重复或支付编号冲突") from exc
            self.connection.execute(
                "UPDATE quick_settlements SET amount_paid=?,amount_refunded=?,state=?,revision=revision+1 "
                "WHERE settlement_id=?",
                (format(paid, "f"), format(refunded, "f"), state, settlement_id),
            )
            self._audit("settlement", settlement_id, "payment.registered", actor_id, {
                "payment_id": payment_id,
                "direction": direction,
                "amount": format(amount, "f"),
                "receipt_no": receipt_no,
                "balance_after": format(net_after, "f"),
                "total_payable": format(payable, "f"),
            })
        return {
            "payment_id": payment_id,
            "settlement_id": settlement_id,
            "receipt_no": receipt_no,
            "direction": direction,
            "amount": format(amount, "f"),
            "balance_after": format(net_after, "f"),
            "state": state,
            "duplicate": False,
        }

    # ----- 结清与结案 -----------------------------------------------------

    def settle(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        self._require(actor_id, "settlement.settle")
        with transaction(self.connection, immediate=True):
            settlement = self._load_settlement(settlement_id)
            if settlement["state"] in {"void", "closed"}:
                raise InvalidState(f"结算单状态 {settlement['state']} 不能完成结算")
            determination = self._load_determination(settlement["determination_id"])
            shares = json.loads(determination["shares_json"])
            all_confirmed, _ = self._all_confirmed(settlement, shares)
            if not all_confirmed:
                raise InvalidState("各方尚未全部确认当前报价版本")
            net = Decimal(settlement["amount_paid"]) - Decimal(settlement["amount_refunded"])
            payable = Decimal(settlement["total_payable"])
            if net != payable:
                raise InvalidState(
                    f"结算净额 {format(net, 'f')} 与应付 {format(payable, 'f')} 不一致，不能完成结算"
                )
            self.connection.execute(
                "UPDATE quick_settlements SET state='settled',settled_at=?,revision=revision+1 "
                "WHERE settlement_id=?",
                (self._now(), settlement_id),
            )
            self._audit("settlement", settlement_id, "settlement.completed", actor_id, {
                "total_payable": format(payable, "f"),
                "quote_version": settlement["quote_version"],
            })
        return self.settlement(actor_id, settlement_id)

    def close_case(self, actor_id: str, settlement_id: str, result: str = "settled") -> dict[str, Any]:
        """案件进入结案状态；只有结算完成才允许。"""
        self._require(actor_id, "case.close")
        result = text_value(result, "结案结果", 32)
        with transaction(self.connection, immediate=True):
            settlement = self._load_settlement(settlement_id)
            if settlement["state"] != "settled":
                raise InvalidState("快速结算未完成，案件不能进入结案状态")
            self.connection.execute(
                "UPDATE quick_settlements SET state='closed',closed_at=?,revision=revision+1 "
                "WHERE settlement_id=?",
                (self._now(), settlement_id),
            )
            self._audit("settlement", settlement_id, "case.closed", actor_id, {"result": result})
        return self.settlement(actor_id, settlement_id)

    # ----- 查询与审计重建 -------------------------------------------------

    def settlement(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        settlement = self._load_settlement(settlement_id)
        determination = self.connection.execute(
            "SELECT determination_id,incident_id,shares_json,content_sha256,state,decided_by,decided_at "
            "FROM liability_determinations WHERE determination_id=?",
            (settlement["determination_id"],),
        ).fetchone()
        shares = json.loads(determination["shares_json"])
        readiness = self._readiness(
            settlement_id, int(settlement["quote_version"]), {p["party_id"] for p in shares}
        ) if int(settlement["quote_version"]) > 0 else {}
        quote = self.connection.execute(
            "SELECT * FROM repair_quote_versions WHERE settlement_id=? AND quote_version=?",
            (settlement_id, int(settlement["quote_version"])),
        ).fetchone() if int(settlement["quote_version"]) > 0 else None
        breakdown_row = self.connection.execute(
            "SELECT breakdown_json FROM settlement_breakdowns WHERE settlement_id=? AND quote_version=?",
            (settlement_id, int(settlement["quote_version"])),
        ).fetchone()
        payments = self.connection.execute(
            "SELECT payment_id,direction,amount,receipt_no,payer_party_id,method,balance_after,registered_by,created_at "
            "FROM settlement_payments WHERE settlement_id=? ORDER BY created_at,payment_id",
            (settlement_id,),
        ).fetchall()
        result = dict(settlement)
        result["determination"] = {
            "determination_id": determination["determination_id"],
            "incident_id": determination["incident_id"],
            "state": determination["state"],
            "parties": shares,
            "content_sha256": determination["content_sha256"],
            "decided_by": determination["decided_by"],
            "decided_at": determination["decided_at"],
        }
        result["active_quote"] = None if quote is None else {
            "quote_version": quote["quote_version"],
            "shop_id": quote["shop_id"],
            "shop_name": quote["shop_name"],
            "quote_ref": quote["quote_ref"],
            "items": json.loads(quote["items_json"]),
            "content_sha256": quote["content_sha256"],
            "submitted_by": quote["submitted_by"],
            "created_at": quote["created_at"],
        }
        result["breakdown"] = None if breakdown_row is None else json.loads(breakdown_row["breakdown_json"])
        result["current_confirmations"] = [
            {"party_id": party_id, **(entry or {}), "state": "pending" if entry is None else entry["decision"]}
            for party_id, entry in sorted(readiness.items())
        ]
        result["payments"] = [dict(row) for row in payments]
        result["net_paid"] = format(Decimal(settlement["amount_paid"]) - Decimal(settlement["amount_refunded"]), "f")
        return result

    def settlement_report(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        """为办案人员和审计人员重建金额变化、报价版本与确认时间线。"""
        self._require(actor_id, "report.read")
        settlement = self._load_settlement(settlement_id)
        quotes = self.connection.execute(
            "SELECT quote_version,shop_id,shop_name,quote_ref,content_sha256,state,submitted_by,created_at "
            "FROM repair_quote_versions WHERE settlement_id=? ORDER BY quote_version",
            (settlement_id,),
        ).fetchall()
        breakdowns = self.connection.execute(
            "SELECT quote_version,breakdown_json FROM settlement_breakdowns WHERE settlement_id=? ORDER BY quote_version",
            (settlement_id,),
        ).fetchall()
        confirmations = self.connection.execute(
            "SELECT confirmation_id,quote_version,party_id,decision,note,state,void_reason,actor_id,created_at,voided_at "
            "FROM party_confirmations WHERE settlement_id=? ORDER BY confirmation_id",
            (settlement_id,),
        ).fetchall()
        payments = self.connection.execute(
            "SELECT payment_id,direction,amount,receipt_no,payer_party_id,method,balance_after,registered_by,created_at "
            "FROM settlement_payments WHERE settlement_id=? ORDER BY created_at,payment_id",
            (settlement_id,),
        ).fetchall()
        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM traffic_audit_events "
            "WHERE entity_type='settlement' AND entity_id=? ORDER BY event_id",
            (settlement_id,),
        ).fetchall()
        liability_events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM traffic_audit_events "
            "WHERE entity_type='liability' AND entity_id=? ORDER BY event_id",
            (settlement["determination_id"],),
        ).fetchall()
        return {
            "settlement": dict(settlement),
            "amount_timeline": [
                {
                    "quote_version": row["quote_version"],
                    "breakdown": json.loads(row["breakdown_json"]),
                }
                for row in breakdowns
            ],
            "quote_versions": [dict(row) for row in quotes],
            "confirmation_ledger": [dict(row) for row in confirmations],
            "payment_ledger": [dict(row) for row in payments],
            "events": [dict(row) | {"payload": json.loads(row["payload_json"])} for row in events],
            "liability_events": [
                dict(row) | {"payload": json.loads(row["payload_json"])} for row in liability_events
            ],
        }
