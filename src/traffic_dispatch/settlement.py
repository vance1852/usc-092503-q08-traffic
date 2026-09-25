"""快速结算单的责任分摊、免赔与金额守恒纯计算。

本模块不接触数据库和时钟，所有金额使用 ``Decimal`` 并量化到分（0.01），
保证赔付项目按责任比例分摊后合计与原金额严格一致，尾差归集到主要责任方。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .errors import ValidationFailed
from .planning import canonical_json, digest

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
WHOLE_SHARE = 10000


def money(value: object, field: str, *, allow_zero: bool = True) -> Decimal:
    """解析并量化金额，拒绝布尔、非有限数和负数。"""
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是十进制金额")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制金额") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限金额")
    if result < ZERO or (not allow_zero and result == ZERO):
        raise ValidationFailed(f"{field} 必须是正数")
    return result.quantize(CENT, rounding=ROUND_HALF_UP)


def text_value(value: object, field: str, maximum: int = 64) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def parse_shares(raw: object) -> list[dict[str, Any]]:
    """解析责任比例，单位为基点，合计必须严格等于 10000。"""
    if not isinstance(raw, list) or not raw:
        raise ValidationFailed("责任方列表不能为空")
    parties: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ValidationFailed("责任方必须是对象")
        party_id = text_value(entry.get("party_id"), "party_id")
        name = text_value(entry.get("name"), "当事人名称", 128)
        share_bp = entry.get("share_bp")
        if isinstance(share_bp, bool) or not isinstance(share_bp, int) or not 0 <= share_bp <= WHOLE_SHARE:
            raise ValidationFailed("责任比例必须是 0 到 10000 的整数基点")
        if party_id in seen:
            raise ValidationFailed(f"责任方重复: {party_id}")
        seen.add(party_id)
        parties.append({"party_id": party_id, "name": name, "share_bp": share_bp})
    if sum(party["share_bp"] for party in parties) != WHOLE_SHARE:
        raise ValidationFailed("责任比例合计必须等于 10000 个基点（100%）")
    return parties


def parse_items(raw: object, field: str, *, allow_empty: bool = False) -> list[dict[str, Any]]:
    """解析赔付/报价项目，金额量化到分。"""
    if raw is None:
        raw = []
    if not isinstance(raw, list) or (not raw and not allow_empty):
        raise ValidationFailed(f"{field} 必须是非空数组")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ValidationFailed(f"{field} 的每一项必须是对象")
        item_id = text_value(entry.get("item_id"), "item_id")
        if item_id in seen:
            raise ValidationFailed(f"项目编号重复: {item_id}")
        seen.add(item_id)
        title = text_value(entry.get("title"), "项目名称", 128)
        amount = money(entry.get("amount"), f"{field}.{item_id}.amount")
        items.append({"item_id": item_id, "title": title, "amount": amount})
    return items


def parse_deductibles(raw: object, party_ids: set[str]) -> dict[str, Decimal]:
    """解析每方免赔额，键必须是责任方。"""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValidationFailed("免赔额必须是以 party_id 为键的对象")
    result: dict[str, Decimal] = {}
    for key, value in raw.items():
        party_id = text_value(key, "免赔额 party_id")
        if party_id not in party_ids:
            raise ValidationFailed(f"免赔额对应了非责任方: {party_id}")
        result[party_id] = money(value, f"deductibles.{party_id}")
    return result


def split_amount(amount: Decimal, shares: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """按责任基点分摊金额，量化后合计恒等于原金额。

    先计算除主要责任方外各方的份额并量化，主要责任方（比例最高，平局时
    取 party_id 最小者）吸收量化尾差，确保分摊守恒且结果确定。
    """
    ordered = sorted(shares, key=lambda party: (-party["share_bp"], party["party_id"]))
    allocated: dict[str, Decimal] = {}
    distributed = ZERO
    for party in ordered[1:]:
        part = (amount * party["share_bp"] / WHOLE_SHARE).quantize(CENT, rounding=ROUND_HALF_UP)
        allocated[party["party_id"]] = part
        distributed += part
    allocated[ordered[0]["party_id"]] = amount - distributed
    return {party_id: format(value, "f") for party_id, value in allocated.items()}


def build_breakdown(
    version: int,
    shares: Sequence[Mapping[str, Any]],
    fixed_items: Sequence[Mapping[str, Any]],
    quote_items: Sequence[Mapping[str, Any]],
    deductibles: Mapping[str, Decimal],
) -> dict[str, Any]:
    """生成某一报价版本下的完整赔付、免赔与分摊计算单。"""
    all_items = [
        {**item, "kind": "repair"} for item in sorted(quote_items, key=lambda item: item["item_id"])
    ] + [
        {**item, "kind": "compensation"} for item in sorted(fixed_items, key=lambda item: item["item_id"])
    ]
    item_rows: list[dict[str, Any]] = []
    allocated_total = {party["party_id"]: ZERO for party in shares}
    total_amount = ZERO
    for item in sorted(all_items, key=lambda item: (item["kind"], item["item_id"])):
        allocations = split_amount(item["amount"], shares)
        item_rows.append({
            "item_id": item["item_id"],
            "kind": item["kind"],
            "title": item["title"],
            "amount": format(item["amount"], "f"),
            "allocations": allocations,
        })
        total_amount += item["amount"]
        for party_id, part in allocations.items():
            allocated_total[party_id] += Decimal(part)
    party_rows: list[dict[str, Any]] = []
    total_deductible = ZERO
    total_payable = ZERO
    for party in sorted(shares, key=lambda item: item["party_id"]):
        allocated = allocated_total[party["party_id"]].quantize(CENT)
        deductible = min(deductibles.get(party["party_id"], ZERO), allocated).quantize(CENT)
        payable = allocated - deductible
        total_deductible += deductible
        total_payable += payable
        party_rows.append({
            "party_id": party["party_id"],
            "name": party["name"],
            "share_bp": party["share_bp"],
            "allocated_total": format(allocated, "f"),
            "deductible": format(deductible, "f"),
            "retained": format(deductible, "f"),
            "payable": format(payable, "f"),
        })
    fingerprint_input = {
        "version": version,
        "shares": [{"party_id": p["party_id"], "share_bp": p["share_bp"]} for p in sorted(shares, key=lambda x: x["party_id"])],
        "items": [
            {"item_id": row["item_id"], "kind": row["kind"], "amount": row["amount"]}
            for row in item_rows
        ],
        "deductibles": {key: format(value, "f") for key, value in sorted(deductibles.items())},
    }
    return {
        "version": version,
        "items": item_rows,
        "parties": party_rows,
        "total_amount": format(total_amount, "f"),
        "total_deductible": format(total_deductible, "f"),
        "total_payable": format(total_payable, "f"),
        "input_sha256": digest(fingerprint_input),
    }


def breakdown_conservation(breakdown: Mapping[str, Any]) -> None:
    """校验存储的计算单守恒：每项分摊之和等于项目金额；各方合计等于总额。"""
    for item in breakdown["items"]:
        total = sum((Decimal(part) for part in item["allocations"].values()), ZERO)
        if total != Decimal(item["amount"]):
            raise ValidationFailed(f"项目 {item['item_id']} 分摊金额不守恒")
    total_payable = sum((Decimal(party["payable"]) for party in breakdown["parties"]), ZERO)
    total_retained = sum((Decimal(party["retained"]) for party in breakdown["parties"]), ZERO)
    if total_payable + total_retained != Decimal(breakdown["total_amount"]):
        raise ValidationFailed("计算单应付与免赔合计不等于总金额")
