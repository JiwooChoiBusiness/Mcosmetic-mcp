"""Write docs/PROMPTS.md from eval/demo_prompts.json, eval/hard_prompts.json and the ERP examples below.

  python scripts/build_prompts_doc.py
Expected answers are for the current data file (snapshot 2026-11-25); check them with
  python eval/check_demo_prompts.py [URL] [--hard]
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
esc = lambda s: s.replace("|", "\\|")  # noqa: E731

ERP = [
    ("글로우 크림 500개 다온코스메틱에 팔면 단가 얼마야? ERP로 조회해줘.", "기본 단가(DB 기준가격) 39,000원 → 8% 할인 → **35,880원**, 합계(부가세 포함) 19,734,000원, 납기 가능일 12/14"),
    ("카밍 크림 500개 지금 재고로 나갈 수 있어?", "재고 3,450 / 출하 예정 1,000 → 가용 **2,450**, 출고 가능"),
    ("SO-20261024-00119 지금 어디쯤이야?", "누리글로벌 베리어 크림 300EA, 11/25 출고 **배송 중**, 송장 LT-4950-5304-2060"),
    ("SO-20260929-00063 인보이스 금액 뽑아줘.", "공급가 9,424,000 + 부가세 942,400 = **10,366,400원**, INV-20260929-00063"),
    ("누리뷰티 카밍 세럼 재주문 ERP에 등록해줘.", "미리보기: **SO-20261125-00146**, 합계 3,135,000원, 희망 납기 12/15 → 가능일 12/17 안내 → \"승인\" 하면 등록 완료"),
    ("다온코스메틱 베리어 세럼 추가 주문도 등록해줘.", "**등록 불가: 희망 납기(deliveryDate) 누락** → 고객에게 납기 확인하라고 안내"),
    ("납기는 12월 12일이래. 다시 등록해줘.", "미리보기: SO-20261125-00143, 단가 10,930원(300개 5% 할인), **가능일 12/15로 조정 필요** 안내"),
    ("누리코스메틱 시카 세럼 주문, ERP 실패한 거 다시 등록해봐.", "미리보기에 **ERR-0010 해결 처리** 포함 → 승인 시 SO-20261125-00129"),
    ("온유글로벌 담당자한테 배송 현황 안내 메일 써서 보내줘.", "받는 사람 sarah.kim@onyuglobal.com, 초안 미리보기 → 승인 시 **메시지 ID MSG-…** 발송 완료"),
    ("이 메일 test@example.com 으로 보내줘.", "미리보기에 **\"마스터에 없는 주소\" 경고**"),
    ("(승인 없이) 그냥 바로 등록해.", "AI가 미리보기를 먼저 보여주고 승인을 받아야 함 (승인 없이 실행되면 안 됨)"),
    ("ERP가 응답 안 하는 상황도 보여줘.", "실패 응답: \"ERP API Timeout (응답 대기 30초 초과)\""),
]


def table(rows, head):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(esc(str(x)) for x in r) + " |" for r in rows]
    return out


def main() -> None:
    P = json.loads((ROOT / "eval" / "demo_prompts.json").read_text(encoding="utf-8"))
    H = json.loads((ROOT / "eval" / "hard_prompts.json").read_text(encoding="utf-8"))
    out = ["# M코스메틱 지식 MCP — 예시 프롬프트", "",
           "외부 AI(또는 Claude)에 MCP `https://mcosmetic.vercel.app/mcp`를 연결한 뒤 그대로 복사해서 물어보면 됩니다.",
           "**기대 답변은 2026-11-25 기준 데이터 원문에서 확인한 값**이라, AI 답이 맞는지 채점하는 시험지로도 쓸 수 있습니다.", "",
           "- 자동 확인: `python eval/check_demo_prompts.py https://mcosmetic.vercel.app/mcp` (심화는 `--hard`) → 서버가 각 질문의 근거를 실제로 찾는지 검사",
           "- 9번(없는 정보)은 AI가 '데이터셋에 없다'고 답하는지 사람이 확인"]
    cat = None
    rows = []
    for i, p in enumerate(P, 1):
        if p["cat"] != cat:
            if rows:
                out += [""] + table(rows, ["#", "프롬프트", "기대 답변", "주로 쓰이는 도구"])
            cat, rows = p["cat"], []
            out += ["", f"## {cat}"]
        rows.append((i, p["prompt"], p["expected"], f"`{p['tool']}`"))
    out += [""] + table(rows, ["#", "프롬프트", "기대 답변", "주로 쓰이는 도구"])
    out += ["", "## 잘 묻는 요령", "",
            "- **고객명·품목명·날짜를 넣으면 정확해집니다.** 예: \"11월 25일 오전 9시쯤 온유글로벌이 보낸 메일\"",
            "- **ID를 알면 ID로 물으세요.** E-1001, RFQ-2026-00120, SO-20261024-00119, MC-CIC-SR050, CUST-00024, ERR-0010",
            "- **개수·목록 질문**(몇 건, 모두, 담당자별)은 AI가 `find_emails`/`find_cases`로 전체를 세므로 정확합니다.",
            "- **근거를 같이 달라고** 하면 행 번호와 원문을 인용합니다. 예: \"근거 원문과 행 번호도 같이 보여줘\"",
            "- 대시보드 KPI(11월 25일 하루)와 누적 건수는 기준이 다르므로 섞지 않도록 되어 있습니다.",
            "", "---", "", "# 어려운 질문 (심화 25개)", "",
            "한 번의 검색으로 끝나지 않는 질문들입니다. AI가 **여러 기록을 이어 보고, 계산하고, 시간 순서를 따지고, 데이터의 모순이나 빠진 것을 찾아야** 답할 수 있습니다.",
            "'풀이 경로'는 AI가 도구를 쓰는 바람직한 순서이고, 기대 답변은 원문에서 계산·확인한 값입니다.", "",
            "- 채점 포인트: 숫자 정확성, 근거(행 번호) 인용, '데이터에 규칙 문서는 없음'처럼 **추론과 사실을 구분**하는지"]
    cat = None
    rows = []
    for i, p in enumerate(H, 1):
        if p["cat"] != cat:
            if rows:
                out += [""] + table(rows, ["#", "프롬프트", "기대 답변", "풀이 경로"])
            cat, rows = p["cat"], []
            out += ["", f"## {cat}"]
        rows.append((f"H{i}", p["prompt"], p["expected"], p["steps"]))
    out += [""] + table(rows, ["#", "프롬프트", "기대 답변", "풀이 경로"])
    out += ["", "---", "", "# ERP 실행 — 말로 시켜 보기", "",
            "주문등록·메일 발송은 AI가 먼저 **\"반영 예정 데이터\"**(미리보기 ID 포함)를 보여주고, \"승인\"이라고 답하면 실행합니다.",
            "(실제 ERP·메일 서버에는 연결되지 않은 모의 ERP이며 결과는 저장되지 않습니다. 금액·형식 규칙은 데이터의 ERP 기록과 같습니다.)", ""]
    out += table([(f"X{i}", q, a) for i, (q, a) in enumerate(ERP, 1)], ["#", "이렇게 말해 보세요", "이런 결과가 나와야 정상"])
    (ROOT / "docs" / "PROMPTS.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"docs/PROMPTS.md: {len(P)} + {len(H)} + {len(ERP)} prompts")


if __name__ == "__main__":
    main()
