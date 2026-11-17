"""Coverage / recall evaluation for the M Cosmetic knowledge MCP.

Question sets
  A. line_literal   : every distinctive non-heading line of the file, queried by its content
                      (field label removed). Proves every fact is reachable.
  B. line_noisy     : the same lines, paraphrase-like: 40% of words dropped, order shuffled,
                      English field label replaced by nothing.
  C. template_para  : per record (emails, cases, master rows, errors, SOs) natural Korean questions
                      that do NOT contain the record ID.
  D. llm            : eval/llm_questions.jsonl written independently by an LLM (if present).

Hit definition: a returned chunk covers the gold line (strict). For template questions that are
inherently ambiguous (several records share the identifying attributes) we also report a lenient
hit where any equivalent record counts.

Usage:  python -m eval.evaluate [--k 1 3 5 10] [--show-misses 20]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cosmetic_kb.kb import KnowledgeBase  # noqa: E402

CLS_KR = {"quote": "견적 요청", "order": "발주 주문", "ship": "출하 일정 문의", "stock": "재고 문의",
          "invoice": "인보이스 발행 요청", "etc": "기타 문의", "": "분류 안 된"}
KEYS_TO_DROP = re.compile(r"^- ?[A-Za-z][A-Za-z ]*:\s*")
rng = random.Random(42)


def base_name(cust: str) -> str:
    return re.sub(r"\s*\d+$", "", cust or "").strip()


def md(dt: str) -> tuple[int, int, int] | None:
    m = re.match(r"\d{4}-(\d{2})-(\d{2})(?: (\d{2}):\d{2})?", dt or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def strip_label(line: str) -> str:
    return KEYS_TO_DROP.sub("", line.strip())


def noisy(text: str) -> str:
    words = text.replace("{", " ").replace("}", " ").replace('"', " ").replace(",", " , ").split()
    words = [w for w in words if w not in {",", ":"}]
    if len(words) > 3:
        keep = [w for w in words if rng.random() > 0.4] or words[:2]
    else:
        keep = words
    rng.shuffle(keep)
    out = " ".join(keep)
    out = re.sub(r"(\d+)EA", r"\1개", out)
    return out


# --------------------------------------------------------------------------- generation
def gen_line_questions(kb: KnowledgeBase):
    norm_count = Counter(strip_label(l) for l in kb.lines)
    lower_lines = [l.lower() for l in kb.lines]
    heading_lines = set()
    for s in kb.sections:
        heading_lines.update({s.line_start, s.line_start + 1})
    lit, noi = [], []
    for i, line in enumerate(kb.lines, 1):
        content = strip_label(line)
        if i in heading_lines or len(content) < 6:
            continue
        if norm_count[content] > 1:  # duplicated line: not uniquely locatable
            continue
        lc = content.lower()
        if sum(1 for l in lower_lines if lc in l) > 2:  # generic text (e.g. '주문등록'): not a locatable fact
            continue
        if re.fullmatch(r"[\[\]{}(),\"\s]*|\"?[a-zA-Z]+\"?,?|\d+,?", content):
            continue
        lit.append({"q": content, "line": i, "set": "line_literal"})
        noi.append({"q": noisy(content), "line": i, "set": "line_noisy"})
    return lit, noi


def gen_template_questions(kb: KnowledgeBase):
    qs = []
    # emails ---------------------------------------------------------------
    key_groups = defaultdict(list)
    for e in kb.emails.values():
        f = e.fields
        item = e.entities.get("itemName", {}).get("value", "")
        d = md(f.get("Received", ""))
        key_groups[(base_name(f.get("Customer", "")), d[:2] if d else None, f.get("Direction"))].append(e.id)
    for e in kb.emails.values():
        f = e.fields
        cust = base_name(f.get("Customer", ""))
        d = md(f.get("Received", ""))
        if not d:
            continue
        M, D, H = d
        ampm = "오전" if H < 12 else "오후"
        h12 = H if H <= 12 else H - 12
        item = e.entities.get("itemName", {}).get("value", "") or ""
        alts = key_groups[(cust, (M, D), f.get("Direction"))]
        cls = CLS_KR.get(f.get("Classification", ""), "")
        if f.get("Direction") == "in":
            q1 = f"{cust}에서 {M}월 {D}일 {ampm} {h12}시쯤 보낸 {item} {cls} 메일 내용 알려줘"
        else:
            q1 = f"{M}월 {D}일 {ampm} {h12}시쯤 우리가 {cust}에 보낸 답장 메일은 뭐라고 했어?"
        line_subject = next(i for i in range(e.line_start, e.line_end + 1) if kb.L(i).startswith("- Subject:"))
        qs.append({"q": q1, "line": line_subject, "set": "tmpl_email_identify", "alts": alts})
        line_assg = next(i for i in range(e.line_start, e.line_end + 1) if kb.L(i).startswith("- Assignee:"))
        qs.append({"q": f"{M}월 {D}일 {cust} {item} 관련 {'수신' if f.get('Direction') == 'in' else '발신'} 메일 담당자 누구?",
                   "line": line_assg, "set": "tmpl_email_field", "alts": alts})
        # body sentence paraphrase
        body_lines = [i for i in range(e.line_start, e.line_end + 1)]
        try:
            b0 = next(i for i in body_lines if kb.L(i).strip() == "Email body:")
            b1 = next(i for i in body_lines if kb.L(i).strip() == "Extracted entities:")
        except StopIteration:
            continue
        cands = [i for i in range(b0 + 1, b1) if len(kb.L(i).strip()) > 14
                 and not re.match(r"^(안녕하세요|감사합니다|M코스메틱|.*드림$)", kb.L(i).strip())]
        if cands:
            li = max(cands, key=lambda i: len(kb.L(i)))
            s = kb.L(li).strip()
            s = re.sub(r"(부탁드립니다|드립니다|합니다|입니다|습니다|해주세요|주세요)[.!]?", "", s)
            s = re.sub(r"(\d+)개", r"\1EA", s)
            qs.append({"q": f"{cust} 메일에서 '{s}' 라고 한 내용", "line": li, "set": "tmpl_email_body",
                       "alts": [x for x in kb.emails if x == e.id]})
    # cases ----------------------------------------------------------------
    case_groups = defaultdict(list)
    for c in kb.cases.values():
        f = c.fields
        try:
            ent = json.loads(f.get("entity", "{}"))
        except json.JSONDecodeError:
            ent = {}
        case_groups[(base_name(f.get("customer", "")), ent.get("itemName"), f.get("caseType"))].append(c.id)
    for c in kb.cases.values():
        f = c.fields
        try:
            ent = json.loads(f.get("entity", "{}"))
        except json.JSONDecodeError:
            ent = {}
        cust = base_name(f.get("customer", ""))
        item = ent.get("itemName") or f.get("title", "")
        alts = case_groups[(cust, ent.get("itemName"), f.get("caseType"))]
        st_line = next(i for i in range(c.line_start, c.line_end + 1) if kb.L(i).startswith("- status:"))
        qs.append({"q": f"{cust} {item} {f.get('caseType', '')} 건 지금 진행 상태가 어떻게 돼?", "line": st_line,
                   "set": "tmpl_case_status", "alts": alts})
        req_line = next(i for i in range(c.line_start, c.line_end + 1) if kb.L(i).startswith("- request:"))
        qs.append({"q": f"{cust}의 {f.get('title', '')}에서 고객이 요청한 사항", "line": req_line,
                   "set": "tmpl_case_request", "alts": alts})
        acts = [i for i in range(c.line_start, c.line_end + 1)
                if kb.L(i).startswith("- 20") and "| action |" in kb.L(i)]
        if acts:
            li = rng.choice(acts)
            parts = [p.strip() for p in kb.L(li)[2:].split("|")]
            d = md(parts[0])
            qs.append({"q": f"{cust} {item} 건에서 {d[0]}월 {d[1]}일 {parts[2]} 결과가 뭐였어?", "line": li,
                       "set": "tmpl_case_action", "alts": alts})
    # master rows ------------------------------------------------------------
    for it in kb.items.values():
        sn = it.get("Short name", it["name"])
        for key, ask in (("Base price", "기본 단가는 얼마야"), ("Stock", "재고 몇 개 있어"),
                         ("Lead date", "납기 가능일은"), ("Back lead date", "후순위(백) 납기일은")):
            li = next(i for i in range(it["line_start"], it["line_end"] + 1) if kb.L(i).strip().startswith(key + ":"))
            qs.append({"q": f"{sn} {ask}?", "line": li, "set": "tmpl_master", "alts": []})
    for code, c in kb.customers.items():
        qs.append({"q": f"{code} 고객사 이름이 뭐야?", "line": c["line"], "set": "tmpl_master", "alts": []})
        if c.get("contact_line"):
            qs.append({"q": f"{base_name(c['name'])} 담당자 이메일 알려줘", "line": c["contact_line"],
                       "set": "tmpl_master", "alts": []})
        if c.get("signature_line"):
            qs.append({"q": f"{base_name(c['name'])} 대표 전화번호랑 휴대폰 번호", "line": c["signature_line"],
                       "set": "tmpl_master", "alts": []})
    for so, d in kb.sales_orders.items():
        cust = base_name(kb.customers.get(d["customerCode"], {}).get("name", ""))
        m = md(d["shipDate"])
        qs.append({"q": f"{cust} {d['item']} {d['orderQty']}개 주문 건 출하 상태랑 송장번호", "line": d["line"],
                   "set": "tmpl_master", "alts": []})
        qs.append({"q": f"{m[0]}월 {m[1]}일 출하 예정인 {cust} 주문", "line": d["line"], "set": "tmpl_master",
                   "alts": []})
    for eid, d in kb.errors.items():
        m = md(d["time"])
        qs.append({"q": f"{m[0]}월 {m[1]}일 {base_name(d['customer'])} {d['failedAction']} 실패 오류 메시지는?",
                   "line": d["line"], "set": "tmpl_master", "alts": []})
    return qs


def load_llm(kb: KnowledgeBase):
    p = ROOT / "eval" / "llm_questions.jsonl"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            d = json.loads(ln)
            out.append({"q": d["q"], "line": int(d["line"]), "set": f"llm_{d.get('style', 'x')}",
                        "answer": d.get("answer", ""), "alts": []})
    return out


# --------------------------------------------------------------------------- scoring
def evaluate(kb: KnowledgeBase, questions, ks=(1, 3, 5, 10)):
    K = max(ks)
    per_set = defaultdict(lambda: {"n": 0, **{f"strict@{k}": 0 for k in ks}, **{f"lenient@{k}": 0 for k in ks}})
    misses = defaultdict(list)
    for q in questions:
        hits = kb.search(q["q"], top_k=K)
        strict_rank = next((r for r, (c, _) in enumerate(hits, 1) if c.line_start <= q["line"] <= c.line_end), None)
        alts = set(q.get("alts") or [])
        ans = (q.get("answer") or "").strip()
        len_rank = strict_rank
        for r, (c, _) in enumerate(hits, 1):
            ok = (c.record_id in alts) or (len(ans) >= 3 and ans.replace(",", "") in c.text.replace(",", ""))
            if ok and (len_rank is None or r < len_rank):
                len_rank = r
                break
        s = per_set[q["set"]]
        s["n"] += 1
        for k in ks:
            s[f"strict@{k}"] += strict_rank is not None and strict_rank <= k
            s[f"lenient@{k}"] += len_rank is not None and len_rank <= k
        if strict_rank is None or strict_rank > 5:
            misses[q["set"]].append({**q, "got": [c.chunk_id for c, _ in hits[:5]], "strict_rank": strict_rank,
                                     "lenient_rank": len_rank})
    return per_set, misses


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", nargs="+", type=int, default=[1, 3, 5, 10])
    ap.add_argument("--show-misses", type=int, default=0)
    ap.add_argument("--only", default=None, help="evaluate only sets whose name starts with this")
    ap.add_argument("--out", default=str(ROOT / "eval" / "results.json"))
    args = ap.parse_args()
    kb = KnowledgeBase()
    lit, noi = gen_line_questions(kb)
    tmpl = gen_template_questions(kb)
    llm = load_llm(kb)
    questions = lit + noi + tmpl + llm
    if args.only:
        questions = [q for q in questions if q["set"].startswith(args.only)]
    t = time.time()
    per_set, misses = evaluate(kb, questions, tuple(args.k))
    dt = time.time() - t
    rows = []
    tot = Counter()
    for name in sorted(per_set):
        s = per_set[name]
        rows.append((name, s))
        for k, v in s.items():
            tot[k] += v
    print(f"{len(questions)} questions, {dt:.1f}s ({1000 * dt / max(len(questions), 1):.1f} ms/query)")
    hdr = "set".ljust(22) + "n".rjust(6) + "".join(f"  strict@{k}".rjust(11) for k in args.k) + \
        "".join(f"  lenient@{k}".rjust(12) for k in args.k)
    print(hdr)
    for name, s in rows + [("TOTAL", tot)]:
        n = max(s["n"], 1)
        print(name.ljust(22) + str(s["n"]).rjust(6)
              + "".join(f"{100 * s[f'strict@{k}'] / n:10.1f}%" for k in args.k)
              + "".join(f"{100 * s[f'lenient@{k}'] / n:11.1f}%" for k in args.k))
    Path(args.out).write_text(json.dumps({
        "n_questions": len(questions), "sets": {n: s for n, s in rows}, "total": dict(tot),
        "misses": {k: v[:200] for k, v in misses.items()}}, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.show_misses:
        for name, ms in misses.items():
            print(f"\n--- misses in {name}: {len(ms)}")
            for m in ms[:args.show_misses]:
                print(f"  L{m['line']} rank={m['strict_rank']}/{m['lenient_rank']} q={m['q'][:90]!r} got={m['got']}")


if __name__ == "__main__":
    main()
