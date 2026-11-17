"""Simulated ERP / mail actions: rules must reproduce the dataset's recorded ERP results,
and write actions must follow preview -> approval -> execute."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cosmetic_kb import erp  # noqa: E402
from cosmetic_kb.kb import KnowledgeBase  # noqa: E402


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase()


def _flows(kb):
    for e in kb.emails.values():
        p = e.extras.get("Processing flow / ERP result")
        if isinstance(p, dict):
            yield e, p


def test_price_rule_matches_all_recorded_lookups(kb):
    rows = [p["erpResult"] for _, p in _flows(kb) if isinstance(p.get("erpResult"), dict) and "rate" in p["erpResult"]]
    assert len(rows) >= 32  # 32 in the 10/22 records + the look-ups added up to 11/25
    for r in rows:
        assert erp.discount_rate(r["qty"]) == r["rate"]
        assert erp.unit_price(r["base"], r["qty"]) == r["price"]


def test_order_prices_match_recorded_orders(kb):
    """Recorded orders follow the discount rule on the base price of their time (taken from the recorded
    ERP price look-ups), not the current master price, which is the DB standard price (no retroactive change)."""
    hist_base = {}
    for _, p in _flows(kb):
        r = p.get("erpResult")
        if isinstance(r, dict) and "base" in r:
            hist_base.setdefault(r["itemCode"], set()).add(r["base"])
    assert all(len(v) == 1 for v in hist_base.values())
    rows = [p["orderResult"] for _, p in _flows(kb) if isinstance(p.get("orderResult"), dict) and p["orderResult"].get("price")]
    assert len(rows) >= 25
    for o in rows:
        item = erp.resolve_item(kb, o["item"])["itemCode"]
        if item in hist_base:
            assert erp.unit_price(next(iter(hist_base[item])), o["qty"]) == o["price"]
        assert kb.items[item]["Base price"] > o["price"]  # master now shows the (higher) DB price


def test_invoice_amounts_match_recorded(kb):
    rows = [p["erpResult"] for _, p in _flows(kb) if isinstance(p.get("erpResult"), dict) and "invoiceNo" in p["erpResult"]]
    assert len(rows) >= 10
    for r in rows:
        s = erp.sales_order_lookup(kb, r["so"])["erpResult"]
        for k in ("invoiceNo", "qty", "price", "amount", "vat", "total", "orderDate", "deliveryDate"):
            assert s[k] == r[k], (r["so"], k)


def test_today_is_snapshot_date(kb):
    assert erp.TODAY == kb.snapshot_date == "2026-11-25"


def test_price_lookup_glow_cream_500(kb):
    r = erp.price_lookup(kb, "글로우 크림 50g", 500, "다온코스메틱")["erpResult"]
    assert (r["base"], r["rate"], r["price"]) == (39000, 0.08, 35880)  # DB standard price 39,000


def test_inventory_allocation_matches_open_sales_orders(kb):
    for item in kb.items:
        open_qty = sum(v["orderQty"] - v["shipQty"] for v in kb.sales_orders.values()
                       if v["itemCode"] == item and v["state"] in ("생산 계획", "출하 준비"))
        r = erp.inventory_lookup(kb, item)["erpResult"]
        assert r["allocated"] == open_qty and r["onHand"] == kb.items[item]["Stock"]
        assert r["available"] == max(r["onHand"] - open_qty, 0)


def test_order_blocked_when_delivery_date_missing(kb):
    r = erp.register_order(kb, case_id="ORD-2026-00143")  # 다온코스메틱 추가 주문, 납기 미기재
    assert r["status"] == "blocked" and r["missing_fields"] == ["deliveryDate"]
    p = erp.register_order(kb, case_id="ORD-2026-00143", delivery_date="2026-12-12")
    assert p["status"] == "preview" and p["changes"][0]["after"]["price"] == 10930


def test_order_preview_then_confirm(kb):
    p = erp.register_order(kb, case_id="ORD-2026-00146")  # 누리뷰티 재주문, ERP 전송대기
    assert p["status"] == "preview" and p["requires_approval"]
    assert "SO-20261125-00146" in p["summary_text"] and "3,135,000원" in p["summary_text"]
    with pytest.raises(erp.ErpInputError):
        erp.register_order(kb, case_id="ORD-2026-00146", confirm=True)  # no preview id
    with pytest.raises(erp.ErpInputError):  # changed content after approval
        erp.register_order(kb, case_id="ORD-2026-00146", quantity=999, confirm=True, preview=p["preview_id"])
    d = erp.register_order(kb, case_id="ORD-2026-00146", confirm=True, preview=p["preview_id"])
    assert d["status"] == "done" and d["orderResult"]["so"] == "SO-20261125-00146" and d["orderResult"]["price"] == 9500
    assert d["simulated"] and d["persisted"] is False


def test_retry_resolves_open_error(kb):
    p = erp.register_order(kb, case_id="RFQ-2026-00129")  # ERP 주문등록 실패 (ERR-0010)
    assert any(c["target"] == "ERR-0010" and c["after"] == "resolved" for c in p["changes"])
    f = erp.register_order(kb, case_id="RFQ-2026-00129", confirm=True, preview=p["preview_id"], simulate_failure=True)
    assert f["status"] == "failed" and "Timeout" in f["orderError"]


def test_already_registered(kb):
    assert erp.register_order(kb, case_id="ORD-2026-00124")["status"] == "already_registered"


def test_send_email_flow(kb):
    args = dict(to="sarah.kim@onyuglobal.com", subject="배송 현황 안내", body="안녕하세요.\n…\nM코스메틱 박민호 드림",
                case_id="ORD-2026-00124")
    p = erp.send_email(kb, **args)
    assert p["status"] == "preview" and p["warnings"] == []
    d = erp.send_email(kb, **args, confirm=True, preview=p["preview_id"])
    assert d["status"] == "done" and d["messageId"].startswith("MSG-") and d["sentAt"].startswith("2026-11-25")
    w = erp.send_email(kb, to="someone@unknown.com", subject="x", body="y", case_id="ORD-2026-00124")
    assert len(w["warnings"]) == 2


def test_preview_id_survives_turns(kb):
    """Platforms that drop tool results between turns: the id is in the visible text, is stable,
    and is accepted in loose forms; re-previewing yields the same id so the AI can execute in one turn."""
    p = erp.register_order(kb, case_id="ORD-2026-00146")
    pid = p["preview_id"]
    assert f"미리보기 ID: {pid}" in p["summary_text"]
    assert erp.register_order(kb, case_id="ORD-2026-00146")["preview_id"] == pid
    for loose in (pid.lower(), pid.replace("PV-", ""), f" '{pid}' "):
        assert erp.register_order(kb, case_id="ORD-2026-00146", confirm=True, preview=loose)["status"] == "done"
    assert "한 번만" in p["next"] and "다시 묻지 않음" in p["next"]
