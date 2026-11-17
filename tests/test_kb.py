"""Unit tests: parsing completeness, dashboard consistency and retrieval-recall floors (current data file)."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cosmetic_kb.kb import KnowledgeBase  # noqa: E402
from eval.evaluate import evaluate, gen_line_questions, gen_template_questions, load_llm  # noqa: E402

OLD_FILE = ROOT / "data" / "ai365-Mcosmetic-knowledge-updated_2026-10-22.txt"


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase()


def test_structure(kb):
    assert kb.snapshot_date == "2026-11-25"
    assert [s.number for s in kb.sections] == list(range(16))
    assert len(kb.emails) == 324 and len(kb.cases) == 78
    assert len(kb.items) == 12 and len(kb.customers) == 12 and len(kb.sales_orders) == 42 and len(kb.errors) == 10
    assert all("contact" in c and "signature" in c for c in kb.customers.values())


def test_previous_snapshot_still_parses():
    old = KnowledgeBase(OLD_FILE)
    assert old.snapshot_date == "2026-10-22" and len(old.emails) == 226 and len(old.cases) == 63


def test_every_line_is_in_a_chunk(kb):
    covered = set()
    for c in kb.chunks:
        covered.update(range(c.line_start, c.line_end + 1))
    heading = {s.line_start for s in kb.sections} | {s.line_start + 1 for s in kb.sections}
    missing = [i for i, l in enumerate(kb.lines, 1) if l.strip() and i not in covered and i not in heading]
    assert missing == []


def test_references_resolve(kb):
    for c in kb.cases.values():
        for r in filter(None, c.fields.get("relatedEmails", "").split(", ")):
            assert r in kb.emails, (c.id, r)
        so = c.fields.get("salesOrderNumber")
        assert not so or so in kb.sales_orders, (c.id, so)
        stamps = [t.split(" | ")[0] for t in c.timeline]
        assert stamps == sorted(stamps), c.id
    for e in kb.emails.values():
        assert not e.fields.get("Case ID") or e.fields["Case ID"] in kb.cases, e.id


def test_email_fields_parsed(kb):
    e = kb.emails["E-1001"]
    assert e.fields["Subject"] == "카밍 세럼 견적 문의드립니다" and e.fields["Status"] == "완료"
    assert e.entities["quantity"]["value"] == "300"
    assert "11월 16일" in e.body
    assert sum("Processing flow / ERP result" in x.extras for x in kb.emails.values()) >= 95


def _dash(kb, label):
    s = next(x for x in kb.sections if x.number == 9)
    for ln in kb.lines[s.line_start - 1:s.line_end]:
        m = re.match(rf"^- {re.escape(label)}: (\d+)", ln)
        if m:
            return int(m.group(1))
    raise KeyError(label)


def test_dashboard_matches_records(kb):
    inbound = [e for e in kb.emails.values() if e.fields["Direction"] == "in"]
    assert _dash(kb, "Cumulative incoming emails (including unclassified)") == len(inbound) == 184
    assert _dash(kb, "Cumulative outgoing emails") == len(kb.emails) - len(inbound) == 140
    assert _dash(kb, "Total work cases") == len(kb.cases)
    today = [e for e in inbound if e.fields["Received"].startswith(kb.snapshot_date)]
    assert _dash(kb, "KPI recv") == len(today) == 14
    assert _dash(kb, "KPI err") == sum(1 for e in kb.errors.values() if e["status"] == "open") == 2


def test_stock_ledger_never_negative_and_lead_dates_future(kb):
    for it in kb.items.values():
        assert it["Stock"] >= 0 and it["Lead date"] > kb.snapshot_date


def test_grep_reaches_every_line(kb):
    for i in range(1, kb.n_lines + 1, 37):
        t = kb.lines[i - 1].strip()
        if t:
            _, hits = kb.grep(t, max_hits=10_000)
            assert any(h["line"] == i for h in hits)


@pytest.mark.parametrize("set_prefix,floor", [("llm_literal", 0.98), ("llm_paraphrase", 0.93),
                                              ("tmpl_", 0.93), ("line_literal", 0.93)])
def test_recall_at5_floor(kb, set_prefix, floor):
    lit, noi = gen_line_questions(kb)
    qs = [q for q in lit + noi + gen_template_questions(kb) + load_llm(kb) if q["set"].startswith(set_prefix)]
    if not qs:
        pytest.skip("question set missing")
    per_set, _ = evaluate(kb, qs, (5,))
    n = sum(s["n"] for s in per_set.values())
    hit = sum(s["strict@5"] for s in per_set.values())
    assert hit / n >= floor, f"{set_prefix}: {hit}/{n}"
