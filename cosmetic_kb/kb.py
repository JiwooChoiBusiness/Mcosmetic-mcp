"""Knowledge base for the AI365 M Cosmetic dataset.

Parses the text snapshot into
  * sections (with 1-based line ranges),
  * searchable chunks (one per email, one per case, one per table row / paragraph),
  * structured records (emails, cases, items, customers, contacts, sales orders, errors ...),
and provides a Korean-aware BM25 search (character bigrams + whole words + ID tokens)
plus exact-text grep, so every line of the file stays reachable.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .gloss import gloss

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data" / "ai365-Mcosmetic-knowledge-updated_2026-10-22.txt"

UNDERLINE_RE = re.compile(r"^(-{5,}|={5,})\s*$")
SECTION_TITLE_RE = re.compile(r"^(\d+)\.\s+(.+?)\s*$")
EMAIL_HEAD_RE = re.compile(r"^EMAIL (E-\d+)\s*$")
CASE_HEAD_RE = re.compile(r"^CASE ((?:RFQ|ORD|INQ)-\d{4}-\d+)\s*$")
KV_RE = re.compile(r"^- ([A-Za-z][A-Za-z ]*?):\s?(.*)$")
EXTRA_LABEL_RE = re.compile(r"^[A-Z][A-Za-z /()-]+:\s*$")  # e.g. "Processing flow / ERP result:"
ENTITY_RE = re.compile(r"^- (\w+):\s?(.*?)\s*\(confidence=([\d.]+), source=(\w+)\)\s*$")

ID_PATTERNS = {
    "email": re.compile(r"\bE-\d{4}\b", re.I),
    "case": re.compile(r"\b(?:RFQ|ORD|INQ)-\d{4}-\d{5}\b", re.I),
    "sales_order": re.compile(r"\bSO-\d{8}-\d{5}\b", re.I),
    "item": re.compile(r"\bMC-[A-Z]{3}-[A-Z]{2}\d{3}\b", re.I),
    "customer": re.compile(r"\bCUST-\d{5}\b", re.I),
    "error": re.compile(r"\bERR-\d{4}\b", re.I),
    "thread": re.compile(r"\bT-\d{4,5}\b", re.I),
}


# --------------------------------------------------------------------------- tokenizer
COMPOUND_RE = re.compile(r"[a-z0-9]+(?:[-_./@:][a-z0-9]+)+")
BASIC_RE = re.compile(r"[a-z]+|\d+|[가-힣]+")
KDATE_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
SLASH_DATE_RE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
ISO_DATE_RE = re.compile(r"\b\d{4}-(\d{2})-(\d{2})\b")
ISO_DT_RE = re.compile(r"\b\d{4}-(\d{2})-(\d{2})[ t](\d{2}):\d{2}")
KDT_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일[^\d]{0,12}?(오전|오후|아침|저녁|밤)?\s*(\d{1,2})\s*시")
THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3})")

# Korean question words -> English field names / labels used in the file.
SYNONYMS: list[tuple[str, str]] = [
    ("납기", "deliverydate lead date 납품"), ("납품", "deliverydate 납기"),
    ("담당자", "assignee assigneduser"), ("담당", "assignee assigneduser"),
    ("수량", "quantity qty orderqty ea"), ("개수", "quantity"),
    ("단가", "unitprice price 단가조회 base"), ("가격", "price unitprice base"),
    ("기본가", "base price"), ("재고", "stock inventory on-hand"),
    ("송장", "tracking"), ("운송장", "tracking"), ("택배", "tracking"),
    ("주문번호", "sales order salesorder so"), ("수주", "sales order"),
    ("고객코드", "customercode cust"), ("거래처", "customer"), ("고객사", "customer"),
    ("품목코드", "itemcode"), ("제품코드", "itemcode"), ("상품코드", "itemcode"),
    ("제목", "subject"), ("보낸", "sender"), ("발신", "sender"), ("수신자", "recipient"),
    ("받는", "recipient"), ("참조", "cc"), ("본문", "body"), ("신뢰도", "confidence"),
    ("분류", "classification"), ("의도", "intent"), ("요약", "summary"), ("해석", "interpretation"),
    ("이유", "reason"), ("근거", "reason"), ("첨부", "attachments"), ("파일", "attachments pdf"),
    ("오류", "error errors err"), ("에러", "error err"), ("실패", "failed error"),
    ("알림", "notifications triage"), ("주소", "shippingaddress addr"), ("배송지", "shippingaddress addr"),
    ("전화", "tel"), ("연락처", "tel contacts"), ("이메일 주소", "mail"), ("메일 주소", "mail"),
    ("스레드", "thread"), ("쓰레드", "thread"), ("케이스", "case"), ("통화", "currency"),
    ("상태", "status state"), ("완료", "done closed"), ("보류", "hold"), ("다음 단계", "next step"),
    ("처리 단계", "work steps"), ("작업 단계", "work steps"), ("타임라인", "timeline"),
    ("이력", "history timeline"), ("변경", "changes prev"), ("견적번호", "quoteref rfq"),
    ("출하", "ship shipment shipdate"), ("배송", "ship tracking"), ("인보이스", "invoice"),
    ("견적", "quote rfq"), ("주문", "order ord"), ("재고문의", "stock inq"),
    ("대시보드", "dashboard kpi"), ("시나리오", "demo scenarios"), ("데모", "demo scenarios"),
    ("필수", "required fields"), ("서명", "signature"), ("시그니처", "signature"),
    ("발송", "sent out email 발송"), ("수신", "in received"), ("받은", "in received"),
    ("보낸 메일", "out"), ("회신", "out reply re"), ("답장", "out reply re"),
    ("출하일", "shipdate"), ("출하 수량", "shipqty"), ("주문 수량", "orderqty"),
    ("후순위", "back lead date"), ("백오더", "back lead date"), ("리드", "lead date"),
    ("시도", "tries"), ("해결", "resolved resolvedat"), ("미분류", "unclassified"),
    ("API", "api"), ("엔드포인트", "api"), ("요청 내용", "request"), ("생성일", "createddate"),
    ("최종", "lastupdateddate"), ("수정일", "lastupdateddate"), ("케이스 유형", "casetype"),
    ("주문가", "order price"), ("후보", "candidates"), ("이슈", "issues"),
    ("초안", "draft"), ("발송 시각", "sentat"), ("발송 시간", "sentat"), ("메시지 ID", "messageid"),
    ("처리 흐름", "processing flow erp result"), ("ERP 결과", "erpresult"), ("입고", "inbounddate"),
    ("청구", "billto"), ("주문일", "orderdate"), ("변경 사항", "detected changes changed kept"),
    ("요청한", "request"), ("요청 사항", "request"), ("요청사항", "request"), ("출하 예정", "shipdate state"),
]


def _norm(text: str) -> str:
    return THOUSANDS_RE.sub("", text.lower())


def tokenize(text: str) -> list[str]:
    t = _norm(text)
    toks: list[str] = []
    for m in COMPOUND_RE.finditer(t):
        toks.append(m.group())
    for m in ISO_DATE_RE.finditer(t):
        toks.append(f"md:{int(m.group(1))}-{int(m.group(2))}")
    for m in KDATE_RE.finditer(t):
        toks.append(f"md:{int(m.group(1))}-{int(m.group(2))}")
    for m in ISO_DT_RE.finditer(t):
        toks.append(f"mdh:{int(m.group(1))}-{int(m.group(2))}-{int(m.group(3))}")
    for m in KDT_RE.finditer(t):
        h = int(m.group(4))
        if m.group(3) in ("오후", "저녁", "밤") and h < 12:
            h += 12
        toks.append(f"mdh:{int(m.group(1))}-{int(m.group(2))}-{h}")
    for m in SLASH_DATE_RE.finditer(t):
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= 12 and 1 <= b <= 31:
            toks.append(f"md:{a}-{b}")
    for m in BASIC_RE.finditer(t):
        w = m.group()
        if "가" <= w[0] <= "힣":
            if len(w) == 1:
                toks.append(w)
            else:
                toks.append("w:" + w)
                toks.extend(w[i:i + 2] for i in range(len(w) - 1))
        else:
            toks.append(w)
    return toks


def expand_query(q: str) -> str:
    extra = [exp for key, exp in SYNONYMS if key.lower() in q.lower()]
    return q + (" " + " ".join(extra) if extra else "")


# Korean aliases for each section, indexed together with its chunks.
SECTION_ALIASES: dict[int, str] = {
    0: "지식베이스 개요 출처 스냅샷 날짜",
    1: "시스템 개요 업무 유형 카테고리 메뉴 내비게이션 화면",
    2: "메일 처리 규칙 워크플로 절차 단계 신뢰도 구간 수신메일 검토",
    3: "업무별 필수 항목 필수값 필드",
    4: "ERP 처리 정의 액션 완료 문구 API 엔드포인트 힌트 안내 메일",
    5: "ERP 품목 마스터 제품 상품 기본 단가 가격 재고 납기 리드타임",
    6: "ERP 고객 마스터 고객사 거래처 고객코드 목록",
    7: "고객 연락처 담당자 이름 이메일 메일 주소 배송지 전화번호 서명 시그니처",
    8: "출하 배송 ERP 참조 데이터 수주 Sales Order 주문번호 송장 운송장 출하일 상태",
    9: "대시보드 현황 KPI 통계 건수 집계 누적",
    12: "처리 오류 에러 실패 타임아웃 재시도",
    13: "알림 확인 필요 미분류 오류 알림",
    14: "데모 시나리오 시연",
    15: "중요 참고 사항 주의 출처 메모",
}


# --------------------------------------------------------------------------- data classes
@dataclass
class Section:
    number: int
    title: str
    line_start: int  # heading line (1-based)
    line_end: int


@dataclass
class Chunk:
    chunk_id: str
    kind: str  # email | case | row | table | paragraph
    section: int
    section_title: str
    line_start: int
    line_end: int
    text: str
    record_id: str | None = None
    context: str = ""  # heading / table lead line shown with the chunk


@dataclass
class Email:
    id: str
    line_start: int
    line_end: int
    fields: dict[str, str] = field(default_factory=dict)
    body: str = ""
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    extras: dict[str, str] = field(default_factory=dict)  # Attachments, candidates ...


@dataclass
class Case:
    id: str
    line_start: int
    line_end: int
    fields: dict[str, str] = field(default_factory=dict)
    timeline: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- knowledge base
class KnowledgeBase:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path or os.environ.get("COSMETIC_KB_FILE") or DEFAULT_DATA)
        raw = self.path.read_text(encoding="utf-8")
        self.lines: list[str] = raw.splitlines()
        self.n_lines = len(self.lines)
        self.sections: list[Section] = []
        self.chunks: list[Chunk] = []
        self.emails: dict[str, Email] = {}
        self.cases: dict[str, Case] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.customers: dict[str, dict[str, Any]] = {}  # code -> {name, contact, signature, line}
        self.sales_orders: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, dict[str, Any]] = {}
        self._parse_sections()
        self._parse_all()
        self._build_index()

    # ----------------------------------------------------------------- helpers
    def L(self, n: int) -> str:
        return self.lines[n - 1]

    def numbered(self, start: int, end: int) -> str:
        start, end = max(1, start), min(self.n_lines, end)
        return "\n".join(f"{i}: {self.lines[i - 1]}" for i in range(start, end + 1))

    def section_of(self, line: int) -> Section:
        for s in self.sections:
            if s.line_start <= line <= s.line_end:
                return s
        return self.sections[0]

    # ----------------------------------------------------------------- parsing
    def _parse_sections(self) -> None:
        heads: list[tuple[int, int, str]] = []
        for i in range(1, self.n_lines):
            if UNDERLINE_RE.match(self.lines[i]) and self.lines[i - 1].strip():
                title_line = i  # 1-based index of the title line == i (0-based i-1 -> +1)
                m = SECTION_TITLE_RE.match(self.lines[i - 1])
                if m:
                    heads.append((title_line, int(m.group(1)), m.group(2)))
                elif not heads:
                    heads.append((title_line, 0, self.lines[i - 1].strip()))
        for k, (ln, num, title) in enumerate(heads):
            end = heads[k + 1][0] - 1 if k + 1 < len(heads) else self.n_lines
            self.sections.append(Section(num, title, ln, end))

    def _body_range(self, s: Section) -> tuple[int, int]:
        return s.line_start + 2, s.line_end  # skip title + underline

    def _parse_all(self) -> None:
        for s in self.sections:
            a, b = self._body_range(s)
            if s.number == 10:
                self._parse_records(s, a, b, EMAIL_HEAD_RE, self._parse_email)
            elif s.number == 11:
                self._parse_records(s, a, b, CASE_HEAD_RE, self._parse_case)
            else:
                self._chunk_plain(s, a, b)
        self._parse_masters()

    def _parse_records(self, s: Section, a: int, b: int, head_re, parser) -> None:
        starts = [i for i in range(a, b + 1) if head_re.match(self.L(i))]
        # anything before the first record (should be blank) -> paragraph chunks
        if starts and starts[0] > a:
            self._chunk_plain(s, a, starts[0] - 1)
        for k, st in enumerate(starts):
            en = starts[k + 1] - 1 if k + 1 < len(starts) else b
            while en > st and not self.L(en).strip():
                en -= 1
            rid = head_re.match(self.L(st)).group(1)
            parser(rid, st, en)
            if s.number == 10:
                self.chunks.append(Chunk(rid, "email", s.number, s.title, st, en,
                                         "\n".join(self.lines[st - 1:en]), rid, f"{s.number}. {s.title} > {rid}"))
                continue
            # cases: header fields and timeline/action history are indexed separately
            split = next((i for i in range(st, en + 1) if self.L(i).strip() == "Case timeline:"), en + 1)
            h_end = split - 1
            while h_end > st and not self.L(h_end).strip():
                h_end -= 1
            self.chunks.append(Chunk(rid, "case", s.number, s.title, st, h_end,
                                     "\n".join(self.lines[st - 1:h_end]), rid, f"{s.number}. {s.title} > {rid}"))
            if split <= en:
                self.chunks.append(Chunk(rid + "#history", "case_history", s.number, s.title, split, en,
                                         "\n".join(self.lines[split - 1:en]), rid,
                                         f"{s.number}. {s.title} > {rid} > timeline/action history"))

    def _parse_email(self, rid: str, st: int, en: int) -> None:
        e = Email(rid, st, en)
        i = st + 1
        mode, buf_key, buf = "head", None, []
        body_lines: list[str] = []

        def flush():
            if buf_key:
                raw = "\n".join(buf).strip()
                try:
                    e.extras[buf_key] = json.loads(raw)
                except json.JSONDecodeError:
                    e.extras[buf_key] = raw

        while i <= en:
            line = self.L(i)
            if mode == "head":
                m = KV_RE.match(line)
                if m:
                    e.fields[m.group(1)] = m.group(2).strip()
                elif line.strip() == "Email body:":
                    mode = "body"
            elif mode == "body":
                if line.strip() == "Extracted entities:":
                    mode = "ent"
                else:
                    body_lines.append(line)
            elif mode == "ent":
                m = ENTITY_RE.match(line)
                if m:
                    e.entities[m.group(1)] = {"value": m.group(2), "confidence": float(m.group(3)), "source": m.group(4)}
                elif EXTRA_LABEL_RE.match(line):
                    mode, buf_key, buf = "extra", line.strip()[:-1], []
            elif mode == "extra":
                if EXTRA_LABEL_RE.match(line):
                    flush()
                    buf_key, buf = line.strip()[:-1], []
                else:
                    buf.append(line)
            i += 1
        if mode == "extra":
            flush()
        e.body = "\n".join(body_lines).strip()
        self.emails[rid] = e

    def _parse_case(self, rid: str, st: int, en: int) -> None:
        c = Case(rid, st, en)
        mode = "head"
        for i in range(st + 1, en + 1):
            line = self.L(i)
            if line.strip() == "Case timeline:":
                mode = "tl"
                continue
            if line.strip() == "Case action history:":
                mode = "act"
                continue
            if not line.startswith("- "):
                continue
            if mode == "head":
                k, _, v = line[2:].partition(":")
                c.fields[k.strip()] = v.strip()
            elif mode == "tl":
                c.timeline.append(line[2:].strip())
            else:
                try:
                    c.actions.append(json.loads(line[2:]))
                except json.JSONDecodeError:
                    c.actions.append({"raw": line[2:]})
        self.cases[rid] = c

    def _chunk_plain(self, s: Section, a: int, b: int) -> None:
        """Paragraph chunks; long bullet tables also get one chunk per row."""
        para: list[int] = []

        def emit(lines: list[int]) -> None:
            if not lines:
                return
            first, last = lines[0], lines[-1]
            lead = self.L(first).strip()
            ctx = f"{s.number}. {s.title}" if s.number else s.title
            n = len(self.chunks)
            self.chunks.append(Chunk(f"S{s.number}-L{first}", "paragraph" if len(lines) <= 4 else "table",
                                     s.number, s.title, first, last,
                                     "\n".join(self.lines[first - 1:last]), context=ctx))
            if len(lines) > 4:
                lead_ctx = lead if not lead.startswith("- ") else ""
                units: list[list[int]] = []
                for ln in lines:
                    t = self.L(ln)
                    if t.startswith("- ") or not units:
                        units.append([ln])
                    else:
                        units[-1].append(ln)
                for u in units:
                    txt = "\n".join(self.lines[u[0] - 1:u[-1]])
                    if txt.strip() == lead_ctx:
                        continue
                    rid = None
                    for pat in ID_PATTERNS.values():
                        m = pat.search(self.L(u[0]))
                        if m:
                            rid = m.group().upper()
                            break
                    self.chunks.append(Chunk(f"S{s.number}-L{u[0]}", "row", s.number, s.title, u[0], u[-1],
                                             txt, record_id=rid,
                                             context=ctx + (f" > {lead_ctx}" if lead_ctx else "")))
            _ = n

        for ln in range(a, b + 1):
            if self.L(ln).strip():
                para.append(ln)
            else:
                emit(para)
                para = []
        emit(para)

    def _parse_masters(self) -> None:
        sec = {s.number: s for s in self.sections}
        # 5. item master
        if 5 in sec:
            a, b = self._body_range(sec[5])
            cur = None
            for ln in range(a, b + 1):
                t = self.L(ln)
                m = re.match(r"^- (MC-[A-Z0-9-]+): (.+)$", t)
                if m:
                    cur = {"itemCode": m.group(1), "name": m.group(2).strip(), "line_start": ln, "line_end": ln}
                    self.items[cur["itemCode"]] = cur
                elif cur and t.startswith("  ") and ":" in t:
                    k, _, v = t.strip().partition(":")
                    v = v.strip()
                    cur[k.strip()] = int(v) if v.isdigit() else v
                    cur["line_end"] = ln
        # 6. customers
        if 6 in sec:
            a, b = self._body_range(sec[6])
            for ln in range(a, b + 1):
                m = re.match(r"^- (CUST-\d+): (.+)$", self.L(ln))
                if m:
                    self.customers[m.group(1)] = {"customerCode": m.group(1), "name": m.group(2).strip(), "line": ln}
        by_name = {v["name"]: v for v in self.customers.values()}
        # 7. contacts + signatures
        if 7 in sec:
            a, b = self._body_range(sec[7])
            mode = "contact"
            for ln in range(a, b + 1):
                t = self.L(ln)
                if t.startswith("Customer email signatures"):
                    mode = "sig"
                elif t.startswith("M Cosmetic sender signature"):
                    mode = "own"
                m = re.match(r"^- (.+?): (\{.*\}|\[.*\])\s*$", t)
                if m and mode in ("contact", "sig") and m.group(1) in by_name:
                    data = json.loads(m.group(2))
                    key = "contact" if mode == "contact" else "signature"
                    by_name[m.group(1)][key] = data
                    by_name[m.group(1)][key + "_line"] = ln
                elif mode == "own" and t.startswith("- {"):
                    self.own_signature = {"data": json.loads(t[2:]), "line": ln}
        # 8. sales orders
        if 8 in sec:
            a, b = self._body_range(sec[8])
            for ln in range(a, b + 1):
                m = re.match(r"^- (SO-[\d-]+): (\{.*\})\s*$", self.L(ln))
                if m:
                    d = json.loads(m.group(2))
                    d["line"] = ln
                    self.sales_orders[m.group(1)] = d
        # 12. errors (13 repeats the open ones)
        if 12 in sec:
            a, b = self._body_range(sec[12])
            for ln in range(a, b + 1):
                t = self.L(ln)
                if t.startswith("- {"):
                    d = json.loads(t[2:])
                    d["line"] = ln
                    self.errors[d["id"]] = d

    # ----------------------------------------------------------------- BM25 index
    def _chunk_tokens(self, c: Chunk) -> list[str]:
        head = f"{c.context} {c.record_id or ''} {SECTION_ALIASES.get(c.section, '')}"
        extra = ""
        if c.kind == "email":
            e = self.emails[c.record_id]
            f = e.fields
            extra = " ".join([f.get("Subject", ""), f.get("Customer", ""), f.get("Summary", "")])
            # Korean label for direction so "보낸 메일"/"받은 메일" work
            extra += " 수신메일 받은" if f.get("Direction") == "in" else " 발신 회신 보낸"
        elif c.kind in ("case", "case_history"):
            f = self.cases[c.record_id].fields
            extra = " ".join([f.get("title", ""), f.get("customer", ""), f.get("caseType", ""), "케이스 건"])
            try:
                extra += " " + str(json.loads(f.get("entity", "{}")).get("itemName", ""))
            except json.JSONDecodeError:
                pass
        elif c.kind in ("row", "table", "paragraph"):
            names = []
            for m in ID_PATTERNS["customer"].finditer(c.text):
                cu = self.customers.get(m.group().upper())
                if cu:
                    names.append(cu["name"])
            for m in ID_PATTERNS["item"].finditer(c.text):
                it = self.items.get(m.group().upper())
                if it:
                    names.append(str(it.get("Short name", it["name"])))
            extra = " ".join(dict.fromkeys(names)) + " " + gloss(c.context + "\n" + c.text)
        return tokenize(head) + tokenize(head) + tokenize(extra) + tokenize(c.text)

    def _build_index(self) -> None:
        self.doc_tf: list[Counter] = []
        self.doc_len: list[int] = []
        df: Counter = Counter()
        for c in self.chunks:
            toks = self._chunk_tokens(c)
            tf = Counter(toks)
            self.doc_tf.append(tf)
            self.doc_len.append(len(toks))
            df.update(tf.keys())
        N = len(self.chunks)
        self.avgdl = sum(self.doc_len) / max(N, 1)
        self.idf = {t: math.log(1 + (N - n + 0.5) / (n + 0.5)) for t, n in df.items()}
        self.postings: dict[str, list[int]] = defaultdict(list)
        for i, tf in enumerate(self.doc_tf):
            for t in tf:
                self.postings[t].append(i)
        self.chunk_by_id = {c.chunk_id: c for c in self.chunks}

    def search(self, query: str, top_k: int = 5, section: int | None = None,
               kind: str | None = None) -> list[tuple[Chunk, float]]:
        q_ids = {m.group().upper() for pat in ID_PATTERNS.values() for m in pat.finditer(query)}
        qtf = Counter(tokenize(expand_query(query)))
        k1, b = 1.2, 0.75
        scores: dict[int, float] = defaultdict(float)
        for t, qn in qtf.items():
            idf = self.idf.get(t)
            if idf is None:
                continue
            w = idf * (1 + 0.3 * (qn - 1))
            if t.startswith("w:") or "-" in t or t.startswith(("md:", "mdh:")):
                w *= 1.5  # whole words / IDs / dates are strong evidence
            for i in self.postings[t]:
                f = self.doc_tf[i][t]
                scores[i] += w * f * (k1 + 1) / (f + k1 * (1 - b + b * self.doc_len[i] / self.avgdl))
        for i in list(scores):
            c = self.chunks[i]
            if c.record_id and c.record_id.upper() in q_ids:
                scores[i] += 25.0
            if c.kind == "table":
                scores[i] *= 0.9  # prefer the precise row
        ql = query.lower()
        want_case = bool(re.search(r"케이스|case|진행\s*상태|건\b|건(?:은|이|의|에서|에|으로)", ql))
        want_mail = bool(re.search(r"메일(?!\s*주소)|email(?!\s*address)", ql))
        for i in list(scores):
            k = self.chunks[i].kind
            if want_case and k in ("case", "case_history"):
                scores[i] *= 1.35
            elif want_mail and not want_case and k == "email":
                scores[i] *= 1.25
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        # entity linking: make sure master rows of items/customers named in the query are in the result
        pinned: list[int] = []
        if section is None and kind is None:
            pinned = self.linked_master_chunks(query)[:max(1, top_k // 2)]
        out: list[tuple[Chunk, float]] = []
        for i, sc in ranked:
            c = self.chunks[i]
            if section is not None and c.section != section:
                continue
            if kind and c.kind != kind:
                continue
            out.append((c, sc))
            if len(out) >= top_k:
                break
        # entity linking: master rows (item/customer/contact) of entities named in the query are
        # guaranteed a place; they take over the lowest-ranked slots if not already present.
        missing = [ci for ci in pinned if all(c is not self.chunks[ci] for c, _ in out)]
        if missing:
            out = out[:max(0, top_k - len(missing))] + [(self.chunks[ci], scores.get(ci, 0.0)) for ci in missing]
        return out

    def linked_master_chunks(self, query: str) -> list[int]:
        """Master-table row chunks for items/customers named in the query, chosen by question intent:
        item facts (재고/단가/납기...) -> item row, contact facts (연락처/주소/이메일...) -> contact row,
        phone/signature -> signature row, codes -> customer master row."""
        if not hasattr(self, "_entity_rows"):
            items: dict[str, int] = {}
            cust: dict[str, dict[str, int]] = defaultdict(dict)
            for idx, c in enumerate(self.chunks):
                if c.kind != "row" or c.section not in (5, 6, 7):
                    continue
                first = c.text.splitlines()[0]
                for it in self.items.values():
                    if c.section == 5 and it["itemCode"] in first:
                        items[str(it.get("Short name", it["name"])).replace(" ", "").lower()] = idx
                for cu in self.customers.values():
                    base = cu["name"].rsplit(" ", 1)[0].replace(" ", "").lower()
                    if c.section == 6 and first.startswith(f"- {cu['customerCode']}:"):
                        cust[base]["master"] = idx
                    elif c.section == 7 and first.startswith(f"- {cu['name']}: {{"):
                        cust[base]["contact"] = idx
                    elif c.section == 7 and first.startswith(f"- {cu['name']}: ["):
                        cust[base]["signature"] = idx
            self._entity_rows = (items, cust)
        items, cust = self._entity_rows
        q = query.replace(" ", "").lower()
        item_int = re.search(r"재고|단가|가격|기본가|납기\s*가능|리드|후순위|백\s*납기|백오더|품목\s*코드|제품\s*코드|마스터",
                             query) and not re.search(r"주문|견적|문의|메일|케이스|건", query)
        contact_int = re.search(r"연락처|이메일\s*주소|메일\s*주소|이메일|주소|배송지|담당자", query) and not \
            re.search(r"메일\s*(담당자|내용)|보낸|받은|건|케이스", query)
        sig_int = re.search(r"전화|휴대폰|핸드폰|대표\s*번호|서명|시그니처|부서", query)
        code_int = re.search(r"고객\s*코드|고객사\s*(이름|명)|CUST", query, re.I)
        hits: list[int] = []
        for name in sorted(items, key=len, reverse=True):  # longest first
            if name in q:
                if item_int:
                    hits.append(items[name])
                q = q.replace(name, " ")
        for name, rows in cust.items():
            if name in q:
                if code_int and "master" in rows:
                    hits.append(rows["master"])
                if contact_int and "contact" in rows:
                    hits.append(rows["contact"])
                if sig_int and "signature" in rows:
                    hits.append(rows["signature"])
        return hits[:3]

    # ----------------------------------------------------------------- exact grep
    def grep(self, pattern: str, regex: bool = False, max_hits: int = 50,
             ignore_case: bool = True) -> tuple[int, list[dict[str, Any]]]:
        flags = re.I if ignore_case else 0
        rx = re.compile(pattern if regex else re.escape(pattern), flags)
        hits, total = [], 0
        for i, line in enumerate(self.lines, 1):
            if rx.search(line):
                total += 1
                if len(hits) < max_hits:
                    hits.append({"line": i, "text": line, "section": self.section_of(i).number,
                                 "record_id": self.record_at(i)})
        return total, hits

    def record_at(self, line: int) -> str | None:
        for coll in (self.emails, self.cases):
            for r in coll.values():
                if r.line_start <= line <= r.line_end:
                    return r.id
        return None

    def chunk_for_line(self, line: int) -> list[Chunk]:
        return [c for c in self.chunks if c.line_start <= line <= c.line_end]

    # ----------------------------------------------------------------- structured views
    def email_summary(self, e: Email) -> dict[str, Any]:
        f = e.fields
        return {"id": e.id, "thread": f.get("Thread ID"), "direction": f.get("Direction"),
                "customer": f.get("Customer"), "sender": f.get("Sender"), "received": f.get("Received"),
                "subject": f.get("Subject"), "classification": f.get("Classification"),
                "intent": f.get("Intent"), "status": f.get("Status"), "work_status": f.get("Work status"),
                "case_id": f.get("Case ID"), "assignee": f.get("Assignee"),
                "lines": f"{e.line_start}-{e.line_end}"}

    def email_full(self, e: Email) -> dict[str, Any]:
        return {"id": e.id, "lines": f"{e.line_start}-{e.line_end}", "fields": e.fields, "body": e.body,
                "extracted_entities": e.entities, "extra_blocks": e.extras}

    def case_summary(self, c: Case) -> dict[str, Any]:
        f = c.fields
        return {"id": c.id, "customer": f.get("customer"), "caseType": f.get("caseType"), "title": f.get("title"),
                "status": f.get("status"), "done": f.get("done"), "assignedUser": f.get("assignedUser"),
                "createdDate": f.get("createdDate"), "lastUpdatedDate": f.get("lastUpdatedDate"),
                "salesOrderNumber": f.get("salesOrderNumber"), "lines": f"{c.line_start}-{c.line_end}"}

    def case_full(self, c: Case) -> dict[str, Any]:
        fields = dict(c.fields)
        for k in ("entity", "prev", "changes"):
            if k in fields:
                try:
                    fields[k] = json.loads(fields[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        return {"id": c.id, "lines": f"{c.line_start}-{c.line_end}", "fields": fields,
                "timeline": c.timeline, "action_history": c.actions}

    def find_customer_codes(self, text: str) -> list[str]:
        """Resolve a customer name/code fragment ("누리뷰티", "누리뷰티 02", "CUST-00002", "02")."""
        t = text.strip().lower().replace(" ", "")
        out = []
        for code, c in self.customers.items():
            name = c["name"].lower().replace(" ", "")
            if t in (code.lower(), name) or (t and t in name) or (t.isdigit() and name.endswith(t.zfill(2))):
                out.append(code)
        return out

    def section_by_ref(self, ref: str | int) -> Section | None:
        s = str(ref).strip().lower()
        for sec in self.sections:
            if s == str(sec.number) or (s and s in sec.title.lower()):
                return sec
        return None


_KB: KnowledgeBase | None = None


def get_kb() -> KnowledgeBase:
    global _KB
    if _KB is None:
        _KB = KnowledgeBase()
    return _KB


def iter_all_record_ids(kb: KnowledgeBase) -> Iterable[str]:
    yield from kb.emails
    yield from kb.cases
