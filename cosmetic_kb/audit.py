"""Cross-checks that a model would otherwise have to do by hand.

erp_order_audit: order mails / cases in a period vs. the ERP Sales Order table -> which orders are
not (correctly) registered in ERP, why, how much, who owns it and what to do next.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .erp import unit_price
from .kb import KnowledgeBase

AUDIT_INTENT = re.compile(r"(ERP|이알피).*(미등록|등록\s*(안|되지|이\s*안|누락)|반영\s*(안|되지)|누락|빠진|불일치|안\s*들어)|"
                          r"(미등록|등록되지\s*않은|등록\s*안\s*된|누락된)\s*(주문|데이터|건)")


def _ent(case) -> dict[str, Any]:
    try:
        return json.loads(case.fields.get("entity") or "{}")
    except json.JSONDecodeError:
        return {}


def _issues(e) -> list[str]:
    raw = e.fields.get("Issues")
    if not raw:
        return []
    try:
        return [i.get("field") or i.get("k") for i in json.loads(raw)]
    except json.JSONDecodeError:
        return []


def _money(qty, price) -> dict[str, int] | None:
    if not qty or not price:
        return None
    amount = int(qty) * int(price)
    vat = round(amount * 0.1)
    return {"amount": amount, "vat": vat, "total": amount + vat}


def erp_order_audit(kb: KnowledgeBase, date_from: str | None = None, date_to: str | None = None,
                    customer: str | None = None) -> dict[str, Any]:
    snap = kb.snapshot_date or "2026-11-25"
    date_from = (date_from or snap[:8] + "01").strip()
    d_to = (date_to or snap).strip()
    d_to_cmp = d_to + " 99:99" if len(d_to) == 10 else d_to
    codes = kb.find_customer_codes(customer) if customer else None

    open_errors = {}
    for err in kb.errors.values():
        if err["status"] == "open":
            open_errors.setdefault(err["caseId"], []).append(err)

    mails = []
    for e in kb.emails.values():
        f = e.fields
        if f.get("Direction") != "in" or f.get("Classification") != "order":
            continue
        if not (date_from <= f.get("Received", "") <= d_to_cmp):
            continue
        if customer and not (e.entities.get("customerCode", {}).get("value") in (codes or []) or customer in f.get("Customer", "")):
            continue
        mails.append(e)
    mails.sort(key=lambda e: e.fields["Received"])

    findings, registered = [], []
    for e in mails:
        f = e.fields
        case = kb.cases.get(f.get("Case ID", ""))
        ent = _ent(case) if case else {}
        req_qty = e.entities.get("quantity", {}).get("value") or ent.get("quantity")
        req_due = e.entities.get("deliveryDate", {}).get("value") or ent.get("deliveryDate")
        item = e.entities.get("itemName", {}).get("value") or ent.get("itemName")
        item_code = e.entities.get("itemCode", {}).get("value") or ent.get("itemCode")
        price = ent.get("unitPrice") or (int(f["Order price"]) if f.get("Order price", "").isdigit() else None)
        if not price and item_code in kb.items and req_qty:
            price = unit_price(kb.items[item_code]["Base price"], int(req_qty))
        flow = e.extras.get("Processing flow / ERP result")
        so = case.fields.get("salesOrderNumber", "") if case else ""
        if not so and isinstance(flow, dict) and isinstance(flow.get("orderResult"), dict):
            so = flow["orderResult"].get("so", "")
        so_row = kb.sales_orders.get(so) if so else None
        base = {"email_id": e.id, "received": f["Received"], "customer": f.get("Customer"), "subject": f.get("Subject"),
                "item": item, "itemCode": item_code, "requested_qty": int(req_qty) if str(req_qty or "").isdigit() else req_qty,
                "requested_delivery": req_due or None, "unit_price": price,
                "expected_amount": _money(req_qty if str(req_qty or "").isdigit() else None, price),
                "case_id": f.get("Case ID") or None, "case_status": case.fields.get("status") if case else None,
                "mail_status": f"{f.get('Status')} / {f.get('Work status')}", "assignee": f.get("Assignee"),
                "source": f"10. EMAIL RECORDS > {e.id} (lines {e.line_start}-{e.line_end})"
                          + (f"; 11. WORK CASES > {case.id} (lines {case.line_start}-{case.line_end})" if case else "")}
        issues = _issues(e)
        errs = open_errors.get(f.get("Case ID"), [])
        intent = f.get("Intent", "")

        problem = None
        if so and not so_row:
            problem = ("SO 번호는 있으나 ERP Sales Order에 없음", f"{so}가 ERP 출하/SO 데이터에 없습니다.",
                       "ERP에서 SO 생성 여부를 확인하고 없으면 재등록")
        elif so_row and (intent == "변경요청" or "quantity" in issues):
            if str(so_row["orderQty"]) != str(req_qty):
                problem = ("ERP 반영 불일치 — 수량 변경 미반영",
                           f"고객 요청 수량 {req_qty}EA ≠ ERP {so}의 주문 수량 {so_row['orderQty']}EA. {f.get('Reason', '')}".strip(),
                           f"담당자 확인 후 ERP {so} 수량 수정({so_row['orderQty']}→{req_qty}EA)과 변경 확인 메일 발송")
        elif not so:
            if errs or f.get("Status") == "오류" or (isinstance(flow, dict) and flow.get("order") == "error"):
                msg = "; ".join(f"{x['id']} {x['message']} (시도 {x['tries']}회, 담당 {x['assignee']})" for x in errs) or \
                      (flow.get("orderError") if isinstance(flow, dict) else "") or f.get("Reason", "")
                problem = ("ERP 미등록 — ERP 전송 실패", msg,
                           f"erp_register_order(case_id='{f.get('Case ID')}')로 재등록 미리보기 → 승인 → 실행"
                           + (f", 성공 시 {errs[0]['id']} 해결 처리" if errs else ""))
            elif f.get("Status") in ("확인 필요", "미분류") or issues:
                missing = [x for x in issues if x not in ("class",)]
                problem = ("ERP 미등록 — 필수 정보 누락·확인 필요",
                           (f"필수값 누락: {', '.join(missing)}. " if missing else "") + f.get("Reason", ""),
                           "고객에게 누락 정보 확인 메일 발송 → 값을 받으면 erp_register_order로 등록")
            else:
                problem = ("ERP 미등록 — 전송 대기", f"주문 메일은 접수됐으나 ERP 주문등록이 아직 실행되지 않았습니다 (상태: {f.get('Work status')}).",
                           f"erp_register_order(case_id='{f.get('Case ID')}')로 미리보기 → 승인 → 등록")
        if problem:
            findings.append({**base, "category": problem[0], "detail": problem[1], "next_action": problem[2],
                             "sales_order": so or None,
                             "erp_sales_order": {k: so_row[k] for k in ("orderQty", "shipDate", "state")} if so_row else None,
                             "open_errors": [x["id"] for x in errs]})
        else:
            reg = {"email_id": e.id, "received": f["Received"], "customer": f.get("Customer"), "item": item,
                   "qty": base["requested_qty"], "sales_order": so,
                   "erp_state": so_row["state"] if so_row else None, "case_id": base["case_id"]}
            fails = [a for a in (case.actions if case else []) if a.get("actionType") == "ERP 주문등록" and a.get("status") == "실패"
                     and a.get("requestedDate", "") >= f["Received"]]
            if fails:
                errs_all = [x["id"] for x in kb.errors.values() if x.get("caseId") == base["case_id"] and x["status"] == "resolved"
                            and x.get("failedAction") == "ERP 주문등록" and x["time"] >= f["Received"]]
                reg["note"] = (f"ERP 주문등록 {len(fails)}회 실패 후 재시도로 등록됨" + (f" ({', '.join(errs_all)} 해결)" if errs_all else ""))
            registered.append(reg)

    by_cat: dict[str, int] = {}
    for x in findings:
        by_cat[x["category"]] = by_cat.get(x["category"], 0) + 1
    pending_total = sum((x["expected_amount"] or {}).get("total", 0) for x in findings if x["category"].startswith("ERP 미등록"))
    return {
        "period": {"from": date_from, "to": d_to}, "customer_filter": customer,
        "summary": {"order_mails_checked": len(mails), "registered_ok": len(registered), "problems": len(findings),
                    "by_category": by_cat, "unregistered_expected_total_krw": pending_total},
        "problems": findings,
        "registered_ok": registered,
        "method": ("기간 내 수신 '주문(order)' 메일을 모두 찾아, 연결된 Case의 Sales Order 번호와 ERP Sales Order 표(섹션 8)를 대조. "
                   "SO가 없으면 미등록(전송대기/전송실패/필수값 누락으로 구분), SO는 있으나 요청 수량과 다르면 반영 불일치로 판단. "
                   "금액은 Case 단가 × 수량, 부가세 10% 포함."),
    }
