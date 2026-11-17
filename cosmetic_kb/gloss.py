"""English -> Korean glossary used at *index* time for the English prose sections
(overview, rules, required fields, ERP definitions, dashboard, errors, notes), so Korean questions
match English source text. Keys are matched case-insensitively as substrings of the chunk text."""

GLOSS: dict[str, str] = {
    "knowledge base": "지식베이스", "snapshot": "스냅샷 기준", "standalone": "스탠드얼론 단독 화면",
    "processes incoming": "받은 메일 수신 처리", "incoming": "수신 받은 들어온", "outgoing": "발신 보낸 나간",
    "customer email": "고객 메일", "classif": "분류 구분", "unclassified": "미분류 분류 안 된",
    "business intent": "업무 의도", "intent": "의도", "extract": "추출 뽑아", "entities": "엔티티 항목 정보 값",
    "structured": "구조화", "groups emails": "묶기 묶어", "work cases": "업무 Case 케이스", "work case": "업무 Case 케이스",
    "assign": "배정 할당", "create": "생성 만들 신규", "erp-related": "ERP 작업", "reply draft": "답장 초안 회신",
    "reply": "답장 회신", "draft": "초안", "tracks": "추적", "processing status": "처리 상태", "status": "상태",
    "main work categories": "주요 업무 유형 카테고리", "categor": "유형 카테고리 분류",
    "main navigation": "메뉴 내비게이션 화면 상단", "navigation": "메뉴 내비게이션",
    "workflow": "처리 순서 절차 흐름 단계", "receive": "수신 받기", "detect": "파악 감지", "determine": "판단 결정",
    "lookup": "조회", "registration": "등록", "execute": "실행 수행", "required": "필수 반드시 필요한 요구",
    "generate": "작성 생성", "applicable": "필요한 경우 해당", "send the email": "메일 발송 보내기",
    "confirmation": "확인 승인", "on hold": "보류", "hold": "보류", "review": "검토 확인",
    "confidence tiers": "신뢰도 구간 단계", "confidence": "신뢰도 확신", "items": "건 개",
    "inbox": "수신메일 받은편지함 화면", "cannot classify": "분류하지 못한 분류 못", "lack": "빠진 누락 없는",
    "required values": "필수값", "moves to": "이동 넘어가", "appropriate business menu": "해당 업무 메뉴",
    "required fields": "필수 항목 필수값 반드시 필요한 정보", "no required fields": "필수 항목 없음",
    "customercode": "고객코드", "itemcode": "품목코드", "quantity": "수량", "deliverydate": "납기 납품일",
    "shippingaddress": "배송지 주소", "salesorder": "주문번호 Sales Order", "currency": "통화",
    "quote reference": "견적번호", "sales order": "주문번호 수주",
    "processing definitions": "처리 정의 액션", "api": "API 엔드포인트 호출",
    "item master": "품목 마스터 제품 목록", "base price": "기본 단가 가격", "stock": "재고",
    "lead date": "납기 가능일 리드타임", "back lead date": "후순위 백오더 납기",
    "customer master": "고객 마스터 고객사 목록", "contacts": "연락처 담당자",
    "signature": "서명 시그니처 전화번호", "shipment": "출하 배송", "reference data": "참조 데이터",
    "dashboard": "대시보드 현황", "kpi recv": "KPI 오늘 수신 받은 메일 수", "kpi auto": "KPI 자동 처리 자동분류",
    "kpi review": "KPI 검토 필요 확인", "kpi erp": "KPI ERP 처리", "kpi err": "KPI 오류 에러",
    "work type summary": "업무 유형별 요약 집계 현황", "total=": "전체 총 건수 합계", "done=": "완료 처리 완료",
    "in_progress": "진행 중 처리 중", "needs_review": "검토 필요 확인 필요", "error=": "오류 에러",
    "cumulative": "누적 지금까지 전체", "total": "전체 총 모두 합계 건수", "including": "포함",
    "email records": "이메일 기록 메일 건수", "outgoing emails": "발신 보낸 메일",
    "dashboard day": "대시보드 당일 하루", "aggregation periods": "집계 기간",
    "different": "다른 서로 다름", "intentionally": "의도적으로 일부러", "separate": "분리 따로",
    "processing errors": "처리 오류 에러 실패", "timeout": "타임아웃 응답 지연 시간 초과",
    "failure": "실패", "resolved": "해결 완료", "open": "미해결 열림 진행", "tries": "시도 횟수 재시도",
    "notifications": "알림", "triage": "확인 필요 분류 검토", "demo scenarios": "데모 시나리오 시연",
    "exported": "내보낸 추출된", "anchored": "기준 고정", "archived": "과거 보관 이전",
    "business records": "업무 기록", "retained": "남아 유지 보존", "original dates": "원래 날짜",
    "read-only": "읽기 전용 수정 불가", "processing actions": "처리 액션 버튼", "disabled": "비활성 작동 안 함 꺼져",
    "do not modify": "바뀌지 않음 수정 안 됨 변경 안 됨", "modify": "수정 변경 바뀌", "source of truth": "기준 원본 근거",
    "scenario": "시나리오", "not actual mail records": "실제 메일 기록 아님 없음",
    "excluded": "제외 빠짐", "styling": "스타일 디자인", "javascript": "자바스크립트 구현", "cdn": "CDN 폰트",
    "source": "출처 원본", "html": "HTML 화면 파일",
    "- quote": "견적 견적요청", "- order": "주문 주문등록 발주", "- ship": "출하 출하일정 배송", "- stock": "재고 재고문의",
    "- invoice": "인보이스 발행 요청", "- etc": "기타", "required": "필수 반드시 필요한 접수 있으면",
}


def gloss(text: str) -> str:
    t = text.lower()
    return " ".join(v for k, v in GLOSS.items() if k in t)
