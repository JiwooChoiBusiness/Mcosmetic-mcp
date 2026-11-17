"""Unit tests: parsing completeness, structured counts and retrieval-recall floors."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cosmetic_kb.kb import KnowledgeBase  # noqa: E402
from eval.evaluate import evaluate, gen_line_questions, gen_template_questions, load_llm  # noqa: E402


@pytest.fixture(scope="module")
def kb():
    return KnowledgeBase()


def test_structure(kb):
    assert kb.n_lines == 14062
    assert [s.number for s in kb.sections] == list(range(16))
    assert len(kb.emails) == 226 and len(kb.cases) == 63
    assert len(kb.items) == 12 and len(kb.customers) == 12 and len(kb.sales_orders) == 27 and len(kb.errors) == 7
    assert all("contact" in c and "signature" in c for c in kb.customers.values())


def test_every_line_is_in_a_chunk(kb):
    covered = set()
    for c in kb.chunks:
        covered.update(range(c.line_start, c.line_end + 1))
    heading = {s.line_start for s in kb.sections} | {s.line_start + 1 for s in kb.sections}
    missing = [i for i, l in enumerate(kb.lines, 1) if l.strip() and i not in covered and i not in heading]
    assert missing == []


def test_email_fields_parsed(kb):
    e = kb.emails["E-1001"]
    assert e.fields["Subject"] == "카밍 세럼 견적 문의드립니다"
    assert e.entities["quantity"]["value"] == "300"
    assert "11월 16일" in e.body
    assert sum("Processing flow / ERP result" in x.extras for x in kb.emails.values()) == 95


def test_dashboard_consistency(kb):
    assert sum(e.fields["Direction"] == "in" for e in kb.emails.values()) == 133
    assert sum(e.fields["Direction"] == "out" for e in kb.emails.values()) == 93


def test_grep_reaches_every_line(kb):
    # exact-text path: every non-blank line is found by grep of its own content
    for i in range(1, kb.n_lines + 1, 37):
        t = kb.lines[i - 1].strip()
        if t:
            _, hits = kb.grep(t, max_hits=10_000)
            assert any(h["line"] == i for h in hits)


@pytest.mark.parametrize("set_prefix,floor", [("llm_literal", 0.98), ("llm_paraphrase", 0.95),
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
