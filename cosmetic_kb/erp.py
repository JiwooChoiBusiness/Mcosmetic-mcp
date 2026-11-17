"""Simulated ERP / mail actions for the M Cosmetic demo.

Nothing here talks to a real ERP or mail server and nothing is stored: every call computes a
response from the snapshot data, using the same response shapes and business rules that appear in
the dataset's recorded ERP results ("Processing flow / ERP result" blocks and Case action history).

Rules reproduced from the data
  * discount by quantity (inferred from 32 recorded price look-ups):
      <300 EA 0%, 300-499 EA 5%, 500-999 EA 8%, >=1000 EA 12%
  * unit price = base x (1 - rate), rounded half-up to 10 KRW        (12,400 x 0.92 = 11,408 -> 11,410)
  * invoice: amount = qty x price, VAT 10%, total = amount + VAT
  * allocated stock = open Sales Orders ('생산 계획', '출하 준비') of the item, available = on-hand - allocated
  * IDs: SO-YYYYMMDD-NNNNN, INV-YYYYMMDD-NNNNN, MSG-NNNNN; APIs as in section 4 of the dataset

Write actions (order registration, mail sending) are two-step: a preview returns the exact data
changes and a preview_id that is an HMAC of those changes; execution requires the same preview_id,
so what runs is exactly what the user approved. Being stateless, repeating a call gives the same result.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .kb import KnowledgeBase

TODAY = "2026-10-22"  # snapshot date
WAREHOUSE = "화성 제1물류센터"
MAIL_FROM = "sales@mcosmetic.co.kr"
OPEN_SO_STATES = ("생산 계획", "출하 준비")
REQUIRED_ORDER_FIELDS = ("customerCode", "itemCode", "quantity", "deliveryDate", "shippingAddress")
_SECRET = (os.environ.get("COSMETIC_PREVIEW_SECRET") or "mcosmetic-demo-preview").encode()
KST = timezone(timedelta(hours=9))


class ErpInputError(ValueError):
    """Bad input that the caller should fix (mapped to ToolError by the server)."""


# --------------------------------------------------------------------------- helpers
def now_hm() -> str:
    return datetime.now(KST).strftime("%H:%M")


def stamp() -> str:
    return f"{TODAY} {now_hm()}"


def _h(*parts: Any) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16)


def erp_ms(*seed: Any) -> int:
    return 700 + _h("ms", *seed) % 600  # recorded ERP calls took 0.7-1.3 s


def discount_rate(qty: int) -> float:
    if qty >= 1000:
        return 0.12
    if qty >= 500:
        return 0.08
    if qty >= 300:
        return 0.05
    return 0.0


def unit_price(base: int, qty: int) -> int:
    return int(math.floor(base * (1 - discount_rate(qty)) / 10 + 0.5)) * 10


def won(n: int | float) -> str:
    return f"{int(n):,}원"


def preview_id(kind: str, plan: dict[str, Any]) -> str:
    body = json.dumps(plan, ensure_ascii=False, sort_keys=True, default=str)
    return "PV-" + hmac.new(_SECRET, f"{kind}|{body}".encode(), hashlib.sha256).hexdigest()[:12]


def resolve_item(kb: KnowledgeBase, item: str) -> dict[str, Any]:
    t = item.strip().upper()
    if t in kb.items:
        return kb.items[t]
    key = item.replace(" ", "").lower()
    hits = [it for it in kb.items.values()
            if key in (it["name"].replace(" ", "").lower(), str(it.get("Short name", "")).replace(" ", "").lower())]
    if not hits:
        hits = [it for it in kb.items.values() if key and key in str(it.get("Short name", "")).replace(" ", "").lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ErpInputError(f"품목 '{item}'을(를) 품목 마스터에서 찾지 못했습니다. 품목코드(MC-…) 또는 정확한 품목명을 주세요.")
    raise ErpInputError(f"품목 '{item}'이(가) 여러 개와 일치합니다: " + ", ".join(f"{h['itemCode']} {h['Short name']}" for h in hits))


def resolve_customer(kb: KnowledgeBase, customer: str | None) -> dict[str, Any] | None:
    if not customer:
        return None
    codes = kb.find_customer_codes(customer)
    if len(codes) == 1:
        return kb.customers[codes[0]]
    if not codes:
        raise ErpInputError(f"고객 '{customer}'을(를) 고객 마스터에서 찾지 못했습니다.")
    raise ErpInputError(f"고객 '{customer}'이(가) 여러 곳과 일치합니다: " +
                        ", ".join(f"{c} {kb.customers[c]['name']}" for c in codes))


def allocation(kb: KnowledgeBase, item_code: str) -> tuple[int, list[dict[str, Any]]]:
    rows = [v for v in kb.sales_orders.values() if v["itemCode"] == item_code and v["state"] in OPEN_SO_STATES]
    return sum(v["orderQty"] - v["shipQty"] for v in rows), [
        {"so": v["so"], "customer": kb.customers.get(v["customerCode"], {}).get("name"),
         "qty": v["orderQty"] - v["shipQty"], "shipDate": v["shipDate"], "state": v["state"], "line": v["line"]}
        for v in rows]


def _envelope(api: str, result: dict[str, Any], *seed: Any) -> dict[str, Any]:
    return {"erp": "done", "erpMs": erp_ms(api, *seed), "erpApi": api, "erpError": "", "asOf": stamp(),
            "erpResult": result, "simulated": True}


# --------------------------------------------------------------------------- look-ups
def price_lookup(kb: KnowledgeBase, item: str, quantity: int, customer: str | None = None) -> dict[str, Any]:
    if quantity <= 0:
        raise ErpInputError("quantity는 1 이상이어야 합니다.")
    it = resolve_item(kb, item)
    cu = resolve_customer(kb, customer)
    alloc, _ = allocation(kb, it["itemCode"])
    available = max(it["Stock"] - alloc, 0)
    rate = discount_rate(quantity)
    res = {"itemName": it["name"], "short": it["Short name"], "itemCode": it["itemCode"], "base": it["Base price"],
           "price": unit_price(it["Base price"], quantity), "rate": rate, "qty": quantity, "stock": it["Stock"],
           "inStock": quantity <= available,
           "lead": it["Lead date"] if quantity <= available else it["Back lead date"]}
    api = f"GET /api/erp/v1/prices?customer={cu['customerCode'] if cu else ''}&item={it['itemCode']}&qty={quantity}"
    out = _envelope(api, res, quantity)
    out["explain"] = (f"기본 단가 {won(it['Base price'])} × (1 - {int(rate * 100)}%) = {won(res['price'])} "
                      f"(10원 단위 반올림). 공급가 {won(res['price'] * quantity)}, 부가세 포함 "
                      f"{won(round(res['price'] * quantity * 1.1))}. 가용 재고 {available:,}EA 기준 납기 가능일 {res['lead']}.")
    out["pricing_rule"] = "수량 할인: 300개 미만 0% / 300~499개 5% / 500~999개 8% / 1,000개 이상 12% (데이터의 ERP 단가 조회 기록 32건에서 확인한 규칙)"
    out["sources"] = {"item_master_lines": f"{it['line_start']}-{it['line_end']}"}
    return out


def inventory_lookup(kb: KnowledgeBase, item: str, quantity: int | None = None) -> dict[str, Any]:
    it = resolve_item(kb, item)
    alloc, orders = allocation(kb, it["itemCode"])
    available = max(it["Stock"] - alloc, 0)
    res = {"itemName": it["name"], "short": it["Short name"], "itemCode": it["itemCode"], "onHand": it["Stock"],
           "allocated": alloc, "available": available, "shortage": max(alloc - it["Stock"], 0),
           "reqQty": quantity, "enough": (quantity <= available) if quantity else None,
           "leadDate": it["Lead date"], "backLeadDate": it["Back lead date"], "warehouse": WAREHOUSE,
           "asOf": stamp(), "allocatedOrders": orders}
    out = _envelope(f"GET /api/erp/v1/inventory/on-hand?item={it['itemCode']}", res, quantity)
    msg = f"현재 재고 {it['Stock']:,}EA - 출하 예정 {alloc:,}EA = 가용 {available:,}EA"
    if res["shortage"]:
        msg += f" (이미 {res['shortage']:,}EA 부족)"
    if quantity:
        msg += f". 요청 {quantity:,}EA → {'출고 가능' if res['enough'] else '재고 부족: 분할 납품 또는 ' + it['Back lead date'] + ' 이후 납품 제안'}"
    out["explain"] = msg
    return out


def shipment_lookup(kb: KnowledgeBase, sales_order: str | None = None, customer: str | None = None) -> dict[str, Any]:
    if not sales_order and not customer:
        raise ErpInputError("sales_order 또는 customer 중 하나는 필요합니다.")
    rows = []
    if sales_order:
        so = sales_order.strip().upper()
        if so not in kb.sales_orders:
            raise ErpInputError(f"Sales Order '{sales_order}'이(가) ERP 출하 데이터에 없습니다.")
        rows = [kb.sales_orders[so]]
    else:
        cu = resolve_customer(kb, customer)
        rows = [v for v in kb.sales_orders.values() if v["customerCode"] == cu["customerCode"]]
    res = [{"matchedBy": 1, **{k: v for k, v in r.items() if k != "line"}, "line": r["line"]} for r in rows]
    api = "GET /api/erp/v1/shipments?" + (f"so={sales_order.strip().upper()}" if sales_order else
                                         f"customer={rows[0]['customerCode'] if rows else ''}")
    out = _envelope(api, res[0] if len(res) == 1 else {"count": len(res), "shipments": res}, sales_order, customer)
    return out


def _so_price(kb: KnowledgeBase, so: str, row: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    for c in kb.cases.values():
        if c.fields.get("salesOrderNumber") == so:
            ent = json.loads(c.fields.get("entity") or "{}")
            if ent.get("unitPrice"):
                return int(ent["unitPrice"]), {"case": c.id, "entity": ent}
    base = kb.items[row["itemCode"]]["Base price"]
    return unit_price(base, row["orderQty"]), None


def sales_order_lookup(kb: KnowledgeBase, sales_order: str) -> dict[str, Any]:
    so = sales_order.strip().upper()
    if so not in kb.sales_orders:
        raise ErpInputError(f"Sales Order '{sales_order}'이(가) ERP에 없습니다.")
    row = kb.sales_orders[so]
    it = kb.items[row["itemCode"]]
    cu = kb.customers.get(row["customerCode"], {})
    price, src = _so_price(kb, so, row)
    amount = price * row["orderQty"]
    vat = round(amount * 0.1)
    ent = (src or {}).get("entity", {})
    res = {"so": so, "invoiceNo": "INV-" + so[3:], "customer": cu.get("name"), "customerCode": row["customerCode"],
           "itemName": it["name"], "short": it["Short name"], "itemCode": it["itemCode"], "qty": row["orderQty"],
           "price": price, "amount": amount, "vat": vat, "total": amount + vat,
           "orderDate": f"{so[3:7]}-{so[7:9]}-{so[9:11]}", "deliveryDate": ent.get("deliveryDate") or row["shipDate"],
           "state": row["state"], "billTo": ent.get("shippingAddress", "")}
    out = _envelope(f"GET /api/erp/v1/sales-orders/{so}", res)
    out["explain"] = f"{row['orderQty']:,}EA × {won(price)} = {won(amount)} + 부가세 10% {won(vat)} = 합계 {won(amount + vat)}"
    out["sources"] = {"sales_order_line": row["line"], "price_from": (src or {}).get("case", "수량 할인 규칙으로 계산")}
    return out


# --------------------------------------------------------------------------- order registration
def _order_plan(kb: KnowledgeBase, case_id: str | None, customer: str | None, item: str | None,
                quantity: int | None, delivery_date: str | None, shipping_address: str | None,
                unit_price_override: int | None) -> dict[str, Any]:
    case = None
    ent: dict[str, Any] = {}
    if case_id:
        cid = case_id.strip().upper()
        if cid not in kb.cases:
            raise ErpInputError(f"Case '{case_id}'을(를) 찾지 못했습니다.")
        case = kb.cases[cid]
        ent = json.loads(case.fields.get("entity") or "{}")
        ent["customerCode"] = case.fields.get("customerCode")
    if customer:
        cu = resolve_customer(kb, customer)
        ent["customerCode"] = cu["customerCode"]
    if item:
        it = resolve_item(kb, item)
        ent["itemCode"], ent["itemName"] = it["itemCode"], it["Short name"]
    if quantity is not None:
        ent["quantity"] = quantity
    if delivery_date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", delivery_date.strip()):
            raise ErpInputError("delivery_date는 YYYY-MM-DD 형식이어야 합니다.")
        ent["deliveryDate"] = delivery_date.strip()
    if shipping_address:
        ent["shippingAddress"] = shipping_address.strip()
    if unit_price_override:
        ent["unitPrice"] = unit_price_override
    if not case and not ent.get("customerCode"):
        raise ErpInputError("case_id 또는 customer/item/quantity를 주세요.")

    missing = [f for f in REQUIRED_ORDER_FIELDS if not ent.get(f)]
    plan: dict[str, Any] = {"case_id": case.id if case else None, "fields": {f: ent.get(f) for f in REQUIRED_ORDER_FIELDS},
                            "missing": missing}
    if case and case.fields.get("salesOrderNumber"):
        plan["already"] = case.fields["salesOrderNumber"]
        return plan
    if missing:
        return plan

    it = kb.items[ent["itemCode"]]
    cu = kb.customers[ent["customerCode"]]
    qty = int(ent["quantity"])
    price = int(ent.get("unitPrice") or unit_price(it["Base price"], qty))
    alloc, _ = allocation(kb, it["itemCode"])
    available = max(it["Stock"] - alloc, 0)
    enough = qty <= available
    possible = it["Lead date"] if enough else it["Back lead date"]
    ship = max(ent["deliveryDate"], possible)
    seq = int(case.id.rsplit("-", 1)[1]) if case else 500 + _h(cu["customerCode"], it["itemCode"], qty) % 400
    so = f"SO-{TODAY.replace('-', '')}-{seq:05d}"
    amount = price * qty
    vat = round(amount * 0.1)
    plan.update({
        "so": so, "customer": cu["name"], "customerCode": cu["customerCode"], "itemCode": it["itemCode"],
        "item": it["Short name"], "qty": qty, "price": price, "price_source": "견적 단가" if ent.get("unitPrice") else "수량 할인 규칙",
        "amount": amount, "vat": vat, "total": amount + vat, "requested": ent["deliveryDate"], "shipDate": ship,
        "soState": "출하 준비" if enough else "생산 계획", "available_before": available,
        "stock": it["Stock"], "allocated": alloc,
        "shortage_before": max(alloc - it["Stock"], 0), "shortage_after": max(alloc + qty - it["Stock"], 0),
        "available_after": max(available - qty, 0), "enough": enough, "shippingAddress": ent["shippingAddress"],
        "quoteRef": (case.fields.get("quoteRef") if case else None),
        "case_status_before": case.fields.get("status") if case else None,
        "open_errors": [e["id"] for e in kb.errors.values() if case and e.get("caseId") == case.id and e["status"] == "open"],
    })
    return plan


def _order_changes(plan: dict[str, Any], when: str) -> list[dict[str, Any]]:
    ch = [{"target": "Sales Order", "action": "생성", "after": {
        "so": plan["so"], "customer": plan["customer"], "item": plan["item"], "qty": plan["qty"],
        "price": plan["price"], "amount": plan["amount"], "vat": plan["vat"], "total": plan["total"],
        "shipDate": plan["shipDate"], "state": plan["soState"], "shippingAddress": plan["shippingAddress"]}}]
    if plan["case_id"]:
        ch += [{"target": plan["case_id"], "field": "status", "before": plan["case_status_before"], "after": "주문등록 완료"},
               {"target": plan["case_id"], "field": "salesOrderNumber", "before": "", "after": plan["so"]},
               {"target": plan["case_id"], "field": "timeline", "add":
                   f"{when} | action | ERP 주문등록 | 완료 | result: Sales Order {plan['so']} 생성"}]
    if plan["enough"]:
        ch.append({"target": f"재고 {plan['item']}", "field": "가용 재고", "before": plan["available_before"],
                   "after": plan["available_after"]})
    else:
        ch.append({"target": f"재고 {plan['item']}", "field": "재고 부족량", "before": plan["shortage_before"],
                   "after": plan["shortage_after"]})
    for err in plan["open_errors"]:
        ch.append({"target": err, "field": "status", "before": "open", "after": "resolved", "note": "ERP 주문등록 재시도 성공"})
    return ch


def _order_text(plan: dict[str, Any]) -> str:
    lines = ["[ERP 주문등록 — 반영 예정 데이터]",
             f"1) Sales Order 생성: {plan['so']} | {plan['customer']} | {plan['item']} {plan['qty']:,}EA | 단가 {won(plan['price'])} ({plan['price_source']})",
             f"   공급가 {won(plan['amount'])} · 부가세 {won(plan['vat'])} · 합계 {won(plan['total'])} | 출하예정 {plan['shipDate']} | 상태 {plan['soState']}",
             f"   배송지 {plan['shippingAddress']}"]
    if plan["case_id"]:
        lines += [f"2) 업무 Case {plan['case_id']}: 상태 {plan['case_status_before']} → 주문등록 완료, SO 번호 (없음) → {plan['so']}, 타임라인에 ERP 주문등록 완료 추가"]
    n = 3 if plan["case_id"] else 2
    if plan["enough"]:
        lines.append(f"{n}) 재고 영향: {plan['item']} 가용 재고 {plan['available_before']:,} → {plan['available_after']:,}")
    else:
        lines.append(f"{n}) 재고 영향: {plan['item']} 재고 {plan['stock']:,} / 출하 예정 {plan['allocated']:,} → "
                     + (f"부족 {plan['shortage_before']:,}EA → {plan['shortage_after']:,}EA" if plan["shortage_before"]
                        else f"가용 {plan['available_before']:,}EA → 부족 {plan['shortage_after']:,}EA"))
        lines.append(f"   ※ 가용 재고 부족 → '생산 계획'으로 등록, 출하예정 {plan['shipDate']} (후순위 납기)")
    if plan["requested"] != plan["shipDate"]:
        lines.append(f"   ※ 고객 희망 납기 {plan['requested']} → 가능일 {plan['shipDate']}로 조정 필요(고객 안내 권장)")
    for err in plan["open_errors"]:
        lines.append(f"   ※ 이전 실패 {err} → 해결 처리")
    return "\n".join(lines)


def register_order(kb: KnowledgeBase, case_id: str | None = None, customer: str | None = None,
                   item: str | None = None, quantity: int | None = None, delivery_date: str | None = None,
                   shipping_address: str | None = None, unit_price_override: int | None = None,
                   confirm: bool = False, preview: str | None = None, simulate_failure: bool = False) -> dict[str, Any]:
    plan = _order_plan(kb, case_id, customer, item, quantity, delivery_date, shipping_address, unit_price_override)
    if plan.get("already"):
        return {"status": "already_registered", "case_id": plan["case_id"], "so": plan["already"], "simulated": True,
                "message": f"{plan['case_id']}은(는) 이미 {plan['already']}로 ERP에 등록되어 있습니다."}
    if plan["missing"]:
        return {"status": "blocked", "case_id": plan["case_id"], "missing_fields": plan["missing"], "fields": plan["fields"],
                "simulated": True,
                "message": "주문등록 필수값이 빠져 ERP에 등록할 수 없습니다: " + ", ".join(plan["missing"]),
                "next": "고객에게 빠진 정보를 요청하는 메일(send_email)을 보내거나, 값을 받아 같은 도구에 해당 파라미터(예: shipping_address)를 넣어 다시 미리보기 하세요."}
    pid = preview_id("order", plan)
    if not confirm:
        return {"status": "preview", "requires_approval": True, "preview_id": pid, "simulated": True,
                "summary_text": _order_text(plan), "changes": _order_changes(plan, f"{TODAY} (승인 시각)"),
                "next": f"사용자에게 summary_text를 그대로 보여주고 승인을 받으면 같은 인자에 confirm=true, preview_id='{pid}'로 다시 호출하세요. 승인 전에는 등록됐다고 말하지 마세요."}
    if preview != pid:
        raise ErpInputError("preview_id가 없거나 미리보기 내용과 다릅니다. confirm=false로 다시 미리보기를 받아 사용자 승인 후 그 preview_id로 실행하세요.")
    when = stamp()
    base = {"orderApi": "POST /api/erp/v1/sales-orders", "erpMs": erp_ms("order", pid), "simulated": True,
            "persisted": False, "requestedDate": when}
    if simulate_failure:
        return {**base, "status": "failed", "order": "error", "orderResult": None,
                "orderError": "ERP API Timeout (응답 대기 30초 초과)",
                "message": "ERP 주문등록 실패(시뮬레이션). 잠시 후 같은 preview_id로 재시도하거나 담당자에게 넘기세요."}
    return {**base, "status": "done", "order": "done", "orderError": "", "completedDate": when,
            "orderResult": {"so": plan["so"], "customer": plan["customer"], "item": plan["item"], "qty": plan["qty"],
                            "date": plan["shipDate"], "price": plan["price"]},
            "invoice_preview": {"amount": plan["amount"], "vat": plan["vat"], "total": plan["total"]},
            "applied_changes": _order_changes(plan, when),
            "message": f"ERP 주문등록 완료: Sales Order {plan['so']} 생성 ({plan['customer']}, {plan['item']} {plan['qty']:,}EA, 출하예정 {plan['shipDate']})"}


# --------------------------------------------------------------------------- mail sending
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _contact_by_email(kb: KnowledgeBase, addr: str) -> dict[str, Any] | None:
    for c in kb.customers.values():
        if (c.get("contact") or {}).get("m", "").lower() == addr.lower():
            return c
    return None


def send_email(kb: KnowledgeBase, to: str, subject: str, body: str, case_id: str | None = None,
               cc: str | None = None, confirm: bool = False, preview: str | None = None,
               simulate_failure: bool = False) -> dict[str, Any]:
    to = to.strip()
    if not EMAIL_RE.match(to):
        raise ErpInputError(f"받는 사람 주소 '{to}'가 올바른 이메일 형식이 아닙니다.")
    if not subject.strip() or not body.strip():
        raise ErpInputError("subject와 body는 비어 있을 수 없습니다.")
    case = None
    if case_id:
        if case_id.strip().upper() not in kb.cases:
            raise ErpInputError(f"Case '{case_id}'을(를) 찾지 못했습니다.")
        case = kb.cases[case_id.strip().upper()]
    contact = _contact_by_email(kb, to)
    warnings = []
    if not contact:
        warnings.append("받는 주소가 고객 연락처 마스터에 없습니다. 수신자를 다시 확인하세요.")
    elif case and contact.get("customerCode") != case.fields.get("customerCode"):
        warnings.append(f"받는 사람({contact['name']})이 Case 고객({case.fields.get('customer')})과 다릅니다.")
    if "M코스메틱" not in body:
        warnings.append("본문에 M코스메틱 서명이 없습니다.")
    plan = {"to": to, "cc": cc or "", "subject": subject.strip(), "body_sha": hashlib.sha256(body.encode()).hexdigest()[:16],
            "case_id": case.id if case else None}
    pid = preview_id("mail", plan)
    msg_id = f"MSG-{84 + _h(pid) % 9000:05d}"
    changes = [{"target": "발신 메일", "action": "추가", "after": {
        "direction": "out", "from": MAIL_FROM, "to": to, "cc": cc or "", "subject": subject.strip(),
        "customer": contact["name"] if contact else "(마스터에 없는 수신자)", "messageId": msg_id}}]
    if case:
        changes.append({"target": case.id, "field": "timeline", "add": f"{TODAY} (발송 시각) | mail (out) | {subject.strip()}"})
    if not confirm:
        text = "\n".join(["[메일 발송 — 반영 예정 데이터]", f"보내는 사람: {MAIL_FROM}", f"받는 사람: {to}"
                          + (f" ({contact['name']} {contact['contact']['n']})" if contact else ""),
                          f"참조: {cc or '-'}", f"제목: {subject.strip()}", "본문:", body.strip(),
                          f"연결 Case: {case.id if case else '-'}"] + [f"⚠ {w}" for w in warnings])
        return {"status": "preview", "requires_approval": True, "preview_id": pid, "simulated": True,
                "summary_text": text, "warnings": warnings, "changes": changes,
                "next": f"사용자에게 summary_text를 보여주고 승인을 받으면 같은 인자에 confirm=true, preview_id='{pid}'로 다시 호출하세요. 승인 전에는 보냈다고 말하지 마세요."}
    if preview != pid:
        raise ErpInputError("preview_id가 없거나 미리보기 내용과 다릅니다(제목·본문·수신자가 바뀌었으면 다시 미리보기 필요).")
    when = stamp()
    if simulate_failure:
        return {"status": "failed", "mail": "error", "mailApi": "POST /api/mail/v1/send", "simulated": True,
                "mailError": "SMTP 451 Temporary failure: 수신 서버 응답 지연", "requestedDate": when,
                "message": "메일 발송 실패(시뮬레이션). 잠시 후 같은 preview_id로 재시도하세요."}
    for c in changes:
        if "add" in c:
            c["add"] = c["add"].replace("(발송 시각)", when.split(" ")[1])
    return {"status": "done", "mail": "sent", "mailApi": "POST /api/mail/v1/send", "mailError": "", "messageId": msg_id,
            "sentAt": when, "simulated": True, "persisted": False, "applied_changes": changes,
            "message": f"메일 발송 완료: {to} / '{subject.strip()}' (메시지 ID {msg_id})"}
