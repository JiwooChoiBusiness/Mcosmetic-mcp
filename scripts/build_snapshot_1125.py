"""Build the 2026-11-25 snapshot by continuing the 2026-10-22 knowledge file.

  python scripts/build_snapshot_1125.py

Keeps every 10/22 record (open ones are closed/advanced), adds the events of 10/23-11/24 and a new
11/25 inbox, and recomputes the Sales Order table, item stock, errors, notifications and dashboard.
Output uses exactly the same text format, so the MCP parser works unchanged.

Business rules come from the data (see cosmetic_kb/erp.py): quantity discount 0/5/8/12 %, 10-won
rounding, VAT 10 %. Stock is a ledger: 10/22 stock - shipped Sales Orders + inbound receipts.
"""
from __future__ import annotations

import collections
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cosmetic_kb.erp import discount_rate, unit_price  # noqa: E402
from cosmetic_kb.kb import KnowledgeBase  # noqa: E402

SRC = ROOT / "data" / "ai365-Mcosmetic-knowledge-updated_2026-10-22.txt"
OUT = ROOT / "data" / "ai365-Mcosmetic-knowledge-updated_2026-11-25.txt"
OLD, NEW = date(2026, 10, 22), date(2026, 11, 25)
kb = KnowledgeBase(SRC)


# ============================================================================ small helpers
def D(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def T(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def ts(x: datetime) -> str:
    return x.strftime("%Y-%m-%d %H:%M")


def kd(x: date | str) -> str:
    x = D(x) if isinstance(x, str) else x
    return f"{x.month}월 {x.day}일"


def won(n: int) -> str:
    return f"{n:,}원"


def h(*p) -> int:
    return int(hashlib.md5("|".join(map(str, p)).encode()).hexdigest(), 16)


def ms(*p) -> int:
    return 760 + h("ms", *p) % 520


# ============================================================================ master data
CU: dict[str, dict] = {}
for code, c in kb.customers.items():
    n = c["contact"]["n"]
    first = n if n[0].isascii() else n.split()[0]
    CU[code] = {"code": code, "name": c["name"], "n": n, "m": c["contact"]["m"], "addr": c["contact"]["addr"],
                "first": first, "hon": f"{c['name']} {n}님"}
BY_NAME = {v["name"]: k for k, v in CU.items()}
_ass = collections.defaultdict(collections.Counter)
for e in kb.emails.values():
    if e.fields.get("Assignee") and e.fields.get("Customer") in BY_NAME:
        _ass[BY_NAME[e.fields["Customer"]]][e.fields["Assignee"]] += 1
ASSIGNEE = {k: (_ass[k].most_common(1)[0][0] if _ass[k] else "이수진") for k in CU}

IT = {k: dict(v) for k, v in kb.items.items()}
# Prices used for the 10/23-11/24 transactions: the base prices in effect when the deals were made.
# The item master itself shows the DB standard prices (see the note in section 15); history is not repriced.
TX_BASE = {"MC-CAL-SR050": 10000, "MC-CAL-CR050": 12000, "MC-GLW-SR050": 11000, "MC-BAR-SR050": 11500,
           "MC-BAL-CR050": 12500, "MC-CIC-SR050": 10500, "MC-CAL-MK100": 8000, "MC-GLW-TN150": 7000,
           "MC-HYD-SR050": 10800, "MC-BAR-CR050": 12800, "MC-HYD-CR050": 12200, "MC-GLW-CR050": 12400}
LEAD_DAYS = {k: (D(v["Lead date"]) - OLD).days - 7 for k, v in IT.items()}
BACK_DAYS = {k: (D(v["Back lead date"]) - OLD).days - 7 for k, v in IT.items()}
# one replenishment per item on its 10/22 lead date
INBOUND = {k: [(D(v["Lead date"]), 3000 if v["Stock"] < 1300 else 2000)] for k, v in IT.items()}


def item_by_short(short: str) -> str:
    return next(k for k, v in IT.items() if v["Short name"] == short)


# ============================================================================ Sales Orders + stock ledger
SO: dict[str, dict] = {}
for k, v in kb.sales_orders.items():
    SO[k] = {kk: vv for kk, vv in v.items() if kk != "line"}
    SO[k]["_created"] = D(k[3:7] + "-" + k[7:9] + "-" + k[9:11])
COUNTER = {"so": 118, "msg": 83, "e_arch": 4000, "e_today": 1100, "case": 131, "err": 8, "thread_etc": 3100}


def stock_at(item: str, x: date) -> int:
    s = IT[item]["Stock"]
    for v in SO.values():
        if v["itemCode"] == item and OLD < D(v["shipDate"]) <= x:
            s -= v["orderQty"]
    return s + sum(q for d_in, q in INBOUND[item] if OLD < d_in <= x)


def next_inbound(item: str, x: date) -> tuple[date, int] | None:
    later = sorted((d_in, q) for d_in, q in INBOUND[item] if d_in > x)
    return later[0] if later else None


# Some 10/22 Sales Orders already exceeded stock (e.g. 밸런스 크림 2,000 planned vs 1,500 on hand):
# add a production receipt the day before each shortfall so the ledger never goes negative.
for _item in IT:
    for _dd in range(1, (NEW - OLD).days + 1):
        _x = OLD + timedelta(days=_dd)
        if stock_at(_item, _x) < 0:
            _need = -stock_at(_item, _x)
            INBOUND[_item].append((_x - timedelta(days=1), (_need // 500 + 2) * 500))


def alloc_at(item: str, x: date) -> int:
    return sum(v["orderQty"] for v in SO.values()
               if v["itemCode"] == item and v["_created"] <= x < D(v["shipDate"]))


def avail_at(item: str, x: date) -> int:
    return max(stock_at(item, x) - alloc_at(item, x), 0)


def so_state(v: dict, x: date) -> str:
    sd = D(v["shipDate"])
    if x < sd - timedelta(days=5):
        return "생산 계획"
    if x < sd:
        return "출하 준비"
    if x < sd + timedelta(days=3):
        return "배송 중"
    return "출하 완료"


def tracking(so: str) -> str:
    n = h("trk", so)
    return f"{['CJ', 'HJ', 'LT'][n % 3]}-{n % 9000 + 1000}-{(n // 9000) % 9000 + 1000}-{(n // 81000000) % 9000 + 1000}"


def so_view(so: str, x: date) -> dict:
    v = SO[so]
    st = so_state(v, x)
    shipped = st in ("배송 중", "출하 완료")
    return {"so": so, "customerCode": v["customerCode"], "itemCode": v["itemCode"], "item": v["item"],
            "orderQty": v["orderQty"], "shipQty": v["orderQty"] if shipped else 0, "shipDate": v["shipDate"],
            "state": st, "tracking": (v.get("tracking") or tracking(so)) if shipped else ""}


def lead_for(item: str, qty: int, x: date) -> tuple[date, bool]:
    ok = qty <= avail_at(item, x)
    return x + timedelta(days=LEAD_DAYS[item] if ok else BACK_DAYS[item]), ok


def new_so(code: str, item: str, qty: int, when: datetime, due: str | None) -> tuple[str, str]:
    x = when.date()
    possible, _ = lead_for(item, qty, x)
    delivery = max(D(due), possible + timedelta(days=2)) if due else possible + timedelta(days=2)
    COUNTER["so"] += 1
    so = f"SO-{x:%Y%m%d}-{COUNTER['so']:05d}"
    SO[so] = {"so": so, "customerCode": code, "itemCode": item, "item": IT[item]["Short name"], "orderQty": qty,
              "shipQty": 0, "shipDate": str(delivery - timedelta(days=2)), "state": "", "tracking": "", "_created": x}
    return so, str(delivery)


def next_msg() -> str:
    COUNTER["msg"] += 1
    return f"MSG-{COUNTER['msg']:05d}"


def next_eid(today: bool) -> str:
    key = "e_today" if today else "e_arch"
    COUNTER[key] += 1
    return f"E-{COUNTER[key]:04d}"


# ============================================================================ cases
CASES: dict[str, dict] = {}
def _raw_timeline(c) -> list[str]:
    """Timeline lines exactly as in the source (the parser strips the trailing space after 'error: ')."""
    block = kb.lines[c.line_start - 1:c.line_end]
    i = block.index("Case timeline:") + 1
    out = []
    while i < len(block) and block[i].startswith("- "):
        out.append(block[i][2:])
        i += 1
    return out


for cid, c in kb.cases.items():
    CASES[cid] = {"fields": dict(c.fields), "timeline": _raw_timeline(c), "actions": [dict(a) for a in c.actions],
                  "new": False, "touched": False}


def ent(case: dict) -> dict:
    return json.loads(case["fields"].get("entity") or "{}")


def set_ent(case: dict, **kw) -> None:
    e = ent(case)
    e.update(kw)
    case["fields"]["entity"] = json.dumps(e, ensure_ascii=False)


def new_case(kind: str, code: str, title: str, request: str, when: datetime, entity: dict, quote_ref: str | None = None) -> str:
    COUNTER["case"] += 1
    cid = f"{kind}-2026-{COUNTER['case']:05d}"
    f = {"customer": CU[code]["name"], "status": "", "request": request, "createdDate": ts(when), "done": "No",
         "caseType": {"RFQ": "견적", "ORD": "주문", "INQ": "문의"}[kind], "title": title, "customerCode": code,
         "assignedUser": ASSIGNEE[code], "lastUpdatedDate": ts(when), "salesOrderNumber": ""}
    if quote_ref:
        f["quoteRef"] = quote_ref
    f["entity"] = json.dumps(entity, ensure_ascii=False)
    f["relatedEmails"] = ""
    CASES[cid] = {"fields": f, "timeline": [], "actions": [], "new": True, "touched": True}
    return cid


def set_field(cid: str, key: str, value: str) -> None:
    f = CASES[cid]["fields"]
    if key in f:
        f[key] = value
        return
    items = list(f.items())
    idx = next(i for i, (k, _) in enumerate(items) if k == "entity")
    items.insert(idx, (key, value))
    CASES[cid]["fields"] = dict(items)


def touch(cid: str, when: datetime, status: str | None = None, done: str | None = None) -> None:
    c = CASES[cid]
    c["touched"] = True
    c["fields"]["lastUpdatedDate"] = ts(when)
    if status:
        c["fields"]["status"] = status
    if done:
        c["fields"]["done"] = done


def tl_mail(cid: str, eid: str, direction: str, when: datetime, subject: str) -> None:
    c = CASES[cid]
    c["timeline"].append(f"{ts(when)} | mail ({direction}) | {eid} | {subject}")
    rel = [x for x in c["fields"].get("relatedEmails", "").split(", ") if x]
    c["fields"]["relatedEmails"] = ", ".join(rel + [eid])
    touch(cid, when)


def tl_action(cid: str, when: datetime, kind: str, result: str, ok: bool = True, err: str = "") -> None:
    c = CASES[cid]
    st = "완료" if ok else "실패"
    c["timeline"].append(f"{ts(when)} | action | {kind} | {st} | result: {result} | error: {err}")
    c["actions"].append({"actionType": kind, "status": st, "requestedDate": ts(when),
                         "completedDate": ts(when) if ok else "", "result": result, "errorMessage": err})
    touch(cid, when)


# ============================================================================ emails
EMAIL_BLOCKS: list[tuple[datetime, str, list[str]]] = []  # (received, id, lines)
STEPS = {"quote": ["ERP 단가 조회", "AI 답변 작성", "확인 후 발송"], "order": ["주문 내용 확인", "ERP 주문등록", "주문확인 메일 (선택)"],
         "ship": ["ERP 출하내역 조회", "AI 답변 작성", "확인 후 발송"], "stock": ["ERP 재고 조회", "AI 답변 작성", "확인 후 발송"],
         "invoice": ["ERP Sales Order 조회", "AI 답변 작성", "확인 후 발송"]}


def steps(cls: str, done_n: int) -> str:
    s = STEPS.get(cls, [])
    return "; ".join(f"{x} [{'done' if i < done_n else 'pending'}]" for i, x in enumerate(s))


def entity_lines(code: str | None, item: str | None, qty, due, quote, so, cust_name=None, addr=None, low=False) -> list[str]:
    c = CU.get(code or "", {})
    it = IT.get(item or "", {})
    return [f"- customer: {cust_name or c.get('name', '')} (confidence={0.98 if c else 0.55}, source=ai)",
            f"- customerCode: {code or ''} (confidence={0.99 if code else 0}, source=master)",
            f"- itemName: {it.get('Short name', '')} (confidence={0.97 if item else 0}, source=ai)",
            f"- itemCode: {item or ''} (confidence={0.98 if item else 0}, source=master)",
            f"- quantity: {qty or ''} (confidence={0.97 if qty else 0}, source=ai)",
            f"- deliveryDate: {due or ''} (confidence={0.95 if due else 0}, source=ai)",
            "- currency: KRW (confidence=0.99, source=master)",
            f"- shippingAddress: {c.get('addr', '') if addr is None else addr} (confidence={0.6 if low else 0.93}, source=master)",
            f"- quoteRef: {quote or ''} (confidence=0.96, source=case)",
            f"- salesOrder: {so or ''} (confidence=0.95, source=case)"]


def add_email(*, eid: str, thread: str, direction: str, customer: str, sender: str, recipient: str, assignee: str,
              when: datetime, subject: str, cls: str = "", intent: str = "", conf: str = "%", work_status: str = "",
              case_id: str = "", summary: str = "", interp: str = "", reason: str = "", needs: str = "",
              status: str = "완료", next_step: str = "완료 — 처리 완료", work_steps: str = "", closed: str = "Yes",
              hold: str = "", issues: str | None = None, order_price: int | None = None, body: str = "",
              entities: list[str] | None = None, attachments: list[dict] | None = None, flow: dict | None = None,
              extra: list[tuple[str, object]] | None = None) -> str:
    L = [f"EMAIL {eid}", f"- Thread ID: {thread}", f"- Direction: {direction}", f"- Customer: {customer}",
         f"- Sender: {sender}", f"- Recipient: {recipient}", f"- Assignee: {assignee}", f"- Received: {ts(when)}",
         f"- Subject: {subject}", f"- Classification: {cls}", f"- Intent: {intent}", f"- AI confidence: {conf}",
         f"- Work status: {work_status}", f"- Case ID: {case_id}", f"- Summary: {summary}",
         f"- AI interpretation: {interp}", f"- Reason: {reason}", f"- Needs: {needs}", f"- Status: {status}"]
    if issues:
        L.append(f"- Issues: {issues}")
    L += [f"- Next step: {next_step}", f"- Work steps: {work_steps}", f"- Closed: {closed}", f"- Hold: {hold}"]
    if order_price:
        L.append(f"- Order price: {order_price}")
    L += ["", "Email body:"] + body.strip("\n").split("\n") + ["", "Extracted entities:"] + (entities or [])
    if attachments:
        L += ["", "Attachments:"] + [f"- {json.dumps(a, ensure_ascii=False)}" for a in attachments]
    for label, obj in (extra or []):
        L += ["", f"{label}:"] + json.dumps(obj, ensure_ascii=False, indent=2).split("\n")
    if flow:
        L += ["", "Processing flow / ERP result:"] + json.dumps(flow, ensure_ascii=False, indent=2).split("\n")
    EMAIL_BLOCKS.append((when, eid, L))
    return eid


def mail_part(draft: str, sent_at: datetime | None) -> dict:
    p = {"variant": 0, "draft": draft, "draftState": "ready"}
    if sent_at:
        p.update({"mail": "sent", "sentAt": ts(sent_at), "mailError": "", "mailApi": "POST /api/mail/v1/send",
                  "messageId": next_msg()})
    return p


def re_(subject: str) -> str:
    return subject if subject.startswith("Re:") else f"Re: {subject}"


def sign(assignee: str) -> str:
    return f"감사합니다.\nM코스메틱 {assignee} 드림"


def out_mail(cid: str | None, code: str, thread: str, when: datetime, subject: str, intent: str, summary: str,
             body: str, assignee: str, attachments=None) -> str:
    eid = next_eid(when.date() == NEW)
    add_email(eid=eid, thread=thread, direction="out", customer=CU[code]["name"], sender=f"{assignee} <sales@mcosmetic.co.kr>",
              recipient=CU[code]["m"], assignee=assignee, when=when, subject=subject, intent=intent, case_id=cid or "",
              summary=summary, body=body, attachments=attachments)
    if cid:
        tl_mail(cid, eid, "out", when, subject)
        tl_action(cid, when, "Email 발송", f"{intent} 발송")
    return eid


def in_head(code: str):
    c = CU[code]
    return dict(customer=c["name"], sender=f"{c['n']} <{c['m']}>", recipient="sales@mcosmetic.co.kr", assignee=ASSIGNEE[code])


# ---------------------------------------------------------------- quote
def quote_flow(code, item, qty, x: datetime):
    st = stock_at(item, x.date())
    lead, ok = lead_for(item, qty, x.date())
    base = TX_BASE[item]
    res = {"itemName": IT[item]["name"], "short": IT[item]["Short name"], "itemCode": item, "base": base,
           "price": unit_price(base, qty), "rate": discount_rate(qty), "qty": qty, "stock": st, "inStock": ok, "lead": str(lead)}
    return {"erpMs": ms(code, item, qty, x), "erpStale": False, "erp": "done", "erpResult": res, "erpError": "",
            "erpApi": f"GET /api/erp/v1/prices?customer={code}&item={item}&qty={qty}"}, res


def quote_text(code, item, qty, due, res, assignee, revised=False) -> str:
    head = "변경 요청하신 수량 기준으로 단가를 다시 확인했습니다.\n" if revised else ""
    s = f"안녕하세요, {CU[code]['hon']}.\n\n{head}문의하신 {IT[item]['Short name']} {qty:,}EA 기준 단가는 {won(res['price'])}이며,\n"
    if due and D(due) >= D(res["lead"]):
        s += f"요청하신 {kd(due)}까지 납품 가능합니다.\n"
    elif due:
        s += (f"요청하신 {kd(due)} 납기는 현재 재고({res['stock']:,}EA) 기준으로 어렵습니다.\n"
              f"가장 빠른 납품 가능일은 {kd(res['lead'])}이며, 재고 수량 내 분할 납품도 가능합니다.\n")
    else:
        s += f"현재 기준 납품 가능일은 {kd(res['lead'])}입니다.\n"
    return s + f"\n확인 후 주문 진행 여부 회신 부탁드립니다.\n\n{sign(assignee)}"


def ev_quote(cid: str, code: str, item: str, qty: int, due: str | None, when: datetime, reply: datetime | None,
             subject: str, body: str, intent: str = "신규요청", today: bool = False, existing_eid: str | None = None) -> None:
    c = CASES[cid]
    a = c["fields"]["assignedUser"]
    flow, res = quote_flow(code, item, qty, when)
    revised = intent == "변경요청"
    draft = quote_text(code, item, qty, due, res, a, revised)
    act = "ERP 단가 재조회" if revised else "ERP 단가 조회"
    if existing_eid is None:
        eid = next_eid(today)
        sent = reply if reply else None
        add_email(eid=eid, thread=f"T-{cid[-5:]}", direction="in", **in_head(code), when=when, subject=subject,
                  cls="quote", intent=intent, conf=f"{95 + h(eid) % 5}%",
                  work_status=("수정견적 회신 완료" if revised else "견적 회신 완료") if sent else "조회완료",
                  case_id=cid, summary=f"{IT[item]['Short name']} / {qty}EA / 단가 및 납기 문의",
                  interp=f"{CU[code]['name']}가 {IT[item]['Short name']} {qty:,}EA의 단가" + (f"와 {kd(due)} 납품 가능 여부를 문의했습니다." if due else "를 문의했습니다."),
                  needs="erp, reply", status="완료" if sent else "진행 중",
                  next_step="완료 — 처리 완료" if sent else "메일 작성 — AI 답변 생성",
                  work_steps=steps("quote", 3 if sent else 1), closed="Yes" if sent else "No", hold="No", body=body,
                  entities=entity_lines(code, item, qty, due, cid, ""), flow={**flow, **mail_part(draft, sent)} if sent else flow)
        tl_mail(cid, eid, "in", when, subject)
    tl_action(cid, when + timedelta(minutes=12), act, f"적용단가 {won(res['price'])} / 재고 {res['stock']:,}EA")
    set_ent(c, itemName=IT[item]["Short name"], itemCode=item, quantity=qty, deliveryDate=due or "", unitPrice=res["price"])
    if reply:
        out_mail(cid, code, f"T-{cid[-5:]}", reply, re_(subject), "수정 견적 회신" if revised else "견적 회신",
                 f"{qty:,}EA 기준 {won(res['price'])} / " + (f"{kd(due)} 납기 가능" if due and D(due) >= D(res['lead']) else f"가능일 {kd(res['lead'])}"),
                 draft, a)
        touch(cid, reply, status="견적 회신 완료")
    else:
        touch(cid, when, status="견적 회신 대기")


def ev_hold(cid: str, code: str, when: datetime, subject: str, body: str, close: bool = False) -> None:
    eid = next_eid(False)
    add_email(eid=eid, thread=f"T-{cid[-5:]}", direction="in", **in_head(code), when=when, subject=subject, cls="quote",
              intent="확인", conf=f"{93 + h(eid) % 5}%", work_status="고객 검토 중" if not close else "견적 종료",
              case_id=cid, summary="고객 검토 / 회신 대기" if not close else "고객 보류 / 견적 종료",
              interp=f"{CU[code]['name']}가 견적 내용을 확인했으며 " + ("내부 검토 후 다시 연락하겠다고 했습니다." if not close else "이번 건은 진행하지 않겠다고 알렸습니다."),
              needs="", work_steps="", hold="No", body=body, entities=entity_lines(code, None, None, None, cid, ""))
    tl_mail(cid, eid, "in", when, subject)
    touch(cid, when, status="견적 종료(고객 보류)" if close else "고객 검토 중", done="Yes" if close else "No")


# ---------------------------------------------------------------- order
def ev_order(cid: str, code: str, item: str, qty: int, due: str, price: int, when: datetime, reply: datetime | None,
             subject: str, body: str, attach: str | None = None, today: bool = False, existing_eid: str | None = None,
             fail_first: bool = False, addr: str | None = None) -> str:
    c = CASES[cid]
    a = c["fields"]["assignedUser"]
    reg = when + timedelta(minutes=(40 if fail_first else 8))
    so, delivery = new_so(code, item, qty, reg, due)
    res = {"so": so, "customer": CU[code]["name"], "item": IT[item]["Short name"], "qty": qty, "date": delivery, "price": price}
    draft = (f"안녕하세요, {CU[code]['hon']}.\n\n주문해 주셔서 감사합니다.\n{IT[item]['Short name']} {qty:,}EA 주문이 정상 접수되었으며, "
             f"{kd(delivery)}까지 납품 예정입니다.\n\nSales Order 번호는 {so}입니다.\n출하 일정이 확정되면 다시 안내드리겠습니다.\n\n{sign(a)}")
    if existing_eid is None:
        eid = next_eid(today)
        flow = {"order": "done", "orderError": "", "orderApi": "POST /api/erp/v1/sales-orders", "orderResult": res}
        add_email(eid=eid, thread=f"T-{cid[-5:]}", direction="in", **in_head(code), when=when, subject=subject, cls="order",
                  intent="주문전환", conf=f"{96 + h(eid) % 4}%", work_status="주문등록 완료", case_id=cid,
                  summary=f"{IT[item]['Short name']} / {qty}EA / {kd(due)} 납기 주문 확정",
                  interp=f"{CU[code]['name']}가 {IT[item]['Short name']} {qty:,}EA를 {kd(due)} 납기로 주문 확정했습니다.",
                  needs="order, reply", work_steps=steps("order", 3), hold="No", order_price=price, body=body,
                  entities=entity_lines(code, item, qty, due, c["fields"].get("quoteRef") or (cid if cid.startswith("RFQ") else ""), "", addr=addr),
                  attachments=[{"name": attach, "size": f"{180 + h(attach) % 60}KB", "type": "po"}] if attach else None,
                  flow={**flow, **mail_part(draft, reply)})
        tl_mail(cid, eid, "in", when, subject)
    if fail_first:
        tl_action(cid, when + timedelta(minutes=2), "ERP 주문등록", "", ok=False, err="ERP API Timeout (응답 대기 30초 초과)")
    tl_action(cid, reg, "ERP 주문등록", f"Sales Order {so} 생성")
    c["fields"]["salesOrderNumber"] = so
    if c["fields"]["caseType"] == "견적":
        c["fields"]["caseType"] = "견적 → 주문"
    set_ent(c, itemName=IT[item]["Short name"], itemCode=item, quantity=qty, deliveryDate=due, unitPrice=price,
            **({"shippingAddress": addr} if addr else {}))
    touch(cid, reg, status="주문등록 완료", done="Yes")
    if reply:
        out_mail(cid, code, f"T-{cid[-5:]}", reply, re_(subject), "주문 확인", f"Sales Order {so} 안내", draft, a)
    return so


# ---------------------------------------------------------------- shipping
def ship_text(code, so, v, a) -> str:
    it = f"{v['item']} {v['orderQty']:,}EA"
    if v["state"] in ("생산 계획", "출하 준비"):
        mid = f"현재 {v['state']} 중이며 {kd(v['shipDate'])} 출하 예정입니다."
    elif v["state"] == "배송 중":
        mid = f"{kd(v['shipDate'])} 출고되어 현재 배송 중입니다.\n송장번호는 {v['tracking']}입니다."
    else:
        mid = f"{kd(v['shipDate'])} 출하 완료되었습니다. (송장번호 {v['tracking']})"
    return f"안녕하세요, {CU[code]['hon']}.\n\n문의하신 {it} 주문건을 확인한 결과\n{mid}\n\nSales Order 번호는 {so}입니다.\n\n{sign(a)}"


def ev_ship(cid: str, code: str, so: str, when: datetime, reply: datetime | None, subject: str, body: str,
            today: bool = False, existing_eid: str | None = None) -> None:
    c = CASES[cid]
    a = c["fields"]["assignedUser"]
    look = when + timedelta(minutes=25)
    v = so_view(so, look.date())
    flow = {"erpMs": ms(so, when), "erpStale": False, "erp": "done", "erpResult": {"matchedBy": 1, **v}, "erpError": "",
            "erpApi": f"GET /api/erp/v1/shipments?so={so}"}
    draft = ship_text(code, so, v, a)
    e = ent(c)
    if existing_eid is None:
        eid = next_eid(today)
        add_email(eid=eid, thread=f"T-{cid[-5:]}", direction="in", **in_head(code), when=when, subject=subject, cls="ship",
                  intent="일정문의", conf=f"{95 + h(eid) % 5}%", work_status="출하 안내 완료" if reply else "조회완료", case_id=cid,
                  summary=f"{v['item']} / {v['orderQty']}EA 출하 일정 문의",
                  interp=f"{CU[code]['name']}가 주문({v['item']} {v['orderQty']:,}EA, {so})의 출하 일정을 문의했습니다.",
                  needs="erp, reply", status="완료" if reply else "진행 중",
                  next_step="완료 — 처리 완료" if reply else "메일 작성 — AI 답변 생성",
                  work_steps=steps("ship", 3 if reply else 1), closed="Yes" if reply else "No", hold="No", body=body,
                  entities=entity_lines(code, v["itemCode"], v["orderQty"], e.get("deliveryDate"), c["fields"].get("quoteRef", ""), so),
                  flow={**flow, **mail_part(draft, reply)} if reply else flow)
        tl_mail(cid, eid, "in", when, subject)
    res = f"{v['state']} / " + (f"출하예정 {v['shipDate']}" if not v["tracking"] else f"Tracking {v['tracking']}")
    tl_action(cid, look, "ERP 출하내역 조회", res)
    if reply:
        out_mail(cid, code, f"T-{cid[-5:]}", reply, re_(subject), "출하 안내",
                 f"{v['state']} / {kd(v['shipDate'])}" + (" 출하 예정" if v["state"] in ("생산 계획", "출하 준비") else " 출하"), draft, a)
        touch(cid, reply, status="출하 안내 완료", done="Yes")
    else:
        touch(cid, look, status="출하 문의 접수", done="No")


# ---------------------------------------------------------------- invoice
def invoice_res(so: str, x: date, price: int, due: str, addr: str) -> dict:
    v = SO[so]
    amount = v["orderQty"] * price
    vat = round(amount * 0.1)
    return {"so": so, "invoiceNo": "INV-" + so[3:], "customer": CU[v["customerCode"]]["name"], "customerCode": v["customerCode"],
            "itemName": IT[v["itemCode"]]["name"], "short": v["item"], "itemCode": v["itemCode"], "qty": v["orderQty"],
            "price": price, "amount": amount, "vat": vat, "total": amount + vat, "orderDate": str(v["_created"]),
            "deliveryDate": due, "state": so_view(so, x)["state"], "billTo": addr}


def ev_invoice(cid: str, code: str, so: str, when: datetime, reply: datetime | None, subject: str, body: str,
               today: bool = False, existing_eid: str | None = None, send_fail: bool = False) -> None:
    c = CASES[cid]
    a = c["fields"]["assignedUser"]
    e = ent(c)
    look = when + timedelta(minutes=30)
    res = invoice_res(so, look.date(), int(e["unitPrice"]), e.get("deliveryDate") or SO[so]["shipDate"], e.get("shippingAddress", ""))
    draft = (f"안녕하세요, {CU[code]['hon']}.\n\n요청하신 {so} 건의 인보이스({res['invoiceNo']})를 첨부하여 보내드립니다.\n\n"
             f"{res['short']} {res['qty']:,}EA, 합계 {won(res['total'])} (VAT 포함)입니다.\n내용 확인 부탁드리며, 수정이 필요하면 회신 주세요.\n\n{sign(a)}")
    flow = {"erpMs": ms(so, "inv"), "erpStale": False, "erp": "done", "erpResult": res, "erpError": "",
            "erpApi": f"GET /api/erp/v1/sales-orders/{so}"}
    if send_fail:
        flow.update({"variant": 0, "draft": draft, "draftState": "ready", "mail": "error", "sentAt": "",
                     "mailError": "SMTP 451 Temporary failure: 수신 서버 응답 지연", "mailApi": "POST /api/mail/v1/send", "messageId": ""})
    if existing_eid is None:
        eid = next_eid(today)
        add_email(eid=eid, thread=f"T-{cid[-5:]}", direction="in", **in_head(code), when=when, subject=subject, cls="invoice",
                  intent="발행요청", conf=f"{95 + h(eid) % 5}%",
                  work_status="인보이스 발송 완료" if reply else ("발송 실패" if send_fail else "주문정보 조회완료"), case_id=cid,
                  summary=f"{so} 인보이스 발행 요청",
                  interp=f"{CU[code]['name']}가 {so}({res['short']} {res['qty']:,}EA) 인보이스 발행을 요청했습니다.",
                  needs="erp, reply", status="완료" if reply else ("오류" if send_fail else "진행 중"),
                  next_step="완료 — 처리 완료" if reply else ("재시도 — 메일 재발송" if send_fail else "메일 작성 — AI 답변 생성"),
                  work_steps=steps("invoice", 3 if reply else 2), closed="Yes" if reply else "No", hold="No", body=body,
                  entities=entity_lines(code, res["itemCode"], res["qty"], res["deliveryDate"], c["fields"].get("quoteRef", ""), so),
                  flow={**flow, **mail_part(draft, reply)} if reply else flow)
        tl_mail(cid, eid, "in", when, subject)
    tl_action(cid, look, "ERP Sales Order 조회", f"{so} / 합계 {won(res['total'])}")
    if send_fail:
        tl_action(cid, look + timedelta(minutes=20), "Email 발송", "", ok=False, err="SMTP 451 Temporary failure: 수신 서버 응답 지연")
        touch(cid, look + timedelta(minutes=20), status="인보이스 발송 실패", done="No")
    if reply:
        out_mail(cid, code, f"T-{cid[-5:]}", reply, re_(subject), "인보이스 발송", f"{res['invoiceNo']} 발송 / 합계 {won(res['total'])}",
                 draft, a, attachments=[{"name": f"{res['invoiceNo']}.pdf"}])
        touch(cid, reply, status="인보이스 발송 완료", done="Yes")
    elif not send_fail:
        touch(cid, look, status="인보이스 요청 접수", done="No")


# ---------------------------------------------------------------- stock
def ev_stock(cid: str, code: str, item: str, qty: int, when: datetime, reply: datetime | None, subject: str, body: str,
             today: bool = False, existing_eid: str | None = None) -> None:
    c = CASES[cid]
    a = c["fields"]["assignedUser"]
    look = when + timedelta(minutes=20)
    x = look.date()
    on, al = stock_at(item, x), alloc_at(item, x)
    av = max(on - al, 0)
    nxt = next_inbound(item, x)
    nxt_d, nxt_q = nxt if nxt else (x + timedelta(days=BACK_DAYS[item]), 2000)
    res = {"itemName": IT[item]["name"], "short": IT[item]["Short name"], "itemCode": item, "onHand": on, "allocated": al,
           "available": av, "reqQty": qty, "enough": qty <= av, "inboundDate": str(nxt_d), "inboundQty": nxt_q,
           "warehouse": "화성 제1물류센터", "asOf": ts(look)}
    if qty <= av:
        mid = f"문의하신 {IT[item]['Short name']}의 현재 가용 재고는 {av:,}EA로,\n요청하신 {qty:,}EA 즉시 공급 가능합니다."
    else:
        mid = (f"문의하신 {IT[item]['Short name']}의 현재 가용 재고는 {av:,}EA로,\n요청하신 {qty:,}EA 중 {av:,}EA는 즉시 공급 가능하며,\n"
               f"나머지는 {kd(nxt_d)} 입고({nxt_q:,}EA) 이후 공급 가능합니다.")
    draft = f"안녕하세요, {CU[code]['hon']}.\n\n{mid}\n\n주문을 원하시면 수량과 희망 납기를 회신 부탁드립니다.\n\n{sign(a)}"
    flow = {"erpMs": ms(item, when), "erpStale": False, "erp": "done", "erpResult": res, "erpError": "",
            "erpApi": f"GET /api/erp/v1/inventory/on-hand?item={item}"}
    if existing_eid is None:
        eid = next_eid(today)
        add_email(eid=eid, thread=f"T-{cid[-5:]}", direction="in", **in_head(code), when=when, subject=subject, cls="stock",
                  intent="재고확인", conf=f"{96 + h(eid) % 4}%", work_status="재고 안내 완료" if reply else "재고조회 완료",
                  case_id=cid, summary=f"{IT[item]['Short name']} / {qty}EA 재고 문의",
                  interp=f"{CU[code]['name']}가 {IT[item]['Short name']} {qty:,}EA 재고를 문의했습니다.", needs="erp, reply",
                  status="완료" if reply else "진행 중", next_step="완료 — 처리 완료" if reply else "메일 작성 — AI 답변 생성",
                  work_steps=steps("stock", 3 if reply else 1), closed="Yes" if reply else "No", hold="No", body=body,
                  entities=entity_lines(code, item, qty, None, "", ""), flow={**flow, **mail_part(draft, reply)} if reply else flow)
        tl_mail(cid, eid, "in", when, subject)
    tl_action(cid, look, "ERP 재고 조회", f"가용 {av:,}EA / 요청 {qty:,}EA")
    if reply:
        out_mail(cid, code, f"T-{cid[-5:]}", reply, re_(subject), "재고 안내", f"가용 재고 {av:,}EA 안내", draft, a)
        touch(cid, reply, status="재고 안내 완료", done="Yes")
    else:
        touch(cid, look, status="재고조회 완료", done="No")


def ev_etc(when: datetime, sender: str, subject: str, body: str, intent: str, today: bool = False, done: bool = True) -> None:
    COUNTER["thread_etc"] += 1
    eid = next_eid(today)
    add_email(eid=eid, thread=f"T-{COUNTER['thread_etc']}", direction="in", customer="(외부 발신)", sender=sender,
              recipient="sales@mcosmetic.co.kr", assignee="", when=when, subject=subject, cls="etc", intent=intent,
              conf=f"{95 + h(eid) % 5}%", work_status="기타 분류", summary="업무 무관 메일" if intent != "뉴스레터" else "업계 뉴스레터",
              interp="업무 요청이 아닌 외부 발신 메일입니다.", needs="", status="완료" if done else "처리 대기",
              next_step="완료 — 처리 완료" if done else "확인 — 보관 또는 삭제", closed="Yes" if done else "No", hold="No",
              body=body, entities=[])


# ============================================================================ existing email patches
EMAIL_PATCH: dict[str, dict[str, str]] = {}


def close_email(eid: str, work_status: str, cls: str | None = None, intent: str | None = None, case_id: str | None = None,
                cls_steps: str | None = None) -> None:
    f = kb.emails[eid].fields
    c = cls or f["Classification"]
    p = {"Status": "완료", "Work status": work_status, "Next step": "완료 — 처리 완료", "Closed": "Yes"}
    if c in STEPS:
        p["Work steps"] = steps(cls_steps or c, 3)
    if cls is not None:
        p["Classification"] = cls
    if intent is not None:
        p["Intent"] = intent
    if case_id is not None:
        p["Case ID"] = case_id
    EMAIL_PATCH[eid] = p


def dt_(s: str) -> datetime:
    return T(s)


# ============================================================================ the story: 2026-10-23 ~ 2026-11-24
E = kb.emails
TODAY_FLAG = False

# --- 10/23: work through the 10/22 inbox -----------------------------------------------------
c = "RFQ-2026-00123"; ev_quote(c, "CUST-00002", "MC-CAL-SR050", 300, "2026-11-16", dt_("2026-10-23 09:20"), dt_("2026-10-23 09:40"), "카밍 세럼 견적 문의드립니다", "", existing_eid="E-1001")
close_email("E-1001", "견적 회신 완료")
c = "RFQ-2026-00120"; ev_quote(c, "CUST-00011", "MC-HYD-SR050", 1000, "2026-11-16", dt_("2026-10-23 09:45"), dt_("2026-10-23 10:05"), "Re: 하이드라 세럼 견적 문의 · 수량 변경", "", intent="변경요청", existing_eid="E-1003")
close_email("E-1003", "수정견적 회신 완료")
close_email("E-1002", "고객 검토 중")

# ORD-00126: shipping address missing -> ask, customer answers, register
c = "ORD-2026-00126"
out_mail(c, "CUST-00032", "T-00126", dt_("2026-10-23 10:20"), "Re: 베리어 크림 300개 발주 드립니다", "배송지 확인 요청", "신규 창고 배송지 확인 요청",
         f"안녕하세요, {CU['CUST-00032']['hon']}.\n\n베리어 크림 50g 300EA 발주 감사합니다.\n말씀하신 신규 창고의 정확한 배송지 주소를 알려주시면 바로 주문 등록하겠습니다.\n\n{sign('이수진')}", "이수진")
touch(c, dt_("2026-10-23 10:20"), status="배송지 확인 요청")
close_email("E-1004", "주문정보 확인 요청")
ADDR_32 = "인천광역시 서구 북항로 120 누리글로벌 신규물류센터"
ev_order(c, "CUST-00032", "MC-BAR-CR050", 300, "2026-11-26", 12160, dt_("2026-10-24 09:12"), dt_("2026-10-24 10:05"),
         "Re: 베리어 크림 300개 발주 드립니다 - 배송지 회신",
         f"안녕하세요.\n\n신규 창고 주소는 {ADDR_32}입니다.\n이 주소로 베리어 크림 50g 300개 납품 부탁드립니다.\n\n감사합니다.\n김도윤 드림", addr=ADDR_32)

# ORD-00124: ready -> register
c = "ORD-2026-00124"
so_124 = ev_order(c, "CUST-00033", "MC-GLW-SR050", 400, "2026-11-23", 10450, dt_("2026-10-23 09:20"), dt_("2026-10-23 09:35"), "주문 요청", "", existing_eid="E-1005")
close_email("E-1005", "주문등록 완료")

# ORD-00112: shipping question + invoice request (after shipment)
c = "ORD-2026-00112"
ev_ship(c, "CUST-00004", "SO-20261017-00102", dt_("2026-10-23 09:20"), dt_("2026-10-23 09:50"), "카밍 크림 800개 출하 일정 문의", "", existing_eid="E-1006")
close_email("E-1006", "출하 안내 완료")
ev_invoice(c, "CUST-00004", "SO-20261017-00102", dt_("2026-10-27 13:40"), dt_("2026-10-27 15:10"), "SO-20261017-00102 인보이스 요청", "", existing_eid="E-1022")
close_email("E-1022", "인보이스 발송 완료")

# ORD-00121: shipping answer + invoice with the AI-suggested SO
c = "ORD-2026-00121"
ev_ship(c, "CUST-00012", "SO-20261018-00109", dt_("2026-10-23 08:50"), dt_("2026-10-23 09:20"), "출하문의", "", existing_eid="E-1007")
close_email("E-1007", "출하 안내 완료")
ev_invoice(c, "CUST-00012", "SO-20261018-00109", dt_("2026-10-23 10:25"), dt_("2026-10-23 11:00"), "인보이스 발행 부탁드립니다", "", existing_eid="E-1023")
close_email("E-1023", "인보이스 발송 완료")

# ORD-00122: ERP retry succeeds, ERR-0008 resolved
c = "ORD-2026-00122"
ev_order(c, "CUST-00022", "MC-CAL-MK100", 500, "2026-11-17", 7360, dt_("2026-10-23 09:00"), dt_("2026-10-23 09:30"), "카밍 워시오프 마스크 500EA 발주", "", existing_eid="E-1008")
close_email("E-1008", "주문등록 완료")
RESOLVE = {"ERR-0008": "2026-10-23 09:08", "ERR-0006": "2026-10-23 09:45"}

# ORD-00108: resend the failed shipping notice
c = "ORD-2026-00108"
v108 = so_view("SO-20261016-00097", date(2026, 10, 23))
out_mail(c, "CUST-00011", "T-00108", dt_("2026-10-23 09:45"), "Re: 베리어 세럼 출하 일정 확인 요청", "출하 안내", "출하 완료 / 10월 23일 출하 (재발송)",
         ship_text("CUST-00011", "SO-20261016-00097", v108, "최지원"), "최지원")
touch(c, dt_("2026-10-23 09:45"), status="출하 안내 완료", done="Yes")
close_email("E-1012", "출하 안내 완료")

# RFQ-00127: item not decided -> ask, answer, quote, later order
c = "RFQ-2026-00127"
out_mail(c, "CUST-00013", "T-00127", dt_("2026-10-23 10:40"), "Re: 글로우 제품 견적 문의", "품목 확인 요청", "글로우 세럼/글로우 크림 중 품목 확인 요청",
         f"안녕하세요, {CU['CUST-00013']['hon']}.\n\n문의하신 '글로우 50g' 제품이 글로우 세럼 50g과 글로우 크림 50g 중 어느 품목인지 확인 부탁드립니다.\n확인되는 대로 600EA 기준 견적을 보내드리겠습니다.\n\n{sign('최지원')}", "최지원")
close_email("E-1009", "품목 확인 요청")
ev_quote(c, "CUST-00013", "MC-GLW-SR050", 600, "2026-12-07", dt_("2026-10-24 09:30"), dt_("2026-10-24 11:10"), "Re: 글로우 제품 견적 문의",
         "안녕하세요.\n\n글로우 세럼 50g입니다. 지난번 주문했던 제품과 같습니다.\n600개 기준 견적 부탁드립니다.\n\n서예진 드림", intent="확인")

# RFQ-00129: unknown 'Nuri' sender -> identified as 누리코스메틱
c = "RFQ-2026-00129"
CASES[c]["fields"]["status"] = "고객 확인 요청"
out_mail(c, "CUST-00012", "T-00129", dt_("2026-10-23 11:05"), "Re: 시카 세럼 견적 요청", "고객 확인 요청", "발신 고객사 확인 요청",
         "안녕하세요, 배성민님.\n\n시카 세럼 50g 600EA 견적 요청 감사합니다.\n정확한 견적을 위해 소속 회사명(누리뷰티/누리글로벌/누리코스메틱/누리리테일 중)을 알려주시겠어요?\n\n" + sign("정하늘"), "정하늘")
close_email("E-1011", "고객 확인 요청")
for k, val in {"customer": "누리코스메틱 12", "customerCode": "CUST-00012"}.items():
    CASES[c]["fields"][k] = val
set_ent(CASES[c], shippingAddress=CU["CUST-00012"]["addr"])  # customer confirmed -> master address
eid_129 = next_eid(False)
add_email(eid=eid_129, thread="T-00129", direction="in", customer="누리코스메틱 12", sender="배성민 <nuri.purchase@gmail.com>",
          recipient="sales@mcosmetic.co.kr", assignee="정하늘", when=dt_("2026-10-24 08:55"), subject="Re: 시카 세럼 견적 요청",
          cls="quote", intent="확인", conf="97%", work_status="견적 회신 완료", case_id=c, summary="발신 고객사 확인: 누리코스메틱 12",
          interp="배성민 님이 누리코스메틱 구매팀 소속이라고 회신했습니다. 고객을 누리코스메틱 12로 확정했습니다.", needs="erp, reply",
          work_steps=steps("quote", 3), hold="No",
          body="안녕하세요.\n\n누리코스메틱 구매팀 배성민입니다. 개인 메일로 보내 혼선을 드렸네요.\n시카 세럼 50g 600개, 11월 21일 납기로 견적 부탁드립니다.\n\n배성민 드림",
          entities=entity_lines("CUST-00012", "MC-CIC-SR050", 600, "2026-11-21", c, ""))
tl_mail(c, eid_129, "in", dt_("2026-10-24 08:55"), "Re: 시카 세럼 견적 요청")
ev_quote(c, "CUST-00012", "MC-CIC-SR050", 600, "2026-11-21", dt_("2026-10-24 09:05"), dt_("2026-10-24 10:30"), "Re: 시카 세럼 견적 요청", "", existing_eid="(inline)")
ev_hold(c, "CUST-00012", dt_("2026-10-27 14:20"), "Re: 시카 세럼 견적 요청", "견적 잘 받았습니다.\n내부 품의 후 주문 여부 다시 연락드리겠습니다.\n\n배성민 드림")

# stock questions of 10/22
ev_stock("INQ-2026-00041", "CUST-00023", "MC-CAL-SR050", 100, dt_("2026-10-23 09:45"), dt_("2026-10-23 10:10"), "카밍 세럼 재고 문의", "", existing_eid="E-1020")
close_email("E-1020", "재고 안내 완료")
ev_stock("INQ-2026-00040", "CUST-00024", "MC-CIC-SR050", 600, dt_("2026-10-23 10:05"), dt_("2026-10-23 10:30"), "시카 세럼 즉시 출고 가능 수량 문의", "", existing_eid="E-1021")
close_email("E-1021", "재고 안내 완료")

# E-1010: quantity-change question -> linked to RFQ-2026-00131, revised quote
c = "RFQ-2026-00131"
e1010 = E["E-1010"].entities
q1010 = int(e1010.get("quantity", {}).get("value") or 1000)
tl_mail(c, "E-1010", "in", dt_("2026-10-22 11:18"), "글로우 토너 수량 변경 시 단가 문의")
ev_quote(c, "CUST-00034", "MC-GLW-TN150", q1010, ent(CASES[c]).get("deliveryDate") or None, dt_("2026-10-23 11:10"), dt_("2026-10-23 11:30"),
         "글로우 토너 수량 변경 시 단가 문의", "", intent="변경요청", existing_eid="E-1010")
close_email("E-1010", "수정견적 회신 완료", case_id=c)
close_email("E-1013", "기타 분류")
close_email("E-1014", "기타 분류")
close_email("E-1016", "자료 발송 완료", cls="etc", intent="자료요청")
out_mail(None, "CUST-00004", "T-3042", dt_("2026-10-23 13:20"), "Re: 지난번 말씀드린 건 관련", "자료 발송", "제품 카탈로그·단가표 발송",
         f"안녕하세요, {CU['CUST-00004']['hon']}.\n\n통화 때 말씀하신 자료로 이해하고 2026년 하반기 제품 카탈로그와 기본 단가표를 첨부해 드립니다.\n다른 자료가 필요하시면 말씀해 주세요.\n\n{sign('김영업')}",
         "김영업", attachments=[{"name": "M코스메틱_2026하반기_카탈로그.pdf"}, {"name": "M코스메틱_기본단가표_202610.xlsx"}])

# --- customers who were reviewing quotes -----------------------------------------------------
ev_order("RFQ-2026-00075", "CUST-00012", "MC-GLW-TN150", 1200, "2026-11-03", 6160, dt_("2026-10-24 13:15"), dt_("2026-10-24 14:20"),
         "Re: 글로우 토너 견적 요청드립니다 - 발주", "안녕하세요.\n\n검토 마쳤습니다. 글로우 토너 150g 1,200개 11월 3일 납품으로 발주합니다.\n\n윤서아 드림",
         attach="발주서_누리코스메틱_글로우토너1200.pdf")
ev_order("RFQ-2026-00087", "CUST-00034", "MC-GLW-TN150", 600, "2026-11-05", 6440, dt_("2026-10-27 10:05"), dt_("2026-10-27 11:00"),
         "Re: 글로우 토너 견적 요청드립니다 - 주문", "안녕하세요.\n\n글로우 토너 150g 600개 주문 진행하겠습니다. 납기는 11월 5일로 부탁드립니다.\n\n문가영 드림")
ev_order("RFQ-2026-00123", "CUST-00002", "MC-CAL-SR050", 300, "2026-11-16", 9500, dt_("2026-10-27 09:40"), dt_("2026-10-27 10:30"),
         "Re: 카밍 세럼 견적 문의드립니다 - 주문", "안녕하세요.\n\n견적 감사합니다. 카밍 세럼 50g 300개 주문하겠습니다.\n11월 16일 납품으로 부탁드립니다.\n\n박지현 드림")
ev_order("RFQ-2026-00115", "CUST-00023", "MC-BAL-CR050", 300, "2026-11-21", 11880, dt_("2026-10-28 15:30"), dt_("2026-10-28 16:20"),
         "Re: 밸런스 크림 견적 요청드립니다 - 주문 확정", "안녕하세요.\n\n내부 검토 완료되어 밸런스 크림 50g 300개 주문합니다.\n11월 21일 납품 부탁드립니다.\n\n이민정 드림",
         attach="PO_온유리테일_밸런스크림300.pdf")
ev_order("RFQ-2026-00120", "CUST-00011", "MC-HYD-SR050", 1000, "2026-11-16", 9500, dt_("2026-10-30 10:10"), dt_("2026-10-30 11:05"),
         "Re: 하이드라 세럼 견적 문의 - 1,000개 주문", "안녕하세요.\n\n수정 견적 확인했습니다. 하이드라 세럼 50g 1,000개 11월 16일 납기로 주문합니다.\n\n최현우 드림",
         attach="발주서_라온코스메틱_하이드라세럼1000.pdf")
ev_order("RFQ-2026-00127", "CUST-00013", "MC-GLW-SR050", 600, "2026-12-07", unit_price(11000, 600), dt_("2026-11-03 09:50"), dt_("2026-11-03 10:40"),
         "Re: 글로우 제품 견적 문의 - 주문", "안녕하세요.\n\n글로우 세럼 50g 600개, 12월 7일 납품으로 주문합니다.\n\n서예진 드림")
ev_hold("RFQ-2026-00093", "CUST-00032", dt_("2026-11-04 16:10"), "Re: 하이드라 크림 견적 요청드립니다",
        "안녕하세요.\n\n하이드라 크림 건은 이번 분기 예산 조정으로 진행하지 않기로 했습니다.\n다음 분기에 다시 문의드리겠습니다.\n\n김도윤 드림", close=True)
out_mail("RFQ-2026-00081", "CUST-00013", "T-00081", dt_("2026-11-10 10:00"), "Re: 카밍 세럼 300EA 견적 요청 - 견적 유효기간 안내", "견적 후속 안내",
         "견적 유효기간(11/30) 안내",
         f"안녕하세요, {CU['CUST-00013']['hon']}.\n\n10월에 드린 카밍 세럼 50g 300EA 견적(9,500원)은 11월 30일까지 유효합니다.\n진행 여부 회신 주시면 바로 준비하겠습니다.\n\n{sign('최지원')}", "최지원")
touch("RFQ-2026-00081", dt_("2026-11-10 10:00"), status="고객 검토 중")

# follow-ups on the orders above


def so_of(cid: str) -> str:
    return CASES[cid]["fields"]["salesOrderNumber"]


ev_ship("RFQ-2026-00115", "CUST-00023", so_of("RFQ-2026-00115"), dt_("2026-11-14 10:20"), dt_("2026-11-14 11:00"),
        "밸런스 크림 출하 일정 확인 부탁드립니다", "안녕하세요.\n\n지난번 주문한 밸런스 크림 50g 300개 출하 일정 확인 부탁드립니다.\n\n이민정 드림")
ev_invoice("RFQ-2026-00075", "CUST-00012", so_of("RFQ-2026-00075"), dt_("2026-11-12 14:10"), dt_("2026-11-12 15:30"),
           f"{so_of('RFQ-2026-00075')} 인보이스 요청", f"안녕하세요.\n\n{so_of('RFQ-2026-00075')} 건 인보이스 발행 부탁드립니다.\n\n윤서아 드림")
ev_invoice("RFQ-2026-00123", "CUST-00002", so_of("RFQ-2026-00123"), dt_("2026-11-17 10:30"), dt_("2026-11-17 11:45"),
           "카밍 세럼 인보이스 요청드립니다", f"안녕하세요.\n\n{so_of('RFQ-2026-00123')} 카밍 세럼 300개 건 인보이스 보내주세요.\n\n박지현 드림")
ev_invoice("RFQ-2026-00120", "CUST-00011", so_of("RFQ-2026-00120"), dt_("2026-11-20 09:40"), dt_("2026-11-20 10:50"),
           f"{so_of('RFQ-2026-00120')} 세금계산서/인보이스 요청", f"담당자님,\n\n하이드라 세럼 1,000개({so_of('RFQ-2026-00120')}) 인보이스 발행 부탁드립니다.\n\n최현우 드림")

# --- new deals --------------------------------------------------------------------------------
n1 = new_case("RFQ", "CUST-00014", "베리어 세럼 50g 건", "500EA 기준 단가 및 11/14 납기 확인", dt_("2026-10-28 09:20"),
              {"itemName": "베리어 세럼 50g", "itemCode": "MC-BAR-SR050", "quantity": 500, "deliveryDate": "2026-11-14", "unitPrice": None,
               "shippingAddress": CU["CUST-00014"]["addr"]})
ev_quote(n1, "CUST-00014", "MC-BAR-SR050", 500, "2026-11-14", dt_("2026-10-28 09:20"), dt_("2026-10-28 10:45"), "베리어 세럼 500EA 견적 요청",
         "안녕하세요.\n\n베리어 세럼 50g 500개 견적 요청드립니다.\n11월 14일까지 납품 가능한지도 알려주세요.\n\n정우성 드림")
ev_order(n1, "CUST-00014", "MC-BAR-SR050", 500, "2026-11-14", unit_price(11500, 500), dt_("2026-11-02 09:15"), dt_("2026-11-02 10:00"),
         "Re: 베리어 세럼 500EA 견적 요청 - 발주", "안녕하세요.\n\n베리어 세럼 50g 500개 발주합니다. 11월 14일 납품 부탁드립니다.\n\n정우성 드림",
         attach="발주서_다온코스메틱_베리어세럼500.pdf")
ev_ship(n1, "CUST-00014", so_of(n1), dt_("2026-11-10 15:20"), dt_("2026-11-10 16:05"), f"{so_of(n1)} 출하 일정 확인 요청",
        f"담당자님,\n\n베리어 세럼 주문건({so_of(n1)}) 출하 일정 확인 부탁드립니다.\n\n정우성")
ev_invoice(n1, "CUST-00014", so_of(n1), dt_("2026-11-18 13:30"), dt_("2026-11-18 14:40"), "인보이스 발행 부탁드립니다",
           f"안녕하세요.\n\n{so_of(n1)} 베리어 세럼 500개 건 인보이스 발행 부탁드립니다.\n\n정우성 드림")

n2 = new_case("RFQ", "CUST-00011", "글로우 크림 50g 건", "200EA 단가 확인", dt_("2026-11-03 11:00"),
              {"itemName": "글로우 크림 50g", "itemCode": "MC-GLW-CR050", "quantity": 200, "deliveryDate": "2026-11-20", "unitPrice": None,
               "shippingAddress": CU["CUST-00011"]["addr"]})
ev_quote(n2, "CUST-00011", "MC-GLW-CR050", 200, "2026-11-20", dt_("2026-11-03 11:00"), dt_("2026-11-03 13:20"), "글로우 크림 소량 견적 문의",
         "안녕하세요.\n\n신규 매장 테스트용으로 글로우 크림 50g 200개 견적 부탁드립니다.\n납기는 11월 20일입니다.\n\n최현우 드림")
ev_order(n2, "CUST-00011", "MC-GLW-CR050", 200, "2026-11-20", 12400, dt_("2026-11-05 10:30"), dt_("2026-11-05 11:15"),
         "Re: 글로우 크림 소량 견적 문의 - 주문", "안녕하세요.\n\n글로우 크림 50g 200개 주문합니다.\n\n최현우 드림")

n3 = new_case("INQ", "CUST-00033", "카밍 워시오프 마스크 100g 재고 문의", "1,500EA 재고 확인", dt_("2026-11-06 09:40"),
              {"itemName": "카밍 워시오프 마스크 100g", "itemCode": "MC-CAL-MK100", "quantity": 1500, "deliveryDate": "", "unitPrice": None,
               "shippingAddress": CU["CUST-00033"]["addr"]})
ev_stock(n3, "CUST-00033", "MC-CAL-MK100", 1500, dt_("2026-11-06 09:40"), dt_("2026-11-06 10:25"), "Calming wash-off mask stock check",
         "Hello,\n\nCould you check if 1,500 units of the Calming Wash-off Mask 100g are available?\nWe are planning a holiday promotion.\n\nBest regards,\nSarah Kim")
n3o = new_case("ORD", "CUST-00033", "카밍 워시오프 마스크 주문 건", "1,500EA 주문", dt_("2026-11-07 09:10"),
               {"itemName": "카밍 워시오프 마스크 100g", "itemCode": "MC-CAL-MK100", "quantity": 1500, "deliveryDate": "2026-11-21", "unitPrice": None,
                "shippingAddress": CU["CUST-00033"]["addr"]})
ev_order(n3o, "CUST-00033", "MC-CAL-MK100", 1500, "2026-11-21", unit_price(8000, 1500), dt_("2026-11-07 09:10"), dt_("2026-11-07 10:00"),
         "Order: Calming Wash-off Mask 1,500 units", "Hello,\n\nPlease register an order for 1,500 units of the Calming Wash-off Mask 100g, delivery by November 21.\nPO attached.\n\nBest regards,\nSarah Kim",
         attach="PO_OnyuGlobal_20261107.pdf")

n4 = new_case("RFQ", "CUST-00034", "하이드라 세럼 50g 건", "300EA 단가 확인", dt_("2026-11-10 13:30"),
              {"itemName": "하이드라 세럼 50g", "itemCode": "MC-HYD-SR050", "quantity": 300, "deliveryDate": "2026-12-01", "unitPrice": None,
               "shippingAddress": CU["CUST-00034"]["addr"]})
ev_quote(n4, "CUST-00034", "MC-HYD-SR050", 300, "2026-12-01", dt_("2026-11-10 13:30"), dt_("2026-11-10 15:00"), "하이드라 세럼 300EA 견적 요청",
         "안녕하세요.\n\n수출용으로 하이드라 세럼 50g 300개 견적 부탁드립니다. 12월 1일 납품 희망합니다.\n\n문가영 드림")
ev_hold(n4, "CUST-00034", dt_("2026-11-12 09:30"), "Re: 하이드라 세럼 300EA 견적 요청", "견적 감사합니다.\n바이어 확인 후 다시 연락드리겠습니다.\n\n문가영 드림")

n5 = new_case("ORD", "CUST-00022", "밸런스 크림 주문 건", "600EA 주문", dt_("2026-11-12 10:45"),
              {"itemName": "밸런스 크림 50g", "itemCode": "MC-BAL-CR050", "quantity": 600, "deliveryDate": "2026-11-28", "unitPrice": None,
               "shippingAddress": CU["CUST-00022"]["addr"]})
ev_order(n5, "CUST-00022", "MC-BAL-CR050", 600, "2026-11-28", unit_price(12500, 600), dt_("2026-11-12 10:45"), dt_("2026-11-12 11:30"),
         "밸런스 크림 600개 발주", "안녕하세요.\n\n밸런스 크림 50g 600개 발주합니다. 11월 28일까지 납품 부탁드립니다.\n\n강태호 드림",
         attach="발주서_누리리테일_밸런스크림600.pdf")

n7 = new_case("INQ", "CUST-00012", "하이드라 크림 50g 재고 문의", "400EA 재고 확인", dt_("2026-11-14 14:00"),
              {"itemName": "하이드라 크림 50g", "itemCode": "MC-HYD-CR050", "quantity": 400, "deliveryDate": "", "unitPrice": None,
               "shippingAddress": CU["CUST-00012"]["addr"]})
ev_stock(n7, "CUST-00012", "MC-HYD-CR050", 400, dt_("2026-11-14 14:00"), dt_("2026-11-14 14:50"), "하이드라 크림 재고 확인 부탁드립니다",
         "담당자님,\n\n하이드라 크림 50g 400개 재고 있을까요?\n\n윤서아")

n6 = new_case("RFQ", "CUST-00024", "카밍 크림 50g 건", "1,000EA 기준 단가 및 12/5 납기 확인", dt_("2026-11-17 09:25"),
              {"itemName": "카밍 크림 50g", "itemCode": "MC-CAL-CR050", "quantity": 1000, "deliveryDate": "2026-12-05", "unitPrice": None,
               "shippingAddress": CU["CUST-00024"]["addr"]})
ev_quote(n6, "CUST-00024", "MC-CAL-CR050", 1000, "2026-12-05", dt_("2026-11-17 09:25"), dt_("2026-11-17 10:40"), "카밍 크림 1,000개 견적 요청",
         "안녕하세요.\n\n연말 기획전용 카밍 크림 50g 1,000개 견적 부탁드립니다. 12월 5일 납품 희망합니다.\n\n오세린 드림")
ev_order(n6, "CUST-00024", "MC-CAL-CR050", 1000, "2026-12-05", unit_price(12000, 1000), dt_("2026-11-19 14:00"), dt_("2026-11-19 14:45"),
         "Re: 카밍 크림 1,000개 견적 요청 - 주문", "안녕하세요.\n\n카밍 크림 50g 1,000개 주문 확정합니다.\n\n오세린 드림", attach="PO_다온리테일_카밍크림1000.pdf")

n8 = new_case("RFQ", "CUST-00004", "글로우 토너 150g 건", "2,000EA 기준 단가 확인", dt_("2026-11-18 10:10"),
              {"itemName": "글로우 토너 150g", "itemCode": "MC-GLW-TN150", "quantity": 2000, "deliveryDate": "2026-12-10", "unitPrice": None,
               "shippingAddress": CU["CUST-00004"]["addr"]})
ev_quote(n8, "CUST-00004", "MC-GLW-TN150", 2000, "2026-12-10", dt_("2026-11-18 10:10"), dt_("2026-11-18 11:30"), "글로우 토너 대량 견적 요청",
         "안녕하세요.\n\n글로우 토너 150g 2,000개 견적 부탁드립니다. 12월 10일 납품 희망합니다.\n\n한지우 드림")
set_field(n8, "prev", json.dumps({"quantity": 2000}, ensure_ascii=False))
ev_quote(n8, "CUST-00004", "MC-GLW-TN150", 2500, "2026-12-10", dt_("2026-11-20 09:05"), dt_("2026-11-20 10:15"), "Re: 글로우 토너 대량 견적 요청 · 수량 변경",
         "안녕하세요.\n\n수량을 2,500개로 늘리면 단가 다시 알려주실 수 있을까요?\n\n한지우 드림", intent="변경요청")
set_field(n8, "changes", json.dumps([{"field": "수량", "previousValue": "2000 EA", "currentValue": "2500 EA",
                                               "changedDate": "2026-11-20 09:05", "sourceEmailId": CASES[n8]["fields"]["relatedEmails"].split(", ")[-2]}], ensure_ascii=False))
touch(n8, dt_("2026-11-20 10:15"), status="고객 검토 중")

n9 = new_case("RFQ", "CUST-00032", "시카 세럼 50g 건", "1,000EA 기준 단가 확인", dt_("2026-11-20 13:40"),
              {"itemName": "시카 세럼 50g", "itemCode": "MC-CIC-SR050", "quantity": 1000, "deliveryDate": "2026-12-12", "unitPrice": None,
               "shippingAddress": ADDR_32})
ev_quote(n9, "CUST-00032", "MC-CIC-SR050", 1000, "2026-12-12", dt_("2026-11-20 13:40"), dt_("2026-11-20 15:10"), "시카 세럼 1,000개 견적 요청",
         f"안녕하세요.\n\n시카 세럼 50g 1,000개 견적 부탁드립니다. 납품지는 신규물류센터({ADDR_32})입니다.\n\n김도윤 드림")
ev_order(n9, "CUST-00032", "MC-CIC-SR050", 1000, "2026-12-12", unit_price(10500, 1000), dt_("2026-11-24 10:20"), dt_("2026-11-24 11:20"),
         "Re: 시카 세럼 1,000개 견적 요청 - 발주", "안녕하세요.\n\n시카 세럼 50g 1,000개 발주합니다. 12월 12일 납품 부탁드립니다.\n\n김도윤 드림",
         attach="발주서_누리글로벌_시카세럼1000.pdf", fail_first=True, addr=ADDR_32)

ev_etc(dt_("2026-10-29 07:30"), "Cosmetic Weekly <newsletter@cosmeticweekly.kr>", "[Cosmetic Weekly] 10월 5주차 화장품 원료 시장 동향",
       "이번 주 원료 시장 동향\n- 히알루론산 원료 가격 전주 대비 3% 상승\n- 연말 기획 세트 수요 증가\n\n구독 해지: unsubscribe", "뉴스레터")
ev_etc(dt_("2026-11-05 08:10"), "패스트로지스 마케팅 <promo@fastlogis-mail.com>", "(광고) 연말 물류 성수기 특가 안내",
       "연말 성수기 수출 물류 특가!\n지금 상담 신청하시면 첫 달 운임 15% 할인.\n\n수신거부: reply STOP", "스팸")
ev_etc(dt_("2026-11-13 07:45"), "뷰티인사이트 <newsletter@beautyinsight.kr>", "주간 뷰티 트렌드 뉴스레터 제45호",
       "이번 주 트렌드: 저자극 진정 라인 검색량 증가, 대용량 토너 수요 확대.\n\n뉴스레터 수신 설정 변경", "뉴스레터")
ev_etc(dt_("2026-11-21 16:30"), "K-뷰티 엑스포 사무국 <info@kbeautyexpo.org>", "2027 K-뷰티 엑스포 참가사 모집 안내",
       "2027년 3월 개최되는 K-뷰티 엑스포 참가사를 모집합니다.\n조기 신청 마감: 12월 15일\n\n사무국 드림", "정보제공")

# ============================================================================ 2026-11-25: today's inbox
NEW_ERRORS: list[dict] = []
TRIAGE: list[dict] = []


def err(kind_cat: str, action: str, msg: str, when: datetime, eid: str, cid: str, code: str, wt: str, assignee: str, status="open", tries=1, resolved=None):
    COUNTER["err"] += 1
    d = {"id": f"ERR-{COUNTER['err']:04d}", "time": ts(when), "emailId": eid, "caseId": cid, "customer": CU[code]["name"],
         "workType": wt, "category": kind_cat, "failedAction": action, "message": msg, "status": status, "assignee": assignee, "tries": tries}
    if resolved:
        d["resolvedAt"] = resolved
    NEW_ERRORS.append(d)
    return d["id"]


# ERR for the 11/24 retry (resolved)
c9 = CASES[n9]
err("ERP 주문등록 실패", "ERP 주문등록", "ERP API Timeout (응답 대기 30초 초과)", dt_("2026-11-24 10:22"),
    c9["fields"]["relatedEmails"].split(", ")[-2], n9, "CUST-00032", "order", c9["fields"]["assignedUser"], status="resolved", resolved="2026-11-24 11:00")

# T1 shipping question for ORD-00124 (in transit)
ev_ship("ORD-2026-00124", "CUST-00033", so_124, dt_("2026-11-25 09:05"), None, "Shipping status - Glow Serum 400 units",
        f"Hello,\n\nCould you share the shipping status and tracking number for our Glow Serum order ({so_124})?\n\nThanks,\nSarah Kim", today=True)
# T2 누리글로벌 asks when the order (in transit) arrives at the new warehouse
ev_ship("ORD-2026-00126", "CUST-00032", so_of("ORD-2026-00126"), dt_("2026-11-25 09:18"), None, "베리어 크림 도착 예정일 문의",
        f"안녕하세요.\n\n신규물류센터 입고 일정을 잡으려고 합니다. 베리어 크림 300개({so_of('ORD-2026-00126')}) 도착 예정일과 송장번호 알려주세요.\n\n김도윤 드림", today=True)
# T3 라온 invoice for the glow cream order
ev_invoice(n2, "CUST-00011", so_of(n2), dt_("2026-11-25 09:31"), None, f"{so_of(n2)} 인보이스 요청",
           f"담당자님,\n\n글로우 크림 200개({so_of(n2)}) 인보이스 발행 부탁드립니다.\n\n최현우 드림", today=True)
# T4 라온 new quote (stock short)
t4 = new_case("RFQ", "CUST-00011", "베리어 크림 50g 건", "1,200EA 기준 단가 및 12/10 납기 확인", dt_("2026-11-25 09:47"),
              {"itemName": "베리어 크림 50g", "itemCode": "MC-BAR-CR050", "quantity": 1200, "deliveryDate": "2026-12-10", "unitPrice": None,
               "shippingAddress": CU["CUST-00011"]["addr"]})
ev_quote(t4, "CUST-00011", "MC-BAR-CR050", 1200, "2026-12-10", dt_("2026-11-25 09:47"), None, "베리어 크림 1,200개 견적 요청",
         "안녕하세요.\n\n베리어 크림 50g 1,200개 견적 부탁드립니다. 12월 10일까지 납품이 가능한지도 알려주세요.\n\n최현우 드림", today=True)
# T5 다온코스메틱 order without delivery date -> needs review
t5 = new_case("ORD", "CUST-00014", "베리어 세럼 추가 주문 건", "300EA 추가 주문 (납기 미기재)", dt_("2026-11-25 10:02"),
              {"itemName": "베리어 세럼 50g", "itemCode": "MC-BAR-SR050", "quantity": 300, "deliveryDate": "", "unitPrice": unit_price(11500, 300),
               "shippingAddress": CU["CUST-00014"]["addr"]})
eid = next_eid(True)
add_email(eid=eid, thread=f"T-{t5[-5:]}", direction="in", **in_head("CUST-00014"), when=dt_("2026-11-25 10:02"), subject="베리어 세럼 300개 추가 주문",
          cls="order", intent="주문전환", conf="91%", work_status="주문정보 확인 필요", case_id=t5, summary="베리어 세럼 50g / 300EA / 납기 미기재",
          interp="다온코스메틱 14가 베리어 세럼 50g 300EA를 추가 주문했습니다. 희망 납기가 메일에 없습니다.",
          reason="주문등록 필수값 deliveryDate(희망 납기)가 메일에 없습니다.", needs="order", status="확인 필요",
          issues=json.dumps([{"k": "field", "field": "deliveryDate", "low": True}]), next_step="항목 확인 — 확인 필요 1건",
          work_steps=steps("order", 0), closed="No", hold="No",
          body="안녕하세요.\n\n지난번과 같은 조건으로 베리어 세럼 50g 300개 추가 주문합니다.\n\n정우성 드림",
          entities=entity_lines("CUST-00014", "MC-BAR-SR050", 300, None, "", ""))
tl_mail(t5, eid, "in", dt_("2026-11-25 10:02"), "베리어 세럼 300개 추가 주문")
touch(t5, dt_("2026-11-25 10:02"), status="주문정보 확인 필요")
TRIAGE.append({"emailId": eid, "status": "확인 필요", "customer": "다온코스메틱 14", "subject": "베리어 세럼 300개 추가 주문", "receivedDate": "2026-11-25 10:02"})
# T6 온유코스메틱 wants to change quantity of an already registered order
so_127 = so_of("RFQ-2026-00127")
eid = next_eid(True)
add_email(eid=eid, thread="T-00127", direction="in", **in_head("CUST-00013"), when=dt_("2026-11-25 10:15"), subject=f"{so_127} 수량 변경 요청",
          cls="order", intent="변경요청", conf="89%", work_status="주문 변경 확인 필요", case_id="RFQ-2026-00127",
          summary=f"{so_127} 글로우 세럼 600EA → 800EA 변경 요청",
          interp=f"온유코스메틱 13이 이미 등록된 주문({so_127})의 수량을 600EA에서 800EA로 늘려 달라고 요청했습니다.",
          reason="이미 ERP에 등록된 Sales Order의 수량 변경이라 담당자 확인 후 ERP 주문 수정이 필요합니다.", needs="order", status="확인 필요",
          issues=json.dumps([{"k": "field", "field": "quantity", "low": False}]), next_step="항목 확인 — 주문 수정 여부 확인",
          work_steps=steps("order", 0), closed="No", hold="No",
          body=f"안녕하세요.\n\n{so_127} 글로우 세럼 주문 수량을 800개로 늘릴 수 있을까요? 납기는 그대로 12월 7일입니다.\n\n서예진 드림",
          entities=entity_lines("CUST-00013", "MC-GLW-SR050", 800, "2026-12-07", "RFQ-2026-00127", so_127))
tl_mail("RFQ-2026-00127", eid, "in", dt_("2026-11-25 10:15"), f"{so_127} 수량 변경 요청")
set_field("RFQ-2026-00127", "changes", json.dumps([{"field": "수량", "previousValue": "600 EA", "currentValue": "800 EA",
                                                           "changedDate": "2026-11-25 10:15", "sourceEmailId": eid}], ensure_ascii=False))
touch("RFQ-2026-00127", dt_("2026-11-25 10:15"), status="주문 변경 확인 필요", done="No")
TRIAGE.append({"emailId": eid, "status": "확인 필요", "customer": "온유코스메틱 13", "subject": f"{so_127} 수량 변경 요청", "receivedDate": "2026-11-25 10:15"})
# T7 누리리테일 stock question
t7 = new_case("INQ", "CUST-00022", "밸런스 크림 50g 재고 문의", "800EA 재고 확인", dt_("2026-11-25 10:28"),
              {"itemName": "밸런스 크림 50g", "itemCode": "MC-BAL-CR050", "quantity": 800, "deliveryDate": "", "unitPrice": None,
               "shippingAddress": CU["CUST-00022"]["addr"]})
ev_stock(t7, "CUST-00022", "MC-BAL-CR050", 800, dt_("2026-11-25 10:28"), None, "밸런스 크림 추가 재고 문의",
         "담당자님,\n\n밸런스 크림 50g 800개 추가로 받을 수 있을까요? 현재 재고 확인 부탁드립니다.\n\n강태호", today=True)
# T8 newsletter
ev_etc(dt_("2026-11-25 10:40"), "Cosmetic Weekly <newsletter@cosmeticweekly.kr>", "[Cosmetic Weekly] 11월 4주차 화장품 원료 시장 동향",
       "이번 주 원료 시장 동향\n- 세라마이드 원료 공급 안정\n- 연말 선물세트 생산 피크\n\n구독 해지: unsubscribe", "뉴스레터", today=True, done=False)
# T9 unknown 'Daon' sender -> customer not determined
t9 = new_case("RFQ", "CUST-00014", "하이드라 크림 50g 건 (고객 미확정)", "500EA 단가 확인", dt_("2026-11-25 10:52"),
              {"itemName": "하이드라 크림 50g", "itemCode": "MC-HYD-CR050", "quantity": 500, "deliveryDate": "2026-12-15", "unitPrice": None, "shippingAddress": ""})
CASES[t9]["fields"].update({"customer": "다온 (고객 미확정)", "customerCode": ""})
eid = next_eid(True)
add_email(eid=eid, thread=f"T-{t9[-5:]}", direction="in", customer="다온 (고객 미확정)", sender="이준호 <daon.buyer@naver.com>",
          recipient="sales@mcosmetic.co.kr", assignee="정하늘", when=dt_("2026-11-25 10:52"), subject="하이드라 크림 견적 요청드립니다",
          cls="quote", intent="신규요청", conf="74%", work_status="고객 확인 필요", case_id=t9, summary="하이드라 크림 50g / 500EA / 단가 문의",
          interp="'다온 구매팀'에서 하이드라 크림 50g 500EA 단가와 12월 15일 납기를 문의했습니다. 개인 메일 주소라 '다온' 고객사 4곳 중 어디인지 확정하지 못했습니다.",
          reason="발신 도메인(naver.com)이 고객 마스터에 없고, 상호 '다온'과 일치하는 고객이 4곳(다온글로벌 34, 다온코스메틱 14, 다온리테일 24, 다온뷰티 04)입니다.",
          needs="erp, reply", status="확인 필요", issues=json.dumps([{"k": "field", "field": "customerCode", "low": True}]),
          next_step="항목 확인 — 고객사 확인", work_steps=steps("quote", 0), closed="No", hold="No",
          body="안녕하세요. 다온 구매팀 이준호입니다.\n\n하이드라 크림 50g 500개 단가와 12월 15일 납품 가능 여부 알려주세요.\n\n이준호 드림",
          entities=entity_lines(None, "MC-HYD-CR050", 500, "2026-12-15", t9, "", cust_name="다온 (미확정)", addr=""),
          extra=[("Manual review candidates", {"customerCode": [
              {"value": "CUST-00004", "label": "다온뷰티 04", "note": "최근 1개월 하이드라 계열 문의 없음"},
              {"value": "CUST-00014", "label": "다온코스메틱 14", "note": "최근 3개월 거래 가장 많음"},
              {"value": "CUST-00024", "label": "다온리테일 24", "note": "11월 카밍 크림 주문 진행 중"},
              {"value": "CUST-00034", "label": "다온글로벌 34", "note": "하이드라 세럼 견적 검토 중(유사 품목)"}]})])
tl_mail(t9, eid, "in", dt_("2026-11-25 10:52"), "하이드라 크림 견적 요청드립니다")
touch(t9, dt_("2026-11-25 10:52"), status="고객 확인 필요")
TRIAGE.append({"emailId": eid, "status": "확인 필요", "customer": "다온 (고객 미확정)", "subject": "하이드라 크림 견적 요청드립니다", "receivedDate": "2026-11-25 10:52"})
# T10 vague mail -> unclassified
eid = next_eid(True)
add_email(eid=eid, thread="T-3110", direction="in", **in_head("CUST-00004"), when=dt_("2026-11-25 11:05"), subject="요청드린 자료 관련",
          cls="", intent="", conf="41%", work_status="분류 필요", summary="요청 내용 불명확",
          interp="다온뷰티 04가 '요청드린 자료'를 다시 보내 달라고 했습니다. 어떤 자료(견적서·카탈로그·인보이스)인지 판단하지 못했습니다.",
          reason="견적서, 카탈로그, 인보이스 중 어느 자료인지 판단할 단서(품목, 주문번호)가 메일에 없습니다.", needs="", status="미분류",
          issues=json.dumps([{"k": "class"}]), next_step="유형 선택 — 업무 유형 선택", work_steps="", closed="No", hold="No",
          body="안녕하세요.\n\n지난번에 요청드린 자료 한 번 더 보내주실 수 있을까요? 팀 공유용으로 필요합니다.\n\n한지우 드림",
          entities=entity_lines("CUST-00004", None, None, None, "", "")[:6],
          extra=[("Classification candidates", [["etc", 41], ["quote", 31], ["invoice", 28]])])
TRIAGE.append({"emailId": eid, "status": "미분류", "customer": "다온뷰티 04", "subject": "요청드린 자료 관련", "receivedDate": "2026-11-25 11:05"})
# T11 누리코스메틱 order (RFQ-00129) -> ERP registration fails, open error
c = "RFQ-2026-00129"
eid = next_eid(True)
e129 = ent(CASES[c])
add_email(eid=eid, thread="T-00129", direction="in", customer="누리코스메틱 12", sender="배성민 <nuri.purchase@gmail.com>",
          recipient="sales@mcosmetic.co.kr", assignee="정하늘", when=dt_("2026-11-25 11:20"), subject="Re: 시카 세럼 견적 요청 - 주문 진행",
          cls="order", intent="주문전환", conf="95%", work_status="ERP 전송 실패", case_id=c, summary="시카 세럼 50g / 600EA / 12월 5일 납기 주문 확정",
          interp="누리코스메틱 12(배성민)가 견적 받은 시카 세럼 50g 600EA를 12월 5일 납기로 주문 확정했습니다.",
          reason="ERP 주문등록 요청이 응답 시간 초과로 실패했습니다.", needs="order, reply", status="오류",
          next_step="재시도 — ERP 주문등록 재시도", work_steps="주문 내용 확인 [done]; ERP 주문등록 [error]; 주문확인 메일 (선택) [pending]",
          closed="No", hold="No", order_price=int(e129["unitPrice"]),
          body="안녕하세요.\n\n품의 완료되어 시카 세럼 50g 600개 주문합니다. 납기는 12월 5일로 변경 부탁드립니다.\n\n배성민 드림",
          entities=entity_lines("CUST-00012", "MC-CIC-SR050", 600, "2026-12-05", c, ""),
          flow={"order": "error", "orderError": "ERP API Timeout (응답 대기 30초 초과)", "orderApi": "POST /api/erp/v1/sales-orders", "orderResult": None})
tl_mail(c, eid, "in", dt_("2026-11-25 11:20"), "Re: 시카 세럼 견적 요청 - 주문 진행")
tl_action(c, dt_("2026-11-25 11:22"), "ERP 주문등록", "", ok=False, err="ERP API Timeout (응답 대기 30초 초과)")
set_ent(CASES[c], deliveryDate="2026-12-05")
CASES[c]["fields"]["caseType"] = "견적 → 주문"
touch(c, dt_("2026-11-25 11:22"), status="ERP 전송 실패", done="No")
err("ERP 주문등록 실패", "ERP 주문등록", "ERP API Timeout (응답 대기 30초 초과)", dt_("2026-11-25 11:22"), eid, c, "CUST-00012", "order", "정하늘")
# T12 다온리테일 shipping question (production plan)
ev_ship(n6, "CUST-00024", so_of(n6), dt_("2026-11-25 11:34"), None, "카밍 크림 1,000개 출하 일정 문의",
        f"안녕하세요.\n\n카밍 크림 1,000개({so_of(n6)}) 출하 일정 확인 부탁드립니다. 기획전 준비 때문에요.\n\n오세린 드림", today=True)
# T13 누리뷰티 re-order, waiting for ERP
t13 = new_case("ORD", "CUST-00002", "카밍 세럼 재주문 건", "300EA 재주문", dt_("2026-11-25 11:47"),
               {"itemName": "카밍 세럼 50g", "itemCode": "MC-CAL-SR050", "quantity": 300, "deliveryDate": "2026-12-15", "unitPrice": 9500,
                "shippingAddress": CU["CUST-00002"]["addr"]})
eid = next_eid(True)
add_email(eid=eid, thread=f"T-{t13[-5:]}", direction="in", **in_head("CUST-00002"), when=dt_("2026-11-25 11:47"), subject="카밍 세럼 재주문",
          cls="order", intent="주문전환", conf="98%", work_status="ERP 전송대기", case_id=t13, summary="카밍 세럼 50g / 300EA / 12월 15일 납기 재주문",
          interp="누리뷰티 02가 지난번과 같은 조건(9,500원)으로 카밍 세럼 50g 300EA를 12월 15일 납기로 재주문했습니다.",
          needs="order", status="처리 대기", next_step="주문등록 — ERP 주문등록", work_steps=steps("order", 0), closed="No", hold="No",
          order_price=9500, body="안녕하세요.\n\n지난번과 같은 조건으로 카밍 세럼 50g 300개 재주문합니다.\n12월 15일까지 납품 부탁드립니다.\n\n박지현 드림",
          entities=entity_lines("CUST-00002", "MC-CAL-SR050", 300, "2026-12-15", "RFQ-2026-00123", ""),
          attachments=[{"name": "발주서_누리뷰티_카밍세럼300_재주문.pdf", "size": "176KB", "type": "po"}])
tl_mail(t13, eid, "in", dt_("2026-11-25 11:47"), "카밍 세럼 재주문")
touch(t13, dt_("2026-11-25 11:47"), status="ERP 전송대기")
# T14 온유리테일 invoice, our reply fails (open error)
c = "RFQ-2026-00115"
ev_invoice(c, "CUST-00023", so_of(c), dt_("2026-11-25 13:10"), None, f"{so_of(c)} 인보이스 발행 요청",
           f"안녕하세요.\n\n밸런스 크림 300개({so_of(c)}) 인보이스 발행 부탁드립니다.\n\n이민정 드림", today=True, send_fail=True)
inv_eid = CASES[c]["fields"]["relatedEmails"].split(", ")[-1]
err("Email 발송 실패", "Email 발송", "SMTP 451 Temporary failure: 수신 서버 응답 지연", dt_("2026-11-25 14:00"), inv_eid, c, "CUST-00023", "invoice", "박민호")


# ============================================================================ render
def render_case(cid: str) -> list[str]:
    c = CASES[cid]
    L = [f"CASE {cid}"] + [f"- {k}: {v}" for k, v in c["fields"].items()]
    L += ["", "Case timeline:"] + [f"- {x}" for x in c["timeline"]]
    if c["actions"]:
        L += ["", "Case action history:"] + [f"- {json.dumps(a, ensure_ascii=False)}" for a in c["actions"]]
    return L


# round-trip safety: untouched 10/22 cases must render exactly as in the source
for cid, src in kb.cases.items():
    if not CASES[cid]["touched"]:
        assert render_case(cid) == kb.lines[src.line_start - 1:src.line_end], cid

# --- final Sales Order table + item master at 11/25
for so, v in SO.items():
    view = so_view(so, NEW)
    if v.get("tracking") and view["tracking"]:
        view["tracking"] = v["tracking"]
    v.update({k: view[k] for k in ("shipQty", "state", "tracking")})
for item in IT:
    for dd in range((NEW - OLD).days + 1):
        x = OLD + timedelta(days=dd)
        assert stock_at(item, x) >= 0, (item, x, stock_at(item, x))
NEW_ITEMS = {}
for item, v in IT.items():
    NEW_ITEMS[item] = dict(v, **{"Stock": stock_at(item, NEW), "Lead date": str(D(v["Lead date"]) + timedelta(days=34)),
                                 "Back lead date": str(D(v["Back lead date"]) + timedelta(days=34))})

# --- errors
ERRS = [dict(kb.errors[k]) for k in kb.errors]
for e in ERRS:
    e.pop("line", None)
    if e["id"] in RESOLVE:
        e["status"] = "resolved"
        e["resolvedAt"] = RESOLVE[e["id"]]
ALL_ERRS = sorted(NEW_ERRORS + ERRS, key=lambda e: e["id"], reverse=True)
OPEN_ERRS = [e for e in ALL_ERRS if e["status"] == "open"]

# --- section text builders
src = kb.lines
sec = {s.number: s for s in kb.sections}


def section_lines(n: int) -> list[str]:
    s = sec[n]
    return src[s.line_start - 1:s.line_end]


def patched_email_block(eid: str) -> list[str]:
    e = kb.emails[eid]
    L = src[e.line_start - 1:e.line_end]
    p = EMAIL_PATCH.get(eid)
    if not p:
        return L
    out = []
    for ln in L:
        m = re.match(r"^- ([A-Za-z ]+): ?(.*)$", ln)
        if m and m.group(1) in p and not ln.startswith("- customer"):
            out.append(f"- {m.group(1)}: {p[m.group(1)]}")
        else:
            out.append(ln)
    return out


out: list[str] = []
# 0 header
out += ["AI365 3.0 – M Cosmetic Email Agent Knowledge Base", "=" * 60, "Source: ai365-Mcosmetic(1).html", f"Snapshot date: {NEW}"]
# 1-4 unchanged
for n in (1, 2, 3, 4):
    out += section_lines(n)
# 5 items
s5 = section_lines(5)
out += s5[:2]
for item, v in NEW_ITEMS.items():
    out += [f"- {item}: {v['name']}", f"  Short name: {v['Short name']}", f"  Base price: {v['Base price']}", f"  Stock: {v['Stock']}",
            f"  Lead date: {v['Lead date']}", f"  Back lead date: {v['Back lead date']}"]
out.append("")
# 6, 7 unchanged
out += section_lines(6) + section_lines(7)
# 8 sales orders (newest first, then the original order)
s8 = section_lines(8)
new_sos = sorted([k for k in SO if k not in kb.sales_orders], key=lambda k: k[-5:], reverse=True)
out += s8[:2]
for k in new_sos + list(kb.sales_orders):
    v = {kk: vv for kk, vv in SO[k].items() if not kk.startswith("_")}
    out.append(f"- {k}: {json.dumps(v, ensure_ascii=False)}")
out.append("")

# --- email set (needed for dashboard)
today_blocks = sorted([b for b in EMAIL_BLOCKS if b[0].date() == NEW], key=lambda b: b[0])
arch_blocks = sorted([b for b in EMAIL_BLOCKS if b[0].date() < NEW], key=lambda b: b[0], reverse=True)
all_email_text = []
for _, _, L in today_blocks:
    all_email_text += L + [""]
for eid in kb.emails:
    all_email_text += patched_email_block(eid) + [""]
for _, _, L in arch_blocks:
    all_email_text += L + [""]

# parse back the headers we need for statistics
recs = []
cur = None
for ln in all_email_text:
    m = re.match(r"^EMAIL (E-\d+)$", ln)
    if m:
        cur = {"id": m.group(1)}
        recs.append(cur)
        continue
    m = re.match(r"^- (Direction|Classification|Status|Received|AI confidence|Work status|Work steps): ?(.*)$", ln)
    if cur is not None and m and m.group(1) not in cur:
        cur[m.group(1)] = m.group(2)
inbound = [r for r in recs if r["Direction"] == "in"]
outbound = [r for r in recs if r["Direction"] == "out"]
today_in = [r for r in inbound if r["Received"].startswith(str(NEW))]
CLS = [("quote", "견적요청"), ("order", "주문등록"), ("ship", "출하일정"), ("stock", "재고문의"), ("invoice", "인보이스 발행요청"), ("etc", "기타")]
bucket = {"완료": "done", "처리 대기": "in_progress", "진행 중": "in_progress", "확인 필요": "needs_review", "오류": "error"}

# 9 dashboard
review = [r for r in today_in if r["Status"] in ("확인 필요", "미분류")]
erp_pending = [r for r in today_in if r["Work status"] in ("ERP 전송대기", "ERP 전송 실패")]
out += ["9. DASHBOARD SNAPSHOT", "-----------------------", f"Date: {NEW}", f"- KPI recv: {len(today_in)}",
        f"- KPI auto: {len(today_in) - len(review)}", f"- KPI review: {len(review)}", f"- KPI erp: {len(erp_pending)}",
        f"- KPI err: {len(OPEN_ERRS)}", "", "Work type summary:"]
for cls, label in CLS:
    rs = [r for r in inbound if r["Classification"] == cls]
    cnt = collections.Counter(bucket.get(r["Status"], "in_progress") for r in rs)
    out.append(f"- {label}: total={len(rs)}; done={cnt['done']}; in_progress={cnt['in_progress']}; needs_review={cnt['needs_review']}; error={cnt['error']}")
unclassified = [r for r in inbound if not r["Classification"]]


def conf_of(r):
    v = r["AI confidence"].rstrip("%")
    return int(v) if v.isdigit() else 0


tiers = [sum(1 for r in inbound if conf_of(r) >= 95), sum(1 for r in inbound if 80 <= conf_of(r) < 95), sum(1 for r in inbound if conf_of(r) < 80)]
total_cases = len(CASES)
# section 2 repeats the confidence tiers: keep them equal to the dashboard
_TIER_LABELS = ("- 95% 이상: 자동분류 — ", "- 80 ~ 94%: 자동분류, 확인 권장 — ", "- 80% 미만: 수신메일에서 담당자 확인 — ")
_end2 = out.index("3. REQUIRED FIELDS BY BUSINESS CATEGORY")
for _i in range(_end2):
    for _lab, _n in zip(_TIER_LABELS, tiers):
        if out[_i].startswith(_lab):
            out[_i] = f"{_lab}{_n} items"
out += ["", f"- Cumulative classified incoming emails: {len(inbound) - len(unclassified)}", f"- Unclassified incoming emails: {len(unclassified)}",
        f"- Cumulative incoming emails (including unclassified): {len(inbound)}", f"- Cumulative outgoing emails: {len(outbound)}",
        f"- Total email records (incoming and outgoing): {len(recs)}", f"- Total work cases: {total_cases}", "", "AI confidence tiers:",
        f"- 95% 이상: 자동분류 — {tiers[0]} items", f"- 80 ~ 94%: 자동분류, 확인 권장 — {tiers[1]} items",
        f"- 80% 미만: 수신메일에서 담당자 확인 — {tiers[2]} items", f"- Confidence total: {sum(tiers)}", "",
        f"Note: KPI recv/auto/review/erp/err are for the {NEW} dashboard day; work type and confidence counts are cumulative in this snapshot.", ""]
# 10 emails
out += ["10. EMAIL RECORDS", "---------------------", ""] + all_email_text
# 11 cases: new ones (newest first) then the originals
out += ["11. WORK CASES", "-----------------", ""]
for cid in sorted([k for k in CASES if CASES[k]["new"]], key=lambda k: k[-5:], reverse=True) + list(kb.cases):
    out += render_case(cid) + [""]
# 12 errors
out += ["12. PROCESSING ERRORS", "-------------------------"] + [f"- {json.dumps(e, ensure_ascii=False)}" for e in ALL_ERRS] + [""]
# 13 notifications
out += ["13. NOTIFICATIONS", "----------------", f'- "count": {len(OPEN_ERRS) + len(TRIAGE)}', '- "errors":']
out += [f"- {json.dumps(e, ensure_ascii=False)}" for e in OPEN_ERRS] + ['- "triage":']
out += [f"- {json.dumps(t, ensure_ascii=False)}" for t in TRIAGE] + [""]
# 14 demo scenarios unchanged
out += section_lines(14)
# 15 notes
out += ["15. IMPORTANT SOURCE-SPECIFIC NOTES", "-----------------------------------",
        f"- The HTML is a standalone display snapshot exported on {NEW}, anchored to {NEW}.",
        "- Source: ai365-Mcosmetic(1).html (embedded mcmail-snapshot JSON).",
        f"- Snapshot date: {NEW}. Archived business records from 2026-09-23 through 2026-11-24 are retained on their original dates.",
        f"- This snapshot continues the {OLD} snapshot: records open on {OLD} were processed between 2026-10-23 and 2026-11-24, and new mails, cases and Sales Orders were added.",
        "- The snapshot contains read-only data for the M Cosmetic Email Agent.",
        "- Processing actions in the standalone HTML are disabled; actions do not modify the snapshot.",
        "- The embedded data is the source of truth for the knowledge extracted here.",
        f"- KPI counts for {NEW} and cumulative email/case counts describe different aggregation periods and are intentionally kept separate.",
        "- Item stock is the on-hand quantity on the snapshot date (after shipments and inbound receipts); lead dates are as of the snapshot date.",
        "- Base price values in the item master are the company DB standard prices (KRW). Prices recorded in quotes, ERP results, orders and invoices are the transaction prices at that time and are not changed retroactively.",
        "- Demo scenario IDs E-2002 through E-2006 appear in scenario definitions but are not actual mail records in this snapshot.",
        "- UI styling, CSS, icons, JavaScript implementation details, and CDN/font configuration were intentionally excluded because they are not business knowledge for the LLM."]

text = "\n".join(out).rstrip("\n") + "\n"
text = re.sub(r"\n{3,}", "\n\n", text)
OUT.write_text(text, encoding="utf-8")
print(f"wrote {OUT.name}: {text.count(chr(10))} lines, {len(recs)} emails (+{len(EMAIL_BLOCKS)}), {total_cases} cases "
      f"(+{sum(1 for c in CASES.values() if c['new'])}), {len(SO)} SOs (+{len(SO) - len(kb.sales_orders)}), "
      f"today in {len(today_in)} / review {len(review)} / open errors {len(OPEN_ERRS)}")
