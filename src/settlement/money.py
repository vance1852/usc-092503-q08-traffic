"""结算金额的确定性计算：免赔扣除、责任比例分摊与零头归集。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Mapping, Sequence


MONEY_QUANTUM = Decimal("0.01")
PERCENT_QUANTUM = Decimal("0.01")
ZERO = Decimal("0")
HUNDRED = Decimal("100")

CALC_RULES_VERSION = "1.0"


class CalcError(ValueError):
    """分摊输入不满足结算计算契约。"""


def money(value: object, field: str = "金额") -> Decimal:
    if isinstance(value, bool):
        raise CalcError(f"{field} 必须是十进制数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CalcError(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise CalcError(f"{field} 必须是有限数值")
    return result.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def percent(value: object, field: str = "责任比例") -> Decimal:
    if isinstance(value, bool):
        raise CalcError(f"{field} 必须是十进制数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CalcError(f"{field} 必须是十进制数值") from exc
    if not result.is_finite() or result < ZERO or result > HUNDRED:
        raise CalcError(f"{field} 必须在 0 到 100 之间")
    quantized = result.quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_UP)
    if quantized != result:
        raise CalcError(f"{field} 最多保留两位小数")
    return quantized


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class QuoteItem:
    """修理厂报价中的一个赔付项目。"""

    item_id: str
    vehicle_id: str
    owner_party_id: str
    kind: str
    amount: Decimal
    deductible_cny: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "QuoteItem":
        item_id = str(raw.get("item_id", "")).strip()
        vehicle_id = str(raw.get("vehicle_id", "")).strip()
        owner_party_id = str(raw.get("owner_party_id", "")).strip()
        kind = str(raw.get("kind", "")).strip()
        if not item_id:
            raise CalcError("赔付项目缺少 item_id")
        if not vehicle_id:
            raise CalcError(f"赔付项目 {item_id} 缺少 vehicle_id")
        if not owner_party_id:
            raise CalcError(f"赔付项目 {item_id} 缺少 owner_party_id")
        if not kind:
            raise CalcError(f"赔付项目 {item_id} 缺少 kind")
        amount = money(raw.get("amount"), f"赔付项目 {item_id}.amount")
        if amount <= ZERO:
            raise CalcError(f"赔付项目 {item_id}.amount 必须大于零")
        deductible = money(raw.get("deductible_cny", 0), f"赔付项目 {item_id}.deductible_cny")
        if deductible < ZERO or deductible > amount:
            raise CalcError(f"赔付项目 {item_id} 的免赔额必须在 0 与项目金额之间")
        return cls(item_id, vehicle_id, owner_party_id, kind, amount, deductible)


def _largest_remainder(net: Decimal, shares: Sequence[tuple[str, Decimal]]) -> dict[str, Decimal]:
    """把 net 按比例切分：先向下取整到分，剩余的每一分按零头从大到小分配（并列取编号最小者）。"""

    cents = (net * 100).to_integral_value(rounding=ROUND_FLOOR)
    raw = [(party_id, cents * ratio) for party_id, ratio in shares]
    floors = {party_id: int(value.to_integral_value(rounding=ROUND_FLOOR)) for party_id, value in raw}
    leftover_cents = int(cents) - sum(floors.values())
    order = sorted(
        ((value - floors[party_id], party_id) for party_id, value in raw),
        key=lambda entry: (-entry[0], entry[1]),
    )
    for index in range(leftover_cents):
        floors[order[index % len(order)][1]] += 1
    return {party_id: Decimal(value).scaleb(-2) for party_id, value in floors.items()}


def compute_settlement(
    shares_raw: Mapping[str, object],
    items_raw: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """根据责任比例与报价项目计算免赔、各方分摊与合计。

    规则（CALC_RULES_VERSION 1.0）：
    - 每个报价项目先扣除绝对免赔额，免赔额由该项目车辆所有方自行承担；
    - 扣除免赔后的可赔金额按已生效责任认定的各方比例分摊；
    - 比例分摊按两位小数四舍五入，零头按最大零头法归集，保证项目分摊分毫不差。
    """

    if not isinstance(shares_raw, Mapping) or not shares_raw:
        raise CalcError("责任比例不能为空")
    if not isinstance(items_raw, Sequence) or isinstance(items_raw, (str, bytes)) or not items_raw:
        raise CalcError("赔付项目不能为空")

    shares: list[tuple[str, Decimal]] = []
    percent_total = ZERO
    for party_id, raw_value in shares_raw.items():
        party_id = str(party_id).strip()
        if not party_id:
            raise CalcError("责任方编号不能为空")
        ratio_percent = percent(raw_value, f"责任比例 {party_id}")
        shares.append((party_id, ratio_percent))
        percent_total += ratio_percent
    shares.sort(key=lambda entry: entry[0])
    if percent_total != HUNDRED:
        raise CalcError(f"责任比例之和必须等于 100，当前为 {percent_total}")
    ratios = [(party_id, value / HUNDRED) for party_id, value in shares]
    party_ids = {party_id for party_id, _ in shares}

    items = tuple(QuoteItem.from_dict(raw) for raw in items_raw)
    if len({item.item_id for item in items}) != len(items):
        raise CalcError("赔付项目 item_id 不能重复")

    party_claim = {party_id: ZERO for party_id in party_ids}
    party_deductible = {party_id: ZERO for party_id in party_ids}
    item_rows: list[dict[str, Any]] = []
    gross_total = ZERO
    deductible_total = ZERO

    for item in items:
        if item.owner_party_id not in party_ids:
            raise CalcError(f"赔付项目 {item.item_id} 的车辆所有方不在责任认定当事人之列")
        net = item.amount - item.deductible_cny
        split = _largest_remainder(net, ratios) if net > ZERO else {party_id: ZERO for party_id in party_ids}
        for party_id, value in split.items():
            party_claim[party_id] += value
        party_deductible[item.owner_party_id] += item.deductible_cny
        gross_total += item.amount
        deductible_total += item.deductible_cny
        item_rows.append({
            "item_id": item.item_id,
            "vehicle_id": item.vehicle_id,
            "owner_party_id": item.owner_party_id,
            "kind": item.kind,
            "amount": format(item.amount, "f"),
            "deductible_cny": format(item.deductible_cny, "f"),
            "claimable_cny": format(net, "f"),
            "split": {party_id: format(value, "f") for party_id, value in sorted(split.items())},
        })

    claim_total = gross_total - deductible_total
    item_rows.sort(key=lambda row: row["item_id"])
    party_rows = []
    for party_id in sorted(party_ids):
        claim = party_claim[party_id].quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        deductible = party_deductible[party_id].quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        party_rows.append({
            "party_id": party_id,
            "liability_percent": format(dict(shares)[party_id], "f"),
            "claim_due_cny": format(claim, "f"),
            "deductible_self_borne_cny": format(deductible, "f"),
            "total_due_cny": format(claim + deductible, "f"),
        })

    computed = {
        "calc_rules_version": CALC_RULES_VERSION,
        "rounding": "input:ROUND_HALF_UP@0.01;split:largest_remainder",
        "items": item_rows,
        "parties": party_rows,
        "gross_total_cny": format(gross_total, "f"),
        "deductible_total_cny": format(deductible_total, "f"),
        "claim_total_cny": format(claim_total, "f"),
    }
    computed["calc_sha256"] = digest({key: value for key, value in computed.items()})
    return computed
