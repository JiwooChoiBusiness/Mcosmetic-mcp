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
    assert len(rows) == 32
    for r in rows:
        assert erp.discount_rate(r["qty"]) == r["rate"]
        assert erp.unit_price(r["base"], r["qty"]) == r["price"]


def test_order_prices_match_recorded_orders(kb):
    rows = [p["orderResult"] for _, p in _flows(kb) if isinstance(p.get("orderResult"), dict) and p["orderResult"].get("price")]
    assert len(rows) == 25
    for o in rows:
        assert erp.unit_price(erp.resolve_item(kb, o["item"])["Base price"], o["qty"]) == o["price"]


def test_invoice_amounts_match_recorded(kb):
    rows = [p["erpResult"] for _, p in _flows(kb) if isinstance(p.get("erpResult"), dict) and "invoiceNo" in p["erpResult"]]
    assert len(rows) == 10
    for r in rows:
        s = erp.sales_order_lookup(kb, r["so"])["erpResult"]
        for k in ("invoiceNo", "qty", "price", "amount", "vat", "total", "orderDate", "deliveryDate"):
            assert s[k] == r[k], (r["so"], k)


def test_price_lookup_glow_cream_500(kb):
    r = erp.price_lookup(kb, "글로우 크림 50g", 500, "다온코스메틱")["erpResult"]
    assert (r["base"], r["rate"], r["price"]) == (12400, 0.08, 11410)


def test_inventory_shortage_barrier_cream(kb):
    r = erp.inventory_lookup(kb, "MC-BAR-CR050", 300)["erpResult"]
    assert (r["onHand"], r["allocated"], r["available"], r["shortage"], r["enough"]) == (1100, 1800, 0, 700, False)


def test_order_blocked_when_address_missing(kb):
    r = erp.register_order(kb, case_id="ORD-2026-00126")
    assert r["status"] == "blocked" and r["missing_fields"] == ["shippingAddress"]


def test_order_with_given_address_goes_to_production_plan(kb):
    r = erp.register_order(kb, case_id="ORD-2026-00126", shipping_address="인천광역시 중구 신규창고로 1")
    assert r["status"] == "preview"
    so = r["changes"][0]["after"]
    assert so["state"] == "생산 계획" and so["shipDate"] == "2026-11-30" and so["price"] == 12160


def test_order_preview_then_confirm(kb):
    p = erp.register_order(kb, case_id="ORD-2026-00124")
    assert p["status"] == "preview" and p["requires_approval"]
    assert "SO-20261022-00124" in p["summary_text"] and "4,598,000원" in p["summary_text"]
    with pytest.raises(erp.ErpInputError):
        erp.register_order(kb, case_id="ORD-2026-00124", confirm=True)  # no preview id
    with pytest.raises(erp.ErpInputError):  # changed content after approval
        erp.register_order(kb, case_id="ORD-2026-00124", quantity=999, confirm=True, preview=p["preview_id"])
    d = erp.register_order(kb, case_id="ORD-2026-00124", confirm=True, preview=p["preview_id"])
    assert d["status"] == "done" and d["orderResult"]["so"] == "SO-20261022-00124" and d["orderResult"]["price"] == 10450
    assert d["simulated"] and d["persisted"] is False


def test_retry_resolves_open_error(kb):
    p = erp.register_order(kb, case_id="ORD-2026-00122")
    assert any(c["target"] == "ERR-0008" and c["after"] == "resolved" for c in p["changes"])
    f = erp.register_order(kb, case_id="ORD-2026-00122", confirm=True, preview=p["preview_id"], simulate_failure=True)
    assert f["status"] == "failed" and "Timeout" in f["orderError"]


def test_already_registered(kb):
    assert erp.register_order(kb, case_id="ORD-2026-00112")["status"] == "already_registered"


def test_send_email_flow(kb):
    args = dict(to="sarah.kim@onyuglobal.com", subject="주문 접수 확인", body="안녕하세요.\n…\nM코스메틱 이수진 드림",
                case_id="ORD-2026-00124")
    p = erp.send_email(kb, **args)
    assert p["status"] == "preview" and p["warnings"] == []
    d = erp.send_email(kb, **args, confirm=True, preview=p["preview_id"])
    assert d["status"] == "done" and d["messageId"].startswith("MSG-") and d["sentAt"].startswith("2026-10-22")
    w = erp.send_email(kb, to="someone@unknown.com", subject="x", body="y", case_id="ORD-2026-00124")
    assert len(w["warnings"]) == 2


def test_preview_id_survives_turns(kb):
    """Platforms that drop tool results between turns: the id is in the visible text, is stable,
    and is accepted in loose forms; re-previewing yields the same id so the AI can execute in one turn."""
    p = erp.register_order(kb, case_id="ORD-2026-00124")
    pid = p["preview_id"]
    assert f"미리보기 ID: {pid}" in p["summary_text"]
    assert erp.register_order(kb, case_id="ORD-2026-00124")["preview_id"] == pid
    for loose in (pid.lower(), pid.replace("PV-", ""), f" '{pid}' "):
        assert erp.register_order(kb, case_id="ORD-2026-00124", confirm=True, preview=loose)["status"] == "done"
    assert "한 번만" in p["next"] and "다시 묻지 않음" in p["next"]
